# fieldwork — Review UI

Stage C's user-facing surface (plan C6/C7, consuming C4/C5). **C6 decides whether
the system is used.** Review throughput is the real bottleneck in every
human-in-the-loop product; a slow review UI silently converts to "we stopped using
it" about three weeks in. This spec is written around one reviewer on one screen —
the plan's decision 4 (single tenant, review by you).

---

## 1. Principles

1. **Keyboard-first from the first commit.** `Tab` between fields, `Enter` to
   accept the current value, `J`/`K` for next/previous document. The mouse is for
   image zooming only. If a reviewer reaches for the mouse to continue, the UI has
   a hole.
2. **One screen.** The original image beside the fields. Both are always visible;
   scrolling is the fallback, not the layout.
3. **Flag first, then everyone.** Load documents ordered by confidence ascending so
   the flagged fields walk in first. The reviewer works the exception list, not
   the whole corpus.
4. **Save a full corrected payload, not a patch.** `reviews.corrected` is the whole
   document (`DATA_MODEL.md` §2), because the diff against `extractions.data` must
   stay computable forever.
5. **Never assume the prediction is wrong.** Most flagged fields are *correct but
   un-grounded* (e.g. a mutilated invoice number). Accepting a correct value is the
   fastest action and the most common one.

---

## 2. Layout

```
┌──────────────────────────────┬────────────────────────────────┐
│  Document image              │  invoice  INV-2026-0417        │
│  (zoom: scrollwheel,         │  [progress 3/12] [conf 0.61]   │
│   pan: drag)                 │                                │
│                              │  # must verify (2)             │
│                              │  ▸ vendor_tax_id   GB123…  [!] │
│                              │  ▸ due_date        2026-05-17 ✓│
│                              │  # accepted                       │
│                              │  ✓ invoice_number   ...         │
│                              │  … line items   (table below)   │
└──────────────────────────────┴────────────────────────────────┘
```

- The active field's surrounding region is highlighted in the image; the reviewer
  sees *where* the model looked. (Becomes a bounding box with Stage C grounding, if
  adopted.)
- Flagged items carry a `reason` code the confidence engine sent
  ([`CONFIDENCE.md`](./CONFIDENCE.md) §5): `cross_check`, `arithmetic`, `logprobs`.
  A bare red dot without a reason is a bug.
- `null` renders as a deliberately different control (`—` styled as "no value")
  from an empty-but-valued output. Accepting `null` is a first-class action.

---

## 3. Keyboard map

| Key | Action |
| --- | --- |
| `Tab` / `Shift+Tab` | next / previous field (flagged first, then canonical order) |
| `Enter` | accept *current displayed value* (auto-accept spacing) |
| `N` | set field to `null` ("it's not on the page") |
| `E` | edit inline (then `Enter` redeems the edit) |
| `Space` | toggle `mark for review` (a field you want to inspect again) |
| `J` / `K` | next / previous document in the queue |
| `S` | submit the whole review (all-or-nothing commit) |

`S` is all-or-nothing: there is no per-field autosave mid-document, because
"reviewed but half-edited" is a state with no meaning. The commit posts the full
`corrected` payload (`reviews.corrected`), which the UI diffs against the
prediction and displays *before* it is final (`Ctrl+Enter` to actually save).

---

## 4. Field controls per type

| Field type | Control | Notes |
| --- | --- | --- |
| string | single-line input | verbatim — no autocorrect, ever |
| number | validated input, JS `parseFloat` + `parse_number`-style guard | reject currency symbols/commas on save |
| date | `YYYY-MM-DD` over a hidden date picker | type the ISO string; the picker is a fallback |
| line items | table; `Tab` moves cell→cell; `Insert` new row, `Delete` removes; `↑/↓` reorder | order is part of the contract ([`EVAL.md`](./EVAL.md) §4) |
| `null` | explicit `N` | not a blank cell |

---

## 5. The promotion path (C8) — deliberately manual

- Every submitted review creates a candidate in the *eval promotion inbox*
  (`eval_cases.source = 'review:promotion'`), the diff (`extraction.data` vs
  `reviews.corrected`) attached.
- Promotion into the eval set is a separate, **reviewed** step (`eval/promote.py`).
  Auto-promoting is forbidden: one careless reviewer poisons the golden set and you
  will not notice for months ([`EVAL.md`](./EVAL.md) §8).
- Dedupe by `document_id + schema_name`; the same source document should produce
  one eval case, not a case per review round.
- A promotion must be validatable right back to the schema (`run_eval.py --check`),
  so a bad batch is caught at the gate.

---

## 6. Metrics the UI measures (and the plan should enforce)

| Metric | Target | Why |
| --- | --- | --- |
| seconds per document | < 30s | plan C6 gate; the "usable" bar |
| review queue size | near-zero for hours | feedback loop on the confidence threshold (§4 in [`CONFIDENCE.md`](./CONFIDENCE.md)) |
| acceptance rate | high | if reviewers constantly correct, the threshold or prompt is wrong |
| false-flag rate | low | count "model said flag, I accepted anyway" — page the confidence author, not the reviewer |

These are the same numbers the eval's per-field `null rate` and `hallucination
rate` already describe ([`ARCHITECTURE.md`](./ARCHITECTURE.md) §4.6) — the review
UI is where they become seconds, not percentages.

---

## 7. What stays out

- **No model contract in the review UI.** Dynamic field definitions are rendered
  from `GET /v1/schemas/{name}` at load time; the UI adds no schema logic of its
  own.
- **No "AI fix it" button.** Review corrections are authoritative input to the
  golden set; a "let the model fix it" button just re-injects the thing you are
  reviewing.
- **No branching review workflows.** One reviewer, one queue, one accept action,
  all-or-nothing commit. Add more state when there is more than one reviewer, that
  day is the day to redesign this screen.