"""Field-level scoring.

Four outcomes per leaf field, because "accuracy" alone hides the distinction
that matters most in extraction:

  correct       expected and predicted agree (including both being empty)
  wrong         both present, values differ
  missed        expected a value, model returned null   -> recall problem
  hallucinated  expected null, model invented a value   -> trust problem

A system with 5% missed is annoying. A system with 5% hallucinated is
dangerous, because nothing downstream can tell which 5%.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

CORRECT, WRONG, MISSED, HALLUCINATED = "correct", "wrong", "missed", "hallucinated"

_WS = re.compile(r"\s+")
_CURRENCY = re.compile(r"[^\d,.\-]")
_INDEX = re.compile(r"\[\d+\]")


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Collapse nested structures into {dotted.path: leaf_value}."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = []
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def template(path: str) -> str:
    """line_items[3].total -> line_items[].total, so rows aggregate."""
    return _INDEX.sub("[]", path)


def is_empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def normalise_text(s: str) -> str:
    return _WS.sub(" ", s).strip().casefold()


def parse_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = _CURRENCY.sub("", v).strip()
    if not s:
        return None
    # Guess the decimal separator: whichever appears last wins.
    if "," in s and "." in s:
        sep = max(s.rfind(","), s.rfind("."))
        s = s[:sep].replace(",", "").replace(".", "") + "." + s[sep + 1 :]
    elif "," in s:
        # A single comma is a decimal separator only if it looks like one.
        s = s.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise_text(a), normalise_text(b)).ratio()


def values_match(expected: Any, got: Any, text_threshold: float = 1.0) -> tuple[bool, float]:
    """Returns (matched, similarity). Similarity is 1.0 for non-text matches."""
    en, gn = parse_number(expected), parse_number(got)
    if en is not None and gn is not None:
        return math.isclose(en, gn, rel_tol=1e-6, abs_tol=0.005), 1.0

    if isinstance(expected, bool) or isinstance(got, bool):
        return expected == got, 1.0

    es, gs = str(expected), str(got)
    if normalise_text(es) == normalise_text(gs):
        return True, 1.0

    sim = similarity(es, gs)
    return sim >= text_threshold, sim


@dataclass
class Comparison:
    path: str
    status: str
    expected: Any = None
    got: Any = None
    similarity: float = 1.0


@dataclass
class CaseScore:
    case: str
    schema: str
    ok: bool
    error: str | None = None
    latency_s: float = 0.0
    comparisons: list[Comparison] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return self.ok and all(c.status == CORRECT for c in self.comparisons)

    def counts(self) -> dict[str, int]:
        out = {CORRECT: 0, WRONG: 0, MISSED: 0, HALLUCINATED: 0}
        for c in self.comparisons:
            out[c.status] += 1
        return out


def score_case(
    case: str,
    schema: str,
    expected: dict[str, Any],
    predicted: dict[str, Any] | None,
    *,
    ok: bool = True,
    error: str | None = None,
    latency_s: float = 0.0,
    text_threshold: float = 1.0,
) -> CaseScore:
    score = CaseScore(case=case, schema=schema, ok=ok, error=error, latency_s=latency_s)

    exp_flat = flatten(expected)
    got_flat = flatten(predicted or {})

    for path in sorted(set(exp_flat) | set(got_flat)):
        e, g = exp_flat.get(path), got_flat.get(path)
        e_empty, g_empty = is_empty(e), is_empty(g)

        if e_empty and g_empty:
            status, sim = CORRECT, 1.0
        elif e_empty:
            status, sim = HALLUCINATED, 0.0
        elif g_empty:
            status, sim = MISSED, 0.0
        else:
            matched, sim = values_match(e, g, text_threshold)
            status = CORRECT if matched else WRONG

        score.comparisons.append(
            Comparison(path=path, status=status, expected=e, got=g, similarity=sim)
        )
    return score


@dataclass
class Report:
    cases: list[CaseScore]

    @property
    def n(self) -> int:
        return len(self.cases)

    @property
    def request_failures(self) -> int:
        return sum(1 for c in self.cases if not c.ok)

    @property
    def exact_rate(self) -> float:
        return _safe(sum(1 for c in self.cases if c.exact), self.n)

    def totals(self) -> dict[str, int]:
        out = {CORRECT: 0, WRONG: 0, MISSED: 0, HALLUCINATED: 0}
        for c in self.cases:
            for k, v in c.counts().items():
                out[k] += v
        return out

    @property
    def field_accuracy(self) -> float:
        t = self.totals()
        return _safe(t[CORRECT], sum(t.values()))

    @property
    def hallucination_rate(self) -> float:
        t = self.totals()
        return _safe(t[HALLUCINATED], sum(t.values()))

    def by_field(self) -> dict[str, dict[str, int]]:
        rows: dict[str, dict[str, int]] = {}
        for case in self.cases:
            for c in case.comparisons:
                row = rows.setdefault(
                    template(c.path), {CORRECT: 0, WRONG: 0, MISSED: 0, HALLUCINATED: 0}
                )
                row[c.status] += 1
        return rows

    def latencies(self) -> tuple[float, float]:
        vals = sorted(c.latency_s for c in self.cases if c.latency_s)
        if not vals:
            return 0.0, 0.0
        p50 = vals[len(vals) // 2]
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        return p50, p95

    def summary(self) -> dict[str, Any]:
        p50, p95 = self.latencies()
        return {
            "cases": self.n,
            "request_failures": self.request_failures,
            "exact_case_rate": round(self.exact_rate, 4),
            "field_accuracy": round(self.field_accuracy, 4),
            "hallucination_rate": round(self.hallucination_rate, 4),
            "totals": self.totals(),
            "latency_p50_s": round(p50, 2),
            "latency_p95_s": round(p95, 2),
        }


def _safe(num: int, den: int) -> float:
    return num / den if den else 0.0
