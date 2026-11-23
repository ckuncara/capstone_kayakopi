"""Spend + trace logger (HARNESS_CHECKLIST box 7: observability / spend).

Wraps an OpenAI (or OpenAI-compatible) client and records every call:
model, token usage, estimated cost, duration — appended to a JSONL file.
The JSONL is the "trace"; the totals feed the spend tracker in the UI.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict

# USD per 1K tokens (update when the real key/model is known — 16 Nov briefing)
DEFAULT_PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

# Course hard budget (US$50 cap on the team API key).
# Override with KAYA_BUDGET_USD if the cap changes.
BUDGET_USD = float(os.environ.get("KAYA_BUDGET_USD", "50"))

# Calls with less headroom than this are refused, so a single call can
# never silently push the total past the cap.
MIN_CALL_HEADROOM_USD = 0.10


class BudgetExhaustedError(RuntimeError):
    """Raised BEFORE an LLM call when the US$ cap is reached.

    Surfaced to the user as a plain, honest message — not a crash.
    """


@dataclass
class TraceEntry:
    ts: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    duration_s: float
    thread_id: str = ""


class SpendTracker:
    """Append-only JSONL trace + running totals."""

    def __init__(self, path: str = "spend_trace.jsonl",
                 budget_usd: float | None = None):
        self.path = path
        self.budget_usd = budget_usd if budget_usd is not None else BUDGET_USD

    def estimate(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = DEFAULT_PRICING.get(model, DEFAULT_PRICING["gpt-4o-mini"])
        return (prompt_tokens * p["input"] + completion_tokens * p["output"]) / 1000.0

    def record(self, model: str, prompt_tokens: int, completion_tokens: int,
               duration_s: float, thread_id: str = "") -> TraceEntry:
        e = TraceEntry(
            ts=time.time(), model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            estimated_cost_usd=self.estimate(model, prompt_tokens, completion_tokens),
            duration_s=duration_s, thread_id=thread_id,
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(e)) + "\n")
        return e

    def totals(self) -> dict:
        total_calls = 0
        total_cost = 0.0
        total_in = 0
        total_out = 0
        if not os.path.exists(self.path):
            return {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_calls += 1
                total_cost += r.get("estimated_cost_usd", 0.0)
                total_in += r.get("prompt_tokens", 0)
                total_out += r.get("completion_tokens", 0)
        return {
            "calls": total_calls,
            "cost_usd": round(total_cost, 6),
            "input_tokens": total_in,
            "output_tokens": total_out,
        }

    # ---- budget layer (US$ cap) --------------------------------------------

    def remaining(self) -> float:
        """US$ left on the cap (never negative)."""
        return max(0.0, self.budget_usd - self.totals()["cost_usd"])

    def would_exceed(self, est_cost: float) -> bool:
        """Would a call of est_cost push us past the cap?"""
        return self.totals()["cost_usd"] + est_cost > self.budget_usd

    def check_budget(self) -> float:
        """Call BEFORE every LLM call.

        Raises BudgetExhaustedError when the cap is reached or the remaining
        headroom is below MIN_CALL_HEADROOM_USD (so a single call can't
        silently push the total over the cap). Returns the US$ remaining.
        """
        rem = self.remaining()
        if rem < MIN_CALL_HEADROOM_USD:
            used = self.totals()["cost_usd"]
            raise BudgetExhaustedError(
                f"API budget exhausted — ${used:.2f} of the ${self.budget_usd:.2f} "
                f"cap is used. Please ask your team before running more questions."
            )
        return rem

    def status(self) -> dict:
        """Sidebar alert level: ok <50% <= warn <80% <= high <95% <= crit."""
        used = self.totals()["cost_usd"]
        pct = (100.0 * used / self.budget_usd) if self.budget_usd else 0.0
        if pct >= 95.0:
            level = "crit"
        elif pct >= 80.0:
            level = "high"
        elif pct >= 50.0:
            level = "warn"
        else:
            level = "ok"
        return {"used_usd": used, "budget_usd": self.budget_usd,
                "pct": pct, "level": level, "remaining_usd": self.remaining()}
