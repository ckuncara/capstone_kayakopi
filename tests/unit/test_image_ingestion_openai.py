import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import chromadb
from chromadb.api.types import EmbeddingFunction
from PIL import Image
import pytest

from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.loader.mysql import Loader, read_csv
from kaya_kopi.ingestion.parsers import image_ingestion_openai as pipeline
from kaya_kopi.ingestion.parsers.invoice_vision_audit import InvoiceOCRResult, LineItemOCR
from kaya_kopi.ingestion.parsers.vector_chunking import build_chunks
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetGuard


class FixedEmbedding(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        return [[1.0, 0.0] for _ in input]

    @staticmethod
    def name():
        return "fixed_test_embedding"


def test_embedding_records_tracker_fields(tmp_path: Path, monkeypatch):
    cfg = AppConfig(sqlite_db_path=tmp_path / "ops.sqlite3", embed_model="text-embedding-3-small")
    guard = BudgetGuard(cfg.sqlite_db_path)
    vectors = object.__new__(SharedChromaRepository)
    vectors.cfg = cfg
    vectors.budget_guard = guard
    vectors.embed_fn = Mock()
    vectors.embed_fn.client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=37), model=cfg.embed_model,
    )
    monkeypatch.setattr(vectors, "get_collection", lambda: Mock())
    chunks = build_chunks("doc1", text="Coffee invoice", strategy="single")

    assert vectors.upsert_chunks("doc1", chunks, "invoice") == 1
    usage = guard.usage_tracker.get_latest_usage()
    assert usage["thread_id"] == "embedding:doc1"
    assert usage["prompt"] == "Embed invoice"
    assert usage["model_name"] == cfg.embed_model
    assert usage["input_tokens"] == 37
    assert usage["output_tokens"] == 0


def test_embedding_usage_recorded_when_chroma_upsert_fails(tmp_path: Path, monkeypatch):
    cfg = AppConfig(sqlite_db_path=tmp_path / "ops.sqlite3", embed_model="text-embedding-3-small")
    guard = BudgetGuard(cfg.sqlite_db_path)
    vectors = object.__new__(SharedChromaRepository)
    vectors.cfg = cfg
    vectors.budget_guard = guard
    vectors.embed_fn = Mock()
    vectors.embed_fn.client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=37), model=cfg.embed_model,
    )
    collection = Mock()
    collection.upsert.side_effect = RuntimeError("Chroma unavailable")
    monkeypatch.setattr(vectors, "get_collection", lambda: collection)
    chunks = build_chunks("doc1", text="Coffee invoice", strategy="single")

    with pytest.raises(RuntimeError, match="Chroma unavailable"):
        vectors.upsert_chunks("doc1", chunks, "invoice")

    assert guard.usage_tracker.get_latest_usage()["input_tokens"] == 37
    vectors.embed_fn.client.embeddings.create.assert_called_once()


def test_full_pipeline_builds_mysql_staging_and_reuses_sqlite(tmp_path: Path, monkeypatch):
    root = tmp_path
    image_root = root / "data" / "images"
    invoices = image_root / "supplier_invoices"
    nested = invoices / "nested"
    other = image_root / "other"
    nested.mkdir(parents=True)
    other.mkdir()
    Image.new("RGB", (4, 3)).save(nested / "invoice.png")
    Image.new("RGB", (4, 3)).save(other / "document.tiff")
    cfg = AppConfig(
        api_key="test-key",
        supplier_invoice_dir=invoices,
        audited_csv_path=invoices / "master.csv",
        sqlite_db_path=root / "ops.sqlite3",
    )
    invoice = InvoiceOCRResult(
        vendor_name="Supplier",
        invoice_number="INV-TEST",
        invoice_date="2025-01-11",
        line_items=[LineItemOCR(description="Tea", quantity=2, unit_price=5, line_total=10)],
        subtotal=10,
        gst_amount=0.9,
        grand_total=10.9,
        raw_markdown_text="Invoice text",
    )
    extract = Mock(return_value=invoice)
    model = Mock()
    model.invoke.return_value = SimpleNamespace(
        content="Other image text", usage_metadata={"input_tokens": 2300, "output_tokens": 40}
    )
    collection = chromadb.EphemeralClient().create_collection(
        f"image_ingestion_test_{uuid4().hex}", embedding_function=FixedEmbedding()
    )
    vectors = object.__new__(SharedChromaRepository)
    vectors.cfg = cfg
    vectors.budget_guard = None
    monkeypatch.setattr(vectors, "get_collection", lambda: collection)
    monkeypatch.setattr(pipeline, "project_root", lambda: root)
    monkeypatch.setattr(pipeline, "AppConfig", lambda: cfg)
    monkeypatch.setattr(pipeline, "SharedChromaRepository", lambda *a, **k: vectors)
    monkeypatch.setattr(pipeline, "ChatOpenAI", lambda **kwargs: model)
    monkeypatch.setattr(pipeline.pipeline, "extract_invoice_with_openai", extract)
    monkeypatch.setattr(sys, "argv", ["image_ingestion_openai"])

    assert pipeline.main() == 0
    headers = read_csv(root / "runtime" / "structured" / "invoices.csv")
    items = read_csv(root / "runtime" / "structured" / "invoice_items.csv")
    assert headers[0]["source_file"] == "supplier_invoices/nested/invoice.png"
    assert headers[0]["mean_ocr_confidence"] == ""
    assert headers[0]["extraction_status"] == "OK"
    assert items[0]["description"] == "Tea"
    assert items[0]["line_total_valid"] == "true"
    sidecar = root / headers[0]["ocr_json"]
    assert json.loads(sidecar.read_text())["text"] == "Invoice text"
    generic = root / "runtime" / "ocr_openai_all" / "other" / "document.tiff.ocr.json"
    assert json.loads(generic.read_text())["text"] == "Other image text"
    usage = BudgetGuard(cfg.sqlite_db_path).usage_tracker.get_latest_usage()
    assert usage["thread_id"].startswith("ocr_vision:")
    assert usage["prompt"] == "Transcribe the image text."
    assert usage["input_tokens"] == 2300
    assert usage["output_tokens"] == 40
    request = model.invoke.call_args.args[0][1].content
    assert request[1]["image_url"]["url"].startswith("data:image/png;base64,")

    loader = Loader(None, root)
    loader.supplier_codes_by_name = {"SUPPLIER": "SUP-1"}
    loader.upsert_source = Mock()
    loader.load_source_documents([], headers, [], [])
    assert loader.upsert_source.call_args.kwargs["raw_text"] == "Invoice text"
    assert loader.upsert_source.call_args.args[3] == "image/png"
    cursor = Mock()
    cursor.fetchone.return_value = None
    cursor.lastrowid = 123
    connection = Mock()
    connection.cursor.return_value = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    loader.connection = connection
    loader.source_ids = {headers[0]["source_file"]: 42}
    loader.execute_many = Mock()
    missing_date = {**headers[0], "invoice_date": ""}
    loader.load_invoices([missing_date], items)
    assert cursor.execute.call_args.args[1][5] is None
    assert loader.execute_many.call_args.args[1][0][2] == "Tea"
    repo = SQLiteOpsRepository(cfg)
    image_hash = repo.compute_hash((nested / "invoice.png").read_bytes())
    assert repo.hash_exists(image_hash)
    assert collection.get(ids=[image_hash])["ids"] == [image_hash]

    sidecar.unlink()
    collection.delete(ids=[image_hash])
    assert pipeline.main() == 0
    assert extract.call_count == 1
    assert model.invoke.call_count == 1
    assert collection.get(ids=[image_hash])["ids"] == [image_hash]
    restored = json.loads(sidecar.read_text())
    assert restored["invoice"]["invoice_number"] == "INV-TEST"
    assert restored["model"] == ""
    with (root / "runtime" / "structured" / "invoices.csv").open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1
    assert rows[0]["source_file"] == "supplier_invoices/nested/invoice.png"


def test_partial_input_cannot_replace_full_staging(tmp_path: Path, monkeypatch):
    image_root = tmp_path / "data" / "images"
    partial = image_root / "supplier_invoices"
    partial.mkdir(parents=True)
    monkeypatch.setattr(pipeline, "project_root", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "AppConfig", lambda: AppConfig(api_key="test-key"))
    monkeypatch.setattr(sys, "argv", ["image_ingestion_openai", "--input-dir", str(partial)])
    with pytest.raises(SystemExit) as error:
        pipeline.main()
    assert error.value.code == 2


def test_failure_does_not_replace_existing_staging(tmp_path: Path, monkeypatch):
    root = tmp_path
    invoices = root / "data" / "images" / "supplier_invoices"
    invoices.mkdir(parents=True)
    (invoices / "one.jpg").write_bytes(b"one")
    (invoices / "two.jpg").write_bytes(b"two")
    staging = root / "runtime" / "structured"
    staging.mkdir(parents=True)
    existing = staging / "invoices.csv"
    existing.write_text("existing staging\n")
    cfg = AppConfig(
        api_key="test-key",
        supplier_invoice_dir=invoices,
        sqlite_db_path=root / "ops.sqlite3",
    )
    monkeypatch.setattr(pipeline, "project_root", lambda: root)
    monkeypatch.setattr(pipeline, "AppConfig", lambda: cfg)
    monkeypatch.setattr(pipeline, "SharedChromaRepository", Mock())
    monkeypatch.setattr(
        pipeline, "stage_invoice", Mock(side_effect=[({"invoice_number": "A"}, []), ValueError("failed")])
    )
    monkeypatch.setattr(sys, "argv", ["image_ingestion_openai"])

    with pytest.raises(ValueError, match="failed"):
        pipeline.main()
    assert existing.read_text() == "existing staging\n"
