# ADR-0007: Evaluation tooling

Status: Proposed · 2026-10-29. Finalised in Phase 3 (Opus designs, the user approves).

## Context
We need retrieval metrics (recall@k, MRR, nDCG), answer metrics (groundedness, citation correctness, correctness, refusal), agent metrics (tool choice, argument validity, task success, steps, cost), a CI gate, and a calibrated judge.

## Options (versions checked 2026-10-29)
| Option | Notes |
| --- | --- |
| **Light pytest harness** | Full control; metrics are simple to compute; the judge rubric is our own and calibrated against hand labels; fits CI directly |
| Ragas 0.4.3 (Apache-2.0) | Ready-made RAG metrics; judge prompts are its own, so they need calibration |
| DeepEval 4.3.2 (Apache-2.0) | pytest-style; many metrics; heavier dependency |
| promptfoo 0.125 (npm, MIT) | Good for prompt and model comparisons and red-teaming; Node toolchain |

## Decision (provisional)
Own the core in a **pytest harness**: the dataset format, retrieval metrics, agent metrics, the CI gate and PR comments. Use a library metric only after it agrees with our hand labels (target: Cohen's κ ≥ 0.6 on about 30 labelled examples). Consider promptfoo for the Phase 5 red-team suite.

## Consequences
The spike's `s2_retrieval.py` metrics move into the harness as the first metric module, and its 40 labelled questions seed the golden set.
