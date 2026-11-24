import os
from pathlib import Path
import subprocess
import sys

import pytest

from kaya_kopi.config import AppConfig
from kaya_kopi.invoice_chat import sqlite_invoice_answer
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


@pytest.fixture
def repo(tmp_path):
    cfg = AppConfig(
        sqlite_db_path=tmp_path / "invoices.db",
        supplier_invoice_dir=tmp_path / "images",
    )
    repository = SQLiteOpsRepository(cfg)
    with repository._connect() as conn:
        for invoice, vendor, day, branch, status, description, price in [
            ("INV-1-001", "Supplier A", "2025-01-01", "AMK-01", "PASS", "Tea 1kg", 10),
            ("INV-1-002", "Supplier A", "2025-12-01", "AMK-01", "PASS", "Tea 1kg", 15),
            ("INV-1-003", "Supplier B", "2025-01-02", "AMK-03", "PASS", "Coffee 1kg", 10),
            ("INV-1-004", "Supplier B", "2025-12-02", "AMK-03", "PASS", "Coffee 1kg", 20),
            ("INV-1-005", "Supplier A", "2025-12-31", "AMK-01", "FAIL (1 issues)", "Tea 1kg", 99),
            ("INV-1-006", "Supplier A", "2024-12-31", "AMK-01", "PASS", "Tea 1kg", 1),
        ]:
            conn.execute(
                """
                INSERT INTO supplier_invoices
                    (file_hash, invoice_number, branch_code, filename, image_path, vendor_name,
                     invoice_date, currency, is_gst_inclusive, audit_status, ocr_grand_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'SGD', 0, ?, ?)
                """,
                (invoice, invoice, branch, invoice, invoice, vendor, day, status, price),
            )
            conn.execute(
                """
                INSERT INTO supplier_invoice_items
                    (file_hash, line_no, description, ocr_quantity, ocr_unit_price,
                     ocr_line_total, is_valid)
                VALUES (?, 1, ?, 1, ?, ?, 1)
                """,
                (invoice, description, price, price),
            )
    return repository


def test_invoice_lookup_is_branch_scoped(repo):
    answer = sqlite_invoice_answer("What is in INV-1-002?", "AMK-01", repo)
    assert "Tea 1kg" in answer
    assert "Supplier A" in answer
    assert "not found" in sqlite_invoice_answer("What is in INV-1-002?", "AMK-03", repo)
    assert "Tea 1kg" in sqlite_invoice_answer("What is in inv-1-002?", "HQ", repo)


def test_price_ranking_uses_full_year_and_flags_failed_audits(repo):
    question = "Which supplier raised prices the most in 2025?"
    hq_answer = sqlite_invoice_answer(question, "HQ", repo)
    assert "Supplier A" in hq_answer
    assert "890.00%" in hq_answer
    assert "INV-1-001" in hq_answer and "INV-1-005" in hq_answer
    assert "FAIL (1 issues)" in hq_answer
    branch_answer = sqlite_invoice_answer(question, "AMK-01", repo)
    assert "Supplier A" in branch_answer
    assert "890.00%" in branch_answer
    assert "INV-1-001" in branch_answer and "INV-1-005" in branch_answer


def test_price_analysis_requests_year_and_handles_no_comparisons(repo):
    assert "specify one calendar year" in sqlite_invoice_answer(
        "Which supplier raised prices the most?", "HQ", repo
    )
    assert "No comparable price increase" in sqlite_invoice_answer(
        "Which supplier raised prices the most in 2024?", "HQ", repo
    )
    assert sqlite_invoice_answer("Explain GST policy", "HQ", repo) is None


def test_ambiguous_prices_on_first_or_last_day_are_excluded(repo):
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO supplier_invoice_items
                (file_hash, line_no, description, ocr_quantity, ocr_unit_price,
                 ocr_line_total, is_valid)
            VALUES ('INV-1-004', 2, 'Coffee 1kg', 1, 21, 21, 1)
            """
        )
    answer = sqlite_invoice_answer("Which supplier raised prices most in 2025?", "HQ", repo)
    assert "Supplier A" in answer
    assert "890.00%" in answer
    assert "1 ambiguous price group(s)" in answer


def test_spending_monthly_supplier_and_audit_queries(repo):
    spend = sqlite_invoice_answer("How much did we spend by supplier in 2025?", "HQ", repo)
    assert "Supplier A: SGD 124.00" in spend
    assert "Supplier B: SGD 30.00" in spend
    assert "Total SGD: 154.00" in spend
    assert "1 failed/unreviewed" in spend

    month = sqlite_invoice_answer("Show monthly spending for Supplier A in 2025", "HQ", repo)
    assert "2025-01: SGD 10.00" in month
    assert "2025-12: SGD 114.00" in month
    assert "Supplier B" not in month
    assert "INV-1-005" in month

    assert "Failed or unreviewed invoices:** 1 of 5" in sqlite_invoice_answer(
        "How many invoices failed audit in 2025?", "HQ", repo
    )
    listing = sqlite_invoice_answer("List invoices from Supplier A in 2025-12", "HQ", repo)
    assert "INV-1-002" in listing and "INV-1-005" in listing
    assert "INV-1-001" not in listing
    assert "FAIL (1 issues)" in listing


def test_price_trends_product_filter_and_explicit_limits(repo):
    answer = sqlite_invoice_answer("How did the price of Tea 1kg change in 2025?", "HQ", repo)
    assert "890.00%" in answer
    assert "Coffee 1kg" not in answer
    assert "FAIL (1 issues)" in answer
    assert "not recognised" in sqlite_invoice_answer(
        "Show invoices from supplier Missing in 2025", "HQ", repo
    )
    assert "specify" in sqlite_invoice_answer("Show invoices between 2024 and 2025", "HQ", repo)
    assert "specify" in sqlite_invoice_answer(
        "Show invoices in 2024 and on 2025-01-01", "HQ", repo
    )
    assert "not averages" in sqlite_invoice_answer(
        "What is the average supplier spend in 2025?", "HQ", repo
    )
    assert "not supported" in sqlite_invoice_answer(
        "What is spending on product tea in 2025?", "HQ", repo
    )
    assert "Select the requested branch" in sqlite_invoice_answer(
        "Show AMK-03 spending in 2025", "HQ", repo
    )


def test_spending_does_not_add_different_currencies_or_replace_missing_with_zero(repo):
    with repo._connect() as conn:
        conn.execute(
            "UPDATE supplier_invoices SET currency='USD', ocr_grand_total=NULL "
            "WHERE invoice_number='INV-1-004'"
        )
    answer = sqlite_invoice_answer("Show spending by supplier in 2025", "HQ", repo)
    assert "Total SGD: 134.00" in answer
    assert "Total USD: unavailable (1 missing amount(s))" in answer


def test_price_decrease_is_not_mistaken_for_increase(repo):
    with repo._connect() as conn:
        conn.execute(
            "UPDATE supplier_invoice_items SET ocr_unit_price=5 WHERE file_hash='INV-1-004'"
        )
    answer = sqlite_invoice_answer("Which supplier's prices fell most in 2025?", "HQ", repo)
    assert "Supplier B" in answer and "-50.00%" in answer
    assert "Supplier A" not in answer


def test_streamlit_chat_uses_sqlite_without_api_key(repo):
    app = Path(__file__).resolve().parents[2] / "src" / "kaya_kopi" / "test_kaya_ocr_chroma.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["OPENAI_API_KEY"] = ""
    env["LOCAL_SQLITE_PATH"] = str(repo.cfg.sqlite_db_path)
    env["SUPPLIER_INVOICE_DIR"] = str(repo.cfg.supplier_invoice_dir)
    code = (
        "import sys; from streamlit.testing.v1 import AppTest; "
        "app = AppTest.from_file(sys.argv[1], default_timeout=60).run(); "
        "app.chat_input[0].set_value('Which supplier raised prices the most in 2025?').run(); "
        "assert not app.exception, str(app.exception); "
        "assert any('Supplier A' in x.value and '890.00%' in x.value for x in app.markdown), "
        "str(app.markdown)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(app)],
        cwd=repo.cfg.sqlite_db_path.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
