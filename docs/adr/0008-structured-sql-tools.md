# ADR-0008: Structured query tools instead of free-form SQL

Status: Proposed · 2026-10-29

## Context
`PLAN.md` calls for "read-only SQL" in the agent. The main SQL workload is SDR questions: filtering by make, model, JASC code, part and date, and aggregates such as "most common JASC codes on CRJ-900 this year".

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Free-form `run_sql` with a read-only role | Flexible | Injection and exfiltration surface; the model writes wrong SQL; hard to test; prompt-injected documents could steer queries |
| **Structured tool (`query_sdr`) with filters and aggregation modes** | Parameterised SQL only; strict schema validation; easy contract tests; spike: both shortlisted models filled it correctly 16/16 | Less flexible; new question types need new modes |

## Decision
Use **structured tools**: `query_sdr` with `mode` in {list, count_by_jasc, count_by_part, count_by_month}, executed as parameterised SQL under the `tools_ro` role against allow-listed views, with a 5 s statement timeout and a 100-row limit. Make and model inputs pass through `model_aliases` normalisation.

## Consequences
- If Phase 3 evals show SQL-type questions that the modes can't answer, add a mode. Free-form SQL is considered again only with an allow-listed view, an AST-checked SELECT-only query and the same role limits.
- The read-only role is enforced at the database level from Phase 2, not only in code.
