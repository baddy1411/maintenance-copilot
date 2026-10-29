# ADR-0006: Self-hosted Langfuse with an OpenTelemetry collector

Status: Proposed · 2026-10-29. Finalised in Phase 7 after checking the current GenAI semantic conventions and Langfuse's OTel ingestion.

## Context
We need end-to-end traces across API, agent, tools, LLM calls and the database, plus a dashboard for latency, errors, tokens, cost, cache hit rate, router split, guardrail triggers and eval scores over time. We also need online evaluation linked to traces, and thumbs-up/down feedback. It must be free or self-hosted.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| **Langfuse (self-hosted, MIT core) + OTel collector** | LLM-native: prompt and response views, cost, scores and feedback on traces, datasets; accepts OTel | Its own stack to run (Postgres, ClickHouse, Redis in recent versions; footprint to be measured); infrastructure metrics are weaker |
| Grafana stack (Tempo, Loki, Prometheus) | Strong for infrastructure metrics, alerts and dashboards; standard | No LLM-specific views; scores and feedback are DIY |
| Both | Best of each | More to run locally |

## Decision
**Instrument with OpenTelemetry only**, with no vendor SDK in app code, and export to a collector. Where the collector sends data depends on the environment:
- **Locally (optional Compose profile):** traces go to **Langfuse**, and metrics go to Prometheus with Grafana for dashboards and alerts. `make up` stays fast without them.
- **On AWS (revised after ops review O9):** traces go to **X-Ray** and metrics and alerts to **CloudWatch**. Langfuse's ClickHouse stack is not self-hosted on Fargate for a demo. The screenshot dashboards for Phase 7 come from the local stack.
- **Alerts that must always work** (ingestion freshness, failed jobs, spend breaker, canary) are sent by the worker itself, by email or webhook, so they don't depend on either backend (DESIGN §6.2).

## Consequences
- App code never depends on Langfuse; we can swap backends by changing collector config.
- Phase 7 must check that Langfuse's resource footprint fits a laptop, and confirm its current licence for self-hosted features.
