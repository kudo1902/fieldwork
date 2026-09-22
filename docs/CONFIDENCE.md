# fieldwork — Confidence engine

Stage C. Turns "the model answered" into "you can trust the answer, or you should
check it". Implements plan tasks C1–C5; the review side that consumes it is
[`REVIEW_UI.md`](./REVIEW_UI.md); the OCR service feeding it is C2/C3.

---

## 1. The problem the signals must solve

The eval already separates the two failure modes
([`EVAL.md`](./EVAL.md) §1):

- **missed** — model returned `null` for a real value. Visible, disease-costly, un-guessable.
- **hallucinated** — model invented a value that looks right. **This is the one
  nothing downstream can detect on its own.**

Confidence's job is to detect the second class without a human: flag *unverifiable*
or *inconsistent* values so they route to review instead of auto-accept. It does
not need to be perfect — it needs to be honest about uncertainty, and it needs to
be tuned against the eval, not against intuition (plan C5).

---

## 2. Four signals, in order of value

From [`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.3. Each produces a per-field score
in `[0,1]` — 1 is "grounded/consistent", 0 is "fabricated/unverifiable".

### 2.1 Arithmetic rules (`rules.py`, C1)

For numeric schemas, cheap and high-precision:

```
sum(line_items.total)            ≈ subtotal
subtotal + tax_amount            ≈ total_amount
quantity × unit_price            ≈ line_items[i].total
currency ∈ ISO 4217 set
issue_date ≤ due_date (both present)
issue_date/purchase_date parse as YYYY-MM-DD
count(line_items) preserved through line totals
```

Rules reference **printed** values only — an invoice where the arithmetic is wrong
is still grounded. Never "fix" a value to satisfy a rule; the rules only *score*.
A rule that catches a mismatch penalises the involved fields, not the whole
document (their totals disagree → `subtotal`, `total_amount`, and the offending
line all lose points).

### 2.2 OCR cross-check (`ocr.py`, C2/C3)

PaddleOCR on CPU extracts raw lines; the cross-check asserts each emitted field
value appears somewhere in the raw text after normalisation. **The single
highest-value check in the system** — a value that appears nowhere on the page was
invented, full stop.

Success is defined with the same normalisation as scoring
([`EVAL.md`](./EVAL.md) §5): `parse_number` + whitespace/case folding. `1500.0`
(JSON number) checks against `1,500.00` (printed) — they must match, and they will
if the model followed the verbatim rule (`score.py` already proves this is
achievable).

| Result | Meaning | Action |
| --- | --- | --- |
| value found verbatim/normalised | grounded | score 1 |
| value absent from OCR text | ungrounded | score ≈ 0 + flag in `flags.cross_check` |
| field is `null` | honest unknown | **excluded** from the cross-check (see §3) |
| OCR itself is low-confidence/excludes the region | check is vacuous | score stays at prior, flag `ocr_low_confidence` |

The `plain` schema (markdown transcription) is exempt from per-field checks — its
"value" is the entire text.

### 2.3 Token logprobs (C4)

Request `logprobs` from vLLM (the current `[call]` does not, so
[`llm.py`](../src/fieldwork/llm.py) gains `logprobs: true` … per-field mean
probability, capped and floored). **Noisier than 1 and 2** — a low-probability
token can still be correct, and a confident wrong token is a hallucination signal
*against* this method. Use it only as a tie-breaker between 1/2, or for fields
where neither applies (free-text descriptions).

### 2.4 Self-consistency (reserved)

N=3 runs at `temperature ≈ 0.7`, compare across runs. Disagreement ⇒ distribute
the answer's probabilities. Costs 3× inference — **reserved for high-value fields
on high-value documents only**, enabled per-schema, never a default.

---

## 3. Nulls are evidence-neutral

A field that emitted `null`:

- contributes **zero** toward confidence (nothing to ground),
- is **excluded** from the cross-check, and
- does not lower it — the model was honest.

But nulls are the drift canary. The *null rate* per field is a telemetry metric
([`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.6): a jump from 2% to 30% on
`vendor_name` means something upstream changed. Confidence and null-rate are read
next to each other, or a "confident" system that is quietly emitting nothing looks
fine.

---

## 4. Combining into a routing decision

1. Per field: `confidence_f = combine(signal scores)` — a weighted blend, weights
   fixed by *signal quality* (OCR ≈ arithmetic > logprobs), not per-document.
2. Per document: aggregate over fields, weighted by field presence (a null field
   again excluded — you do not score what was honestly not extracted as a failure).
3. Route ([`worker.py`](C5)):

```
confidence ≥ threshold → auto-accept (status done, reviewed_by = 'auto')
confidence <  threshold → status review, pushed to the review queue
```

The **threshold is a tunable**, tuned against the eval set such that every
hallucinated field in the eval lands below it and as many exact matches above it.
The feedback loop is the review queue size: if reviewers can't keep pace, the
threshold is too low; if reviewers are finding nothing, it is too high
([`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.6 metric 3).

**Grounding as a correctness claim, not a guarantee.** OCR says the value is on the
page; it does not say the model read the *right* copy of it. Auto-accept is a
claim of "grounded and consistent", never "correct". Distinguish these in the UI
([`REVIEW_UI.md`](./REVIEW_UI.md)).

---

## 5. Field-level output shipped to the reviewer

`flags` JSONB on the extraction row carries per-field machinery, and the review UI
renders only what it needs:

```json
{
  "confidence": 0.94,
  "fields": {
    "total_amount":  { "confidence": 0.98, "grounded_in_ocr": true },
    "vendor_tax_id": { "confidence": 0.31, "grounded_in_ocr": false, "reason": "cross_check" }
  }
}
```

The reviewer is the last holdout — the UI must show **why** a field is flagged
(reason codes: `cross_check`, `arithmetic`, `logprobs`), not just a red dot.
Un-explainable confidence is a bug.

---

## 6. What Stage C must prove

- Every hallucinated field in the eval lands under the threshold
  (below → review, above → auto-accept), and every arithmetic-correct exact match
  lands above it.
- One doc clears review in under 30 seconds (plan C6 gate).
- The OCR cross-check has a near-zero false "grounded" rate on the eval's
  hallucination subset (a value *present* in OCR but misread is the accepted
  residual class; a value *invented* that the check blesses is a failure).
- Confidence stays out of the `extract()` core: it runs in the worker next to
  validation, after `llm.py` returns. [The seam holds.]