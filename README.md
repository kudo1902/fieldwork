# fieldwork

Structured extraction from document images, using a self-hosted Gemma 4 VLM
behind an OpenAI-compatible endpoint.

**Phase 0** — an eval harness, so every later decision is measured.
**Phase 1** — the thinnest vertical slice: upload → extract → validated JSON.

Phase 2 (queue, object storage, Postgres, review UI) and Phase 3 (auth, rate
limits, caching, metrics) are not built yet. The `extract()` core is already
shaped so they slot in without rewriting it.

---

## Architecture

The full target architecture — including Phase 2-4 components that are not built yet — is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), with an interactive diagram at
[`docs/architecture.html`](docs/architecture.html).

The build order, task by task, is in
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).

How it gets served and released: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

All design docs:

| Doc | Covers |
| --- | --- |
| [`docs/EVAL.md`](docs/EVAL.md) | how the numbers are made and read; baselines, sweeps |
| [`docs/PROMPTING.md`](docs/PROMPTING.md) | the prompt/schema iteration loop, `PROMPT_VERSION` |
| [`docs/API.md`](docs/API.md) | endpoint contract, SSE protocol, error ladder |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Postgres schema, JSONB conventions, MinIO layout |
| [`docs/QUEUEING.md`](docs/QUEUEING.md) | job lifecycle, queue split, retries/DLQ |
| [`docs/CONFIDENCE.md`](docs/CONFIDENCE.md) | rules, OCR cross-check, confidence, routing threshold |
| [`docs/REVIEW_UI.md`](docs/REVIEW_UI.md) | the Stage C review screen, keyboard-first |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model and controls |
| [`docs/TELEMETRY.md`](docs/TELEMETRY.md) | metrics, alerts, latency SLO |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | failure remediation, restore drill |
| [`docs/TESTING.md`](docs/TESTING.md) | unit / integration / eval-gate test layers |
| [`docs/MODULES.md`](docs/MODULES.md) | per-module contracts, seams, dependencies |

## Layout

```
src/fieldwork/
  config.py      env-driven settings
  schemas.py     Pydantic target schemas + strict-JSON-schema hardening
  prompts.py     system / user / repair prompts  <- iterate here
  preprocess.py  EXIF, HEIC, PDF→pages, resize, data URLs
  llm.py         the only module that talks to the model
  extract.py     bytes in, validated data out
  api.py         Phase 1 Flask app
eval/
  dataset/       your ground truth (see eval/dataset/README.md)
  score.py       field-level scoring
  run_eval.py    Phase 0 harness
web/index.html   Phase 1 frontend, no build step
```

## 1. Serve the model (GPU box)

Gemma 4 12B is the recommended starting point: unified encoder-free
multimodal, 256K context, Apache 2.0, and it fits in 16GB VRAM. Step up to the
31B dense or 26B A4B MoE only if the eval says you need to.

```bash
pip install vllm
vllm serve google/gemma-4-12B-it \
  --served-model-name gemma-4-12B-it \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image": 8}'
```

Flag syntax drifts between vLLM releases — check
<https://recipes.vllm.ai/Google/gemma-4-12B-it> against your installed version.
Lower `--max-model-len` first if you hit OOM at startup.

For laptop development without the GPU box, anything OpenAI-compatible works
(Ollama, LM Studio) — just point `FIELDWORK_LLM_BASE_URL` at it.

### Visual token budget

Gemma 4 exposes a per-image visual token budget (70 / 140 / 280 / 560 / 1120)
that trades speed against OCR detail. The kwarg name depends on your vLLM
version, so it is not hardcoded: put it in `FIELDWORK_EXTRA_BODY` as JSON and it is
passed through verbatim. **Verify it actually takes effect** (latency and
`prompt_tokens` should both move) before trusting it — if it doesn't, use
`FIELDWORK_MAX_IMAGE_DIM` instead, which controls the same trade-off by resizing.

## 2. Install the app

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env    # then point FIELDWORK_LLM_BASE_URL at your GPU box
```

## 3. Phase 0 — build and run the eval

Read `eval/dataset/README.md` first; it explains what to collect and how to
write ground truth. Then:

```bash
python eval/run_eval.py --check      # validate ground truth, no GPU needed
python eval/run_eval.py              # run it
```

You get field-level accuracy, a worst-fields-first table, and — the number that
matters most — a hallucination rate. Every run is saved to `eval/runs/<ts>/`.

Change a prompt in `prompts.py`, re-run against the previous run, and see
whether it actually helped:

```bash
python eval/run_eval.py --baseline eval/runs/2026-09-21T10-00-00 --tag "stricter null rule"
```

Useful sweeps:

```bash
python eval/run_eval.py --max-dim 1024      # is the resize costing accuracy?
python eval/run_eval.py --model gemma-4-31B-it   # is the bigger model worth it?
python eval/run_eval.py --text-threshold 0.95    # fuzzy match for long text
```

## 4. Phase 1 — run the service

```bash
flask --app fieldwork.api run --debug --port 8080
```

Open <http://localhost:8080>. Drag in an image, pick a document type, extract.
The header shows whether your model server is reachable and serving the model
you configured.

API:

| endpoint | purpose |
| --- | --- |
| `GET /healthz` | model server reachability + served models |
| `GET /v1/schemas` | available document types |
| `GET /v1/schemas/{name}` | the JSON schema sent to the model |
| `POST /v1/extractions` | multipart `file` + `schema` → validated JSON |

## Design notes

**Everything is nullable.** The model is told that a null is correct and a guess
is a defect. Schema strictness is enforced at validation and review time, not by
forcing the model to fill every field.

**Strict guided decoding.** `json_schema_for()` rewrites the Pydantic schema so
every object forbids extra properties and requires every key. Combined with
vLLM's `response_format`, malformed JSON is close to impossible; the repair loop
in `llm.py` exists for truncation and other edge cases.

**Text in an image is data, never instructions.** The system prompt says so
explicitly, and nothing downstream should re-inject extracted text into a
prompt without validating it first. This matters as soon as you accept uploads
from anyone but yourself.

**Synchronous by design.** Two separate things are sync here and only one is temporary.
Flask and `extract()` are sync because the Python tier does almost no CPU work — it waits on
vLLM — and that stays. What is temporary is running inference *inside the request*: Phase 1
holds a GPU slot for the duration, which is right for proving the UX and wrong for real
traffic. Phase 2 moves inference onto an RQ worker and leaves `extract()` untouched.

## Known limits

- No auth, no rate limiting, no persistence. Do not expose this to the internet.
- Uploads are buffered fully in memory before the size check.
- PDFs are capped at `FIELDWORK_MAX_PDF_PAGES` pages; longer documents are truncated
  silently.
- No content-hash caching yet, so re-uploading the same file re-runs inference.
