# fieldwork — Operations runbook

What to do when a number moves, a container dies, or the nightly build goes red.
Companion to [`DEPLOYMENT.md`](./DEPLOYMENT.md) (topology, releases),
[`SECURITY.md`](./SECURITY.md) (the D9 checklist), and [`TELEMETRY.md`](./TELEMETRY.md)
(what the alerts mean). Single-operator system — "escalate" always ends with you.

---

## 0. How to triage anything

Before touching a config file, answer three questions:

1. **Symptom** — `curl -fsS …/v1/healthz`, the telemetry panels, `docker compose ps`.
2. **What changed last?** — `git log --oneline`, the deployed `TAG`, `PROMPT_VERSION`,
   `FIELDWORK_*` edits. The most common root cause of "it was fine yesterday" is a
   deliberate change yesterday.
3. **Can it wait?** — queue depth slope, p95 burn, disk %, DLQ. If nothing is about
   to cross a hard limit, schedule the fix instead of doing it at 2 a.m.

Then follow the section below that matches. If an alert has **no** section here,
fix the alert or write the section — an alert you cannot act on is noise.

---

## 1. vLLM down / model load failure

**Symptom:** `healthz` 503; worker requests fail with transport errors; queue depth
climbs; `model_unavailable` critical.

**Triage:**
```bash
docker compose ps vllm
docker compose logs --tail=200 vllm
nvidia-smi                      # driver vs expected CUDA; VRAM actually free
```

**Common causes and fixes:**

| Cause | Fix |
| --- | --- |
| OOM at startup | Lower `--max-model-len` first — cheaper than quantization. Watch `--gpu-memory-utilization` against *actual* VRAM (a 40 GB claim on a 24 GB card OOMs) |
| Driver/CUDA mismatch after a host update | Reinstall the NVIDIA Container Toolkit; re-run `docker run --rm --gpus all nvidia-smi` |
| `served-model-name` mismatch | `healthz` shows `served_models` vs `configured_model` — sign it `--served-model-name` the way `.env` points |
| Model load slow (first start) | The compose `start_period: 600s` healthcheck absorbs this — do not shorten it |

**Restart:** `docker compose restart vllm`. Queued jobs drain once it's healthy —
*workers retry; nothing is lost in Redis*. Do **not** restart the API to fix the
model; the jobs live in the queue, not in the API process.

---

## 2. Queue backing up / GPU saturated

**Symptom:** `queue_stuck` or `queue depth` rising, p95 blowing its SLO,
`docs/hour` flat.

**Triage:** worker count vs GPU slots (compose default 4 — must exceed 1 or vLLM
has nothing to batch), `--max-num-seqs`, incoming rate (is a client flooding? plan
D3 rate limits).

**Fix order (cheap → expensive):** raise worker replicas → lower `max_dim` → raise
`--max-num-seqs` → quantization → model size → second GPU. Each change re-verified
by the capacity method ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §6) — not by feel.

---

## 3. Worker crash / dead-letter growth

**Symptom:** `dlg_growth` warning; `FailedJobRegistry` filling; workers re-claiming
the same jobs.

**Know the design first** ([`QUEUEING.md`](./QUEUEING.md) §5): transport errors are
retryable; schema-validation failures after the repair loop are **not** — they are
written as `extractions.status='failed'`, not DLQ cruft. A DLQ full of validation
failures means the schema or prompt is wrong, not the network.

**Triage:**
```bash
# peek at the last failed job's error
rq info --url redis://redis:6379
# classify: transport vs validation
```

**Fix:** re-enqueue transient failures after the root cause resolves (`rq requeue`);
leave validation failures in place and fix the schema/prompt. Do not clear the DLQ
until you know which kind you are deleting.

---

## 4. Postgres

**Migration failure mid-deploy:** migrations are add-only ([`DEPLOYMENT.md`](./DEPLOYMENT.md)
§7). Rollback = redeploy the previous `TAG` (the new schema's added columns are
inert to old code). Get the fix in as the *next* additive migration. Never drop a
column to "roll back" — that breaks the old image's compatibility, which is the
property you are relying on.

```bash
docker compose run --rm migrate      # runs alembic upgrade head
```

**Disk full:** monitor `pg_data`, run the D6 reaper backlog check (how many
`documents`/`extractions` rows are past `expires_at` but still present).

---

## 5. Redis

Job state lives here. The compose runs `--appendonly yes`, so a restart recovers
state; a crash of the *process* is handled by RQ's registries + enqueue-time
`Retry` ([`QUEUEING.md`](./QUEUEING.md) §4/§5). Watch memory growth (job `meta`
size, SSE pub/sub channels); if `maxmemory` evictions would ever hit
job metadata, that is an incident, not a tuning knob.

---

## 6. MinIO

| Symptom | Cause | Fix |
| --- | --- | --- |
| Presigned PUT 403s, opaque | Signature vs the browser's hostname, or CORS unset | [`DEPLOYMENT.md`](./DEPLOYMENT.md) §4 — `MINIO_SERVER_URL`, CORS rule, never a path-prefix proxy |
| Upload works but SSE "hangs" | Caddy buffering SSE | `flush_interval -1` ([`DEPLOYMENT.md`](./DEPLOYMENT.md) §3) |
| Disk full / lifecycle stalls | Retention rule behind | Check reaper backlog + lifecycle rule ([`DATA_MODEL.md`](./DATA_MODEL.md) §4) |

**Never delete objects out-of-band.** Deletions go through the deletion endpoint or
the retention lifecycle — bytes and rows must die together or you leak or orphan
([`SECURITY.md`](./SECURITY.md) §10).

---

## 7. Nightly eval regression (E5 gate red)

**Trigger:** the nightly eval against staging fails its accuracy or hallucination gate.

**Triage order:**

1. **What changed since the last green run?** `git log` since that baseline; diff
   `prompts.py`/`schemas.py` (`PROMPT_VERSION` should move together — a diff
   without a bump is already a bug), model swap, `max_dim` default, vLLM image bump.
   Every run records these in `summary.json` — the eval exists precisely so this
   question is answerable ([`EVAL.md`](./EVAL.md) §6).
2. **Confirm the signal.** Re-run with `--baseline <last green>` — a regression
   that vanishes on re-run was a flake; one that reproduces is a regression.
3. **Decide.** Fix forward (prompt/schema rule), revert a config (max_dim/model in
   `config.py`), or revert the vLLM image pin. Whichever you pick, the fix lands
   with a *new* eval run showing the gate green again.

**Never** widen the gate to absorb the regression. Lowering a ceiling because you
broke something is how "we stopped using it" starts.

---

## 8. Edge / certificates

- **Caddy public TLS:** renews itself; a silent certificate failure shows up as
  browser warnings or `caddy_data` volume growth. Tailscale certs otherwise
  ([`DEPLOYMENT.md`](./DEPLOYMENT.md) §5).
- **App deploys** use the `--no-deps` discipline ([`DEPLOYMENT.md`](./DEPLOYMENT.md)
  §7) — they must never touch the vLLM container, whose model load takes minutes.

---

## 9. Restore drill (plan E6 — the "is a backup a backup?" gate)

Run quarterly, from scratch, in exactly the steps below. Time it; record RTO.

1. **Postgres:** verify the cron `pg_dump` produced a fresh, non-empty file.
2. Restore into a *scratch* postgres (`docker run` a throwaway), then run the probe
   query set: row counts for `extractions`/`documents`, the most recent extraction,
   and a JSONB payload query (`data->>'vendor_name'`). The last one proves the
   GIN/JSONB data survived, not just the schema.
3. **MinIO:** `mc mirror` the bucket (or a subset) to scratch; verify object
   listing and a download hash match the originals.
4. **Cross-check:** pick one document, confirm its bytes (MinIO) and its
   extraction rows (Postgres) still point at the same `sha256`/`object_key`.
5. Record elapsed time and RTO/RPO. **An untested backup is not a backup** — and a
   drill you cannot finish in the declared RTO is a queue of work, not a plan.

---

## 10. Retention & deletion verification (plan D6)

On a schedule, confirm the two halves reaped together:

```sql
-- reaper backlog: past-expiry rows still present
SELECT count(*) FROM documents WHERE expires_at < now();
-- lifecycle expectation: same docs' objects should be gone from MinIO listing
```

End-to-end check: delete one document via the deletion endpoint, then confirm the
row is gone *and* the object is gone (both halves, [`SECURITY.md`](./SECURITY.md)
§13.6).

---

## 11. The three-line synthesis

- **Before every deploy:** `git log`, `TAG`, `PROMPT_VERSION` — write down what
  changes so the eval gate can tell you if that change was the regression.
- **After every incident:** the runbook gains a section, or the alert gains a fix
  step. Un-triaged alerts become noise, and noise gets ignored.
- **When in doubt at 2 a.m.:** restart nothing, delete nothing, leave the DLQ alone;
  check `docker compose ps` and the last deploy, and decide in the morning unless
  something is about to exceed a hard limit.