"""Two processes, one SIGKILL: an agent run survives its worker dying (ADR-0004; SPIKE §3 marked this
UNVERIFIED). Needs Docker."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings
from tests.agent_fixtures import create_run, events, run_row, seed

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


def proc_env(db: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "ENV"))}
    env |= {"ENV": "test", "DB_HOST": db.db_host, "DB_PORT": str(db.db_port), "DB_NAME": db.db_name}
    for role in ("migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw"):
        env[f"DB_PASSWORD_{role.upper()}"] = getattr(db, f"db_password_{role}").get_secret_value()
    return env


def start(mode: str, run_id: object, tmp: Path, db: Settings) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - our own interpreter and module, fixed arguments
        [sys.executable, "-m", "tests.agent_proc", mode, str(run_id), str(tmp / "marker"), str(tmp / "calls.log")],
        cwd=ROOT, env=proc_env(db), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def test_run_resumes_in_a_new_process_after_sigkill(db: Settings, tmp_path: Path) -> None:
    seed(db)
    run_id = create_run(db)

    first = start("hang", run_id, tmp_path, db)
    deadline = time.monotonic() + 60
    while not (tmp_path / "marker").exists():
        assert first.poll() is None, first.communicate()
        assert time.monotonic() < deadline, "worker never reached the third act turn"
        time.sleep(0.05)
    first.send_signal(signal.SIGKILL)  # no cleanup, no finally blocks: a dead worker
    first.wait(timeout=10)
    assert first.returncode == -signal.SIGKILL

    assert run_row(db, run_id)["status"] == "running"  # left looking alive, which is what reap_runs is for
    assert [k for k, _ in events(db, run_id)].count("tools") == 2  # two completed tool rounds
    calls_before = (tmp_path / "calls.log").read_text().splitlines()
    assert calls_before == ["act 0", "act 1", "act 2"]

    second = start("finish", run_id, tmp_path, db)
    out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    assert out.strip() == "pending_approval"

    calls_after = (tmp_path / "calls.log").read_text().splitlines()[len(calls_before) :]
    # Resumed at the interrupted step: act turn 2 again, then diagnose and draft. Turns 0 and 1 and their
    # tool rounds were not redone.
    assert calls_after == ["act 2", "structured Diagnosis", "structured WorkOrderDraft"]
    statuses = [p for k, p in events(db, run_id) if k == "status"]
    assert statuses[-2] == {"status": "running", "attempt": 2, "resumed": True}
    assert statuses[-1]["status"] == "pending_approval"
    assert [k for k, _ in events(db, run_id)].count("tools") == 2
    assert run_row(db, run_id)["attempts"] == 2
