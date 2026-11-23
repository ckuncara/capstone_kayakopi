"""Curated, local SQLite answers for invoice questions."""

import re
from kaya_kopi.invoice_analytics import sqlite_analytics_answer
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


def sqlite_invoice_answer(question: str, branch_code: str, repo: SQLiteOpsRepository) -> str | None:
    """Return a local answer for supported questions, or None for semantic search."""
    invoice_numbers = set(re.findall(r"\bINV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b", question, re.I))
    if len(invoice_numbers) == 1:
        invoice_number = invoice_numbers.pop()
        invoice = repo.find_invoice(invoice_number, branch_code)
        if invoice is None:
            return f"Invoice {invoice_number} was not found in SQLite for the selected branch."
        lines = [
            f"**Invoice {invoice['invoice_number']}** — {invoice['vendor_name']}; "
            f"date: {invoice['invoice_date']}; branch: {invoice['branch_code']}; "
            f"audit: {invoice['audit_status']}.",
            "",
            "Line items (OCR):",
        ]
        for item in invoice["items"]:
            lines.append(
                f"- Line {item['line_no']}: {item['description']}; "
                f"quantity {item['ocr_quantity']}; unit price {item['ocr_unit_price']} "
                f"{invoice['currency']}; line total {item['ocr_line_total']}; "
                f"line validation: {'PASS' if item['is_valid'] else 'FAIL'}."
            )
        if not invoice["items"]:
            lines.append("- No line items are stored.")
        lines.append(
            f"\nOCR subtotal: {invoice['ocr_subtotal']}; GST: {invoice['ocr_gst_amount']}; "
            f"total: {invoice['ocr_grand_total']} {invoice['currency']}."
        )
        if invoice["audit_status"] != "PASS":
            lines.append(f"Audit issues: {invoice['audit_issues']}")
        return "\n".join(lines)
    if len(invoice_numbers) > 1:
        return "Please ask about one invoice at a time for SQLite lookup."
    return sqlite_analytics_answer(question, branch_code, repo)
