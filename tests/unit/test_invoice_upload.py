import io
from pathlib import Path
from unittest.mock import MagicMock, Mock

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from kaya_kopi.config import AppConfig
from kaya_kopi.copilot import chat_backend
from kaya_kopi.copilot import invoice_upload
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (
    InvoiceOCRResult, LineItemOCR, validate_invoice_calculations,
)
from kaya_kopi.repositories.mysql_invoice_sync import InvoiceSyncResult, pending_syncs
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetGuard


@pytest.fixture
def upload_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = AppConfig(
        api_key="test-key",
        sqlite_db_path=tmp_path / "ops.sqlite3",
        supplier_invoice_dir=tmp_path / "invoices",
        audited_csv_path=tmp_path / "invoices" / "master.csv",
    )
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path)
    vectors = Mock()
    vectors.ingest_document.return_value = 1
    invoice = InvoiceOCRResult(
        vendor_name="Supplier",
        invoice_number="INV-TEST",
        invoice_date="2025-01-11",
        line_items=[LineItemOCR(description="Tea", quantity=2, unit_price=5, line_total=10)],
        subtotal=10,
        gst_amount=0.9,
        grand_total=10.9,
        raw_markdown_text="Tea",
    )
    extract = Mock(return_value=invoice)
    monkeypatch.setattr(invoice_upload, "extract_invoice_with_openai", extract)
    sync = Mock(return_value=InvoiceSyncResult(purchase_id=17))
    monkeypatch.setattr(invoice_upload, "sync_invoice", sync)
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(image, format="PNG")
    return cfg, repo, budget, vectors, extract, sync, image.getvalue()


def test_upload_stores_local_invoice_indexes_and_syncs(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    result = invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    assert isinstance(result, invoice_upload.InvoiceUploadResult)
    file_hash = repo.compute_hash(raw)
    assert result.invoice_number == "INV-TEST"
    assert result.sync.purchase_id == 17
    assert repo.hash_exists(file_hash)
    assert repo.get_line_items(file_hash)[0]["description"] == "Tea"
    assert vectors.ingest_document.call_args.kwargs["parent_doc_id"] == file_hash
    assert sync.call_args.args[1] == pending_syncs(repo)[0]["upload_id"]
    assert cfg.audited_csv_path.exists()
    assert (cfg.supplier_invoice_dir / result.filename).exists()
    assert extract.call_args.kwargs["mime_type"] == "image/png"

    skipped = invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    assert isinstance(skipped, invoice_upload.SkippedInvoice)
    assert "image is already stored" in skipped.reason
    assert extract.call_count == 1


def test_force_upload_reprocesses_existing_image(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    upload_id = pending_syncs(repo)[0]["upload_id"]

    result = invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget, force=True)

    assert isinstance(result, invoice_upload.InvoiceUploadResult)
    assert extract.call_count == 2
    assert len(repo.get_all_invoices()) == 1
    assert vectors.ingest_document.call_count == 2
    assert sync.call_args.args[1] == upload_id


def test_force_upload_replaces_duplicate_invoice_number_and_retries_cleanup(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    old_hash = repo.compute_hash(raw)
    upload_id = pending_syncs(repo)[0]["upload_id"]
    extract.return_value.invoice_number = "inv-test"
    different = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(different, format="PNG")
    new_raw = different.getvalue()
    new_hash = repo.compute_hash(new_raw)
    vectors.delete_invoice_document.side_effect = [RuntimeError("Chroma unavailable"), None]

    with pytest.raises(RuntimeError, match="Chroma unavailable"):
        invoice_upload.upload_invoice(new_raw, cfg, repo, vectors, budget, force=True)

    pending = pending_syncs(repo)
    assert len(pending) == 1
    assert pending[0]["upload_id"] == upload_id
    assert pending[0]["file_hash"] == new_hash
    invoice_upload.retry_invoice_upload(repo, vectors, upload_id, new_hash)
    assert len(repo.get_all_invoices()) == 1
    assert repo.get_invoice_by_hash(new_hash)["invoice_number"] == "inv-test"
    assert vectors.delete_invoice_document.call_args_list == [((old_hash,),), ((old_hash,),)]
    assert sync.call_args.args[1] == upload_id


def test_force_upload_rejects_conflicting_supplier(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    original_hash = repo.compute_hash(raw)
    upload_id = pending_syncs(repo)[0]["upload_id"]
    extract.return_value.vendor_name = "Different supplier"
    different = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(different, format="PNG")

    with pytest.raises(ValueError, match="identity changed"):
        invoice_upload.upload_invoice(different.getvalue(), cfg, repo, vectors, budget, force=True)

    assert repo.get_invoice_by_hash(original_hash)["vendor_name"] == "Supplier"
    assert pending_syncs(repo)[0]["upload_id"] == upload_id
    assert sync.call_count == 1
    vectors.delete_invoice_document.assert_not_called()


def test_chroma_failure_stays_pending_and_retry_does_not_repeat_ocr(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    vectors.ingest_document.side_effect = [RuntimeError("Chroma unavailable"), 1]
    with pytest.raises(RuntimeError, match="Chroma unavailable"):
        invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    pending = pending_syncs(repo)
    assert len(pending) == 1
    sync.assert_not_called()

    result = invoice_upload.retry_invoice_upload(
        repo, vectors, pending[0]["upload_id"], pending[0]["file_hash"]
    )
    assert result.purchase_id == 17
    assert extract.call_count == 1
    assert cfg.audited_csv_path.exists()
    sync.assert_called_once()


def test_mysql_failure_remains_pending_for_retry(upload_context):
    cfg, repo, budget, vectors, extract, sync, raw = upload_context
    sync.side_effect = [OSError("MySQL unavailable"), InvoiceSyncResult(None, reason="unmatched")]
    with pytest.raises(OSError, match="MySQL unavailable"):
        invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    pending = pending_syncs(repo)
    assert len(pending) == 1
    result = invoice_upload.retry_invoice_upload(
        repo, vectors, pending[0]["upload_id"], pending[0]["file_hash"]
    )
    assert result.purchase_id is None
    assert vectors.ingest_document.call_count == 2
    assert extract.call_count == 1


def test_duplicate_number_cannot_replace_existing_invoice(upload_context):
    cfg, repo, budget, vectors, extract, _, raw = upload_context
    invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    extract.return_value.invoice_number = "inv-test"
    different = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(different, format="PNG")
    skipped = invoice_upload.upload_invoice(different.getvalue(), cfg, repo, vectors, budget)
    assert isinstance(skipped, invoice_upload.SkippedInvoice)
    assert "inv-test is already stored" in skipped.reason
    assert len(repo.get_all_invoices()) == 1
    assert vectors.ingest_document.call_count == 1


def test_duplicate_is_rejected_inside_sqlite_transaction(upload_context):
    cfg, repo, budget, vectors, extract, _, raw = upload_context
    invoice_upload.upload_invoice(raw, cfg, repo, vectors, budget)
    invoice = extract.return_value.model_copy(update={"invoice_number": "inv-test"})
    with pytest.raises(ValueError, match="already stored"):
        repo.save_invoice(
            "new-hash", None, "new.jpg", "new.jpg", invoice,
            validate_invoice_calculations(invoice), sync_mysql=True, allow_replace=False,
        )
    assert len(repo.get_all_invoices()) == 1
    assert len(pending_syncs(repo)) == 1


def test_existing_image_is_not_overwritten(upload_context):
    cfg, repo, _, _, _, _, raw = upload_context
    file_hash = repo.compute_hash(raw)
    target = cfg.supplier_invoice_dir / f"INV-TEST-{file_hash[:12]}.jpg"
    target.write_bytes(b"existing image")
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        repo.save_image_as_invoice_jpg(
            raw, "INV-TEST", file_hash, overwrite_existing=False, include_hash_suffix=True
        )
    assert target.read_bytes() == b"existing image"


def test_upload_page_navigation_preserves_chat(upload_context, tmp_path: Path, monkeypatch):
    cfg, _, _, _, _, _, _ = upload_context
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(cfg.sqlite_db_path))
    monkeypatch.setattr(invoice_upload, "AppConfig", lambda: cfg)
    monkeypatch.setattr(chat_backend, "retrieve_all_threads", lambda: [])
    app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
    app = AppTest.from_file(str(app_path), default_timeout=30).run()
    assert not app.exception
    app.session_state["usage_tracker"] = LLMUsageTracker(db_path=tmp_path / "old-usage.sqlite3")
    app.run()
    assert app.session_state["usage_tracker"].db_path == cfg.sqlite_db_path

    next(button for button in app.button if button.label == "Upload supplier invoices").click().run()
    assert not app.exception
    assert any(header.value == "Upload supplier invoices" for header in app.header)
    assert app.get("file_uploader")

    next(button for button in app.button if button.label == "← Back to Chat").click().run()
    assert not app.exception
    assert any(button.label == "Upload supplier invoices" for button in app.button)


@pytest.mark.parametrize("force", [False, True])
def test_upload_page_skips_duplicates_and_continues_batch(upload_context, monkeypatch, force):
    cfg, repo, budget, vectors, _, _, raw = upload_context
    st = MagicMock()
    st.session_state = {}
    st.button.side_effect = lambda label, **kwargs: label == "Run OCR and save invoices"
    st.checkbox.return_value = force
    first = Mock(name="first")
    first.name = "existing.png"
    first.getvalue.return_value = raw
    second = Mock(name="second")
    second.name = "new.png"
    second.getvalue.return_value = raw
    st.file_uploader.return_value = [first, second]
    saved = invoice_upload.InvoiceUploadResult(
        "INV-NEW", "new.jpg", {"all_valid": True, "line_checks": []},
        InvoiceSyncResult(purchase_id=17),
    )
    process = Mock(side_effect=[
        invoice_upload.SkippedInvoice("Invoice number INV-TEST is already stored."),
        saved,
    ])
    monkeypatch.setattr(invoice_upload, "st", st)
    monkeypatch.setattr(invoice_upload, "AppConfig", lambda: cfg)
    monkeypatch.setattr(invoice_upload, "SQLiteOpsRepository", lambda _: repo)
    monkeypatch.setattr(invoice_upload, "BudgetGuard", lambda *args: budget)
    monkeypatch.setattr(invoice_upload, "SharedChromaRepository", lambda *args, **kwargs: vectors)
    monkeypatch.setattr(invoice_upload, "pending_syncs", lambda _: [])
    monkeypatch.setattr(invoice_upload, "upload_invoice", process)

    invoice_upload.render_invoice_upload_page()

    st.info.assert_called_once_with("Skipped existing.png: Invoice number INV-TEST is already stored.")
    st.error.assert_not_called()
    st.exception.assert_not_called()
    assert process.call_count == 2
    assert all(call.kwargs["force"] is force for call in process.call_args_list)
    assert any("INV-NEW" in call.args[0] for call in st.success.call_args_list)
