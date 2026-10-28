"""Shared helpers for the Phase 0 spike. Throwaway code: not production quality."""

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).parent
load_dotenv(ROOT.parent / ".env")

BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
BUDGET_USD = float(os.environ.get("SPIKE_BUDGET_USD", "1.00"))
LEDGER = ROOT / ".cache" / "spend.jsonl"

# Exclude providers that store or train on prompts (see docs/DATA.md).
PROVIDER_PREFS = {"data_collection": "deny", "require_parameters": True}


class BudgetExceeded(RuntimeError):
    pass


def client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set (put it in ../.env)")
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=60, max_retries=1)


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(line)["cost"] for line in LEDGER.read_text().splitlines() if line)


def record(kind: str, model: str, usage, latency_s: float) -> float:
    """Append one request's real cost (from OpenRouter's usage.cost) to the ledger."""
    u = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage or {})
    cost = float(u.get("cost") or 0.0)
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({
            "ts": time.time(), "kind": kind, "model": model, "cost": cost,
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
            "latency_s": round(latency_s, 3),
        }) + "\n")
    return cost


def strict_schema(model_cls) -> dict:
    """Pydantic JSON schema -> OpenAI 'strict' form: every object closed (additionalProperties: false)
    and every property required (optional ones stay nullable). GPT-6 Luna rejects the default schema."""
    schema = model_cls.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            node.pop("default", None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


_SUPPORTED: dict[str, set[str]] = {}


def supported_params(model: str) -> set[str]:
    """Parameters the model's endpoints accept (from /models). With require_parameters=True,
    sending an unsupported one (e.g. temperature on claude-haiku-5.5) makes OpenRouter return 404."""
    if not _SUPPORTED:
        import urllib.request
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=30) as r:
            for m in json.load(r)["data"]:
                _SUPPORTED[m["id"]] = set(m.get("supported_parameters") or [])
    return _SUPPORTED.get(model, set())


def optional(model: str, **params) -> dict:
    """Keep only the optional sampling params this model supports."""
    ok = supported_params(model)
    return {k: v for k, v in params.items() if k in ok}


def check_budget() -> None:
    """Hard cap: refuse to send another request once the ledger reaches the budget."""
    if (s := spent()) >= BUDGET_USD:
        raise BudgetExceeded(f"spike budget reached: ${s:.4f} >= ${BUDGET_USD:.2f}")
