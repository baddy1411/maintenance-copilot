"""SDR sync end to end on real Postgres with a fake FAA server, and the agent's query_sdr tool against the
tools_ro role (docs/sdr.md, ADR-0008). Needs Docker."""

import csv
import io
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast

import httpx2
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import check_citations
from app.agent.schemas import Citation
from app.agent.tools import MAX_TOOL_CHARS, Tools
from app.config import Settings
from app.ingest.fetch import SafeFetcher, allowed
from app.ingest.sdr import HEADER, FileRejected, parse_file, sync_sdr
from app.llm import CallContext
from app.retrieval.search import Retriever

pytestmark = pytest.mark.integration


def report(cn: str, when: str = "03/14/2025", *, make: str = "BOEING", model: str = "7378H4", jasc: str = "3242",
           part: str = "BRAKE", cond: str = "WORN", text: str = "BRAKE WORN BEYOND LIMITS. REPLACED.",
           **extra: str) -> dict[str, str]:  # fmt: skip
    d = dict.fromkeys(HEADER, "")
    d.update(OperatorControlNumber=cn, DifficultyDate=when, SubmissionDate="2025-03-20T00:00:00",
             OperatorDesignator="SWAA", AircraftMake=make, AircraftModel=model, JASCCode=jasc, PartName=part,
             PartCondition=cond, StageOfOperationCode="IN", HowDiscoveredCode="V", NatureOfConditionA="O",
             NatureOfConditionB="X", RegistryNNumber="N8600A", Discrepancy=text, **extra)  # fmt: skip
    return d


def csv_bytes(rows: list[dict[str, str]], header: list[str] = HEADER) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    return out.getvalue().encode()


# --- parsing and file checks (no database) ---


def test_rows_are_parsed_normalised_and_cleaned() -> None:
    rows = parse_file(csv_bytes([report("A1", text="LEAK\x00 AT\x07 FITTING\nSEE ATTACHED", model="7378")]), 2025)
    [r] = rows
    assert (r.sdr_id, r.difficulty_date, r.make, r.model, r.model_family) == ("A1", date(2025, 3, 14), "Boeing",
                                                                             "737-8", "737")  # fmt: skip
    assert (r.nature, r.submitted_on, r.operator_code, r.source_year) == (["O", "X"], date(2025, 3, 20), "SWAA", 2025)
    assert r.discrepancy == "LEAK AT FITTING\nSEE ATTACHED"  # control characters removed, newline kept
    assert not hasattr(r, "registration")  # the N-number isn't kept


def test_row_hash_changes_with_content_and_not_otherwise() -> None:
    a, b, c = (parse_file(csv_bytes([report("A1", text=t)]), 2025)[0] for t in ("X LEAK", "X LEAK", "Y LEAK"))
    assert a.row_hash == b.row_hash != c.row_hash
    assert parse_file(csv_bytes([report("A1", text="X LEAK")]), 2026)[0].row_hash != a.row_hash  # year counts


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html><body>Request Access</body></html>", "header differs"),
        (csv_bytes([report("A1")], header=HEADER[:-1]), "header differs"),
        (csv_bytes([report("A1")], header=[*HEADER[:5], "Renamed", *HEADER[6:]]), "header differs"),
        (b"\xff\xfe" + csv_bytes([report("A1")]), "not UTF-8"),
        (csv_bytes([]), "no rows"),
        (csv_bytes([report(f"A{i}", make="" if i < 5 else "BOEING") for i in range(100)]), "AircraftMake empty"),
        (
            csv_bytes([report(f"A{i}", when="2025-03-14" if i < 2 else "03/14/2025") for i in range(100)]),
            "rows unreadable",
        ),
    ],
)
def test_bad_files_are_rejected_whole(body: bytes, reason: str) -> None:
    with pytest.raises(FileRejected, match=reason):
        parse_file(body, 2025)


def test_a_few_bad_rows_are_dropped_without_rejecting_the_file() -> None:
    rows = [report(f"A{i}") for i in range(200)] + [report("", text="no id"), report("A0", text="duplicate id")]
    parsed = parse_file(csv_bytes(rows), 2025)
    assert len(parsed) == 200 and parsed[0].discrepancy.startswith("BRAKE")


# --- sync on Postgres ---


@dataclass
class FakeFAA:
    files: dict[int, bytes] = field(default_factory=dict)
    fetched: list[int] = field(default_factory=list)

    def handler(self, req: httpx2.Request) -> httpx2.Response:
        year = int(req.url.path.rsplit("-", 1)[-1].removesuffix(".csv"))
        self.fetched.append(year)
        if year not in self.files:
            return httpx2.Response(404)
        return httpx2.Response(200, content=self.files[year], headers={"content-type": "text/csv"})


@dataclass
class Env:
    settings: Settings
    pool: AsyncConnectionPool
    fetcher: SafeFetcher
    faa: FakeFAA

    async def sync(self, years: list[int]) -> Any:
        return await sync_sdr(self.settings, self.fetcher, self.pool, years=years)

    async def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()


@pytest.fixture
def clean(db: Settings) -> Iterator[None]:
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE sdr, model_aliases, quarantine, ingest_runs")
    yield


@pytest.fixture
async def env(db: Settings, clean: None) -> AsyncIterator[Env]:
    faa = FakeFAA()

    async def resolver(host: str) -> list[str]:
        return ["23.10.1.1"]

    async with (
        AsyncConnectionPool(db.dsn("ingest_rw"), min_size=1, max_size=2, open=False) as pool,
        httpx2.AsyncClient(transport=httpx2.MockTransport(faa.handler)) as http,
    ):
        fetcher = SafeFetcher(http, allowed(db.ingest_allowed_hosts), "test", resolver=resolver, min_interval_s=0,
                              attempts=1)  # fmt: skip
        yield Env(db, pool, fetcher, faa)


YEAR_2025 = [report(f"S25-{i:03d}") for i in range(40)]
YEAR_2026 = [report(f"S26-{i:03d}", "01/05/2026", model="CL6002D24", make="CNDAIR", jasc="2130") for i in range(20)]


async def test_sync_inserts_then_reports_unchanged(env: Env) -> None:
    env.faa.files = {2025: csv_bytes(YEAR_2025), 2026: csv_bytes(YEAR_2026)}
    r = await env.sync([2025, 2026])
    assert (r.fetched, r.inserted, r.updated, r.unchanged, r.tombstoned, r.quarantined) == (60, 60, 0, 0, 0, 0)
    [(fam, model, n)] = await env.rows("SELECT model_family, model, count(*) FROM sdr WHERE source_year = 2026 "
                                       "GROUP BY 1, 2")  # fmt: skip
    assert (fam, model, n) == ("CRJ", "CRJ900", 20)
    assert await env.rows("SELECT kind, raw, canonical FROM model_aliases ORDER BY kind, raw") == [
        ("make", "BOEING", "Boeing"), ("make", "CNDAIR", "Bombardier"),
        ("model", "BOEING 7378H4", "737-800"), ("model", "CNDAIR CL6002D24", "CRJ900")]  # fmt: skip
    r = await env.sync([2025, 2026])
    assert (r.inserted, r.updated, r.unchanged, r.tombstoned) == (0, 0, 60, 0)
    assert await env.rows("SELECT status, fetched, inserted, tombstoned FROM ingest_runs ORDER BY id") == [
        ("succeeded", 60, 60, 0), ("succeeded", 60, 0, 0)]  # fmt: skip


async def test_amended_reports_update_and_withdrawn_ones_are_tombstoned_in_their_year_only(env: Env) -> None:
    env.faa.files = {2025: csv_bytes(YEAR_2025), 2026: csv_bytes(YEAR_2026)}
    await env.sync([2025, 2026])
    amended = [*YEAR_2025[:37], report("S25-037", text="AMENDED: BRAKE ASSEMBLY CRACKED.")]  # 38, 39 withdrawn
    env.faa.files[2025] = csv_bytes(amended)
    r = await env.sync([2025])  # 2026 isn't re-read, so nothing of it may be touched
    assert (r.inserted, r.updated, r.unchanged, r.tombstoned) == (0, 1, 37, 2)
    rows = await env.rows("SELECT sdr_id FROM sdr WHERE tombstoned_at IS NOT NULL ORDER BY 1")
    assert rows == [("S25-038",), ("S25-039",)]
    [(text,)] = await env.rows("SELECT discrepancy FROM sdr WHERE sdr_id = 'S25-037'")
    assert text.startswith("AMENDED")
    env.faa.files[2025] = csv_bytes(YEAR_2025)  # the withdrawn ones come back
    r = await env.sync([2025])
    assert (r.inserted, r.updated, r.tombstoned) == (0, 3, 0)
    assert await env.rows("SELECT count(*) FROM sdr WHERE tombstoned_at IS NOT NULL") == [(0,)]


async def test_a_report_moved_to_another_year_stays_live(env: Env) -> None:
    env.faa.files = {2025: csv_bytes(YEAR_2025), 2026: csv_bytes(YEAR_2026)}
    await env.sync([2025, 2026])
    moved = report("S25-000", "01/02/2026")  # the operator corrected the difficulty date
    env.faa.files = {2025: csv_bytes(YEAR_2025[1:]), 2026: csv_bytes([*YEAR_2026, moved])}
    r = await env.sync([2025, 2026])
    assert (r.updated, r.tombstoned) == (1, 1)  # tombstoned by 2025, then revived and moved by 2026
    assert await env.rows("SELECT source_year, difficulty_date, tombstoned_at FROM sdr WHERE sdr_id = 'S25-000'") == [
        (2026, date(2026, 1, 2), None)]  # fmt: skip


async def test_a_truncated_file_is_quarantined_and_tombstones_nothing(env: Env) -> None:
    env.faa.files = {2025: csv_bytes(YEAR_2025)}
    await env.sync([2025])
    env.faa.files[2025] = csv_bytes(YEAR_2025[:30])  # 75% of what's stored
    r = await env.sync([2025])
    assert (r.quarantined, r.tombstoned, r.inserted, r.updated) == (1, 0, 0, 0)
    assert await env.rows("SELECT count(*) FROM sdr WHERE tombstoned_at IS NULL") == [(40,)]
    [(reason,)] = await env.rows("SELECT reason FROM quarantine")
    assert "30 reports, but 40 already stored" in reason
    [(status,)] = await env.rows("SELECT status FROM ingest_runs ORDER BY id DESC LIMIT 1")
    assert status == "failed"


async def test_one_missing_year_fails_the_run_but_keeps_the_others(env: Env) -> None:
    env.faa.files = {2026: csv_bytes(YEAR_2026)}
    r = await env.sync([2025, 2026])
    assert (r.failed, r.inserted) == (1, 20) and "HTTP 404" in r.errors[0]
    assert env.faa.fetched == [2025, 2026]
    [(status, error)] = await env.rows("SELECT status, error FROM ingest_runs")
    assert status == "failed" and "2025" in error


async def test_a_bad_header_is_quarantined_without_touching_stored_reports(env: Env) -> None:
    env.faa.files = {2025: csv_bytes(YEAR_2025)}
    await env.sync([2025])
    env.faa.files[2025] = b"<html>Request Access</html>"
    r = await env.sync([2025])
    assert (r.quarantined, r.tombstoned) == (1, 0)
    assert await env.rows("SELECT count(*) FROM sdr WHERE tombstoned_at IS NULL") == [(40,)]


# --- the query_sdr tool, as tools_ro ---


@pytest.fixture
async def tools(env: Env, db: Settings) -> AsyncIterator[Tools]:
    rows = [
        report("Q1", "02/10/2025", text="LH MAIN GEAR BRAKE OVERHEAT WARNING ON TAXI IN. BRAKE REPLACED."),
        report("Q2", "03/11/2025", model="7378", text="NO 3 BRAKE WORN. REPLACED BRAKE ASSEMBLY."),
        report("Q3", "03/12/2025", model="737824", jasc="3242", part="BRAKE 100% WEAR", text="BRAKE PIN AT LIMIT."),
        report("Q4", "04/01/2025", model="737824", jasc="2130", part="OUTFLOW VALVE", cond="FAILED",
               text="CABIN ALTITUDE WARNING. OUTFLOW VALVE FAILED; REPLACED."),
        report("Q5", "04/02/2025", make="AIRBUS", model="A320214", jasc="3242", text="BRAKE TEMP HIGH."),
        report("Q6", "05/02/2025", text="'; DROP TABLE sdr; -- BRAKE"),
    ]  # fmt: skip
    env.faa.files = {2025: csv_bytes(rows)}
    await env.sync([2025])
    async with AsyncConnectionPool(db.dsn("tools_ro"), min_size=1, max_size=2, open=False) as ro:
        yield Tools(retriever=cast(Retriever, None), ro_pool=ro)


async def call(tools: Tools, **args: Any) -> Any:
    import json

    return await tools.call("query_sdr", json.dumps(args), CallContext(kind="agent_tool"))


async def test_list_matches_model_or_family_newest_first_with_citable_evidence(tools: Tools) -> None:
    r = await call(tools, mode="list", aircraft="Boeing 737-800", text="brake")
    assert not r.error
    assert list(r.evidence) == ["sdr:Q6", "sdr:Q3", "sdr:Q1"]  # 737-800 only: Q2 is a 737-8 (MAX)
    assert "aircraft '737-800'" in r.text and "3 reports (2025-02-10 to 2025-05-02)" in r.text
    assert "not failure rates" in r.text
    family = await call(tools, mode="list", aircraft="737", text="brake")
    assert set(family.evidence) == {"sdr:Q1", "sdr:Q2", "sdr:Q3", "sdr:Q6"}
    cites, dropped = check_citations([Citation(chunk_id="sdr:Q1", quote="BRAKE OVERHEAT WARNING ON TAXI IN"),
                                      Citation(chunk_id="sdr:Q1", quote="the brake caught fire on landing")],
                                     r.evidence)  # fmt: skip
    assert [c["doc_id"] for c in cites] == ["sdr:Q1"] and dropped == ["sdr:Q1: quote not found in passage"]


async def test_count_modes(tools: Tools) -> None:
    by_jasc = await call(tools, mode="count_by_jasc", aircraft="737")
    assert by_jasc.text.splitlines()[1:3] == ["JASC 3242: 4", "JASC 2130: 1"] and by_jasc.evidence == {}
    by_part = await call(tools, mode="count_by_part", jasc="32")
    assert "part BRAKE: 4" in by_part.text  # Q1, Q2, Q5, Q6; Q3's part is "BRAKE 100% WEAR"
    by_month = await call(tools, mode="count_by_month", aircraft="737", date_from="2025-03-01", date_to="2025-04-30")
    assert by_month.text.splitlines()[1:][:2] == ["month 2025-03: 2", "month 2025-04: 1"]


async def test_part_filter_treats_percent_and_underscore_literally(tools: Tools) -> None:
    r = await call(tools, mode="list", part="100%")
    assert list(r.evidence) == ["sdr:Q3"]
    assert (await call(tools, mode="list", part="%%")).evidence == {}


async def test_injection_attempts_are_just_search_terms(tools: Tools, env: Env) -> None:
    for args in ({"aircraft": "737' OR '1'='1"}, {"text": "'; DROP TABLE sdr; --"}, {"part": "x' OR 1=1 --"}):
        r = await call(tools, mode="list", **args)
        assert not r.error
    assert (await call(tools, mode="list", aircraft="737' OR '1'='1")).evidence == {}
    assert await env.rows("SELECT count(*) FROM sdr") == [(6,)]


async def test_withdrawn_reports_are_not_returned(tools: Tools, env: Env) -> None:
    async with env.pool.connection() as conn:
        await conn.execute("UPDATE sdr SET tombstoned_at = now() WHERE sdr_id = 'Q1'")
    assert "sdr:Q1" not in (await call(tools, mode="list", aircraft="737", text="brake")).evidence


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"mode": "list"}, "give at least one of aircraft, jasc, text or part"),
        ({"mode": "list", "jasc": "32A"}, "jasc"),
        ({"mode": "sum", "jasc": "32"}, "mode"),
        ({"mode": "list", "jasc": "32", "limit": 500}, "limit"),
        ({"mode": "list", "jasc": "32", "date_from": "2025-05-01", "date_to": "2025-01-01"}, "date_from is after"),
    ],
)
async def test_invalid_arguments_go_back_to_the_model(tools: Tools, args: dict[str, Any], message: str) -> None:
    r = await call(tools, **args)
    assert r.error and message in r.text and "call again" in r.text


async def test_no_match_suggests_the_family(tools: Tools) -> None:
    r = await call(tools, mode="list", aircraft="A320-251N")
    assert r.evidence == {} and "0 reports" in r.text and "Try the family" in r.text


async def test_list_output_stays_under_the_tool_cap(env: Env, db: Settings) -> None:
    long = "CORROSION FOUND ON LOWER SKIN. " * 300
    env.faa.files = {2025: csv_bytes([report(f"L{i:02d}", text=long) for i in range(20)])}
    await env.sync([2025])
    async with AsyncConnectionPool(db.dsn("tools_ro"), min_size=1, max_size=1, open=False) as ro:
        r = await call(Tools(retriever=cast(Retriever, None), ro_pool=ro), mode="list", aircraft="737", limit=20)
    assert len(r.text) <= MAX_TOOL_CHARS + 100 and 5 <= len(r.evidence) < 20


def test_tools_ro_cannot_write_or_read_the_base_table(connect: Any, db: Settings) -> None:
    """Two layers: no grants on the base tables, and tools_ro sessions default to read-only transactions."""
    ro = connect("tools_ro")
    for sql in ("SELECT count(*) FROM sdr", "UPDATE tools_sdr SET jasc = '0000'", "DELETE FROM model_aliases"):
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
            ro.execute(sql)
