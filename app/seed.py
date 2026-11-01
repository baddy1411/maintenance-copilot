"""Synthetic fleet (DATA.md "Synthetic data"): fictional MX-### registrations on real aircraft types, so AD
and handbook lookups are meaningful. Only `migrator` can write `fleet`, so this is an operator command
(`copilot seed`). It's idempotent."""

import psycopg

from app.config import Settings

FLEET = [  # registration, type, engines, hours, cycles
    ("MX-001", "Boeing 737-800", "CFM56-7B26", 41210.5, 24890),
    ("MX-002", "Boeing 737-800", "CFM56-7B26", 38774.0, 22105),
    ("MX-003", "Airbus A320-214", "CFM56-5B4/P", 35120.2, 20977),
    ("MX-004", "Airbus A320-214", "CFM56-5B4/P", 29840.7, 18322),
    ("MX-005", "Bombardier CRJ900 (CL-600-2D24)", "GE CF34-8C5", 31055.1, 27410),
    ("MX-006", "Embraer ERJ 170-200 (E175)", "GE CF34-8E5", 18402.3, 14120),
    ("MX-007", "ATR 72-212A (72-600)", "PW127M", 22517.8, 26340),
    ("MX-008", "De Havilland DHC-8-402 (Dash 8-400)", "PW150A", 27760.4, 30215),
    ("MX-009", "Boeing 787-9", "GEnx-1B74/75", 21003.6, 3140),
    ("MX-010", "Airbus A350-941", "Trent XWB-84", 15880.0, 2210),
    ("MX-011", "Cessna 208B Grand Caravan", "PT6A-114A", 9120.5, 11030),
    ("MX-012", "Bell 412EP", "PT6T-3D", 7342.9, 15880),
]


def seed_fleet(settings: Settings) -> int:
    with psycopg.connect(settings.dsn("migrator"), autocommit=True) as conn:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO fleet (registration, type, engines, hours, cycles) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (registration) DO UPDATE SET type = EXCLUDED.type, engines = EXCLUDED.engines",
            FLEET,
        )
        row = conn.execute("SELECT count(*) FROM fleet").fetchone()
    return int(row[0]) if row else 0
