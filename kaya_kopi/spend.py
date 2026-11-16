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

    def __init__(self, path: str = "spend_trace.jsonl"):
        self.path = path

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

    def totals(self, thread_id: str | None = None) -> dict:
        # FIX 2: optional thread_id filter so the UI can answer 'how much did
        # THIS conversation cost?' on demand (smell #4).
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
                if thread_id is not None and r.get("thread_id", "") != thread_id:
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
