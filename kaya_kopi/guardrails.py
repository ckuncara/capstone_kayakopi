"""Guardrails (HARNESS_CHECKLIST box 3: guardrails in code).

Input guard  — blocks prompt-injection patterns + oversized input.
Output guard — strips PII (NRIC / email / phone) and forces an honest
               "I don't know" shape when the model admits it has no data.

These are pure functions so they are unit-testable without an LLM.
"""
from __future__ import annotations

import re

# --- input -----------------------------------------------------------------

BLOCKED_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"you\s+are\s+(now|no\s+longer)",
    r"disregard\s+(your|all)\s+(system|safety|prior)",
    r"reveal\s+(your|the)\s+(system\s+)?prompt",
    r"pretend\s+(to\s+be|you\s+are)",
    r"new\s+instructions?\s*:",
]
_BLOCKED = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]
MAX_INPUT_CHARS = 8_000


class InputBlockedError(ValueError):
    """Raised when user input trips the input guard."""


def check_input(text: str) -> str:
    """Return cleaned text, or raise InputBlockedError."""
    t = (text or "").strip()
    if not t:
        raise InputBlockedError("Empty input.")
    if len(t) > MAX_INPUT_CHARS:
        raise InputBlockedError(f"Input too long ({len(t)} > {MAX_INPUT_CHARS} chars).")
    for rx in _BLOCKED:
        if rx.search(t):
            raise InputBlockedError("Input blocked by guardrail (prompt-injection pattern).")
    return t


# --- output ----------------------------------------------------------------

PII_PATTERNS = [
    # SG NRIC: 1-2 letters + 6-7 digits (incl. -4)
    (re.compile(r"\b[A-Za-z]?\d{6}[A-Za-z]?\d?\b"), "[REDACTED-ID]"),
    # emails
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    # SG phone: 8 digits, optionally +65
    (re.compile(r"\+?6?5[\s-]?\d{4}[\s-]?\d{4}\b"), "[REDACTED-PHONE]"),
]


def sanitize_output(text: str) -> str:
    """Strip PII from model output before it is shown to the user."""
    out = text or ""
    for rx, repl in PII_PATTERNS:
        out = rx.sub(repl, out)
    return out


HONEST_UNKNOWN_MARKERS = [
    "not available", "no data", "cannot determine", "insufficient data",
    "don't have", "don\u2019t have", "not in", "unable to answer", "no information",
    # widened so honest refusals in any common phrasing are recognized
    "cannot answer", "can\u2019t answer", "could not answer", "could\u2019t answer",
    "can't find", "could not find", "not sure", "no matching", "no record",
    "not in the data", "no matching record", "not present", "not found",
    "won\u2019t guess", "will not guess", "won't guess", "beyond my knowledge",
    "outside my scope", "not something i can", "not provided",
]


def is_honest_unknown(text: str) -> bool:
    """True if the answer already concedes it cannot be answered from data."""
    low = (text or "").lower()
    return any(m in low for m in HONEST_UNKNOWN_MARKERS)
