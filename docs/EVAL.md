# fieldwork — Evaluation

Phase 0's reason for existing. Every decision in this project is either measured
against the eval set or it is vibes. This document says *how the numbers are made,
how to read them, and what counts as an improvement*. The dataset layout and ground
truth rules live in [`eval/dataset/README.md`](../eval/dataset/README.md).

Companion to [`ARCHITECTURE.md`](./ARCHITECTURE.md) (§principle 4) and the
implementation plan's Stage A.

---

## 1. The numbers that matter

The eval produces exactly four headline numbers. Watch them separately; their costs
are different.

| Number | Definition | What it costs you |
| --- | --- | --- |
| **Field accuracy** | correct fields / all fields compared | headline health |
| **Exact-case rate** | cases where every field matched | how often you can skip review |
| **Hallucination rate** | expected `null`, model invented a value | **trust** — invisible downstream |
| **Latency p50/p95** | per-case request time | budget, model sizing, SLO |

Optimise hallucination rate toward zero first. A missed field is visible to the
person who has to re-key it; an invented one looks identical to a correct one until
something downstream breaks.

Scoring is implemented in `eval/score.py`; the four per-field statuses are
`correct`, `wrong`, `missed`, `hallucinated`.

---

## 2. Ground truth — the part that determines the rest

Collected per [`eval/dataset/README.md`](../eval/dataset/README.md). Rules in short:

- Transcribe what is **printed**, not what is correct. A wrong total on the invoice
  is the expected value.
- `null` for anything absent, cropped, blurred, or illegible — never a guess.
- Numbers as JSON numbers, dates as `YYYY-MM-DD`, everything else verbatim.

Three rules that are easy to get wrong:

1. **Include documents with genuinely absent fields.** They are the only way to
   measure hallucination, and they are the subset everyone forgets to collect.
2. **`--check` proves shape, not correctness.** `python eval/run_eval.py --check`
   validates that ground truth parses against the Pydantic schema. A *wrong* value
   passes `--check` silently. Spot-check a sample against the originals before
   trusting a new batch.
3. **Ground truth is the model's contract.** If the dataset says the date is
   `2026-04-17` but the document prints `17/04/2026` with no other evidence, the
   rule in the prompt is "return null when genuinely ambiguous" — the ground truth
   should say `null` for that ambiguity, not whichever reading you happen to favour.

---

## 3. How a run works

```bash
python eval/run_eval.py --check            # validate ground truth, no model needed
python eval/run_eval.py                    # run everything (defaults from config)
python eval/run_eval.py --schema invoice --concurrency 4
python eval/run_eval.py --baseline eval/runs/2026-09-21T10-00-00 --tag "stricter null rule"
```

Each case is: image + ground truth → `extract_file` → score against ground truth.
Every run is saved to `eval/runs/<timestamp>/`:

| File | Contents |
| --- | --- |
| `summary.json` | tag, model, base_url, max_dim, text_threshold, summary numbers, `by_field` |
| `results.json` | per-case: ok, error, exact, latency, and the full comparison list |
| `predictions.json` | every raw model response (`result.to_dict()`) |

The summary renders as a worst-fields-first table plus the per-case failures.

Useful flags (`python eval/run_eval.py --help`):

| Flag | Tests | Question it answers |
| --- | --- | --- |
| `--max-dim 1024` | resize cost | is the resize costing accuracy? |
| `--model gemma-4-31B-it` | model size | is the bigger model worth it? |
| `--text-threshold 0.95` | fuzzy text match | are long descriptions degrading the score? |
| `--baseline <run>` | best/previous | did this change help? |

---

## 4. Reading the field table

`by_field` aggregates rows by template — `line_items[3].total` folds into
`line_items[].total` — so per-line accuracy is visible as one row instead of one
row per invoice line.

**Alignment caveat.** Line items are compared positionally (item *i* vs item *i*),
because the prompt says "in the order printed". One extra or invented line therefore
shifts every row after it: a good line becomes `missed` (prediction shifted) or
`hallucinated` (extra row), even though the extraction "looks right". That is
intentional — order is part of the contract — but it means:

- a dense 30-item invoice dominates the per-field totals of a dozen receipts, and
- a single duplicated line item inflates *both* missed and hallucinated rates.

When you compare runs, compare the same dataset. When you compare against a target,
say explicitly whether line items count in "field accuracy" (see plan decision 3).

---

## 5. Numbers and text comparison

`score.py` normalises both sides before comparing:

- **Numbers.** `"1,500.00"`, `"1.500,00"`, `"€ 1 500,00"` and `1500.0` all match
  (decimal-separator guessing, currency stripping, tolerance ~0.005). `bool` is
  never coerced to `1`.
- **Text.** Exact match after whitespace/case folding by default. `--text-threshold`
  < 1 turns on `SequenceMatcher` fuzzy matching for long descriptions — use it only
  when the field genuinely varies in transcription (vendor name, long descriptions),
  never for IDs or dates.

---

## 6. Baseline workflow — the actual discipline

1. Something changes: a prompt, a schema, a resize default, a model.
2. Run tagged, with `--baseline <previous best run>`.
3. **The change ships only if the measured deltas move the right way** — and a
   hallucination regression never ships, even if accuracy improved.

Read the deltas in the summary as `+1.2%` (green = good for that metric,
red = bad). `request_failures` is a hard red too — a change that breaks requests
does not get credit for fewer hallucinations.

### Comparability

A run's `summary.json` records model, base_url, max_dim, and text_threshold. Two
runs are comparable only when these match. `prompt_version` (introduced in Stage A,
task A10) must be recorded too and bumped on every prompt/schema change — without
it, runs from before and after a prompt edit are silently compared as if equivalent,
which is exactly the failure the cache key (see [`ARCHITECTURE.md`](./ARCHITECTURE.md)
§4.5) protects against on the serving side.

---

## 7. Sweeps

Sweep scripts are single flags, not programs:

- **max_dim** (1024 / 1536 / 2048): pick the knee where field accuracy stops
  climbing but latency still improves. This is the primary speed/accuracy dial
  (plan A6).
- **model size** (12B vs 31B): only after A5 is short of target (plan A7).
- **quantization** (FP8 / AWQ): run the *entire* eval set against each variant — a
  quantisation that only degrades on blurry handwriting will show up as a rich,
  specific per-field delta, not a flat drop (plan A8).

Record every decision by writing the winning value into `config.py` (plan A9) and
noting it in the README.

---

## 8. Guarding the number against yourself

- **Don't tune on the set you report.** Keep 5–10 held-out cases that never guide a
  prompt edit; measure final performance on them.
- **Promotions (plan C8) are a leak risk.** Corrected extractions promoted into the
  eval set are exactly-shaped trees with roots in your own model's mistakes. They
  must be reviewed before promotion, or the golden set slowly asserts whatever the
  model currently does.
- **Gridlock is data, not failure.** If accuracy is flat across max_dim *and* model
  sizes, the model cannot read your documents — more engineering will not fix it.
  Reconsider scope (plan A gate).