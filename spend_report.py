"""CLI: spend report (HARNESS_CHECKLIST box 7).

Reads spend_trace.jsonl and prints a summary you can paste into the team
chat or run anytime. No dependencies beyond the stdlib.

    python spend_report.py            # summary + last 5 days + top threads
    python spend_report.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

from kaya_kopi.spend import SpendTracker, BUDGET_USD


def _load(path: str) -> list[dict]:
    rows: list[dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def summarize(path: str) -> dict:
    rows = _load(path)
    total = sum(r.get("estimated_cost_usd", 0.0) for r in rows)
    by_day = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    by_thread = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    by_model = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    for r in rows:
        c = r.get("estimated_cost_usd", 0.0)
        by_day[_day(r.get("ts", 0))]["calls"] += 1
        by_day[_day(r.get("ts", 0))]["cost"] += c
        th = r.get("thread_id") or "(none)"
        by_thread[th]["calls"] += 1
        by_thread[th]["cost"] += c
        m = r.get("model", "?")
        by_model[m]["calls"] += 1
        by_model[m]["cost"] += c
    return {
        "calls": len(rows),
        "cost_usd": round(total, 6),
        "budget_usd": BUDGET_USD,
        "remaining_usd": round(max(0.0, BUDGET_USD - total), 6),
        "pct_used": round(100.0 * total / BUDGET_USD, 2) if BUDGET_USD else 0.0,
        "input_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("completion_tokens", 0) for r in rows),
        "by_day": {k: {"calls": v["calls"], "cost_usd": round(v["cost"], 6)}
                   for k, v in sorted(by_day.items())},
        "by_thread": {k: {"calls": v["calls"], "cost_usd": round(v["cost"], 6)}
                      for k, v in sorted(by_thread.items(), key=lambda x: -x[1]["cost"])},
        "by_model": {k: {"calls": v["calls"], "cost_usd": round(v["cost"], 6)}
                     for k, v in by_model.items()},
    }


def print_report(path: str) -> None:
    s = summarize(path)
    print(f"Kaya Kopi spend report")
    print(f"  total:        ${s['cost_usd']:.4f}  ({s['pct_used']:.1f}% of ${s['budget_usd']:.0f} budget)")
    print(f"  remaining:    ${s['remaining_usd']:.4f}")
    print(f"  calls:        {s['calls']}")
    print(f"  tokens:       {s['input_tokens']} in / {s['output_tokens']} out")
    print(f"\nBy day:")
    for d, v in s["by_day"].items():
        print(f"  {d}  ${v['cost_usd']:.4f}  ({v['calls']} calls)")
    print(f"\nTop threads by spend:")
    for th, v in list(s["by_thread"].items())[:8]:
        print(f"  {th}  ${v['cost_usd']:.4f}  ({v['calls']} calls)")
    print(f"\nBy model:")
    for m, v in s["by_model"].items():
        print(f"  {m}  ${v['cost_usd']:.4f}  ({v['calls']} calls)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", default=os.environ.get("KAYA_SPEND_TRACE", "spend_trace.jsonl"),
                    help="path to the spend_trace.jsonl file")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()
    if args.json:
        print(json.dumps(summarize(args.trace), indent=2, ensure_ascii=False))
    else:
        print_report(args.trace)


if __name__ == "__main__":
    main()
