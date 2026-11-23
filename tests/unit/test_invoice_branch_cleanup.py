import csv
import sqlite3

import chromadb

from scripts.remove_invoice_branch_tags import remove_tags


def test_removes_only_invoice_tags_and_preserves_embeddings(tmp_path):
    sqlite_path = tmp_path / "invoices.db"
    invoice_dir = tmp_path / "invoices"
    invoice_dir.mkdir()
    master = invoice_dir / "master.csv"
    individual = invoice_dir / "INV-1.csv"
    for path in (master, individual):
        path.write_text("invoice_number,branch_code,vendor\nINV-1,AMK-01,Example\n", encoding="utf-8")
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE supplier_invoices (filename TEXT, branch_code TEXT)")
        conn.execute("INSERT INTO supplier_invoices VALUES ('INV-1.jpg', 'AMK-01')")

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    collection = client.create_collection("test-invoices", embedding_function=None)
    collection.add(
        ids=["invoice", "other"],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        documents=[
            "Invoice: INV-1 | Branch: AMK-01 | Vendor: Example\nOriginal branch: AMK-01",
            "Branch-specific non-invoice document",
        ],
        metadatas=[
            {"source_type": "invoice", "branch_code": "AMK-01", "invoice_number": "INV-1"},
            {"source_type": "guide", "branch_code": "AMK-01"},
        ],
    )
    args = (sqlite_path, tmp_path / "chroma", "test-invoices", invoice_dir, master)
    assert remove_tags(*args) == {"sqlite_invoices": 1, "csv_files": 2, "chroma_chunks": 1}
    assert collection.get(ids=["invoice"], include=["metadatas"])["metadatas"][0]["branch_code"] == "AMK-01"
    assert remove_tags(*args, apply=True) == {"sqlite_invoices": 1, "csv_files": 2, "chroma_chunks": 1}
    result = collection.get(ids=["invoice"], include=["metadatas", "documents", "embeddings"])
    assert result["metadatas"][0] == {"source_type": "invoice", "invoice_number": "INV-1"}
    assert result["documents"][0] == "Invoice: INV-1 | Vendor: Example\nOriginal branch: AMK-01"
    assert list(result["embeddings"][0]) == [1.0, 0.0]
    assert collection.get(ids=["other"], include=["metadatas"])["metadatas"][0]["branch_code"] == "AMK-01"
    with sqlite3.connect(sqlite_path) as conn:
        assert conn.execute("SELECT branch_code FROM supplier_invoices").fetchone()[0] is None
    for path in (master, individual):
        with path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            assert reader.fieldnames == ["invoice_number", "vendor"]
            assert list(reader) == [{"invoice_number": "INV-1", "vendor": "Example"}]
    assert remove_tags(*args, apply=True) == {"sqlite_invoices": 0, "csv_files": 0, "chroma_chunks": 0}
