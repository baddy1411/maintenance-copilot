# ADR-0005: AWS hosting on ECS Fargate

Status: Proposed · 2026-10-29 · revised in DESIGN v2 (cost review C3, C8). Costs are re-estimated with current prices before any `terraform apply` in Phase 8.

## Context
We need containers for `api`, `worker` and `mcp`, plus RDS Postgres with pgvector, deployed with Terraform and scaled horizontally, at portfolio cost, and torn down after the demo.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| **ECS Fargate + ALB** | Fine-grained autoscaling per service; SSE works through the ALB with a raised idle timeout; mature Terraform modules; no cluster to manage | ALB plus NAT cost even when idle |
| App Runner | Simplest; scale to near-zero | Fewer networking controls; long-lived SSE and the worker pattern are less natural; more limited VPC egress options |
| EKS | Matches the Helm target (Phase 8b) | Control-plane cost and much more operational work |
| A single EC2 instance with Compose | Cheapest | No autoscaling story for the load tests |

## Decision
- **ECS Fargate behind an ALB**, running three services from one image, plus the one-shot `migrate` task.
- **RDS Postgres:** Single-AZ for demos. Multi-AZ is a Terraform variable, switched on for any window whose availability we report.
- **Instance size:** db.t4g.large (8 GB) for full history (about 200k `halfvec` chunks; SDR is FTS-only).
- **Secrets:** Secrets Manager. **No ElastiCache** (ADR-0009).
- **Kubernetes:** through the Phase 8b Helm chart, tested on a local kind or k3d cluster, not EKS.

**Demo network topology (revised after cost review C3):**
- Fargate tasks run in **public subnets** with `assign_public_ip`. Their security groups allow inbound only from the ALB.
- No NAT gateway and no interface endpoints. NAT is needed anyway for egress to OpenRouter and FAA, so interface endpoints would add about $1.92 per day to save cents.
- RDS sits in private subnets with no public access.
- "Private subnets + NAT" is a Terraform variable for anyone deploying it for real.

### Idle cost per day (us-east-1, **all prices ASSUMED**, re-priced before any apply)
| Item | Demo default | Private + Multi-AZ |
| --- | --- | --- |
| ALB | $0.73 | $0.73 |
| NAT gateway | — | $1.08 per AZ |
| Interface endpoints (4 × 2 AZ) | — | $1.92 (optional) |
| RDS db.t4g.medium Single-AZ / db.t4g.large Multi-AZ | $1.56 | ~$6.2 |
| Fargate, 3–4 tasks at 0.5 vCPU / 1 GB | $1.8–2.4 | $1.8–2.4 |
| Public IPv4 | $0.36–0.48 | $0.12 |
| Storage, logs, Secrets Manager | ~$0.10 | ~$0.15 |
| **Total** | **≈ $4.6–5.3 per day** | **≈ $11–14 per day** |

A stack forgotten for two weeks would cost about $70–200, which is more than all the model spend so far.

## Consequences
- An AWS Budget alarm is created before the first apply (`PLAN.md` Phase 8). Every resource carries a `ttl` tag, and a scheduled reminder fires when the stack is older than its TTL. `make destroy` removes everything except the final snapshot, which is kept by default (DESIGN §5.3).
- Load-test model spend is estimated and capped per run. Infrastructure-only ramps use a stub LLM endpoint.
