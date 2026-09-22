#!/usr/bin/env python
"""Phase 0 harness: run the dataset, score it, save the run.

    python eval/run_eval.py --check            # validate ground truth only
    python eval/run_eval.py                    # run everything
    python eval/run_eval.py --schema invoice --concurrency 4
    python eval/run_eval.py --baseline eval/runs/2026-09-21T10-00-00

Every run is written to eval/runs/<timestamp>/ so you can diff a prompt or
model change against a known baseline instead of trusting your memory of
"it seemed better".
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fieldwork import schemas  # noqa: E402
from fieldwork.config import settings  # noqa: E402
from fieldwork.extract import extract_file  # noqa: E402
from score import (  # noqa: E402
    CORRECT,
    HALLUCINATED,
    MISSED,
    WRONG,
    Report,
    score_case,
)

DATASET = ROOT / "eval" / "dataset"
RUNS = ROOT / "eval" / "runs"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".pdf"}

console = Console()


def discover(schema_filter: str | None, limit: int | None) -> list[tuple[str, str, Path, Path]]:
    """Yield (case_id, schema, image_path, expected_path)."""
    cases = []
    for schema_dir in sorted(p for p in DATASET.iterdir() if p.is_dir()):
        schema = schema_dir.name
        if schema_filter and schema != schema_filter:
            continue
        if schema not in schemas.REGISTRY:
            console.print(f"[yellow]skipping {schema}/ - no such schema in REGISTRY[/]")
            continue
        for img in sorted(schema_dir.iterdir()):
            if img.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            expected = img.parent / f"{img.stem}.expected.json"
            if not expected.exists():
                console.print(f"[yellow]no ground truth for {img.name}, skipping[/]")
                continue
            cases.append((f"{schema}/{img.stem}", schema, img, expected))
    return cases[:limit] if limit else cases


def check(cases) -> int:
    """Validate ground truth against the Pydantic schemas. No GPU needed."""
    problems = 0
    for case_id, schema, _img, exp_path in cases:
        try:
            raw = json.loads(exp_path.read_text())
        except json.JSONDecodeError as exc:
            console.print(f"[red]{case_id}: invalid JSON - {exc}[/]")
            problems += 1
            continue
        try:
            schemas.get(schema).model.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{case_id}: does not match schema[/]\n  {exc}")
            problems += 1
    total = len(cases)
    if problems:
        console.print(f"\n[red]{problems}/{total} ground-truth files need fixing[/]")
    else:
        console.print(f"\n[green]all {total} ground-truth files valid[/]")
    return 1 if problems else 0


def run_one(case_id, schema, img, exp_path, text_threshold, max_dim):
    result = extract_file(img, schema, max_dim=max_dim)
    expected = json.loads(exp_path.read_text())
    score = score_case(
        case_id,
        schema,
        expected,
        result.data,
        ok=result.ok,
        error=result.error,
        latency_s=result.latency_s,
        text_threshold=text_threshold,
    )
    return score, result


def render(report: Report, baseline: dict | None) -> None:
    s = report.summary()

    t = Table(title="Summary", show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column(justify="right")

    def row(label, value, key=None, higher_is_better=True, pct=False):
        text = f"{value:.1%}" if pct else str(value)
        if baseline and key and key in baseline:
            delta = value - baseline[key]
            if abs(delta) > 1e-9:
                good = (delta > 0) == higher_is_better
                colour = "green" if good else "red"
                shown = f"{delta:+.1%}" if pct else f"{delta:+.2f}"
                text += f"  [{colour}]{shown}[/]"
        t.add_row(label, text)

    row("cases", s["cases"])
    row("request failures", s["request_failures"], "request_failures", False)
    row("field accuracy", report.field_accuracy, "field_accuracy", True, pct=True)
    row("exact-match cases", report.exact_rate, "exact_case_rate", True, pct=True)
    row("hallucination rate", report.hallucination_rate, "hallucination_rate", False, pct=True)
    row("latency p50", f"{s['latency_p50_s']}s")
    row("latency p95", f"{s['latency_p95_s']}s")
    console.print(t)
    console.print()

    ft = Table(title="By field (worst first)")
    ft.add_column("field")
    ft.add_column("acc", justify="right")
    ft.add_column("ok", justify="right", style="green")
    ft.add_column("wrong", justify="right", style="yellow")
    ft.add_column("missed", justify="right", style="cyan")
    ft.add_column("halluc", justify="right", style="red")

    rows = []
    for field_name, counts in report.by_field().items():
        total = sum(counts.values())
        acc = counts[CORRECT] / total if total else 0.0
        rows.append((acc, field_name, counts))
    for acc, field_name, c in sorted(rows):
        ft.add_row(
            field_name,
            f"{acc:.0%}",
            str(c[CORRECT]),
            str(c[WRONG]),
            str(c[MISSED]),
            str(c[HALLUCINATED]),
        )
    console.print(ft)
    console.print()

    bad = [c for c in report.cases if not c.exact]
    if bad:
        console.print(f"[bold]{len(bad)} case(s) not exact:[/]")
        for case in bad[:10]:
            if not case.ok:
                console.print(f"  [red]{case.case}[/]: request failed - {case.error}")
                continue
            issues = [c for c in case.comparisons if c.status != CORRECT]
            console.print(f"  [yellow]{case.case}[/] ({len(issues)} field(s))")
            for c in issues[:6]:
                console.print(
                    f"      {c.path}: [dim]{c.status}[/] "
                    f"expected={_short(c.expected)} got={_short(c.got)}"
                )
        if len(bad) > 10:
            console.print(f"  [dim]... and {len(bad) - 10} more, see results.json[/]")


def _short(v, n: int = 40) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", help="only run this schema directory")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--text-threshold", type=float, default=1.0,
                    help="similarity at which a text field counts as correct (1.0 = exact)")
    ap.add_argument("--max-dim", type=int, help="override FIELDWORK_MAX_IMAGE_DIM for this run")
    ap.add_argument("--model", help="override FIELDWORK_LLM_MODEL")
    ap.add_argument("--base-url", help="override FIELDWORK_LLM_BASE_URL")
    ap.add_argument("--baseline", help="a previous eval/runs/<ts> directory to diff against")
    ap.add_argument("--tag", default="", help="free-text label stored with the run")
    ap.add_argument("--check", action="store_true", help="validate ground truth and exit")
    args = ap.parse_args()

    if args.model:
        settings.llm_model = args.model
    if args.base_url:
        settings.llm_base_url = args.base_url

    if not DATASET.exists():
        console.print(f"[red]no dataset at {DATASET}[/] - see eval/dataset/README.md")
        return 1

    cases = discover(args.schema, args.limit)
    if not cases:
        console.print("[red]no cases found[/] - see eval/dataset/README.md")
        return 1

    if args.check:
        return check(cases)

    console.print(
        f"[bold]{len(cases)}[/] cases · model [cyan]{settings.llm_model}[/] "
        f"· {settings.llm_base_url} · max_dim {args.max_dim or settings.max_image_dim}\n"
    )

    scores, raws = [], []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("extracting", total=len(cases))
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(
                    run_one, cid, schema, img, exp, args.text_threshold, args.max_dim
                )
                for cid, schema, img, exp in cases
            ]
            for future in as_completed(futures):
                score, result = future.result()
                scores.append(score)
                raws.append({"case": score.case, **result.to_dict()})
                progress.advance(task)

    scores.sort(key=lambda s: s.case)
    report = Report(scores)

    baseline = None
    if args.baseline:
        bpath = Path(args.baseline)
        bfile = bpath / "summary.json" if bpath.is_dir() else bpath
        if bfile.exists():
            baseline = json.loads(bfile.read_text())["summary"]
        else:
            console.print(f"[yellow]baseline {bfile} not found, ignoring[/]")

    console.print()
    render(report, baseline)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    out = RUNS / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "tag": args.tag,
                "model": settings.llm_model,
                "base_url": settings.llm_base_url,
                "max_dim": args.max_dim or settings.max_image_dim,
                "text_threshold": args.text_threshold,
                "summary": report.summary(),
                "by_field": report.by_field(),
            },
            indent=2,
        )
    )
    (out / "results.json").write_text(
        json.dumps(
            [
                {
                    "case": s.case,
                    "schema": s.schema,
                    "ok": s.ok,
                    "error": s.error,
                    "exact": s.exact,
                    "latency_s": s.latency_s,
                    "comparisons": [vars(c) for c in s.comparisons],
                }
                for s in scores
            ],
            indent=2,
            ensure_ascii=False,
        )
    )
    (out / "predictions.json").write_text(json.dumps(raws, indent=2, ensure_ascii=False))
    console.print(f"\n[dim]run saved to eval/runs/{stamp}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
