from pathlib import Path
import sqlite3
from unittest.mock import Mock
from langchain_core.messages import AIMessage
from kaya_kopi.config import AppConfig
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker
from kaya_kopi.ingestion.parsers import invoice_vision_audit
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
    guard.log_usage("ocr_vision", "Invoice OCR", "gpt-4o-mini", 100_000, 10_000)
    usage = guard.usage_tracker.get_latest_usage()
    assert usage["thread_id"] == "ocr_vision"
    assert usage["prompt"] == "Invoice OCR"
    assert usage["total_tokens"] == 110_000
    with pytest.raises(BudgetExceededError):
        guard.check_budget_or_raise()


def test_tracker_and_budget_share_database(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "usage.sqlite3"
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(db_path))
    tracker = LLMUsageTracker()
    guard = BudgetGuard(db_path, monthly_limit_usd=1.0)
    cost = guard.log_usage("embedding:doc", "Embed invoice", "text-embedding-3-small", 1000, 0)
    assert cost == pytest.approx(0.00002)
    assert guard.get_current_month_spend() == pytest.approx(cost)
    assert tracker.get_latest_usage()["thread_id"] == "embedding:doc"


def test_monthly_spend_uses_tracker_and_excludes_other_months(tmp_path: Path):
    guard = BudgetGuard(tmp_path / "usage.sqlite3")
    with sqlite3.connect(guard.sqlite_path) as conn:
        conn.execute("INSERT INTO llm_usage (timestamp, total_cost) VALUES (datetime('now'), 0.2)")
        conn.execute(
            "INSERT INTO llm_usage (timestamp, total_cost) "
            "VALUES (datetime('now', 'start of month', '-1 second'), 5.0)"
        )
        conn.execute("CREATE TABLE api_usage_logs (cost_usd REAL)")
        conn.execute("INSERT INTO api_usage_logs (cost_usd) VALUES (0.3)")
    guard.usage_tracker.get_current_month_spend = Mock(
        wraps=guard.usage_tracker.get_current_month_spend
    )

    assert guard.get_current_month_spend() == pytest.approx(0.2)
    guard.usage_tracker.get_current_month_spend.assert_called_once_with()


def test_tracker_current_month_spend(tmp_path: Path):
    tracker = LLMUsageTracker(db_path=tmp_path / "usage.sqlite3")
    assert tracker.get_current_month_spend() == 0.0
    with sqlite3.connect(tracker.db_path) as conn:
        conn.execute("INSERT INTO llm_usage (timestamp, total_cost) VALUES (datetime('now'), 0.2)")
        conn.execute(
            "INSERT INTO llm_usage (timestamp, total_cost) "
            "VALUES (datetime('now', 'start of month', '-1 second'), 5.0)"
        )
        conn.execute(
            "INSERT INTO llm_usage (timestamp, total_cost) "
            "VALUES (datetime('now', 'start of month', '+1 month'), 6.0)"
        )

    assert tracker.get_current_month_spend() == pytest.approx(0.2)
    assert tracker.get_total_spend() == pytest.approx(11.2)


def test_usage_panel_budget_matches_current_month(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "10")
    tracker = LLMUsageTracker(db_path=tmp_path / "usage.sqlite3")
    with sqlite3.connect(tracker.db_path) as conn:
        conn.execute("INSERT INTO llm_usage (timestamp, total_cost) VALUES (datetime('now'), 2)")
        conn.execute(
            "INSERT INTO llm_usage (timestamp, total_cost) "
            "VALUES (datetime('now', 'start of month', '-1 second'), 5)"
        )
    assert tracker.get_budget_info() == {
        "total_budget": 10.0, "total_spent": 2.0, "budget_left": 8.0, "percentage_used": 20.0,
    }


def test_invoice_ocr_logs_tracker_fields(tmp_path: Path, monkeypatch):
    invoice = _sample_invoice()
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = {
        "raw": AIMessage(content="", usage_metadata={
            "input_tokens": 1750, "output_tokens": 350, "total_tokens": 2100,
        }),
        "parsed": invoice,
        "parsing_error": None,
    }
    monkeypatch.setattr(invoice_vision_audit, "ChatOpenAI", Mock(return_value=model))
    guard = BudgetGuard(tmp_path / "usage.sqlite3")

    invoice_vision_audit.extract_invoice_with_openai(b"image", budget_guard=guard, branch_code="AMK-01")

    usage = guard.usage_tracker.get_latest_usage()
    assert usage["thread_id"] == "ocr_vision:AMK-01"
    assert usage["model_name"] == "gpt-4o-mini"
    assert usage["input_tokens"] == 1750
    assert usage["output_tokens"] == 350
    model.with_structured_output.assert_called_once_with(InvoiceOCRResult, include_raw=True)


def test_invoice_ocr_logs_usage_before_reporting_parse_failure(tmp_path: Path, monkeypatch):
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = {
        "raw": AIMessage(content="", usage_metadata={
            "input_tokens": 1750, "output_tokens": 350, "total_tokens": 2100,
        }),
        "parsed": None,
        "parsing_error": ValueError("Invalid output"),
    }
    monkeypatch.setattr(invoice_vision_audit, "ChatOpenAI", Mock(return_value=model))
    guard = BudgetGuard(tmp_path / "usage.sqlite3")

    with pytest.raises(ValueError, match="Invalid output"):
        invoice_vision_audit.extract_invoice_with_openai(b"image", budget_guard=guard)

    assert guard.usage_tracker.get_latest_usage()["total_tokens"] == 2100


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