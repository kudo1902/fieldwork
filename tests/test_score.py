#!/usr/bin/env python
"""Scoring tests. Stdlib only -- runs without installing anything.

    python tests/test_score.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from score import (  # noqa: E402
    CORRECT,
    HALLUCINATED,
    MISSED,
    WRONG,
    Report,
    flatten,
    parse_number,
    score_case,
    template,
    values_match,
)

failures: list[str] = []


def eq(got, want, label):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def test_flatten():
    eq(
        flatten({"a": 1, "b": {"c": "x"}, "d": [{"e": 2}]}),
        {"a": 1, "b.c": "x", "d[0].e": 2},
        "flatten nested",
    )
    eq(flatten({"items": []}), {"items": []}, "flatten empty list")
    eq(template("line_items[3].total"), "line_items[].total", "template indices")


def test_parse_number():
    cases = {
        "1,500.00": 1500.0,     # en-US thousands
        "1.500,00": 1500.0,     # de-DE thousands
        "€ 1 500,00": 1500.0,   # currency + space separator
        "12,34": 12.34,         # bare decimal comma
        "1,234": 1234.0,        # bare thousands comma
        "$8.50": 8.5,
        "-42": -42.0,
        "": None,
        "N/A": None,
    }
    for raw, want in cases.items():
        eq(parse_number(raw), want, f"parse_number({raw!r})")
    eq(parse_number(1500), 1500.0, "parse_number(int)")
    eq(parse_number(True), None, "parse_number(bool) must not become 1.0")


def test_values_match():
    eq(values_match(1500.0, "1,500.00")[0], True, "numeric across formats")
    eq(values_match("Acme Ltd", "  acme   ltd ")[0], True, "text normalisation")
    eq(values_match(1500.0, 1500.01)[0], False, "cent difference is wrong")
    eq(values_match("Acme Ltd", "Acme Limited")[0], False, "exact by default")
    eq(values_match("Acme Ltd", "Acme Ltd.", text_threshold=0.9)[0], True, "fuzzy threshold")


def test_statuses():
    expected = {"a": "x", "b": "y", "c": None, "d": "z"}
    predicted = {"a": "x", "b": "WRONG", "c": "invented", "d": None}
    s = score_case("t", "invoice", expected, predicted)
    by_path = {c.path: c.status for c in s.comparisons}
    eq(by_path["a"], CORRECT, "correct")
    eq(by_path["b"], WRONG, "wrong")
    eq(by_path["c"], HALLUCINATED, "hallucinated")
    eq(by_path["d"], MISSED, "missed")
    eq(s.exact, False, "case not exact")
    eq(s.counts(), {CORRECT: 1, WRONG: 1, MISSED: 1, HALLUCINATED: 1}, "counts")


def test_both_empty_is_correct():
    s = score_case("t", "invoice", {"a": None}, {"a": None})
    eq(s.exact, True, "null == null is exact")


def test_extra_line_item_is_hallucinated():
    expected = {"line_items": [{"total": 10.0}]}
    predicted = {"line_items": [{"total": 10.0}, {"total": 99.0}]}
    s = score_case("t", "invoice", expected, predicted)
    by_path = {c.path: c.status for c in s.comparisons}
    eq(by_path["line_items[0].total"], CORRECT, "first item")
    eq(by_path["line_items[1].total"], HALLUCINATED, "invented item")


def test_report():
    good = score_case("g", "invoice", {"a": "x"}, {"a": "x"}, latency_s=1.0)
    bad = score_case("b", "invoice", {"a": "x"}, {"a": None}, latency_s=3.0)
    failed = score_case("f", "invoice", {"a": "x"}, None, ok=False, error="boom")
    r = Report([good, bad, failed])
    eq(r.n, 3, "case count")
    eq(r.request_failures, 1, "request failures")
    eq(round(r.exact_rate, 3), round(1 / 3, 3), "exact rate")
    eq(round(r.field_accuracy, 3), round(1 / 3, 3), "field accuracy")
    eq(r.totals()[MISSED], 2, "missed total")
    eq(r.by_field()["a"][CORRECT], 1, "by_field")
    eq(r.summary()["latency_p50_s"], 3.0, "p50 of [0,1,3]")


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
        fn()

if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("all scoring tests passed")
