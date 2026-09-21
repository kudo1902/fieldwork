# fieldwork — Architecture

Interactive diagram: [`docs/architecture.html`](./architecture.html) (source of truth:
[`docs/architecture.json`](./architecture.json)).

This describes the **target** system. Today only the extraction core and a synchronous
Phase 1 endpoint exist; see [Phases](#10-phases) for what is real.

---

## 1. Principles

1. **Nothing leaves the box.** Documents contain IDs, bank details, medical data. Self-hosting
   is the feature, not an implementation detail.
2. **The model is a service, not a library.** Everything goes through an OpenAI-compatible
   endpoint, so the model, the quantization, and the serving engine can change without touching
   application code.
3. **A null is correct; a guess is a defect.** Every schema field is nullable and the model is
   told so. Hallucination is measured separately from accuracy because only one of them is
   detectable downstream.
4. **The eval set gates every change.** Prompt edits, model swaps, resize changes, quantization —
   none of them ship without a measured comparison.

---

## 2. Component choices

| Layer | Choice | Why this over the alternatives |
| --- | --- | --- |
| Frontend | Next.js (App Router) + React | Server components for the shell, client components for the review UI. Alternative: keep the current vanilla page for an internal-only tool — it has no build step and works today. |
| Edge | Caddy | Automatic TLS, trivial config, `request_body max_size`. Alternatives: Nginx (more config, more control), Traefik (better if you go container-orchestrated). |
| API | FastAPI + Pydantic v2 | Async-native (matches the extraction core), OpenAPI generated for free, and Pydantic is already the schema layer. Alternative: Litestar, marginally faster, smaller ecosystem. |
| Queue | Redis + **arq** | `extract()` is already `async`; arq is async-native and ~500 lines of concepts. Celery's async support is bolted on and its worker model fights an async core. Alternative: RQ if you want dead-simple and sync. |
| Workers | arq worker processes | One process per GPU slot ×2, so vLLM always has requests to batch. |
| Inference | **vLLM** serving Gemma 4 12B | Continuous batching, paged attention, prefix caching, native JSON-Schema guided decoding. Alternatives: SGLang (competitive, sometimes faster on structured output), TGI, llama.cpp (CPU/low-VRAM only). |
| Object store | MinIO | S3 API on your own disk, so presigned uploads work exactly as they would on S3 and you can migrate later without code changes. |
| Database | PostgreSQL 16 + SQLAlchemy 2.0 + Alembic | `JSONB` + GIN index means you can query inside extracted payloads without a schema migration per document type. |
| OCR cross-check | PaddleOCR | Strong multilingual line detection, runs on CPU. Alternatives: Surya (better layout), Tesseract (weakest, but zero friction). |
| Telemetry | OpenTelemetry → Prometheus + Grafana | One trace per extraction spanning API → queue → worker → vLLM. |

---

## 3. Request lifecycle

```
1. POST /v1/uploads              -> {document_id, presigned_put_url}
2. PUT  <presigned_put_url>      -> bytes go browser -> MinIO, never through the API
3. POST /v1/extractions          -> {document_id, schema, options}
                                    API computes the cache key, checks for a hit,
                                    otherwise enqueues -> 202 {job_id}
4. GET  /v1/extractions/{id}/events (SSE)
                                    queued -> preprocessing -> inferring -> validating -> done
5. Worker: MinIO GET -> preprocess -> vLLM (guided JSON) -> Pydantic validate
           -> business rules -> OCR cross-check -> confidence -> Postgres
6. confidence >= threshold ? auto-accept : push to the review queue
7. Human corrects -> correction stored -> case added to the eval dataset
```

Step 2 is the one worth defending: image bytes never touch the API process. The API stays
small, fast, and memory-flat no matter how large the uploads get.

---

## 4. Techniques that carry real weight

### 4.1 Structured decoding

The chain is `Pydantic model → JSON Schema → hardened → vLLM response_format`.

"Hardened" ([`schemas.json_schema_for`](../src/fieldwork/schemas.py)) means every object gets
`additionalProperties: false` and **every** property is listed in `required`. Since all fields
are already nullable, "required" means *always emit this key*. That distinction matters: a
missing key and an explicit `null` are different failure modes, and only one of them is easy
to score.

vLLM compiles the grammar once per distinct schema and caches it. Keep schemas stable —
generating a schema per request throws that cache away.

### 4.2 Preprocessing

| Problem | Technique |
| --- | --- |
| Sideways phone photos | `ImageOps.exif_transpose`, then drop the tag |
| iPhone HEIC | `pillow-heif`'s opener, registered at import |
| Multi-page PDFs | `pypdfium2` — no Poppler system binary, unlike `pdf2image` |
| Decompression bombs | `Image.MAX_IMAGE_PIXELS = 80_000_000` |
| Transparent PNGs | Flatten onto white; the model reads black-on-transparent as black-on-black |
| Token cost | Resize longest edge to `FIELDWORK_MAX_IMAGE_DIM`, LANCZOS |

Deskew is deliberately **not** implemented. Gemma 4 handles moderate rotation, so measure on
the eval set before adding OpenCV to the dependency tree.

### 4.3 Confidence — four cheap signals, in order of value

1. **Arithmetic rules.** `sum(line_items.total) ≈ subtotal`, `subtotal + tax ≈ total`. Nearly
   free, and it catches the exact failure mode that matters: an invented number.
2. **OCR cross-check.** Run PaddleOCR, normalise both sides, and assert each extracted value
   appears somewhere in the raw OCR text. A value that appears *nowhere* on the page was
   invented. This is the single highest-value check in the system.
3. **Token logprobs.** Ask vLLM for `logprobs`; low mean probability across a field's tokens
   correlates with uncertainty. Free, but noisier than the first two.
4. **Self-consistency.** Run N=3 at `temperature≈0.7` and compare. Disagreement means low
   confidence. Costs 3× — reserve it for high-value fields on high-value documents.

Grounding is worth adding once the basics work: Gemma 4 supports detection and pointing, so
you can request a bounding box per field, overlay it in the review UI, and treat a field with
no box as suspect.

### 4.4 Queueing and concurrency

- Two queues: `interactive` (a human is watching) and `bulk`. Same workers, different priority.
- Worker concurrency should **exceed** 1 per GPU. vLLM batches continuously, so a single
  in-flight request wastes the GPU. Start at 4 concurrent and tune against p95.
- Retries with exponential backoff, max 3, then a dead-letter queue. Transport errors are
  retryable; schema-validation failures after repair are not.
- Propagate the OTel trace context into the job payload manually — arq will not do it for you.

### 4.5 Caching

Cache key:

```
sha256(image_bytes) + schema_name + prompt_version + model_id + max_dim
```

All five parts are mandatory. Omitting `prompt_version` is the classic bug: you improve the
prompt, and every previously-seen document silently keeps serving the old, worse answer.

### 4.6 Observability

Auto-instrument FastAPI, SQLAlchemy, and Redis via `opentelemetry-instrumentation-*`. Beyond
the standard RED metrics, three custom ones actually predict problems:

- **queue depth** — the first thing to move when the GPU is saturated
- **per-field null rate** — the drift canary. If `vendor_name` null rate jumps from 2% to 30%,
  something changed upstream (a new document template, a bad resize, a model swap)
- **review-queue size** — if humans can't keep up, your confidence threshold is wrong

### 4.7 Security

| Risk | Control |
| --- | --- |
| Prompt injection inside the image | System-prompt guard (implemented); never re-inject extracted text into another prompt unescaped |
| Wrong file type | `python-magic` MIME sniffing, never the extension |
| Decompression bomb | Pixel-count cap before decode |
| SSRF | No fetch-by-URL. If you add it, use a strict allowlist |
| GPU tier exposure | vLLM on a private Docker network with no published port |
| PII retention | TTL column + reaper job, MinIO lifecycle rules, deletion endpoint, audit log on every read |

---

## 5. Data model

```
tenants        (id, name, created_at)
api_keys       (id, tenant_id, hash, scopes, last_used_at)
documents      (id, tenant_id, sha256, object_key, mime, pages, bytes, expires_at)
extractions    (id, document_id, schema_name, prompt_version, model_id,
                status, data JSONB, confidence, flags JSONB,
                latency_ms, usage JSONB, created_at)
reviews        (id, extraction_id, reviewer, corrected JSONB, note, created_at)
eval_cases     (id, document_id, schema_name, expected JSONB, source)
audit_log      (id, tenant_id, actor, action, subject_id, at)
```

- `UNIQUE (sha256, schema_name, prompt_version, model_id)` on `extractions` is what makes the
  cache correct rather than merely fast.
- GIN index on `extractions.data` lets you query inside payloads (`data->>'vendor_name'`)
  without a migration per document type.
- `reviews.corrected` is the raw material for `eval_cases`. That promotion should be a
  deliberate, reviewed step, not automatic — a careless reviewer would otherwise poison the
  golden set.

---

## 6. Capacity planning

Do not guess; the eval harness already reports p50/p95. The method:

1. Run the eval at concurrency 1 → single-request latency at your chosen `max_dim`.
2. Raise `--concurrency` until p95 exceeds your SLO. That value is your `--max-num-seqs`.
3. `throughput ≈ concurrency / mean_latency`
4. `docs_per_day ≈ throughput × 86400 × utilisation` (utilisation is well under 1.0 — traffic
   is bursty).

The knobs, in order of effect: `max_dim` / visual token budget → quantization → model size →
a second GPU.

---

## 7. Deployment

Single host, Docker Compose:

```
caddy      :443      TLS, rate limit, body cap
api        :8080     FastAPI (gunicorn + uvicorn workers)
worker     ×N        arq, no published port
vllm       :8000     --gpus all, private network only
redis      :6379     private
postgres   :5432     private
minio      :9000     private; Caddy proxies presigned URLs
```

- The vLLM container needs the NVIDIA Container Toolkit and `--gpus all`.
- Keep model weights on a **mounted volume**, not baked into the image. A 30GB image is
  miserable to build, push, and pull.
- `pg_dump` on a cron; MinIO bucket replication if the documents matter.
- Kubernetes only if you outgrow one box. You probably will not.

## 8. CI

- `ruff` + `mypy` + `pytest` on every PR.
- **The eval suite as a nightly job** against a staging vLLM, with a hard gate on field
  accuracy and hallucination rate. This is the check that actually protects the product;
  unit tests cannot tell you the model got worse.

---

## 9. Deliberate omissions

- **No fine-tuning.** Prompt and schema changes are cheaper and reversible. Revisit with
  LoRA (Unsloth / PEFT) only if the eval shows a *systematic* error that prompting cannot fix.
- **No RAG.** There is no corpus to retrieve from; the document is the entire context.
- **No agent loop.** Extraction is one call with a fixed schema. An agent would add latency,
  cost, and failure modes for nothing.

---

## 10. Phases

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Eval harness, scoring, ground-truth tooling | **Built** |
| 1 | Extraction core, synchronous API, upload UI | **Built** |
| 2 | Presigned uploads, Redis + arq, Postgres, MinIO, SSE, review UI | Next |
| 3 | Auth, rate limits, caching, confidence engine, OCR cross-check | After |
| 4 | OTel + Prometheus + Grafana, retention, audit, nightly eval gate | After |
