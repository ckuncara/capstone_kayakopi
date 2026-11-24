"""Supplier invoice upload and synchronization page."""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from dataclasses import dataclass

import pymysql
import streamlit as st
from chromadb.errors import ChromaError
from httpx import HTTPError
from openai import OpenAIError
from PIL import Image

from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.invoice_ocr_openai import index_invoice, load_stored_invoice
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (
    extract_invoice_with_openai,
    validate_invoice_calculations,
)
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.repositories.mysql_invoice_sync import (
    InvoiceSyncResult,
    pending_syncs,
    sync_invoice,
)
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard


@dataclass(frozen=True)
class InvoiceUploadResult:
    invoice_number: str
    filename: str
    audit: dict
    sync: InvoiceSyncResult


@dataclass(frozen=True)
class SkippedInvoice:
    reason: str


def retry_invoice_upload(
    repo: SQLiteOpsRepository, vectors: SharedChromaRepository, upload_id: str, file_hash: str
) -> InvoiceSyncResult:
    with closing(repo._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        superseded = conn.execute(
            "SELECT file_hash FROM superseded_invoice_chunks WHERE upload_id=?", (upload_id,)
        ).fetchall()
        for entry in superseded:
            old_hash = entry["file_hash"]
            if not conn.execute(
                "SELECT 1 FROM supplier_invoices WHERE file_hash=?", (old_hash,)
            ).fetchone():
                vectors.delete_invoice_document(old_hash)
            conn.execute("DELETE FROM superseded_invoice_chunks WHERE file_hash=?", (old_hash,))
    record = repo.get_invoice_by_hash(file_hash)
    invoice = load_stored_invoice(repo, file_hash)
    audit = validate_invoice_calculations(invoice)
    index_invoice(vectors, file_hash, record["filename"], record["image_path"], invoice, audit)
    repo.save_individual_invoice_csv(
        record["filename"], record["image_path"], None, invoice, audit
    )
    repo.sync_master_csv_in_image_folder()
    return sync_invoice(repo, upload_id)


def upload_invoice(
    raw_bytes: bytes,
    cfg: AppConfig,
    repo: SQLiteOpsRepository,
    vectors: SharedChromaRepository,
    budget: BudgetGuard,
    force: bool = False,
) -> InvoiceUploadResult | SkippedInvoice:
    """Extract and persist an invoice, or skip it when already stored."""
    file_hash = repo.compute_hash(raw_bytes)
    if not force and repo.hash_exists(file_hash):
        return SkippedInvoice("This invoice image is already stored.")
    with Image.open(io.BytesIO(raw_bytes)) as image:
        image_format = image.format
        mime_type = (
            {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image_format)
            if image_format is not None else None
        )
        if mime_type is None:
            raise ValueError("Only JPEG, PNG and WebP invoice images are supported.")
        image.verify()

    invoice = extract_invoice_with_openai(
        raw_bytes,
        mime_type=mime_type,
        ocr_model=cfg.ocr_model,
        api_key=cfg.api_key,
        budget_guard=budget,
        branch_code=None,
    )
    if not invoice.invoice_number.strip() or not invoice.vendor_name.strip():
        raise ValueError("OCR must identify an invoice number and supplier name.")
    if not force and repo.invoice_number_exists(invoice.invoice_number):
        return SkippedInvoice(f"Invoice number {invoice.invoice_number} is already stored.")
    repo.check_sync_identity(file_hash, invoice.invoice_number, invoice.vendor_name)
    audit = validate_invoice_calculations(invoice)
    stored_name, image_path = repo.save_image_as_invoice_jpg(
        raw_bytes, invoice.invoice_number, file_hash,
        overwrite_existing=False, include_hash_suffix=True,
    )
    repo.save_invoice(
        file_hash, None, stored_name, image_path, invoice, audit,
        sync_mysql=True, allow_replace=force,
    )
    pending = next((entry for entry in pending_syncs(repo) if entry["file_hash"] == file_hash), None)
    if pending is None:
        raise RuntimeError("The saved invoice has no pending upload sync.")
    result = retry_invoice_upload(repo, vectors, pending["upload_id"], file_hash)
    return InvoiceUploadResult(invoice.invoice_number, stored_name, audit, result)


def _show_sync_result(result: InvoiceSyncResult) -> None:
    if result.purchase_id is not None and not result.review_required:
        st.success(result.message)
    else:
        st.warning(result.message)


def render_invoice_upload_page() -> None:
    if st.button("← Back to Chat"):
        st.session_state.show_invoice_upload = False
        st.rerun()

    st.header("Upload supplier invoices")
    st.caption(
        "OpenAI extracts and audits each invoice. Results are saved to SQLite, ChromaDB, "
        "and the related MySQL invoice tables; invoices are shared across branches."
    )
    cfg = AppConfig()
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)

    notice = st.session_state.pop("invoice_upload_sync_notice", None)
    if notice is not None:
        _show_sync_result(notice)
    for pending in pending_syncs(repo):
        st.warning(f"Invoice upload sync pending: {pending['invoice_number']}")
        if st.button("Retry invoice sync", key=f"upload_retry_{pending['upload_id']}"):
            try:
                vectors = SharedChromaRepository(cfg, budget_guard=budget)
                result = retry_invoice_upload(
                    repo, vectors, pending["upload_id"], pending["file_hash"]
                )
            except (
                OpenAIError, ChromaError, HTTPError, pymysql.MySQLError, sqlite3.Error,
                ValueError, OSError, RuntimeError,
            ) as exc:
                st.error("Invoice sync failed; it remains pending for retry.")
                st.exception(exc)
            else:
                st.session_state.invoice_upload_sync_notice = result
                st.rerun()

    files = st.file_uploader(
        "Choose supplier invoice images",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
    )
    force = st.checkbox(
        "Force upload existing invoices",
        value=False,
        help="Reprocess matching invoices and replace their stored data.",
    )
    if st.button("Run OCR and save invoices", type="primary", disabled=not files):
        if not cfg.api_key:
            st.error("Set OPENAI_API_KEY before uploading invoices.")
            return
        for file in files:
            try:
                with st.spinner(f"Processing {file.name}..."):
                    vectors = SharedChromaRepository(cfg, budget_guard=budget)
                    result = upload_invoice(file.getvalue(), cfg, repo, vectors, budget, force=force)
            except BudgetExceededError as exc:
                st.error(str(exc))
                break
            except (
                OpenAIError, ChromaError, HTTPError, pymysql.MySQLError, sqlite3.Error,
                ValueError, OSError, RuntimeError,
            ) as exc:
                st.error(
                    f"Upload incomplete for {file.name}. Some data may already be saved; "
                    "refresh the page and retry any pending invoice sync."
                )
                st.exception(exc)
                continue
            if isinstance(result, SkippedInvoice):
                st.info(f"Skipped {file.name}: {result.reason}")
                continue
            st.success(f"Saved invoice {result.invoice_number} to SQLite and ChromaDB.")
            _show_sync_result(result.sync)
            if not result.audit["all_valid"]:
                st.warning("The invoice arithmetic requires review.")
                for issue in result.audit["issues"]:
                    st.write(issue)
            st.dataframe(result.audit["line_checks"], use_container_width=True)
