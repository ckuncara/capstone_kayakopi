"""Delete uploaded invoices across their local stores with retryable cleanup."""

import csv
import os
from pathlib import Path
import tempfile
from collections.abc import Callable
from contextlib import closing

from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


def _ensure_pending_table(repo: SQLiteOpsRepository) -> None:
    with closing(repo._connect()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_invoice_deletions (
                file_hash TEXT PRIMARY KEY,
                invoice_number TEXT NOT NULL,
                filename TEXT NOT NULL
            )"""
        )


def pending_invoice_deletions(repo: SQLiteOpsRepository) -> list[dict]:
    _ensure_pending_table(repo)
    with closing(repo._connect()) as conn, conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT file_hash, invoice_number, filename FROM pending_invoice_deletions"
            )
        ]


def _invoice_paths(repo: SQLiteOpsRepository, filename: str) -> tuple[Path, Path]:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("The invoice filename is not a safe plain filename.")
    image = repo.cfg.supplier_invoice_dir / filename
    csv_path = image.with_suffix(".csv")
    protected = {repo.cfg.audited_csv_path.resolve(), repo.cfg.sqlite_db_path.resolve()}
    if image.resolve() in protected or csv_path.resolve() in protected or image == csv_path:
        raise ValueError("Invoice files overlap a protected data store.")
    return image, csv_path


def _check_file_conflicts(repo: SQLiteOpsRepository, conn, file_hash: str, filename: str) -> None:
    targets = {str(path.absolute()).casefold() for path in _invoice_paths(repo, filename)}
    for row in conn.execute(
        """SELECT file_hash, filename FROM supplier_invoices WHERE file_hash <> ?
           UNION ALL
           SELECT file_hash, filename FROM pending_invoice_deletions WHERE file_hash <> ?""",
        (file_hash, file_hash),
    ):
        other = {str(path.absolute()).casefold() for path in _invoice_paths(repo, row["filename"])}
        if targets & other:
            raise ValueError(f"Invoice files are also referenced by invoice {row['file_hash']}.")


def _remove_from_master(path: Path, invoice_number: str) -> None:
    if not path.exists():
        return
    temporary_path = None
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or "invoice_number" not in reader.fieldnames:
                raise ValueError(f"Master invoice CSV has no invoice_number column: {path}")
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError(f"Master invoice CSV has duplicate columns: {path}")
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8", dir=path.parent, delete=False
            ) as output:
                temporary_path = Path(output.name)
                writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    if None in row:
                        raise ValueError(f"Master invoice CSV has extra columns: {path}")
                    if row["invoice_number"] != invoice_number:
                        writer.writerow(row)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def delete_invoice(
    repo: SQLiteOpsRepository,
    file_hash: str,
    delete_chunks: Callable[[str], None],
    expected_invoice_number: str | None = None,
    delete_mysql: Callable[[str], None] | None = None,
) -> None:
    """Delete an invoice and its files; interrupted operations can be retried by file hash."""
    if not file_hash:
        raise ValueError("The invoice chunk has no parent document ID.")
    _ensure_pending_table(repo)
    with closing(repo._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT invoice_number, filename FROM pending_invoice_deletions WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        if pending is None:
            invoice = conn.execute(
                "SELECT invoice_number, filename FROM supplier_invoices WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
            if invoice is None or not invoice["invoice_number"]:
                raise ValueError("No matching SQLite invoice was found; no data was deleted.")
            if expected_invoice_number is not None and invoice["invoice_number"] != expected_invoice_number:
                raise ValueError("The chunk and SQLite invoice numbers do not match; no data was deleted.")
            _check_file_conflicts(repo, conn, file_hash, invoice["filename"])
            conn.execute(
                "INSERT INTO pending_invoice_deletions VALUES (?, ?, ?)",
                (file_hash, invoice["invoice_number"], invoice["filename"]),
            )
            conn.execute("DELETE FROM supplier_invoices WHERE file_hash = ?", (file_hash,))
            pending = invoice
    with closing(repo._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT invoice_number, filename FROM pending_invoice_deletions WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        if pending is None:
            raise ValueError("Invoice cleanup is no longer pending.")
        if conn.execute(
            "SELECT 1 FROM supplier_invoices WHERE file_hash = ?", (file_hash,)
        ).fetchone():
            raise ValueError("This invoice was re-uploaded during cleanup; resolve the conflict first.")
        _check_file_conflicts(repo, conn, file_hash, pending["filename"])
        if conn.execute(
            "SELECT 1 FROM supplier_invoices WHERE invoice_number = ?",
            (pending["invoice_number"],),
        ).fetchone():
            raise ValueError("Another invoice now uses this invoice number; resolve the conflict first.")

        sync = conn.execute(
            "SELECT upload_id FROM mysql_invoice_sync WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if sync is not None:
            if delete_mysql is None:
                raise ValueError("MySQL cleanup is required for this invoice.")
            delete_mysql(sync["upload_id"])
            conn.execute("DELETE FROM mysql_invoice_sync WHERE upload_id = ?", (sync["upload_id"],))
        image, invoice_csv = _invoice_paths(repo, pending["filename"])
        _remove_from_master(repo.cfg.audited_csv_path, pending["invoice_number"])
        image.unlink(missing_ok=True)
        invoice_csv.unlink(missing_ok=True)
        delete_chunks(file_hash)
        conn.execute("DELETE FROM pending_invoice_deletions WHERE file_hash = ?", (file_hash,))
