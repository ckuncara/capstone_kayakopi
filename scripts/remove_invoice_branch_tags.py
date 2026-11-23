#!/usr/bin/env python3
"""Remove legacy invoice branch tags from local SQLite, CSV and ChromaDB stores."""

import argparse
import csv
import os
from pathlib import Path
import re
import sqlite3
import tempfile

import chromadb


_HEADER_BRANCH = re.compile(r"^(Invoice:[^\r\n]*? \| )Branch: [^|\r\n]* \| (?=Vendor:)")


def _read_csv_without_branch(path: Path) -> tuple[list[str], list[dict]] | None:
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"Invalid CSV header: {path}")
        if "branch_code" not in reader.fieldnames:
            return None
        fields = [field for field in reader.fieldnames if field != "branch_code"]
        rows = []
        for row in reader:
            if None in row:
                raise ValueError(f"CSV has extra columns: {path}")
            row.pop("branch_code")
            rows.append(row)
        return fields, rows


def _replace_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary_path = Path(file.name)
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def remove_tags(sqlite_path: Path, chroma_path: Path, collection_name: str,
                invoice_dir: Path, master_csv: Path, apply: bool = False) -> dict[str, int]:
    """Preview or remove invoice-only tags; repeat safely after an interrupted run."""
    for path in (sqlite_path, chroma_path):
        if not path.exists():
            raise FileNotFoundError(path)
    with sqlite3.connect(sqlite_path) as conn:
        invoices = conn.execute("SELECT filename, branch_code FROM supplier_invoices").fetchall()
    csv_paths = {master_csv}
    for filename, _ in invoices:
        if not filename or Path(filename).name != filename:
            raise ValueError("An invoice filename is missing or is not a plain filename.")
        csv_paths.add((invoice_dir / filename).with_suffix(".csv"))
    csv_updates = {}
    for path in csv_paths:
        if path.is_file():
            update = _read_csv_without_branch(path)
            if update is not None:
                csv_updates[path] = update

    client = chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_collection(collection_name, embedding_function=None)
    changes = []
    offset = 0
    while True:
        batch = collection.get(
            where={"source_type": "invoice"},
            include=["metadatas", "documents", "embeddings"],
            limit=100,
            offset=offset,
        )
        if not batch["ids"]:
            break
        if batch["metadatas"] is None or batch["documents"] is None or batch["embeddings"] is None:
            raise RuntimeError("ChromaDB did not return invoice metadata, documents and embeddings.")
        for index, chunk_id in enumerate(batch["ids"]):
            metadata = batch["metadatas"][index] or {}
            document = batch["documents"][index]
            if document is None:
                raise RuntimeError(f"Invoice chunk {chunk_id} has no document.")
            cleaned = _HEADER_BRANCH.sub(r"\1", document, count=1)
            if "branch_code" in metadata or cleaned != document:
                embedding = batch["embeddings"][index]
                if cleaned != document and embedding is None:
                    raise RuntimeError(f"Invoice chunk {chunk_id} has no embedding.")
                changes.append((chunk_id, "branch_code" in metadata, document, cleaned, embedding))
        offset += len(batch["ids"])

    counts = {
        "sqlite_invoices": sum(branch is not None for _, branch in invoices),
        "csv_files": len(csv_updates),
        "chroma_chunks": len(changes),
    }
    if not apply:
        return counts
    for chunk_id, tagged, original, cleaned, embedding in changes:
        kwargs = {"ids": [chunk_id]}
        if tagged:
            kwargs["metadatas"] = [{"branch_code": None}]
        if cleaned != original:
            kwargs["documents"] = [cleaned]
            kwargs["embeddings"] = [embedding.tolist() if hasattr(embedding, "tolist") else embedding]
        collection.update(**kwargs)
    for path, (fields, rows) in csv_updates.items():
        _replace_csv(path, fields, rows)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("UPDATE supplier_invoices SET branch_code = NULL WHERE branch_code IS NOT NULL")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--chroma", type=Path, required=True)
    parser.add_argument("--collection", default="kaya_kopi_knowledge")
    parser.add_argument("--invoice-dir", type=Path, required=True)
    parser.add_argument("--master-csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Apply changes; otherwise only preview.")
    args = parser.parse_args()
    result = remove_tags(
        args.sqlite, args.chroma, args.collection, args.invoice_dir, args.master_csv, args.apply
    )
    print(("Updated" if args.apply else "Would update") + f": {result}")


if __name__ == "__main__":
    main()
