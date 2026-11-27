import base64
from typing import Optional
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from kaya_kopi.config import AppConfig
from kaya_kopi.security.openai_proxy import (
    model_temperature_kwargs,
    openai_client_options,
    reasoning_effort_kwargs,
)
from kaya_kopi.security.budget_guard import BudgetGuard

TOLERANCE = 0.02


class LineItemOCR(BaseModel):
    description: str = Field(description="Exact item description printed on invoice")
    quantity: float = Field(default=1.0, description="Exact printed quantity")
    unit_price: float = Field(description="Exact printed unit price")
    line_total: float = Field(description="Exact printed line amount")


class InvoiceOCRResult(BaseModel):
    vendor_name: str = Field(description="Supplier or merchant name")
    vendor_address: Optional[str] = Field(default=None, description="Supplier street address")
    vendor_gst_reg_no: Optional[str] = Field(default=None, description="GST Reg No / UEN")
    bill_to: Optional[str] = Field(default=None, description="Bill To entity and address")
    invoice_number: str = Field(description="Invoice number e.g. INV-012508-182")
    invoice_date: Optional[str] = Field(default=None, description="Invoice date in YYYY-MM-DD")
    payment_terms: Optional[str] = Field(default=None, description="Payment terms if printed")
    currency: str = Field(default="SGD", description="3-letter currency code")
    is_gst_inclusive: bool = Field(
        default=False,
        description="True if line prices already include GST, False if GST is added on top",
    )
    gst_rate_percent: float = Field(default=9.0, description="GST percentage printed on invoice")
    line_items: list[LineItemOCR] = Field(description="All printed line items")
    subtotal: float = Field(description="Exact printed subtotal before GST")
    gst_amount: float = Field(default=0.0, description="Exact printed GST amount")
    grand_total: float = Field(description="Exact printed final total payable")
    raw_markdown_text: str = Field(description="Full faithful Markdown OCR transcription")


def extract_invoice_with_openai(
    file_bytes: bytes,
    mime_type: str = "image/jpeg",
    ocr_model: str = "gpt-6-luna",
    api_key: str | None = None,
    budget_guard: BudgetGuard | None = None,
    branch_code: str | None = "HQ",
) -> InvoiceOCRResult:
    """Checks budget guardrail, runs OpenAI Vision OCR, and logs token usage."""
    if budget_guard:
        budget_guard.check_budget_or_raise()

    model_used = ocr_model or "gpt-6-luna"
    b64_img = base64.b64encode(file_bytes).decode("utf-8")

    cfg = AppConfig(api_key=api_key or "")
    llm = ChatOpenAI(
        model=model_used,
        **model_temperature_kwargs(model_used, 0.0),
        **reasoning_effort_kwargs(model_used),
        **openai_client_options(cfg, "ocr", api_key=api_key),
    ).with_structured_output(InvoiceOCRResult, include_raw=True)

    messages = [
        SystemMessage(
            content=(
                "You are an accounting OCR parser. Extract exact printed numbers for every "
                "line item, subtotal, GST, and grand total. Convert invoice_date to YYYY-MM-DD "
                "if printed as DD/MM/YYYY. NEVER auto-correct math errors—always report the "
                "exact numbers printed on the invoice."
            )
        ),
        HumanMessage(
            content=[
                {"type": "text", "text": "Extract all structured fields and Markdown OCR text."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
            ]
        ),
    ]
    response = llm.invoke(messages)
    result = response["parsed"]

    if budget_guard:
        raw_message = response["raw"]
        budget_guard.log_response(
            thread_id=f"ocr_vision:{branch_code}" if branch_code else "ocr_vision",
            prompt="Extract all structured fields and Markdown OCR text.",
            response=raw_message,
            requested_model=model_used,
        )

    if response["parsing_error"]:
        raise response["parsing_error"]
    if not isinstance(result, InvoiceOCRResult):
        raise ValueError("OpenAI did not return a parsed invoice.")
    return result


def validate_invoice_calculations(invoice: InvoiceOCRResult) -> dict:
    """Preserves original OCR-read values alongside calculated values and variances."""
    issues: list[str] = []
    line_checks: list[dict] = []
    calc_lines_sum = 0.0

    for idx, item in enumerate(invoice.line_items, start=1):
        calc_line = round(item.quantity * item.unit_price, 2)
        diff = round(item.line_total - calc_line, 2)
        is_ok = abs(diff) <= TOLERANCE
        calc_lines_sum += calc_line

        line_checks.append(
            {
                "line_no": idx,
                "description": item.description,
                "ocr_qty": item.quantity,
                "ocr_unit_price": item.unit_price,
                "ocr_line_total": item.line_total,
                "calc_line_total": calc_line,
                "line_diff": diff,
                "is_valid": is_ok,
            }
        )
        if not is_ok:
            issues.append(
                f"Line {idx} ('{item.description}'): OCR={item.line_total:.2f} vs "
                f"Calc={calc_line:.2f} ({item.quantity} x {item.unit_price:.2f}, diff={diff:+.2f})"
            )

    calc_lines_sum = round(calc_lines_sum, 2)
    rate = invoice.gst_rate_percent / 100.0

    if invoice.is_gst_inclusive:
        calc_grand = calc_lines_sum
        calc_gst = round(calc_grand * (rate / (1 + rate)), 2) if rate > 0 else 0.0
        calc_sub = round(calc_grand - calc_gst, 2)
    else:
        calc_sub = calc_lines_sum
        calc_gst = round(calc_sub * rate, 2)
        calc_grand = round(calc_sub + calc_gst, 2)

    sub_diff = round(invoice.subtotal - calc_sub, 2)
    gst_diff = round(invoice.gst_amount - calc_gst, 2)
    grand_diff = round(invoice.grand_total - calc_grand, 2)

    if abs(sub_diff) > TOLERANCE:
        issues.append(f"Subtotal: OCR={invoice.subtotal:.2f} vs Calc={calc_sub:.2f} ({sub_diff:+.2f})")
    if abs(gst_diff) > TOLERANCE:
        issues.append(f"GST: OCR={invoice.gst_amount:.2f} vs Calc={calc_gst:.2f} ({gst_diff:+.2f})")
    if abs(grand_diff) > TOLERANCE:
        issues.append(
            f"Grand Total: OCR={invoice.grand_total:.2f} vs Calc={calc_grand:.2f} ({grand_diff:+.2f})"
        )

    return {
        "all_valid": len(issues) == 0,
        "issues": issues,
        "line_checks": line_checks,
        "ocr_subtotal": invoice.subtotal,
        "calc_subtotal": calc_sub,
        "subtotal_diff": sub_diff,
        "ocr_gst": invoice.gst_amount,
        "calc_gst": calc_gst,
        "gst_diff": gst_diff,
        "ocr_grand_total": invoice.grand_total,
        "calc_grand_total": calc_grand,
        "grand_total_diff": grand_diff,
    }