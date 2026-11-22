from pathlib import Path
from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (
    InvoiceOCRResult,
    LineItemOCR,
    validate_invoice_calculations,
)
from kaya_kopi.ingestion.parsers.vector_chunking import build_chunks
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa
import pytest


def _sample_invoice(gst_amount: float = 85.62, grand_total: float = 1036.95) -> InvoiceOCRResult:
    return InvoiceOCRResult(
        vendor_name="HENG HUAT COFFEE ROASTERS",
        vendor_address="Blk 776 Bedok Ring Road #14-822 Singapore 453144",
        vendor_gst_reg_no="M7-7946412-1",
        bill_to="KAYA KOPI HOLDINGS PTE LTD",
        invoice_number="INV-012508-182",
        invoice_date="2025-08-20",
        payment_terms="30 days",
        currency="SGD",
        is_gst_inclusive=False,
        gst_rate_percent=9.0,
        line_items=[
            LineItemOCR(description="Tea dust 5kg", quantity=8.0, unit_price=48.39, line_total=387.12),
            LineItemOCR(description="Kopi powder 5kg", quantity=9.0, unit_price=62.69, line_total=564.21),
        ],
        subtotal=951.33,
        gst_amount=gst_amount,
        grand_total=grand_total,
        raw_markdown_text="# TAX INVOICE\nINV-012508-182",
    )


def test_validate_invoice_math_pass():
    inv = _sample_invoice()
    audit = validate_invoice_calculations(inv)
    assert audit["all_valid"] is True
    assert audit["issues"] == []
    assert audit["ocr_subtotal"] == 951.33
    assert audit["calc_subtotal"] == 951.33
    assert audit["ocr_gst"] == 85.62
    assert audit["calc_gst"] == 85.62
    assert audit["ocr_grand_total"] == 1036.95
    assert audit["calc_grand_total"] == 1036.95


def test_validate_invoice_math_detects_gst_mismatch():
    inv = _sample_invoice(gst_amount=80.00, grand_total=1031.33)
    audit = validate_invoice_calculations(inv)
    assert audit["all_valid"] is False
    assert audit["ocr_gst"] == 80.00
    assert audit["calc_gst"] == 85.62
    assert audit["gst_diff"] == -5.62
    assert any("GST" in issue for issue in audit["issues"])


def test_pdpa_scrubber():
    raw = "Contact Tan S1234567A at +65 9123 4567 or tan.ops@kayakopi.sg"
    scrubbed = scrub_pdpa(raw)
    assert "S1234567A" not in scrubbed
    assert "9123 4567" not in scrubbed
    assert "tan.ops@kayakopi.sg" not in scrubbed
    assert "[REDACTED_NRIC]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed


def test_budget_circuit_breaker(tmp_path: Path):
    db_path = tmp_path / "budget_test.db"
    guard = BudgetGuard(db_path, monthly_limit_usd=0.01)
    assert guard.check_budget_or_raise() == 0.0
    guard.log_usage("ocr_vision", "gpt-4o-mini", prompt_tokens=100_000, completion_tokens=10_000)
    with pytest.raises(BudgetExceededError):
        guard.check_budget_or_raise()


def test_vector_chunking_strategies():
    single = build_chunks("doc1", text="Hello world", strategy="single")
    assert len(single) == 1

    exact = build_chunks("doc2", text="A" * 500, strategy="exact_count", num_chunks=4, overlap_chars=10)
    assert len(exact) == 4


def test_sqlite_and_csv_sync(tmp_path: Path):
    inv_dir = tmp_path / "supplier_invoices"
    cfg = AppConfig(
        supplier_invoice_dir=inv_dir,
        audited_csv_path=inv_dir / "supplier_invoices_audited.csv",
        sqlite_db_path=tmp_path / "kaya_ops.db",
    )
    repo = SQLiteOpsRepository(cfg)
    inv = _sample_invoice()
    audit = validate_invoice_calculations(inv)

    repo.save_invoice("hash123", "AMK-01", "INV-012508-182.jpg", "data/images/supplier_invoices/INV-012508-182.jpg", inv, audit)
    indiv_path = repo.save_individual_invoice_csv("INV-012508-182.jpg", "data/images/supplier_invoices/INV-012508-182.jpg", "AMK-01", inv, audit)
    repo.sync_master_csv_in_image_folder()

    assert indiv_path.endswith("INV-012508-182.csv")
    assert (inv_dir / "INV-012508-182.csv").exists()
    assert (inv_dir / "supplier_invoices_audited.csv").exists()
    assert repo.hash_exists("hash123") is True
    assert repo.invoice_number_exists("INV-012508-182") is True