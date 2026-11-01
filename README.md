# Maintenance Copilot

**Status: in progress (Phase 2 of `PLAN.md`).** Nothing here is production-ready yet, and no claim on this page is unsupported. Measured results so far are in [`docs/SPIKE.md`](docs/SPIKE.md).

Cited Q&A and an approval-gated troubleshooting agent over live FAA maintenance data: Airworthiness Directives, Service Difficulty Reports and the FAA AMT handbooks.

- Scope: [`docs/SCOPE.md`](docs/SCOPE.md) · Data: [`docs/DATA.md`](docs/DATA.md) · Design (approved v2): [`docs/DESIGN.md`](docs/DESIGN.md) · Decisions: [`docs/adr/`](docs/adr/) · Progress: [`docs/PROGRESS.md`](docs/PROGRESS.md) · Build log: [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) · Ingestion: [`docs/ingestion.md`](docs/ingestion.md) · Retrieval: [`docs/retrieval.md`](docs/retrieval.md) · Q&A: [`docs/qa.md`](docs/qa.md) · Agent runs: [`docs/agent-runs.md`](docs/agent-runs.md) · Approvals: [`docs/approvals.md`](docs/approvals.md) · Auth: [`docs/auth.md`](docs/auth.md)

## Development

You need Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env   # then fill in the values
make db                # Postgres + pgvector, bootstrapped
make migrate           # apply all migrations
make test              # unit + integration tests (testcontainers)
make lint              # ruff + mypy
```

Every API call needs a token. Locally, the dev issuer that `make env` sets up provides them:

```bash
TOKEN=$(uv run copilot dev-token --user tech-1 --role technician)   # or supervisor, admin
curl -H "Authorization: Bearer $TOKEN" localhost:8000/runs/<run_id>
```
