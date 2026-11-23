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
    (re.compile(r"[A-Z][0-9]{6,7}[A-Z]"), "[REDACTED-ID]"),
    # emails
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED-EMAIL]"),
    # SG phone: 8 digits, optionally +65
    (re.compile(r"(?<![0-9])(?:65 )?6?[0-9]{4} ?[0-9]{4}(?![0-9])"), "[REDACTED-PHONE]"),
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


# ---------------------------------------------------------------------------
# CLIENT CONFIRMED GUARDRAIL POLICY
# Serene Lim, Finance & Admin Manager, Kaya Kopi Holdings.
# Reply on the Q14-Q23 chain, received 23 Nov 2026.
# Policy lives in code, not in the prompt (HARNESS_CHECKLIST box 3).
# These constants are the single source of truth; the system prompt and
# tools must not contradict them.
# ---------------------------------------------------------------------------

# Q1 - who may use the copilot
AUTHORIZED_ROLES = [
    "outlet_manager",
    "ops_team",
    "client_finance_admin",   # "me and the boss"
    "boss",
]
UNAUTHORIZED_ROLES = ["counter_staff"]

# Q2 - named authors on work messages are fine; never show phone numbers
ALLOW_NAMED_AUTHORS = True
NEVER_SHOW_PERSONAL_PHONE = True

# Q3 - show the relevant part of a message, not the whole chat
MAX_MESSAGE_EXCERPT = "relevant_part_only"

# Q4 - never display, at the individual level
NEVER_DISPLAY_INDIVIDUAL = {
    "phone_number",
    "personal_email",
    "home_address",
    "nric_number",
    "one_persons_pay_rate",
}
DISPLAY_ALLOWED = {
    "author_name",                # Q2: names are fine on work messages
    "pay_totals_by_outlet",       # Q4: pay totals by outlet are fine
    "supplier_name",              # Q5: supplier business info permitted
    "supplier_business_address",  # Q5
    "supplier_gst_registration",  # Q5: printed on invoices anyway
}

# Q6 - one access level for everyone; rule Q4 covers pay
ACCESS_MODEL = "single_level"

# Q7 - refuse bulk exports of messages, documents, personal info
ALLOW_BULK_EXPORT = False

# Q8 - operational traces
TRACE_RETENTION_MONTHS = 6
TRACE_REDACTION = "same_as_q4"

# Q9 - always-refuse categories (beyond personal-data requests)
ALWAYS_REFUSE_CATEGORIES = [
    "one_persons_pay",
    "one_persons_performance",
    "one_persons_discipline",
    "legal_advice",
    "tax_advice",
]
ESCALATE_TO = "client_finance_admin"
PRICING_BEHAVIOR = "show_numbers_decision_to_boss"

# Q10 - read-only, confirmed
READ_ONLY = True
FORBIDDEN_ACTIONS = ["modify_records", "place_orders", "send_messages"]


def is_authorized_role(role: str) -> bool:
    """Q1: True if the role may use the copilot."""
    return role in AUTHORIZED_ROLES and role not in UNAUTHORIZED_ROLES


def may_display(field: str) -> bool:
    """Q4/Q5: True if a data field may be shown in an answer."""
    if field in NEVER_DISPLAY_INDIVIDUAL:
        return False
    if field in DISPLAY_ALLOWED:
        return True
    return False


def is_bulk_export(request: str) -> bool:
    """Q7: heuristic flag for a bulk-export request (to be refused)."""
    low = (request or "").lower()
    markers = ["export all", "download all", "give me all", "full list of every",
               "every message", "all messages", "bulk", "entire export",
               "every invoice", "all invoices", "full sales", "entire sales",
               "all records", "every record", "dump the full", "complete database"]
    return any(m in low for m in markers)


def must_refuse(category: str) -> bool:
    """Q9: True if the question category must always be refused."""
    return category in ALWAYS_REFUSE_CATEGORIES
