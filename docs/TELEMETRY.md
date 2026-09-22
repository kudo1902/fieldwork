# fieldwork — Telemetry and alerting

What to measure, why, and what an alert looks like. Implements plan E1–E3 and the
three metrics named in [`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.6. Lifecycle
context (job states, SSE) lives in [`QUEUEING.md`](./QUEUEING.md).

Companion to [`RUNBOOK.md`](./RUNBOOK.md) — this document *defines* the numbers;
that one tells you what to do when they move.

---

## 1. What telemetry is for here

Three questions, one observability stack:

1. **Can the box keep up?** — throughput, latency, queue depth, GPU utilisation.
2. **Is accuracy silently drifting?** — the per-field null-rate canary.
3. **Is the human in the loop?** — review-queue size feeding the confidence threshold.

Everything defaults to self-hosted inside the private network: an OpenTelemetry
collector exports to Prometheus, Grafana reads Prometheus
([`SECURITY.md`](./SECURITY.md) §9). Grafana's own auth rides on the human-session
path (plan D2); nothing telemetry-related gets a published port.

---

## 2. Traces — one per extraction

Auto-instrument Flask, SQLAlchemy, and Redis
(`opentelemetry-instrumentation-*`). A single extraction is one trace:

```
api (enqueue) → redis (enqueue) → worker (claim) → worker stages → vLLM → postgres (write)
```

Rules that keep traces useful:

- **`PROMPT_VERSION` is a span attribute** on the inference span. When the nightly
  eval regresses you filter by it; without it, "what prompt was live?" is a git
  archaeology question.
- **RQ does not propagate context for you.** Stash `{trace_id, span_id}` in
  `job.meta` by hand ([`QUEUEING.md`](./QUEUEING.md) §3, plan E1) — otherwise the
  trace breaks exactly at the queue boundary, where it matters most.
- **Stage-named worker spans** (`preprocessing`, `inferring`, `validating`,
  `rules`, `routing`). Breaks p95 down to the stage that owns it: inference should
  dominate; if preprocessing does, bump `max_dim`, not the prompt.
- Keep attribute cardinality bounded: `job_id`, `document_id`, `tenant_id`,
  `schema_name`, `prompt_version`, `model_id`, `max_dim`, `pages`,
  `repair_attempts`, `cached`. No extracted payload values, ever
  ([`SECURITY.md`](./SECURITY.md) §5 — payload content is PII, audit-log matters).

---

## 3. The three metrics that predict problems

These are the reason this project has telemetry at all. The rest of the dashboards
are decoration; these three are early warning.

| # | Metric | What it predicts | How computed | Alert on |
| --- | --- | --- | --- | --- |
| 1 | **Queue depth** (`interactive`, `bulk`) | GPU/worker saturation — first thing to move when throughput falls | Redis `LLEN`, a low-frequency gauge | depth > N sustained 5 min (N sized by the capacity method in ARCHITECTURE §6) |
| 2 | **Per-field null rate** | Drift canary — a change upstream (new template, resize, model swap) | **bounded-cardinality gauge**, see below | a field's 24h rate jumping > 3× its 7-day baseline |
| 3 | **Review-queue size** | Confidence threshold mismatch / reviewer can't keep up | gauge written by the routing step (plan C5) | size > reviewer throughput over the last hour |

**Metric 2 is the one most often built wrong.** Per-request labels like
`field=<vendor_name>` on a logged "is this field null" counter are fine (bounded by
schema fields), but do not create one timeseries *per extraction*. Build it as a
low-frequency (`cron`-style) aggregation job: for the last 24h window compute null
rate grouped by `(schema_name, field_name)` from Postgres and write it as a gauge
with those two labels only.

**The null-rate jump IS the drift gate** the nightly eval formalises (plan E5):
2% → 30% on one field means something changed upstream. Both the metric and the
eval gate exist — the metric fires in hours, the gate fires on a schedule.

---

## 4. Standard metrics (needed, but boring)

| Layer | Metrics | Source |
| --- | --- | --- |
| HTTP (api) | RED: rate, errors, duration | auto-instrumentation |
| Queue | job duration, requeue count, FailedJobRegistry size | RQ integration / gauges |
| vLLM | request latency, prompt/completion tokens, prefix-cache hit rate, GPU util/VRAM | vLLM `/metrics` (private net only) + DCGM exporter |
| Postgres | connections, slowest queries, table growth (esp. `extractions`, `audit_log`) | postgres_exporter |
| MinIO | bucket size, lifecycle deletions pending | minio metrics |

Note what is **not** here: per-field accuracy/hallucination as a timeseries. Those
belong to the eval harness and are computed by it
([`EVAL.md`](./EVAL.md)); alerting on them is the **nightly eval gate** (plan E5),
not a Prometheus alert. Do not build two sources of truth for accuracy.

---

## 5. Latency SLO

The capacity method ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §6) is built around "p95
exceeding your SLO" — so the SLO has to exist before you can use the method.
It's a plan decision (#7, default in `IMPLEMENTATION_PLAN.md`):

> **p95 extraction latency < 30 s** at default `max_dim` (1536) and `gemma-4-12B-it`,
> measured on the eval set at concurrency 1.

Once set:

- Verify with the eval's own p95 (`summary.json`), at the SLO's exact
  concurrency/max_dim — never guess from a dashboard spike.
- **Alert on error-budget burn, not on raw p95.** A burn-rate alert on the last
  5m vs 1h windows fires when the budget is actually being consumed in a quarter
  hour, and stays quiet through harmless traffic shapes. Alerting on the raw p95
  number instead pages you on every busy hour.
- The knob order stays the ARCHITECTURE one: `max_dim` / visual token budget →
  quantization → model size → a second GPU.

---

## 6. Dashboards and alert rules (plan E3)

**Dashboard panels** (one dashboard, four rows):

1. Throughput & health: `healthz` model available, docs/hour, exact-case rate.
2. Latency: stage-breakdown p50/p95, inference dominant; SLO burn panel.
3. Queues & GPU: depth (both queues), worker busyness, GPU util/VRAM.
4. Accuracy drift: per-field null-rate grid, review-queue size, DLQ size.

**Alert rules** (start here, one line each):

| Alert | Expression (sketch) | For | Severity |
| --- | --- | --- | --- |
| model_unavailable | `healthz ok == 0` | 2 min | critical |
| queue_stuck | Queue depth > budget AND no `done` events | 10 min | critical |
| null_rate_jump | per-field rate > 3× baseline | 30 min | warning |
| review_backlog | review size > hourly reviewer throughput | 1 h | warning |
| burn_rate | burn-rate alert on p95 SLO | 5 m | critical |
| dlq_growth | FailedJobRegistry > 50 | 1 h | warning |

Severity discipline: **critical pages, warning waits for business hours.** A single
operator who is woken for warnings is an operator who stops reading alerts.

---

## 7. Once it exists, the gates it enables

- **Plan A6** (`--max-dim` knee) and **A7** (model swap) are judged on eval + these
  metrics: the eval proves accuracy, the latency metric proves the SLO holds at the
  new config.
- **Plan C5** (confidence threshold) uses metric 3 as its feedback loop
  ([`CONFIDENCE.md`](./CONFIDENCE.md) §4).
- **Plan D6** (retention/reaper) uses the storage and table-growth panels to see a
  reaper stall.
- **The RUNBOOK triages every one of these alerts** — if a number moves and the
  runbook has no section for it, write that section down before declaring the
  problem solved.