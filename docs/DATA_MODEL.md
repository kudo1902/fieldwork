# fieldwork — Data model

PostgreSQL 16 scheme for Stage B onwards. Implements the sketch in
[`ARCHITECTURE.md`](./ARCHITECTURE.md) §5, plus the object-store layout. Owned by
the `src/fieldwork/db/` package; migrations are Alembic (plan B2).

---

## 1. Core decisions

1. **SQLAlchemy 2.0 models, JSONB for payloads.** Extracted data is stored as
   `JSONB` and queried with GIN — you can search inside payloads
   (`data->>'vendor_name'`) without a migration per document type.
2. **The cache is a database actually being right.** The unique constraint in §2 is
   what makes the application-level cache (`extract.py`, plan D4) correct rather
   than merely fast. There is an enum of exactly five cache-key parts; deviations
   are bugs.
3. **`tenant_id` on every table, even while single-tenant.** Decision 2 in the plan
   is cheap now and expensive later; retrofitting tenancy is a migration across
   every table. The column costs nothing, is `NOT NULL`, and defaults to tenant 1.
4. **Migrations are add-only** (see [`DEPLOYMENT.md`](./DEPLOYMENT.md) §7). Add
   columns, never drop — an old image must be able to run against a new schema
   during rolling deploys.
5. **Document bytes never enter Postgres.** They live in MinIO under the object key
   conventions in §4; the DB holds the hash, key, and lifecycle metadata.

---

## 2. Tables

### tenants

| column | type | notes |
| --- | --- | --- |
| id | `bigserial PK` | |
| name | `text NOT NULL UNIQUE` | |
| created_at | `timestamptz NOT NULL DEFAULT now()` | |

Seed row: tenant 1, `"default"`. All other tables reference it.

### api_keys

| column | type | notes |
| --- | --- | --- |
| id | `bigserial PK` | |
| tenant_id | `bigint NOT NULL REFERENCES tenants` | |
| name | `text NOT NULL` | a human name for the key |
| hash | `text NOT NULL` | argon2id; the raw key shown exactly once at creation |
| scopes | `text[] NOT NULL` | e.g. `{extractions:write, extractions:read}`; empty = no access |
| disabled | `bool NOT NULL DEFAULT false` | rotation without deletion |
| last_used_at | `timestamptz` | |
| created_at | `timestamptz NOT NULL DEFAULT now()` | |

Unique on `(tenant_id, name)`. **Hashed storage is non-negotiable** — a leaked DB
never leaks usable keys ([`SECURITY.md`](./SECURITY.md)).

### documents

| column | type | notes |
| --- | --- | --- |
| id | `uuid PK DEFAULT gen_random_uuid()` | returned to the client |
| tenant_id | `bigint NOT NULL REFERENCES tenants` | |
| sha256 | `text NOT NULL` | content hash of the original bytes |
| object_key | `text NOT NULL` | MinIO key of the original bytes (§4) |
| mime | `text NOT NULL` | from `python-magic` sniffing, never the extension |
| pages | `int NOT NULL` | after PDF render / decode |
| bytes | `bigint NOT NULL` | size of the original upload |
| expires_at | `timestamptz NOT NULL` | retention TTL (plan D6) |
| created_at | `timestamptz NOT NULL DEFAULT now()` | |

Indexes: `(sha256)`, `(tenant_id, created_at)` for retention sweeps.

### extractions

| column | type | notes |
| --- | --- | --- |
| id | `uuid PK DEFAULT gen_random_uuid()` | the `document_id`/`job_id` surfaces over the API |
| tenant_id | `bigint NOT NULL REFERENCES tenants` | |
| document_id | `uuid NOT NULL REFERENCES documents` | |
| sha256 | `text NOT NULL` | denormalised from `documents` so the uniqueness constraint needs no join |
| schema_name | `text NOT NULL` | key of `schemas.REGISTRY` |
| prompt_version | `int NOT NULL` | `PROMPT_VERSION` at inference time (plan A10) |
| model_id | `text NOT NULL` | e.g. `gemma-4-12B-it` |
| status | `text NOT NULL` | `pending \| done \| failed` (see [`QUEUEING.md`](./QUEUEING.md)) |
| data | `jsonb` | the validated, nullable payload; null while pending |
| confidence | `real` | 0–1 document-confidence (Stage C, [`CONFIDENCE.md`](./CONFIDENCE.md)) |
| flags | `jsonb NOT NULL DEFAULT '{}'` | per-field/per-signal notes, e.g. `{"cross_check": {...}}` |
| latency_ms | `int` | |
| usage | `jsonb` | the model's usage dict (tokens) — copied from `llm.py` |
| created_at | `timestamptz NOT NULL DEFAULT now()` | |

Constraints:

```sql
UNIQUE (sha256, schema_name, prompt_version, model_id)
```

This is the cache-correctness constraint. Omitting any one part silently corrupts
the cache — the five-part key exists in one place and one place only.

Indexes: `(tenant_id, created_at)` (default listing), GIN on `data`
(`CREATE INDEX ... USING gin (data jsonb_path_ops)`) for payload queries, and
`(status)` for queue rebuilds.

**JSONB conventions for `data`:**

- Shape is precisely `pydantic.model_dump(mode="json")` of the schema model —
  every key present, values nullable, line items as arrays of objects.
- A `null` value means "honestly absent" — *never* coerce, compute, or default it
  on read. Downstream code that treats null as a mistake is the bug.
- Arrays preserve printed order (`line_items`).

### reviews

| column | type | notes |
| --- | --- | --- |
| id | `bigserial PK` | |
| extraction_id | `uuid NOT NULL REFERENCES extractions` | |
| reviewer | `text NOT NULL` | authenticated actor |
| corrected | `jsonb NOT NULL` | **full** corrected payload, not a patch |
| note | `text` | |
| created_at | `timestamptz NOT NULL DEFAULT now()` | |

`corrected` is a full payload so the diff against `extractions.data` is always
computable (Stage C review). This table is the raw material for `eval_cases` (§3).

### eval_cases

| column | type | notes |
| --- | --- | --- |
| id | `bigserial PK` | |
| document_id | `uuid NOT NULL REFERENCES documents` | |
| schema_name | `text NOT NULL` | |
| expected | `jsonb NOT NULL` | ground truth payload |
| source | `text NOT NULL` | `manual` (authored) or `review:promotion` |
| created_at | `timestamptz NOT NULL DEFAULT now()` |

Promotion from reviews into `eval_cases` is a **deliberate, reviewed step**, not a
trigger — one careless reviewer would otherwise poison the golden set
([`EVAL.md`](./EVAL.md) §8, plan C8).

### audit_log

| column | type | notes |
| --- | --- | --- |
| id | `bigserial PK` | |
| tenant_id | `bigint NOT NULL REFERENCES tenants` | |
| actor | `text NOT NULL` | API key name, or `user:<id>` for humans |
| action | `text NOT NULL` | `extraction:read`, `document:delete`, `review:submit`, ... |
| subject_id | `uuid` | the document/extraction affected |
| at | `timestamptz NOT NULL DEFAULT now()` | |

Append-only; no UPDATE/DELETE privileges for application roles
([`SECURITY.md`](./SECURITY.md) §audit).

---

## 3. The cache as data

- On `POST /v1/extractions`, the API computes
  `sha256 + schema_name + prompt_version + model_id + max_dim`, looks up an
  `extractions` row for that key with `status = 'done'`, and serves `data` without
  enqueueing (a cache hit). `max_dim` is stored via `flags.max_dim`.
- A cache entry is invalidated by *any component changing* — which is exactly why
  bumping `PROMPT_VERSION` (prompts/schemas) empties it, and why `model_id` is in
  the key.
- Cached results are served **only after** tenancy checks — cache lookup is scoped
  by `tenant_id` at the query level, never returning another tenant's document.

---

## 4. Object store (MinIO)

Bucket: `documents` (created at deploy; lifecycle rule scoped to it).

Object key:

```
{tenant_id}/{sha256[:2]}/{sha256}
```

- Content-addressable, so two copies of the same bytes within a tenant share one
  object (dedup), while the hash prefix scopes listing per shard.
- **No cross-tenant sharing of objects:** key starts with `tenant_id`, so tenants
  can never address each other's objects by hash alone.
- Retention: the `expires_at` TTL is mirrored in a MinIO lifecycle rule that
  deletes objects tagged `expires` older than the retention window (plan D6). The
  DB reaper removes `documents`/`extractions` rows; the lifecycle rule removes the
  bytes. Both must exist — deleting one without the other leaks or orphans.
- Presigned PUT/GET signatures cover the host and path; see
  [`DEPLOYMENT.md`](./DEPLOYMENT.md) §4 for the hostname gotcha.