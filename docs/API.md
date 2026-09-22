# fieldwork — API contract

The HTTP surface of the service: current Phase 1 endpoints (implemented in
`src/fieldwork/api.py`) and the Stage B additions (designed, not yet built).
Everything is JSON. Errors are JSON, never HTML.

Cross-references: [`ARCHITECTURE.md`](./ARCHITECTURE.md) request lifecycle,
[`QUEUEING.md`](./QUEUEING.md) for job semantics, [`DATA_MODEL.md`](./DATA_MODEL.md)
for the objects endpoints return.

---

## 0. Conventions

- Base URL: `https://<host>/v1` (or `http://localhost:8080/v1` in dev).
- Every error response shares the envelope: `{"ok": false, "error": "...", "status": <code>}`.
- Success responses are plain payloads (**no** `ok` wrapper) — a 200 *is* the signal.
  The one exception is the synchronous extraction endpoint, whose body carries an
  `ok` field because a 200/502 split alone cannot express a validated-but-impossible
  extract (see §4).
- Current routes are synchronous. Stage B adds an async job protocol under the same
  `/v1` prefix; `/v1/extractions` keeps the same name but changes semantics to
  "enqueue", so nothing about the route name implies sync/async — the *status code*
  does (200 sync today, 202 async after Stage B).
- Dates are ISO 8601 (`YYYY-MM-DD`), times UTC-ish RFC3339.

---

## 1. `/healthz` — GET

Model server reachability. One round-trip to the LLM endpoint's `/models`.

```json
{
  "ok": true,
  "llm_base_url": "http://localhost:8000/v1",
  "configured_model": "gemma-4-12B-it",
  "served_models": ["gemma-4-12B-it"],
  "model_available": true
}
```

| Status | Meaning |
| --- | --- |
| 200 | Model endpoint reachable; `model_available` tells you if the configured model is among those served |
| 503 | Model endpoint unreachable / error; body is the error envelope with `llm_base_url` + `error` |

---

## 2. `/v1/schemas` — GET

List registered document types.

```json
[
  { "name": "invoice", "hint": "This is a commercial invoice or bill.", "fields": ["invoice_number", "..."] },
  { "name": "receipt", "hint": "...", "fields": [...] },
  { "name": "plain", "hint": "...", "fields": ["title", "language", "markdown"] }
]
```

---

## 3. `/v1/schemas/{name}` — GET

The hardened JSON schema actually sent to the model (see
[`PROMPTING.md`](./PROMPTING.md) §5 and `schemas.json_schema_for`).

```json
{
  "name": "invoice",
  "hint": "This is a commercial invoice or bill.",
  "json_schema": { "type": "object", "properties": {...}, "additionalProperties": false, "required": [...] }
}
```

| Status | Meaning |
| --- | --- |
| 200 | Schema returned |
| 404 | Unknown schema name (envelope: `{ok:false, error:"unknown schema 'x'; known: [...]", ...}`) |

---

## 4. `/v1/extractions` — POST (Phase 1, synchronous)

Multipart form: `file` (required), `schema` (optional, default `invoice`),
`max_dim` (optional integer, overrides the resize for this request).

The API buffered the whole upload and ran inference before responding. That is the
"one request grabs one GPU slot" model — correct for proving the UX, wrong for
traffic. Stage B replaces it; `extract()` underneath does not change.

**Response** (`ExtractionResult.to_dict()` + `filename`):

```json
{
  "ok": true,
  "schema": "invoice",
  "data": { "invoice_number": "INV-2026-0417", "...": "..." },
  "error": null,
  "attempts": 1,
  "latency_s": 12.434,
  "pages": 1,
  "usage": { "prompt_tokens": 1234, "completion_tokens": 321, "total_tokens": 1555 },
  "model": "gemma-4-12B-it",
  "source_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "filename": "invoice.jpg"
}
```

| Field | Meaning |
| --- | --- |
| `ok` | true = a Pydantic-valid extraction (data may still be mostly `null`). **Not** a signal of quality — nulls are correct answers |
| `source_sha256` | content hash of the uploaded bytes; the base of the cache key (stage D) and dedup |
| `attempts` | 1 + number of repair-loop retries ([`PROMPTING.md`](./PROMPTING.md) §6) |
| `data` | the validated, nullable payload, always emitting every schema key |

| Status | Meaning |
| --- | --- |
| 200 | Extraction succeeded (`ok: true`) |
| 400 | No `file` part; unknown schema; bad `max_dim`; empty upload |
| 413 | Upload exceeded `FIELDWORK_MAX_UPLOAD_MB` (Werkzeug enforces before any byte is read) |
| 415 | File could not be decoded (`UnsupportedFile`) |
| 502 | `ok: false` — request failed or failed validation even after the repair loop |
| 503 | Model endpoint unreachable for this request |

---
  
## 5. Stage B — reroute around sync

Same routes, new semantics. **[Build tasks B4–B7, B9]**

### 5.1 `POST /v1/uploads` — B4

Handshake for presigned uploads. The API never sees the image bytes — they go
browser → MinIO (see [`DEPLOYMENT.md`](./DEPLOYMENT.md) §4 for the hostname/CORS
gotchas).

```json
// request:  {}
// response: 201
{
  "document_id": "0195d4f9-...",
  "presigned_put_url": "https://s3.fieldwork.example.com/...?...signature",
  "expires_s": 600
}
```

`POST /v1/extractions` is later handed the `document_id`, never bytes.

### 5.2 `POST /v1/extractions` — B6 (async)

```json
// request:
{ "document_id": "0195d4f9-...", "schema": "invoice", "options": { "max_dim": 1536 } }
// response: 202
{ "job_id": "0195d4fa-...", "status": "queued", "location": "/v1/extractions/0195d4fa-..." }
```

Enqueue only. The API computes the cache key first; on a hit it returns the cached
extraction directly (200, with `"cached": true`) instead of enqueueing.

### 5.3 `GET /v1/extractions/{id}` — B6

Job status + result when done. `200` means `status == "done"` (or a cache hit) and
the body is the extraction; otherwise `200` with `{status: "queued"|"started"...}`,
no `data`. Failures: `422` (schema validation after repairs) becomes `ok:false`
state, not a dropped job — see [`QUEUEING.md`](./QUEUEING.md) §5.

### 5.4 `GET /v1/extractions/{id}/events` — B7 (SSE)

`text/event-stream`. Events mirror the worker's progress:

```
event: status
data: {"job_id":"...", "status":"queued",     "at":"2026-09-22T12:00:01Z"}

event: status
data: {"job_id":"...", "status":"preprocessing","at":"..."}

event: status
data: {"job_id":"...", "status":"inferring",   "at":"..."}

event: status
data: {"job_id":"...", "status":"validating",  "at":"..."}

event: done
data: {"job_id":"...", "status":"done", "document_id":"...", "schema":"invoice", "prompt_version":3, "model":"gemma-4-12B-it", "confidence":0.94, "data":{...}, "usage":{...}, "at":"..."}
```

Rules:
- On connect, replay the current state (a reconnecting browser must not see a
  half-lifecycle — see [`QUEUEING.md`](./QUEUEING.md) §4 rebuild).
- `done` replaces `status` as the event name and carries the full result.
- If the job died: `event: error, data: {status:"failed", "error":"transport: ..."}`.
- SSE through Caddy needs `flush_interval -1` — see [`DEPLOYMENT.md`](./DEPLOYMENT.md) §3.

---

## 6. Error ladder (all routes)

| Code | When |
| --- | --- |
| 400 | malformed request, unknown form part, bad `max_dim` |
| 404 | unknown schema, unknown document/job |
| 413 | body over cap (Werkzeug `MAX_CONTENT_LENGTH`, or Caddy `request_body max_size`) |
| 415 | undecodable file bytes |
| 422 | (async) result failed validation after repairs — `ok:false` state |
| 429 | (Stage D) rate limit / quota — token bucket ([`SECURITY.md`](./SECURITY.md)) |
| 5xx | server-side fault or the LLM endpoint failed |

Networking errors to the LLM endpoint are 502/503 with the transport error in
`error` — they are never retried silently inside a request (retries live in the
queue, [`QUEUEING.md`](./QUEUEING.md) §5).

---

## 7. Versioning

`/v1` is frozen at the route level. Breaking changes bump a **new** major
(`/v2/...`), keeping `/v1` alive long enough to migrate consumers. Non-breaking
additions (new fields on a response object) do not bump anything, but they do bump
`PROMPT_VERSION`-independent contract notes — read the CHANGELOG, not the diff.