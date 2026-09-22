# fieldwork — Queueing and job lifecycle

How work moves from the API to the GPU and back. Redis + RQ (§choice in
[`ARCHITECTURE.md`](./ARCHITECTURE.md), tasks B5–B8, worker deployment in
[`DEPLOYMENT.md`](./DEPLOYMENT.md)). SSE protocol in [`API.md`](./API.md) §5.

---

## 1. Non-negotiables

1. **`extract()` never changes.** The seam is `bytes → extraction`; if Stage B
   forces edits to it, the seam was wrong.
2. **An RQ worker runs one job at a time and forks per job.** Concurrency is the
   number of worker *processes*, not a thread or prefetch setting.
3. **Worker processes ≥ GPU slots × 2, minimum 4.** vLLM batches continuously; a
   single in-flight request leaves the GPU idle between tokens.
4. **Sync inside the job.** The job is a long request. Only the *boundary* is async.

---

## 2. Queues

Two queues, one worker pool:

```
rq worker interactive bulk      # command order = drain order
```

- **`interactive`** — a human is watching an SSE stream. Drained first.
- **`bulk`** — background throughput.

This is priority by *drain order*, not by interrupting a running job. A long bulk
job already in flight will not be preempted by an interactive enqueue. Accept that;
preemption is a scheduler feature you do not want to pay for. The mitigation for
interactive waits is the retry budget in §5, and the observation that bulk jobs are
short-lived by comparison with what a human perceives.

---

## 3. Job payload

What a job carries (in RQ `job.meta`, plus the arguments):

```json
{
  "document_id": "0195d4f9-...",
  "object_key": "1/9f/9f86...",
  "schema_name": "invoice",
  "prompt_version": 3,
  "model_id": "gemma-4-12B-it",
  "options": { "max_dim": 1536 },
  "tenant_id": 1,
  "trace_context": { "trace_id": "...", "span_id": "..." }
}
```

- `trace_context` is written **by hand** — RQ will not propagate OTel context for
  you. Without it, traces break exactly at the queue boundary, which is where you
  need them most (plan E1).
- `prompt_version` and `model_id` ride along so the worker can re-check the cache
  key under its own feet and so the `extractions` row is written correctly
  ([`DATA_MODEL.md`](./DATA_MODEL.md) §2).

---

## 4. Lifecycle

Two state machines that look similar and must not collide:

**RQ statuses** (what Redis knows): `queued → started → finished | failed`.

**Product statuses** (what the client sees): `queued → preprocessing → inferring →
validating → done | failed`.

The worker maps product stages onto RQ's single `started` and emits SSE events on
every transition:

| Product status | Who observes | SSE event | Trigger |
| --- | --- | --- | --- |
| queued | API, SSE | `status` | enqueued |
| preprocessing | SSE | `status` | MinIO GET + `preprocess.to_page_images` |
| inferring | SSE | `status` | `llm.extract` round-trip (the long pole) |
| validating | SSE | `status` | Pydantic validation (+ Stage C rules/OCR) |
| done | SSE, DB | `done` | `extractions` row written |
| failed | SSE, DB | `error` | see §5 |

**SSE rebuild on connect.** `GET /v1/extractions/{id}/events` replays the current
state first (queued/preprocessing/...) before streaming, so a reconnecting browser
resumes mid-lifecycle instead of hanging waiting for the next event. Implementation
is a Redis pub/sub channel per job keyed `extractions:{id}`; the worker writes a
`key:last` snapshot it can hand to late joiners.

**Worker crash mid-job.** The fork dies with the child; the job sits in RQ's
`StartedJobRegistry` and is re-enqueued on worker restart. RQ's default retry
behaviour, not a custom mechanism, owns recovery. The `stop_grace_period: 120s` in
Compose (`DEPLOYMENT.md` §6) gives in-flight jobs time to finish before the worker
dies.

---

## 5. Failures and retries

- **Enqueue-time:** `Retry(max=3, interval=[10, 30, 60])`.
- `FailedJobRegistry` (1000 cap) is the dead-letter queue. Its members need a
  periodic review — a stuck batch fills it invisibly.
- **What is retryable:**

  | Failure | Retry? | Why |
  | --- | --- | --- |
  | transport / server error from the LLM endpoint, MinIO GET, Redis | yes | transient; may succeed next tick |
  | schema validation failed after the repair loop (`ok:false` in `llm.py`) | **no** | will fail identically — the document or schema is the problem, not the network |

- Non-retryable validation failures are written as `extractions.status = 'failed'`
  with the error, surfaced as `422` at `/v1/extractions/{id}` and `event: error` on
  SSE. They keep their DB row: a failed-but-answered attempt is data, and the
  `eval/promote` path may want it.
- **Backoff is at enqueue time, not in the worker.** The worker never sleeps; it
  goes back to the queue and re-encounters the `Retry` policy.

---

## 6. Throughput and backpressure

- `docs_per_day` and `--max-num-seqs` come from the capacity method in
  [`ARCHITECTURE.md`](./ARCHITECTURE.md) §6 — measure, do not guess.
- **Queue depth is the first metric that moves when the GPU saturates.** Alert on
  depth, not on worker CPU.
- Rate limits (plan D3) apply *at enqueue*, so a client flooding the queue is
  throttled before it can grow Redis unboundedly.
- **Two queues share one pool.** If `bulk` starves `interactive` consistently,
  rebalance worker process counts against them (`rq worker interactive -n 2` and a
  second pool) — but start with the single-pool drain order; it is simpler and the
  eval can tell you whether it matters.

---

## 7. What Stage B must prove (plan B10 gate)

- 50 documents through the queue with zero lost jobs.
- Progress visible end-to-end (SSE events in order; DB status transitions match).
- Killing a worker mid-job results in a retry or re-enqueue, not a silent drop.
- `extract()` diffed before/after the migration: byte-identical.