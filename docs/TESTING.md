# fieldwork — Testing strategy

What gets tested, in which layer, and who (which CI job) runs it. Companion to
[`EVAL.md`](./EVAL.md) (the measurement harness) and plan E4a/E4b (toolchain + CI)
and B10 (queue integration test). Today the suite is a single stdlib file,
`tests/test_score.py`, covering scoring only.

---

## 1. The four layers

| Layer | What it proves | Needs GPU? | Runs where |
| --- | --- | --- | --- |
| **Unit** (pytest) | logic correctness: scoring, schemas, preprocessing, repair loop, rules | no | every PR |
| **Integration** (plan B10) | the wiring: 50 docs through the real queue → worker → vLLM → Postgres | yes | scheduled, at least pre-release |
| **Eval gate** (plan E5) | the model did not get worse | yes | nightly on staging vLLM |
| **Deploy** | migrations apply additively, healthz green | prod | on every release |

The eval gate is the one that *protects the product*: unit tests cannot tell you the
model got worse — only the eval set can. The other layers protect you from yourself
(score bugs, wiring bugs, deploy bugs) so the eval gate measures the model, not
your typos.

---

## 2. Unit-test boundaries

**Never call vLLM in unit tests.** Inject a fake OpenAI client that records
`chat.completions.create` requests, then assert:

- `response_format` is `{"type":"json_schema", ..., "strict": true}` (the hardening
  actually reached the wire),
- `model`, `temperature=0`, `max_tokens`, and `extra_body` pass-through match
  config,
- the repair loop appends assistant + REPAIR_INSTRUCTION messages on the second
  response and stops at the attempt cap ([`PROMPTING.md`](./PROMPTING.md) §6).

The seam is `llm.client()` (module-level) — the fake replaces it, and no network
ever happens.

Other easy-to-get-wrong units worth explicit tests:

- `schemas._harden()` — objects get `additionalProperties:false` + full `required`,
  recursing into line-item arrays.
- `preprocess` — synthetic PIL images only: EXIF rotation honoured, transparent PNG
  flattened to white, resize respects `max_dim`, undecomposable bytes raise
  `UnsupportedFile`, PDF page cap enforced, decompression-bomb guard trips.
- `score.py` — already covered; convert the stdlib runner to pytest (§3).
- `config` — env parsing incl. `FIELDWORK_EXTRA_BODY` JSON and defaults.
- `rules`/`confidence` (Stage C) — pure functions in `rules.py`/`confidence.py` get
  table-driven tests before the OCR service exists (their inputs are numbers and
  OCR tokens, easy to fake).

**Fixtures rule:** never use the golden eval dataset as unit fixtures — it is the
measurement, not the harness ([`EVAL.md`](./EVAL.md) §8). Commit a small synthetic
corpus under `tests/fixtures/` (a generated invoice image, a rotated HEIC, a
2-page PDF). Generated, not photographed, so they are reproducibly small and never
contain PII.

---

## 3. Toolchain adoption (plan E4a) — the concrete scope

1. **Dev extras** in `pyproject.toml`: `ruff`, `mypy`, `pytest`.
2. **Configs:** `[tool.ruff]` (line length 88, default rules + `BLE` off or scoped),
   `[tool.mypy]` (strict for `src/fieldwork`, permissive for `eval/` until annotated),
   `[tool.pytest.ini_options]` (testpaths, `addopts = "-q"`).
3. **Convert `tests/test_score.py`** to pytest functions (it is already
   function-per-test — the changes are the runner and `assert`-driven style).
4. **Annotate the untyped modules** (`preprocess.py`, `eval/run_eval.py`,
   `api.py`, `llm.py` return types) so `mypy --strict` holds on `src/`.
5. **CI (plan E4b):** on every PR — `ruff check`, `ruff format --check`, `mypy`,
   `pytest` (unit). Integration + eval gate on their own schedules; deploy tests on
   release tags.

---

## 4. Integration layer (plan B10)

The 50-document queue test runs against the real compose stack. Its invariants
(already the plan gate, [`QUEUEING.md`](./QUEUEING.md) §7):

- zero lost jobs (every enqueued job reaches `done` or a documented `failed`),
- progress visible (SSE events in order; DB status transitions match),
- a killed worker re-enqueues, never silently drops,
- `extract()` is byte-identical before/after the async migration (the seam check).

Mark it `@pytest.mark.integration` and keep it out of the PR path — it needs the
whole stack and a GPU.

---

## 5. What to *not* test

- **Prompt prose.** "the system prompt contains the null rule" is a silk-to-silk
  test — the string is present by construction. Behavior belongs in the eval gate.
  The one structural prompt test worth having: **any diff to `prompts.py` or
  `schemas.py` in a PR must bump `PROMPT_VERSION`** — enforce it as a CI check
  (cheap, and it prevents the plan-D4 cache poisoning in
  [`PROMPTING.md`](./PROMPTING.md) §3).
- **JSONB content.** Schema-valid payloads are the model's output; asserting exact
  values in unit tests re-hardcodes ground truth, which is the eval's job.
- **The eval numbers.** Unit tests assert *logic*; accuracy/hallucination is the
  nightly gate's verdict.

---

## 6. Who runs what (summary)

| CI job | Command | GPU | Frequency |
| --- | --- | --- | --- |
| quality | `ruff check && ruff format --check && mypy` | no | PR |
| unit | `pytest` | no | PR |
| prompt_version gate | diff check on `prompts.py`/`schemas.py` → `PROMPT_VERSION` | no | PR |
| integration | `pytest -m integration` | yes | on schedule + pre-release |
| eval gate | `python eval/run_eval.py --baseline <last green> --tag nightly` | yes | nightly, hard gate |
| deploy | `alembic upgrade head` on scratch + `healthz` | — | release tags |