"""Service Difficulty Reports sync (DESIGN §6.2, docs/sdr.md). The FAA publishes one CSV per year of
difficulty dates, re-generated with each day's new and amended reports.

Each sync re-reads the current year and the `sdr_years_back` before it. A file is refused whole, and recorded
in quarantine, if any check fails: the 76-column header must match exactly, key columns must be mostly
filled, and the file must keep at least `sdr_min_kept` of the reports already stored for its year (a
truncated download must not tombstone thousands of reports). A file that passes is applied in one
transaction per year: new and changed reports are upserted by `row_hash`, and reports of that year that
have disappeared from the file are tombstoned (not deleted). No embeddings: SDRs are searched by filters
and full text only, so a sync costs nothing.
"""

import csv
import dataclasses
import hashlib
import io
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.ingest.common import SyncReport
from app.ingest.fetch import FetchFailed, FetchRefused, SafeFetcher
from app.ingest.sdr_models import normalise_make, normalise_model
from app.ingest.store import quarantine
from app.ingest.text import clean

logger = logging.getLogger(__name__)

SOURCE = "sdr"
PARSER_VERSION = "1"  # part of every row_hash: a parser change rewrites every report once
HEADER = (
    "OperatorControlNumber,DifficultyDate,SubmissionDate,OperatorDesignator,SubmitterDesignator,SubmitterTypeCode,"
    "ReceivingRegionCode,ReceivingDistrictOffice,SDRType,JASCCode,NatureOfConditionA,NatureOfConditionB,"
    "NatureOfConditionC,PrecautionaryProcedureA,PrecautionaryProcedureB,PrecautionaryProcedureC,"
    "PrecautionaryProcedureD,StageOfOperationCode,HowDiscoveredCode,RegistryNNumber,AircraftMake,AircraftModel,"
    "AircraftSerialNumber,AircraftTotalTime,AircraftTotalCycles,EngineMake,EngineModel,EngineSerialNumber,"
    "EngineTotalTime,EngineTotalCycles,PropellerMake,PropellerModel,PropellerSerialNumber,PropellerTotalTime,"
    "PropellerTotalCycles,PartMake,PartName,PartNumber,PartSerialNumber,PartCondition,PartLocation,PartTotalTime,"
    "PartTotalCycles,PartTimeSince,PartSinceCode,ComponentMake,ComponentModel,ComponentName,ComponentPartNumber,"
    "ComponentSerialNumber,ComponentLocation,ComponentTotalTime,ComponentTotalCycles,ComponentTimeSince,"
    "ComponentSinceCode,FuselageStationFrom,FuselageStationTo,StringerFrom,StringerFromSide,StringerTo,"
    "StringerToSide,WingStationFrom,WingStationFromSide,WingStationTo,WingStationToSide,ButtLineFrom,"
    "ButtLineFromSide,ButtlineTo,ButtlineToSide,WaterLineFrom,WaterLineTo,CrackLength,NumberOfCracks,"
    "CorrosionLevel,StructuralOther,Discrepancy"
).split(",")
# Share of rows allowed to have the column empty (measured on 2024-2026: at most 0.6%).
MAX_EMPTY = {"AircraftMake": 0.02, "AircraftModel": 0.02, "JASCCode": 0.02, "Discrepancy": 0.02}
MAX_BAD_ROWS = 0.01  # rows dropped for a missing id or an unreadable date
MAX_TEXT = 20_000  # longest discrepancy seen: 9,109 characters

COLUMNS = ("sdr_id", "difficulty_date", "jasc", "make_raw", "model_raw", "make", "model", "model_family",
           "part_name", "part_number", "part_condition", "part_location", "stage", "how_discovered", "nature",
           "operator_code", "submitted_on", "discrepancy", "source_year", "row_hash")  # fmt: skip


class FileRejected(ValueError):
    pass


@dataclass(frozen=True)
class SdrRow:
    sdr_id: str
    difficulty_date: date
    jasc: str | None
    make_raw: str
    model_raw: str
    make: str
    model: str
    model_family: str | None
    part_name: str | None
    part_number: str | None
    part_condition: str | None
    part_location: str | None
    stage: str | None
    how_discovered: str | None
    nature: list[str]
    operator_code: str | None
    submitted_on: date | None
    discrepancy: str
    source_year: int
    row_hash: str

    def values(self) -> tuple[Any, ...]:
        return tuple(getattr(self, c) for c in COLUMNS)


def _text(v: str | None, limit: int = 500) -> str | None:
    v = clean(v or "").strip()
    return v[:limit] or None


def _date(v: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(v.strip()[:10], fmt).date()
    except ValueError:
        return None


def parse_row(d: dict[str, str], year: int) -> SdrRow | None:
    """None when the row has no id or no readable difficulty date (counted by the caller)."""
    sdr_id = (d.get("OperatorControlNumber") or "").strip()
    when = _date(d.get("DifficultyDate") or "", "%m/%d/%Y")
    if not sdr_id or when is None:
        return None
    make_raw, model_raw = (d.get("AircraftMake") or "").strip(), (d.get("AircraftModel") or "").strip()
    model, family = normalise_model(make_raw, model_raw)
    fields = dict(
        sdr_id=sdr_id, difficulty_date=when, jasc=_text(d.get("JASCCode"), 8), make_raw=make_raw,
        model_raw=model_raw, make=normalise_make(make_raw), model=model, model_family=family,
        part_name=_text(d.get("PartName")), part_number=_text(d.get("PartNumber"), 100),
        part_condition=_text(d.get("PartCondition"), 100), part_location=_text(d.get("PartLocation")),
        stage=_text(d.get("StageOfOperationCode"), 8), how_discovered=_text(d.get("HowDiscoveredCode"), 8),
        nature=[c for c in (_text(d.get(f"NatureOfCondition{x}"), 8) for x in "ABC") if c],
        operator_code=_text(d.get("OperatorDesignator"), 8),
        submitted_on=_date(d.get("SubmissionDate") or "", "%Y-%m-%d"),
        discrepancy=_text(d.get("Discrepancy"), MAX_TEXT) or "", source_year=year,
    )  # fmt: skip
    digest = hashlib.sha256(f"{PARSER_VERSION}\x1e{'\x1f'.join(map(str, fields.values()))}".encode())
    return SdrRow(**fields, row_hash=digest.hexdigest())  # type: ignore[arg-type]


def parse_file(body: bytes, year: int) -> list[SdrRow]:
    """All checks that don't need the database. Raises FileRejected with the reason."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRejected(f"not UTF-8: {exc}") from None
    csv.field_size_limit(MAX_TEXT * 10)
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header != HEADER:
        got = "none" if header is None else f"{len(header)} columns, first {header[:3]}"
        raise FileRejected(f"header differs from the pinned 76 columns ({got})")
    rows: list[SdrRow] = []
    total = bad = 0
    empty = dict.fromkeys(MAX_EMPTY, 0)
    seen: set[str] = set()
    for values in reader:
        total += 1
        if len(values) != len(HEADER):
            bad += 1
            continue
        d = dict(zip(HEADER, values, strict=True))
        for col in empty:
            empty[col] += not d[col].strip()
        row = parse_row(d, year)
        if row is None or row.sdr_id in seen:
            bad += 1
            continue
        seen.add(row.sdr_id)
        rows.append(row)
    if total == 0:
        raise FileRejected("no rows")
    if bad / total > MAX_BAD_ROWS:
        raise FileRejected(f"{bad} of {total} rows unreadable (limit {MAX_BAD_ROWS:.0%})")
    for col, n in empty.items():
        if n / total > MAX_EMPTY[col]:
            raise FileRejected(f"{col} empty in {n} of {total} rows (limit {MAX_EMPTY[col]:.0%})")
    return rows


async def apply_year(conn: AsyncConnection[Any], year: int, rows: list[SdrRow], min_kept: float) -> dict[str, int]:
    """Upsert one year's reports and tombstone the ones that disappeared, in one transaction. A per-year
    advisory lock stops a scheduled sync and a manual one interleaving."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('sdr:' || %s::text))", (year,))
        cur = await conn.execute("SELECT count(*) FROM sdr WHERE source_year = %s AND tombstoned_at IS NULL", (year,))
        stored = int((await cur.fetchone() or [0])[0])
        if stored and len(rows) < min_kept * stored:
            raise FileRejected(f"{len(rows)} reports, but {stored} already stored for {year} (min {min_kept:.0%})")
        await conn.execute("CREATE TEMP TABLE sdr_in (LIKE sdr INCLUDING DEFAULTS EXCLUDING GENERATED) ON COMMIT DROP")
        await conn.execute("ALTER TABLE sdr_in DROP COLUMN tsv")
        async with conn.cursor().copy(f"COPY sdr_in ({', '.join(COLUMNS)}) FROM STDIN") as copy:
            for r in rows:
                await copy.write_row(r.values())
        cols = ", ".join(COLUMNS)
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "sdr_id")
        cur = await conn.execute(
            f"""INSERT INTO sdr ({cols}) SELECT {cols} FROM sdr_in
                ON CONFLICT (sdr_id) DO UPDATE SET {sets}, tombstoned_at = NULL, ingested_at = now()
                WHERE sdr.row_hash IS DISTINCT FROM EXCLUDED.row_hash OR sdr.tombstoned_at IS NOT NULL
                RETURNING (xmax = 0)"""  # noqa: S608
        )
        written = [bool(r[0]) for r in await cur.fetchall()]
        cur = await conn.execute(
            """UPDATE sdr SET tombstoned_at = now() WHERE source_year = %s AND tombstoned_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM sdr_in i WHERE i.sdr_id = sdr.sdr_id)""",
            (year,),
        )
        tombstoned = cur.rowcount
        await upsert_aliases(conn, rows)
    inserted = sum(written)
    return {"inserted": inserted, "updated": len(written) - inserted,
            "unchanged": len(rows) - len(written), "tombstoned": tombstoned}  # fmt: skip


async def upsert_aliases(conn: AsyncConnection[Any], rows: Iterable[SdrRow]) -> None:
    """Record every raw make and make+model seen with its canonical form (DESIGN §5.1 `model_aliases`), so
    the mapping can be inspected and corrected in one place."""
    makes = {(r.make_raw, r.make) for r in rows if r.make_raw}
    models = {(f"{r.make_raw} {r.model_raw}", r.model) for r in rows if r.model_raw}
    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO model_aliases (kind, raw, canonical) VALUES (%s, %s, %s)
               ON CONFLICT (kind, raw) DO UPDATE SET canonical = EXCLUDED.canonical
               WHERE model_aliases.canonical <> EXCLUDED.canonical""",
            [("make", raw, can) for raw, can in sorted(makes)] + [("model", raw, can) for raw, can in sorted(models)],
        )


async def sync_sdr(
    settings: Settings, fetcher: SafeFetcher, pool: AsyncConnectionPool, *, years: list[int] | None = None
) -> SyncReport:
    report = SyncReport()
    this_year = date.today().year
    years = years or list(range(this_year - settings.sdr_years_back, this_year + 1))
    big = dataclasses.replace(fetcher, max_bytes=settings.sdr_max_bytes, timeout_s=300.0)
    async with pool.connection() as conn:
        cur = await conn.execute("INSERT INTO ingest_runs (source) VALUES (%s) RETURNING id", (SOURCE,))
        report.run_id = int((await cur.fetchone() or [0])[0])
    crash: Exception | None = None
    try:
        for year in years:  # oldest first: a report moved to a later year is tombstoned, then revived
            url = settings.sdr_url.format(year=year)
            try:
                got = await big.get(url)
            except (FetchFailed, FetchRefused) as exc:
                report.failed += 1
                report.errors.append(f"{year}: {exc}")
                continue
            try:
                rows = parse_file(got.body, year)
                report.fetched += len(rows)
                async with pool.connection() as conn:
                    out = await apply_year(conn, year, rows, settings.sdr_min_kept)
            except FileRejected as exc:
                async with pool.connection() as conn:
                    await quarantine(conn, SOURCE, url, str(exc), hashlib.sha256(got.body).hexdigest())
                report.quarantined += 1
                report.errors.append(f"{year}: rejected: {exc}")
                continue
            report.inserted += out["inserted"]
            report.updated += out["updated"]
            report.unchanged += out["unchanged"]
            report.tombstoned += out["tombstoned"]
            logger.info("sync_sdr %s: %s", year, out)
    except Exception as exc:  # close the run record as failed, then re-raise for the queue
        report.stopped_early = f"crashed: {type(exc).__name__}: {exc}"
        crash = exc
    ok = report.stopped_early is None and report.failed == 0 and report.quarantined == 0
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE ingest_runs SET finished_at = now(), status = %s, fetched = %s, inserted = %s,
                      updated = %s, quarantined = %s, tombstoned = %s, error = %s WHERE id = %s""",
            ("succeeded" if ok else "failed", report.fetched, report.inserted, report.updated, report.quarantined,
             report.tombstoned, report.stopped_early or "; ".join(report.errors[:20]) or None, report.run_id),
        )  # fmt: skip
    logger.info("sync_sdr: %s", report)
    if crash:
        raise crash
    return report
