# fieldwork — Module & component design

The unit-level contract for every module: responsibility, public interface,
invariants, dependencies, and what each test layer proves. This is the design
document below the [`ARCHITECTURE.md`](./ARCHITECTURE.md) layer. Modules marked
**(plan)** are not built yet — their design is the spec an implementer works from.

**The two seams everything respects:**

- `llm.py` is the *only* module that talks to the model. Everything else treats the
  model as a black box behind an OpenAI-compatible endpoint
  ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §principle 2).
- `extract.py` is the *stable* seam. Stage B moves the call site from a request
  into a worker; `extract_bytes` itself must not change (plan B5).

Import direction matters and is acyclic: `config ← everything`, `schemas → prompts
(conceptually, via the same schema sent to vLLM)`, `llm → {config, prompts,
schemas}`, `extract → {preprocess, llm, schemas}`, `api → {extract, llm, schemas,
config}`. Nothing imports `api`.

---

## 1. Extraction core (built)

### 1.1 `src/fieldwork/config.py` — env-driven settings

| | |
| --- | --- |
| Responsibility | The one place env config is read (`FIELDWORK_*`, `.env`) |
| Public interface | `Settings` (pydantic-settings), singleton `settings` |
| Invariants | No other module reads `os.environ` or `.env`; `extra_body` is parsed JSON and passed verbatim to the model ([`PROMPTING.md`](./PROMPTING.md) §4); defaults are tuned-and-frozen, not arbitrary (plan A9) |
| Dependencies | none (pydantic-settings only) |
| Tests | env parsing, `FIELDWORK_EXTRA_BODY` JSON decode, defaults (unit) |

### 1.2 `src/fieldwork/schemas.py` — target schemas + hardening

| | |
| --- | --- |
| Responsibility | Define document types (Pydantic models) and produce the hardened JSON schema the model must emit |
| Public interface | `LineItem`, `Invoice`, `Receipt`, `PlainDocument`; `DocumentType(name, model, hint)`; `REGISTRY`; `get(name)`; `json_schema_for(model)` |
| Invariants | Every field `Optional`; **field descriptions are instructions to the model**, so they are authored like prompts ([`PROMPTING.md`](./PROMPTING.md) §1); `_harden` adds `additionalProperties:false` + all keys `required`; **no I/O, no model calls** |
| Dependencies | pydantic only |
| Tests | `_harden` recurses into line items; schema-for-the-wire has strict flags (unit) |

New document type = new Pydantic model + `REGISTRY` entry. Nothing else. This is
the scope boundary (plan risk register).

### 1.3 `src/fieldwork/prompts.py` — prompt text

| | |
| --- | --- |
| Responsibility | System / user / repair prompt text; the Phase 0 iteration surface |
| Public interface | `SYSTEM_PROMPT`, `USER_INSTRUCTION` (`.format(hint=…)`), `REPAIR_INSTRUCTION` (`.format(error=…)`), **`PROMPT_VERSION`** (plan A10, not yet present) |
| Invariants | No logic; the repair loop's instruction must not change behaviour semantics silently (it carries the schema error verbatim) |
| Dependencies | none |
| Tests | **behaviour belongs to the eval gate, not unit tests** ([`TESTING.md`](./TESTING.md) §5); CI enforces that any diff bumps `PROMPT_VERSION` |

### 1.4 `src/fieldwork/preprocess.py` — image normalisation

| | |
| --- | --- |
| Responsibility | Bytes → normalised RGB page images → base64 JPEG data URLs |
| Public interface | `UnsupportedFile(ValueError)`; `to_page_images(data, *, max_dim, max_pages) -> list[Image.Image]`; `to_data_urls(pages) -> list[str]`; `prepare_file(path, **kw)`; `prepare_bytes(data, **kw)` |
| Invariants | **No pydantic, no LLM concepts** — pure image pipeline; side effects at import (HEIC opener registration, `Image.MAX_IMAGE_PIXELS = 80_000_000`) are intentional and documented; PDF page cap + silent truncation is a known limit (README) |
| Dependencies | pillow, pillow-heif, pypdfium2, config |
| Tests | EXIF rotation, flattened transparency, resize cap, bomb guard, PDF cap, undecodable → `UnsupportedFile` (unit, synthetic fixtures) |

"Most extraction accuracy is won or lost here, not in the prompt" — the knob
methodology is [`EVAL.md`](./EVAL.md) §7 / [`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.2.

### 1.5 `src/fieldwork/llm.py` — the model boundary

| | |
| --- | --- |
| Responsibility | All OpenAI-compatible calls; the repair loop; the `ExtractionResult` contract |
| Public interface | `client() -> OpenAI`; `extract(image_urls, doctype) -> ExtractionResult`; `ExtractionResult` dataclass + `to_dict()` |
| Invariants | **Only module importing `openai`**; `response_format={"type":"json_schema","strict":true}` always; transport errors are not repaired (returned as `ok=False`), only validation/parse failures feed the repair loop (max `FIELDWORK_MAX_REPAIR_ATTEMPTS`); extraction latency/`attempts`/`usage` recorded on the result |
| Dependencies | openai, config, prompts, schemas |
| Tests | fake client: response_format reaches the wire, repair loop messaging + cap, `ok=False` on transport (unit, [`TESTING.md`](./TESTING.md) §2) |

### 1.6 `src/fieldwork/extract.py` — the stable seam (bytes → validated data)

| | |
| --- | --- |
| Responsibility | Orchestrate the pipeline; stamp `source_sha256` |
| Public interface | `extract_bytes(data, schema_name, *, max_dim=None) -> ExtractionResult`; `extract_file(path, schema_name, *, max_dim=None)` |
| Invariants | **Does not change under Stage B** (plan B5 check); no HTTP; caching lives *around* it (plan D4), never inside |
| Dependencies | preprocess, llm, schemas |
| Tests | smoke: pipeline ordering, `source_sha256` present (integration/eval gate) |

### 1.7 `src/fieldwork/__init__.py`

Exports the public API for library users: `extract_bytes`, `extract_file`,
`ExtractionResult`. Nothing else.

**Phase 1 call chain:** `POST /v1/extractions → extract_bytes → to_page_images →
to_data_urls → llm.extract → (schemas.json_schema_for → OpenAI → pydantic validate)
→ ExtractionResult → JSON`.

---

## 2. API and web (built)

### 2.1 `src/fieldwork/api.py` — Flask application

| | |
| --- | --- |
| Responsibility | HTTP surface: serve the SPA, `healthz`, schema listing, synchronous extraction |
| Public interface | `create_app() -> Flask`; module-level `app` |
| Invariants | Error envelope `{ok:false,error,status}` for every HTTP error ([`API.md`](./API.md) §0); `MAX_CONTENT_LENGTH` from settings enforced by Werkzeug *before* reading bytes; **no business logic** — the largest route just reads the upload and calls `extract_bytes` |
| Dependencies | flask, extract, llm (`client()` for healthz), schemas, config, preprocess (`UnsupportedFile`) |
| Tests | route/status-code behaviour with a monkeypatched `extract_bytes` (unit); real stack in integration |

### 2.2 `web/index.html` — Phase 1 SPA

| | |
| --- | --- |
| Responsibility | Drag-in upload, schema pick, extraction result view; the header shows model availability |
| Invariants | **No build step**, vanilla JS only ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §2); reads `/v1/schemas` and posts multipart; later becomes upload/progress/SSE + review screens (plan B9, C6) |
| Tests | manual, plus the browser's visual-check; no unit harness |

---

## 3. Eval harness (built)

### 3.1 `eval/score.py` — field-level scoring

| | |
| --- | --- |
| Responsibility | Turn `(expected, predicted)` into four-status comparisons and report aggregates |
| Public interface | `flatten`, `template`, `is_empty`, `normalise_text`, `parse_number`, `similarity`, `values_match`; `score_case(...) -> CaseScore`; `Report` |
| Invariants | Pure functions — no I/O, no randomness; line items compared positionally ([`EVAL.md`](./EVAL.md) §4); `bool` never coerced to a number |
| Dependencies | stdlib only (already true — tests run before install) |
| Tests | the existing `tests/test_score.py` (unit) |

### 3.2 `eval/run_eval.py` — the harness

| | |
| --- | --- |
| Responsibility | Discover cases, run extract, score, render, persist the run |
| Public interface | `discover`, `check`, `run_one`, `render`, `main`; CLI flags (`--schema`, `--concurrency`, `--max-dim`, `--model`, `--baseline`, `--tag`, `--check`, …) |
| Invariants | Every run writes `summary.json`/`results.json`/`predictions.json`, capturing model/base_url/max_dim/text_threshold (+ `prompt_version` from plan A10); baseline diff is the discipline ([`EVAL.md`](./EVAL.md) §6) |
| Dependencies | fieldwork, score, rich |
| Tests | none (stdin-free CLI; its correctness is downstream of `score.py`'s tests) |

---

## 4. Stage B — async backbone (plan)

### 4.1 `src/fieldwork/db/` — PostgreSQL access

| | |
| --- | --- |
| Responsibility | SQLAlchemy 2.0 models + Alembic migrations exactly per [`DATA_MODEL.md`](./DATA_MODEL.md) |
| Public interface | model classes (`Tenant`, `ApiKey`, `Document`, `Extraction`, `Review`, `EvalCase`, `AuditLog`); data-access helpers enforcing the **query-level tenant filter** ([`SECURITY.md`](./SECURITY.md) §7); Alembic env |
| Invariants | add-only migrations; payload queries go through the GIN index; `UNIQUE(sha256, schema_name, prompt_version, model_id)` is cache correctness |
| Dependencies | sqlalchemy, alembic, config, storage (object keys) |
| Tests | migration up/down on scratch, unique key, tenant filter returns zero for other tenants (integration) |

### 4.2 `src/fieldwork/storage.py` — MinIO adapter

| | |
| --- | --- |
| Responsibility | Presigned PUT/GET, object-key construction, lifecycle config |
| Public interface | `presign_put(object_key, ttl)`, `presign_get(object_key, ttl)`, `object_key_for(tenant_id, sha256)`, `delete_object(object_key)` |
| Invariants | Object keys are `{tenant}/{sha256[:2]}/{sha256}` ([`DATA_MODEL.md`](./DATA_MODEL.md) §4); signatures cover the browser-visible hostname ([`DEPLOYMENT.md`](./DEPLOYMENT.md) §4); **delete only via this module or the retention lifecycle** ([`RUNBOOK.md`](./RUNBOOK.md) §6) |
| Dependencies | boto3/minio SDK, config |
| Tests | key layout, hostname signing (integration w/ MinIO in compose) |

### 4.3 `src/fieldwork/queue.py` — enqueue side

| | |
| --- | --- |
| Responsibility | Build job payloads and enqueue to `interactive`/`bulk` |
| Public interface | `enqueue_extraction(document_id, schema_name, options) -> job_id`; queue-name constants |
| Invariants | **Trace context is put in `job.meta` by hand** ([`QUEUEING.md`](./QUEUEING.md) §3); `Retry(max=3, interval=[10,30,60])` set at enqueue time; job carries `prompt_version` and `model_id` |
| Dependencies | rq, redis, config, telemetry (span context) |
| Tests | payload fields, retry policy on the enqueue call (unit w/ fakeredis) |

### 4.4 `src/fieldwork/worker.py` — job handler

| | |
| --- | --- |
| Responsibility | Execute a job: stages, SSE publication, persistence, routing |
| Public interface | job function (the RQ entrypoint); stage helpers (`preprocess`, `infer`, `validate`, `persist`, `route`) |
| Invariants | Maps product stages (queued/preprocessing/inferring/validating/done) onto RQ statuses and publishes SSE ([`QUEUEING.md`](./QUEUEING.md) §4); validation failures are **non-retryable** and written as `status='failed'`; does **not** call `extract.py` internals — calls `extract_bytes` whole (the seam) |
| Dependencies | rq, redis (pub/sub), storage, extract, db, ocr, rules, confidence (Stage C) |
| Tests | the plan B10 gate: 50 docs, kill-a-worker, zero lost jobs (integration) |

---

## 5. Stage C — trust (plan)

### 5.1 `src/fieldwork/rules.py` — arithmetic/format rules

| | |
| --- | --- |
| Responsibility | Signal 1: check printed numbers/consistency without recomputing the answer |
| Public interface | per-schema rule checks returning per-field `[0,1]` scores (sum of line totals ≈ subtotal ≈ total; date sanity; currency code membership) |
| Invariants | Rules **score only, never correct values** ([`CONFIDENCE.md`](./CONFIDENCE.md) §2.1); reference printed values (a wrong-but-printed total is grounded) |
| Dependencies | schemas (types only) |
| Tests | table-driven on synthetic payloads (unit) |

### 5.2 `src/fieldwork/ocr.py` — PaddleOCR service + cross-check

| | |
| | --- |
| Responsibility | Raw OCR text (C2) and the ground-the-value check (C3) |
| Public interface | `extract_lines(page_image) -> list[str]`; `cross_check(values, lines, schema) -> per-field evidence` |
| Invariants | Uses the **same** normalisation as `score.py` (`parse_number` + whitespace/case) so `1500.0` ↔ `1,500.00` matches; null fields are excluded, never penalised ([`CONFIDENCE.md`](./CONFIDENCE.md) §3); `plain` schema exempt |
| Dependencies | paddleocr, preprocess, score (`parse_number`) |
| Tests | fake OCR lines vs hallucinated/grounded values (unit); low-confidence-OCR vacuous-check path |

### 5.3 `src/fieldwork/confidence.py` — combining and routing

| | |
| --- | --- |
| Responsibility | Blend the four signals into per-field + document confidence, decide auto-accept vs review |
| Public interface | `combine(signal_scores, presence) -> document_confidence`; `decide(confidence, threshold) -> route` |
| Invariants | Weights fixed by signal quality (OCR ≈ arithmetic > logprobs), not per document; **the threshold is tuned on the eval set** (C5), never intuited; emits `flags.reason_code` per field for the reviewer ([`CONFIDENCE.md`](./CONFIDENCE.md) §5) |
| Dependencies | rules, ocr, llm (usage/logprobs) |
| Tests | every hallucinated eval field below threshold, exact matches above (runs on eval set, not CI unit) |

### 5.4 `eval/promote.py` — corrections → eval cases (C8 / plan C8)

| | |
| --- | --- |
| Responsibility | Reviewed promotion of `reviews.corrected` into `eval_cases`, deliberately manual |
| Public interface | `collect_candidates()`, `validate(candidate)` (must pass `run_eval.py --check`), `promote(candidate)` with dedupe by `(document_id, schema_name)` |
| Invariants | **Never automatic** — one careless reviewer poisons the golden set ([`EVAL.md`](./EVAL.md) §8); promotion must re-validate against the schema |
| Dependencies | db, schemas |
| Tests | dedupe, invalid-candidate rejection (unit) |

---

## 6. Stage D — production (plan)

### 6.1 `src/fieldwork/auth.py` — API keys + sessions

| | |
| --- | --- |
| Responsibility | Argon2-hashed API keys with scopes; OIDC/JWT for humans; rate limiting (Redis token bucket) |
| Public interface | `hash_key`, `verify_key`, `require_scope(scope)`, `rate_bucket(key, quota)`, session creation/refresh |
| Invariants | Keys shown once at creation, stored hashed only ([`SECURITY.md`](./SECURITY.md) §6); tenant isolation is the **query filter** (`db`), not auth logic; `audit_log` written on every `extraction:read` |
| Dependencies | argon2, jwt/oidc, redis, db, config |
| Tests | hash/verify round-trip, scope enforcement, bucket counter (unit) |

---

## 7. Stage E — operations (plan)

### 7.1 `src/fieldwork/telemetry.py` — OTel init + custom metrics

| | |
| --- | --- |
| Responsibility | Instrumentation init, `PROMPT_VERSION`-stamped spans, the three predictive gauges ([`TELEMETRY.md`](./TELEMETRY.md) §3) |
| Public interface | `init()`, `start_extraction_span(job)`, `record_queue_depth()`, `record_null_rates()` (the low-frequency aggregation job), span-attribute set |
| Invariants | Bounded cardinality (never per-extraction field labels in a timeseries); no extracted payload values in attributes ([`SECURITY.md`](./SECURITY.md) §5); RQ boundary handled here + in `queue.py` (`job.meta`) |
| Dependencies | opentelemetry, config, db |
| Tests | attribute whitelist, null-rate job on scratch DB (unit/integration) |

---

## 8. Dependencies in one picture

```
┌─ config ──────────────────────────────────────────────────────────────┐
│   preprocess ─► llm ─► (OpenAI / vLLM)                                 │
│   extract  ─► preprocess, llm, schemas        [the stable seam]        │
│   api      ─► extract, llm, schemas, config   │                        │
│   db/storage/queue/worker ◄─ api (B)          │                        │
│   worker   ─► extract_bytes, storage, db,     │  <- seam holds; worker │
│               ocr, rules, confidence          │     calls extract(), not its guts
│   telemetry ◄─ queue, worker, api (span ctx)  │
│   auth ◄─ api (keys, rate, sessions)          │
└────────────────────────────────────────────────────────────────────────┘
```

Planned modules slot *around* the core, never *into* it. If a Stage B–E design
requires modifying `extract.py` or `llm.py`'s contract, that is a seam violation —
stop and revisit the boundary, don't widen it.