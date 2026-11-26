"""Deterministic arithmetic audit for OCR-extracted supplier invoices.

The OCR model is treated as untrusted evidence. This module never corrects the
printed values; it only compares them with values derived from other printed
fields so discrepancies can be stored and surfaced to the analyst.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _compare(printed: Decimal | None, calculated: Decimal | None) -> bool | None:
    if printed is None or calculated is None:
        return None
    return abs(printed - calculated) <= TOLERANCE


def _money_text(value: Decimal | None) -> str:
    return "not printed" if value is None else f"{value:.2f}"


def db_flag(value: bool | None) -> int | None:
    """Convert a three-state check result to a MySQL TINYINT(1) value."""
    return None if value is None else int(value)


def audit_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
    """Return line checks and invoice-level arithmetic checks.

    ``invoice`` is the OCR result converted to a plain dict. Missing values stay
    missing; they are never coerced to zero. The returned values are ready for
    both MySQL DECIMAL columns and Streamlit display.
    """
    raw_lines = invoice.get("line_items") or []
    line_checks: list[dict[str, Any]] = []
    issues: list[str] = []
    notes: list[str] = []

    if not isinstance(raw_lines, list) or not raw_lines:
        notes.append("No line items were extracted; item-level checks are unavailable.")

    for index, raw in enumerate(raw_lines if isinstance(raw_lines, list) else [], start=1):
        item = raw if isinstance(raw, dict) else {}
        description = str(item.get("description") or "").strip()[:500] or f"Line {index}"
        quantity = _money(item.get("quantity"))
        unit_price = _money(item.get("unit_price"))
        printed = _money(
            item.get("amount") if item.get("amount") is not None else item.get("line_total")
        )
        calculated = None
        if quantity is not None and unit_price is not None:
            calculated = (quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        difference = None if printed is None or calculated is None else printed - calculated
        if printed is None or calculated is None:
            status = "UNVERIFIED"
        elif abs(difference) <= TOLERANCE:
            status = "MATCH"
        else:
            status = "MISMATCH"
            issues.append(
                f"Line {index} ('{description}'): printed {_money_text(printed)} vs "
                f"{_money_text(quantity)} x {_money_text(unit_price)} = "
                f"{_money_text(calculated)} (diff {difference:+.2f})."
            )
        line_checks.append({
            "line_no": index,
            "description": description,
            "quantity": None if quantity is None else float(quantity),
            "unit_price": None if unit_price is None else float(unit_price),
            "line_amount": None if printed is None else float(printed),
            "expected_line_amount": None if calculated is None else float(calculated),
            "line_amount_diff": None if difference is None else float(difference),
            "check_status": status,
        })

    unverified_lines = sum(1 for line in line_checks if line["check_status"] == "UNVERIFIED")
    if unverified_lines:
        notes.append(
            f"{unverified_lines} line item(s) lack a printed quantity, unit price or amount; "
            "their arithmetic could not be verified."
        )

    amounts = [line["line_amount"] for line in line_checks if line["line_amount"] is not None]
    line_sum = None
    if amounts and len(amounts) == len(line_checks):
        line_sum = sum((Decimal(str(value)) for value in amounts), Decimal("0")).quantize(CENT)

    printed_subtotal = _money(invoice.get("subtotal"))
    printed_gst = _money(invoice.get("gst_amount"))
    printed_total = _money(invoice.get("total_amount"))
    raw_rate = invoice.get("gst_rate_percent")
    try:
        rate = Decimal(str(raw_rate)) if raw_rate is not None else None
    except (InvalidOperation, TypeError, ValueError):
        rate = None
    if rate is not None and not rate.is_finite():
        rate = None
    inclusive = invoice.get("is_gst_inclusive")
    inclusive = inclusive if isinstance(inclusive, bool) else None

    expected_subtotal: Decimal | None = None
    expected_gst: Decimal | None = None
    expected_total: Decimal | None = None

    if inclusive is True and rate is not None and rate >= 0:
        gross = line_sum if line_sum is not None else printed_total
        if gross is not None:
            expected_total = gross
            expected_gst = (gross * rate / (Decimal("100") + rate)).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
            expected_subtotal = (gross - expected_gst).quantize(CENT)
    else:
        expected_subtotal = line_sum
        if printed_subtotal is not None and printed_gst is not None:
            expected_total = printed_subtotal + printed_gst
        elif line_sum is not None and printed_gst is not None:
            expected_total = line_sum + printed_gst
        if inclusive is False and rate is not None and rate >= 0 and printed_subtotal is not None:
            expected_gst = (printed_subtotal * rate / Decimal("100")).quantize(
                CENT, rounding=ROUND_HALF_UP
            )

    subtotal_match = _compare(printed_subtotal, expected_subtotal)
    gst_match = _compare(printed_gst, expected_gst)
    total_match = _compare(printed_total, expected_total)

    if subtotal_match is False:
        issues.append(
            f"Subtotal: printed {_money_text(printed_subtotal)} vs "
            f"sum of line amounts {_money_text(expected_subtotal)} "
            f"(diff {printed_subtotal - expected_subtotal:+.2f})."
        )
    elif subtotal_match is None:
        notes.append("Subtotal could not be checked against line amounts.")

    if gst_match is False:
        basis = f"{rate}% of subtotal" if rate is not None and inclusive is False else "GST-inclusive gross total"
        issues.append(
            f"GST: printed {_money_text(printed_gst)} vs {basis} "
            f"{_money_text(expected_gst)} (diff {printed_gst - expected_gst:+.2f})."
        )
    elif gst_match is None:
        notes.append(
            "GST was not independently checked against a printed rate; "
            "only total versus subtotal plus GST was compared."
        )

    if total_match is False:
        issues.append(
            f"Total: printed {_money_text(printed_total)} vs "
            f"subtotal + GST {_money_text(expected_total)} "
            f"(diff {printed_total - expected_total:+.2f})."
        )
    elif total_match is None:
        notes.append("Grand total could not be checked against subtotal plus GST.")

    if issues:
        status = "REVIEW"
    elif any(value is None for value in (subtotal_match, gst_match, total_match)) or unverified_lines:
        status = "UNVERIFIED"
    else:
        status = "PASS"

    def as_float(value: Decimal | None) -> float | None:
        return None if value is None else float(value)

    return {
        "status": status,
        "issues": issues,
        "notes": notes,
        "notes_text": "; ".join([*issues, *notes]),
        "line_checks": line_checks,
        "subtotal_match": subtotal_match,
        "gst_match": gst_match,
        "total_match": total_match,
        "expected_subtotal": as_float(expected_subtotal),
        "expected_gst": as_float(expected_gst),
        "expected_total": as_float(expected_total),
        "subtotal_diff": None if subtotal_match is None else as_float(printed_subtotal - expected_subtotal),
        "gst_diff": None if gst_match is None else as_float(printed_gst - expected_gst),
        "total_diff": None if total_match is None else as_float(printed_total - expected_total),
    }
