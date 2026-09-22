# fieldwork — Implementation Plan

Companion to [`ARCHITECTURE.md`](./ARCHITECTURE.md). That document says *what* the system is;
this one says *in what order to build it, and how you know each step is done*.

**Sizes** assume one developer who knows the stack: `S` ≈ half a day, `M` ≈ 1–2 days,
`L` ≈ 3–5 days. They are relative weights, not commitments.

**The critical path runs through Stage A.** Every estimate after it is conditional on the
baseline numbers, because those numbers decide the model size, the resize default, and
whether the product is viable at all. Do not start Stage B before Stage A's gate.

---

## Stage A — Ground truth and baseline

Nothing here ships to a user. It exists so that every later decision is measured instead of
argued. This is the stage people skip and the one that determines whether the rest works.

| ID | Task | Files | Size | Depends |
| --- | --- | --- | --- | --- |
| A1 | Stand up vLLM on the GPU box, serving `gemma-4-12B-it`. Assumes the box is already provisioned (NVIDIA toolkit, drivers, VRAM) — if it is not, that provisioning is your real A0 | — | M | — |
| A2 | Point `.env` at the box, smoke-test the Phase 1 UI on 3 real documents | `.env` | S | A1 |
| A3 | Collect 30–50 documents into `eval/dataset/<schema>/` | `eval/dataset/` | **L** | — |
| A4 | Write ground truth; pass `run_eval.py --check`, then spot-check a sample against the originals — `--check` proves schema shape, not correctness | `eval/dataset/` | **L** | A3 |
| A5 | Baseline run, tagged and saved | `eval/runs/` | S | A2, A4 |
| A6 | Sweep `--max-dim` (1024 / 1536 / 2048), pick the knee | — | S | A5 |
| A7 | Sweep model size (12B vs 31B) if A5 is short of target | — | M | A5 |
| A8 | Evaluate FP8 / AWQ quantization against the eval set | — | M | A6 |
| A9 | Freeze defaults in `config.py`; record the decision in the README | `src/fieldwork/config.py` | S | A6–A8 |
| A10 | Introduce `PROMPT_VERSION` in `prompts.py`; stamp it into eval run metadata | `src/fieldwork/prompts.py`, `eval/run_eval.py` | S | A5 |

**A3/A4 are the two biggest tasks in the entire plan and they are pure manual labour.**
Budget for that honestly. Deliberately include documents where fields are genuinely absent —
that subset is the only way to measure hallucination, and it is the one everyone forgets.

**Watch out**
- vLLM flag syntax drifts between releases; check the recipe against your installed version.
- If it OOMs at startup, lower `--max-model-len` before reaching for quantization.
- Verify `FIELDWORK_EXTRA_BODY` (visual token budget) actually moves `prompt_tokens`. If it
  does not, delete it and use `max_dim` alone.
- Add A10 (`PROMPT_VERSION`) before any caching or DB schema work — the D4 cache key and the
  `extractions` unique constraint both need it, and retrofitting it later is a migration.

**Gate:** a baseline you trust, with hallucination rate reported separately. Before A5, agree
what "field accuracy" counts — the scorer weights every leaf equally and aligns line items
positionally (one shifted line = one MISSED + one HALLUCINATED), so a dense 30-item invoice
swamps a dozen receipts in the headline number. Decide line-item weighting first, or the A6
knee will reflect dataset composition, not model quality. If field accuracy is far off target
after A7 and A8, stop and reconsider scope — more engineering will not fix a model that cannot
read the documents.

---

## Stage B — Async backbone

Replaces the synchronous Phase 1 endpoint. No new user-visible capability, so resist the urge
to add features here; the point is that inference stops blocking HTTP.

| ID | Task | Files | Size | Depends |
| --- | --- | --- | --- | --- |
| B1 | Compose skeleton: postgres, redis, minio, vllm; healthchecks | `docker/compose.yml` | M | A1 |
| B2 | SQLAlchemy 2.0 models + Alembic baseline migration | `src/fieldwork/db/` | M | B1 |
| B3 | Storage adapter: presigned PUT/GET, bucket lifecycle | `src/fieldwork/storage.py` | M | B1 |
| B4 | `POST /v1/uploads` returning a presigned URL | `src/fieldwork/api.py` | S | B3 |
| B5 | RQ queue + worker; job lifecycle and state transitions | `src/fieldwork/queue.py`, `worker.py` | **L** | B2, B3 |
| B6 | `POST /v1/extractions` → 202 + `job_id`; `GET` for status | `src/fieldwork/api.py` | M | B5 |
| B7 | SSE endpoint relaying worker progress via Redis pub/sub | `src/fieldwork/api.py` | M | B5 |
| B8 | Retries, backoff, dead-letter queue | `src/fieldwork/worker.py` | S | B5 |
| B9 | Frontend: presigned upload, poll/SSE, progress states | `web/` | M | B4, B7 |
| B10 | Integration test: 50 documents through the queue | `tests/test_pipeline.py` | M | B8 |

**Watch out**
- **SSE through a proxy needs `flush_interval -1` in Caddy**, or responses buffer and progress
  arrives all at once at the end. This costs people an afternoon every time. (Caddy only
  arrives in D5 — while building B7, SSE runs over plain Flask, so this note bites at deploy
  time, not on your first B9 test.)
- MinIO needs CORS configured for browser PUTs, or the upload fails with an opaque error.
- Worker concurrency must exceed 1 per GPU or vLLM has nothing to batch. Start at 4.
- Keep `extract()` untouched. If Stage B forces changes to it, the seam was wrong.

**Gate:** 50 concurrent documents complete with zero lost jobs, progress visible throughout,
and killing a worker mid-job results in a retry rather than a silent drop.

---

## Stage C — Trust

This is what makes the output usable by someone who is not you. It is also the stage most
likely to be cut under pressure, and the one whose absence shows up as "we stopped using it".

| ID | Task | Files | Size | Depends |
| --- | --- | --- | --- | --- |
| C1 | Rules engine: arithmetic, date sanity, currency, format | `src/fieldwork/rules.py` | M | B5 |
| C2 | PaddleOCR service + raw-text extraction | `src/fieldwork/ocr.py` | M | B1 |
| C3 | Cross-check: assert each value appears in the OCR text | `src/fieldwork/ocr.py` | M | C2 |
| C4 | Confidence score combining C1 + C3 (+ logprobs) | `src/fieldwork/confidence.py` | M | C1, C3 |
| C5 | Routing: auto-accept above threshold, else review queue | `src/fieldwork/worker.py` | S | C4 |
| C6 | Review UI: image beside editable fields, keyboard-first | `web/` | **L** | B9, C5 |
| C7 | Corrections persisted, with a diff against the prediction | `src/fieldwork/db/`, `api.py` | M | C6 |
| C8 | Reviewed promotion of corrections into `eval/dataset/` | `eval/promote.py` | M | C7 |
| C9 | Re-baseline on the enlarged eval set | `eval/runs/` | S | C8 |

**Watch out**
- **C6 decides whether the system is used.** Review throughput is the real bottleneck in every
  human-in-the-loop product. Tab between fields, Enter to accept, never reach for the mouse.
  A slow review UI silently converts to "we stopped using it" about three weeks in.
- C8 must stay a deliberate, reviewed step. Auto-promoting corrections lets one careless
  reviewer poison the golden set, and you will not notice for months.
- Tune the C5 threshold against the eval set, not against intuition.

**Gate:** hallucination rate measured on the enlarged set and below your agreed ceiling; a
reviewer can clear a document in under 30 seconds.

---

## Stage D — Production readiness

| ID | Task | Files | Size | Depends |
| --- | --- | --- | --- | --- |
| D1 | API keys: hashed storage, scopes, per-tenant isolation | `src/fieldwork/auth.py` | M | B2 |
| D2 | OIDC/JWT for human sessions | `src/fieldwork/auth.py` | M | D1 |
| D3 | Rate limits and per-tenant quotas (Redis token bucket) | `src/fieldwork/api.py` | S | D1 |
| D4 | Result cache on the five-part key | `src/fieldwork/extract.py` | S | B2 |
| D5 | Caddy: TLS, body caps, SSE flush, security headers | `docker/Caddyfile` | S | B1 |
| D6 | Retention TTL, reaper job, deletion endpoint | `src/fieldwork/worker.py` | M | B2 |
| D7 | Audit log on every read of extracted data | `src/fieldwork/db/` | S | D1 |
| D8 | Secrets out of `.env` (SOPS/age or Vault) | `docker/` | S | D5 |
| D9 | Security review of uploads, SSRF, injection, tenancy | — | M | D1–D8 |

**Watch out**
- The cache key must include `prompt_version` (introduced in A10). Omit it and improving a
  prompt silently keeps serving the old answer for every document already seen.
- Tenant isolation belongs in a query-level filter, not in route handlers. One forgotten
  `WHERE tenant_id` is a cross-tenant data leak.

**Gate:** D9 passes, and vLLM is confirmed unreachable from outside the private network.

---

## Stage E — Operations

| ID | Task | Files | Size | Depends |
| --- | --- | --- | --- | --- |
| E1 | OTel auto-instrumentation; trace context into RQ job meta | `src/fieldwork/telemetry.py` | M | B5 |
| E2 | Prometheus metrics incl. queue depth and per-field null rate | `src/fieldwork/telemetry.py` | M | E1 |
| E3 | Grafana dashboards + alerts | `docker/grafana/` | M | E2 |
| E4a | Adopt the toolchain: dev deps + ruff/mypy/pytest configs, convert `tests/test_score.py` to pytest, annotate the untyped modules | `pyproject.toml`, `tests/` | M | — |
| E4 | CI: ruff, mypy, pytest on PR | `.github/workflows/` | S | E4a |
| E5 | Nightly eval against staging, gated on accuracy regression | `.github/workflows/` | M | A5, E4 |
| E6 | Backups: `pg_dump` cron, MinIO replication, restore drill | `docker/` | M | B1 |

**Watch out**
- RQ will not propagate trace context for you; put it in the job `meta` by hand or your
  traces break at the queue boundary, which is exactly where you need them.
- **Per-field null rate is the drift canary.** A jump from 2% to 30% on one field means
  something changed upstream. Alert on it.
- E5 is the check that actually protects the product. Unit tests cannot tell you the model
  got worse.

**Gate:** a restore drill succeeds, and an induced accuracy regression fails the nightly build.

---

## Decisions still open

| # | Decision | Needed by | Default if unanswered |
| --- | --- | --- | --- |
| 1 | Which document types beyond invoice/receipt? | A3 | Invoice + receipt only |
| 2 | Single tenant or multi-tenant? | B2 | Single — but leave `tenant_id` on every table |
| 3 | Accuracy target and hallucination ceiling — including whether line items count in the "field accuracy" denominator | A5 gate | 95% field accuracy, <1% hallucination |
| 4 | Who reviews low-confidence results? | C6 | You; design for one reviewer |
| 5 | Retention period | D6 | 90 days |
| 6 | Expected volume | B5 sizing | 100 docs/day |
| 7 | Latency SLO — p95 extraction latency at default `max_dim` on 12B, measured at concurrency 1 | A5 / B5 sizing | 30 s (see [`TELEMETRY.md`](./TELEMETRY.md) §5) |

Decisions 2 and 6 are cheap now and expensive later. Multi-tenancy retrofitted onto a
single-tenant schema is a migration across every table.

Decision 7 is the SLO the capacity method ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §6) and the
p95 burn-rate alert key off — pick it before Stage B sizing, not after the box is running.

---

## Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Eval set never gets built; everything is tuned by vibes | **High** | Treat A3/A4 as the blocking gate they are. No Stage B until `--check` passes. |
| 12B is not accurate enough on your documents | Medium | A7/A8 before committing. Fall back to 31B or narrow the document scope. |
| Review UI too slow to use | Medium | Keyboard-first from the first commit; measure seconds-per-document. |
| GPU is a single point of failure | Medium | Queue absorbs outages; jobs retry. Accept the downtime or add a second box. |
| Scope creep into general document AI | Medium | The schema registry is the boundary. New document type = new Pydantic model, nothing else. |
| vLLM upgrade breaks flags or decoding | Low | Pin the image tag; re-run the eval after any bump. |

---

## Sequencing

```
A1─A2─┐
      ├─A5─A6─A7─A8─A9 ══ GATE ══╗
A3─A4─┘                          ║
                                 ▼
              B1─B2─B3─B4─B5─B6─B7─B8─B9─B10 ══ GATE ══╗
                                                       ▼
                        C1─C2─C3─C4─C5─C6─C7─C8─C9 ══ GATE ══╗
                                                             ▼
                                    D1…D9 ══ GATE ══► E1…E6
```

A3/A4 run in parallel with A1/A2 — the dataset needs no GPU. Today `eval/dataset/` holds only
`_template.expected.json`, so A3/A4 are the critical path from this moment; everything else in
the plan is gated on them.

E4 (CI) can also start immediately; it depends on nothing.

**Rough totals:** Stage A ≈ 2 weeks (dominated by A3/A4), B ≈ 2 weeks, C ≈ 2.5 weeks,
D ≈ 1.5 weeks, E ≈ 1.5 weeks. Call it **9–10 weeks** for one developer to a production system,
with a usable internal tool at the end of Stage C.
