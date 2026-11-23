import json
from pathlib import Path
import sys
from unittest.mock import Mock
from uuid import uuid4

import chromadb
from chromadb.api.types import EmbeddingFunction
import pytest

from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers import invoice_ocr_openai
from kaya_kopi.ingestion.parsers.invoice_vision_audit import InvoiceOCRResult, LineItemOCR
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetGuard


@pytest.fixture
def setup_invoice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    images = tmp_path / "invoices"
    images.mkdir()
    cfg = AppConfig(
        api_key="test-key",
        supplier_invoice_dir=images,
        audited_csv_path=images / "master.csv",
        sqlite_db_path=tmp_path / "ops.sqlite3",
    )
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)
    invoice = InvoiceOCRResult(
        vendor_name="Supplier",
        invoice_number="INV-TEST",
        line_items=[LineItemOCR(description="Tea", quantity=2, unit_price=5, line_total=10)],
        subtotal=10,
        gst_amount=0.9,
        grand_total=10.9,
        raw_markdown_text="Tea",
    )
    extract = Mock(return_value=invoice)
    monkeypatch.setattr(invoice_ocr_openai, "extract_invoice_with_openai", extract)
    vectors = Mock()
    vectors.ingest_document.return_value = 1
    return cfg, repo, budget, vectors, extract


def test_ingestion_stores_invoice_and_sidecar(setup_invoice, tmp_path: Path):
    cfg, repo, budget, vectors, extract = setup_invoice
    source = cfg.supplier_invoice_dir / "INV-TEST.jpg"
    original = b"original-image"
    source.write_bytes(original)
    output = tmp_path / "sidecars"

    assert invoice_ocr_openai.ingest_invoice(
        source, cfg.supplier_invoice_dir, output, cfg, repo, vectors, budget
    )

    file_hash = repo.compute_hash(original)
    assert source.read_bytes() == original
    assert repo.hash_exists(file_hash)
    assert repo.get_line_items(file_hash)[0]["description"] == "Tea"
    vector_call = vectors.ingest_document.call_args.kwargs
    assert vector_call["parent_doc_id"] == file_hash
    assert vector_call["extra_metadata"]["audit_status"] == "PASS"
    payload = json.loads((output / "INV-TEST.jpg.ocr.json").read_text())
    assert payload["invoice"]["invoice_number"] == "INV-TEST"
    assert payload["audit"]["all_valid"]
    assert "words" not in payload
    assert not invoice_ocr_openai.ingest_invoice(
        source, cfg.supplier_invoice_dir, output, cfg, repo, vectors, budget
    )
    assert extract.call_count == 1
    assert vectors.ingest_document.call_count == 1


def test_failed_vector_write_can_be_retried(setup_invoice, tmp_path: Path):
    cfg, repo, budget, vectors, _ = setup_invoice
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / "supplier.png"
    source.write_bytes(b"input-image")
    vectors.ingest_document.side_effect = [RuntimeError("Chroma unavailable"), 1]

    with pytest.raises(RuntimeError, match="Chroma unavailable"):
        invoice_ocr_openai.ingest_invoice(
            source, input_dir, tmp_path / "sidecars", cfg, repo, vectors, budget
        )
    assert not repo.hash_exists(repo.compute_hash(source.read_bytes()))

    assert invoice_ocr_openai.ingest_invoice(
        source, input_dir, tmp_path / "sidecars", cfg, repo, vectors, budget
    )
    assert repo.hash_exists(repo.compute_hash(source.read_bytes()))
    assert len(list(cfg.supplier_invoice_dir.glob("*.png"))) == 1


def test_duplicate_invoice_number_does_not_replace_existing_record(setup_invoice, tmp_path: Path):
    cfg, repo, budget, vectors, _ = setup_invoice
    first = cfg.supplier_invoice_dir / "first.jpg"
    second = cfg.supplier_invoice_dir / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    output = tmp_path / "sidecars"

    assert invoice_ocr_openai.ingest_invoice(
        first, cfg.supplier_invoice_dir, output, cfg, repo, vectors, budget
    )
    assert not invoice_ocr_openai.ingest_invoice(
        second, cfg.supplier_invoice_dir, output, cfg, repo, vectors, budget
    )
    assert repo.hash_exists(repo.compute_hash(b"first"))
    assert not repo.hash_exists(repo.compute_hash(b"second"))
    assert vectors.ingest_document.call_count == 1


def test_invoice_is_indexed_in_chroma(setup_invoice, tmp_path: Path, monkeypatch):
    cfg, repo, budget, _, _ = setup_invoice
    source = cfg.supplier_invoice_dir / "invoice.jpg"
    source.write_bytes(b"image")

    class FixedEmbedding(EmbeddingFunction):
        def __call__(self, input):
            return [[1.0, 0.0] for _ in input]

        @staticmethod
        def name():
            return "fixed_test_embedding"

    collection = chromadb.EphemeralClient().create_collection(
        f"invoice_ocr_test_{uuid4().hex}", embedding_function=FixedEmbedding()
    )
    vectors = object.__new__(SharedChromaRepository)
    vectors.cfg = cfg
    vectors.budget_guard = None
    monkeypatch.setattr(vectors, "get_collection", lambda: collection)

    assert invoice_ocr_openai.ingest_invoice(
        source, cfg.supplier_invoice_dir, tmp_path / "sidecars", cfg, repo, vectors, budget
    )
    stored = collection.get(ids=[repo.compute_hash(b"image")])
    assert stored["metadatas"][0]["invoice_number"] == "INV-TEST"
    assert "Tea" in stored["documents"][0]


def test_cli_honors_limit_and_writes_master_csv(setup_invoice, tmp_path: Path, monkeypatch):
    cfg, repo, _, vectors, _ = setup_invoice
    (cfg.supplier_invoice_dir / "first.jpg").write_bytes(b"first")
    (cfg.supplier_invoice_dir / "second.jpg").write_bytes(b"second")
    monkeypatch.setattr(invoice_ocr_openai, "AppConfig", lambda: cfg)
    monkeypatch.setattr(invoice_ocr_openai, "SharedChromaRepository", lambda *a, **k: vectors)
    monkeypatch.setattr(
        sys,
        "argv",
        ["invoice_ocr_openai", "--output-dir", str(tmp_path / "sidecars"), "--limit", "1"],
    )

    assert invoice_ocr_openai.main() == 0
    assert repo.hash_exists(repo.compute_hash(b"first"))
    assert not repo.hash_exists(repo.compute_hash(b"second"))
    assert "INV-TEST" in cfg.audited_csv_path.read_text()
