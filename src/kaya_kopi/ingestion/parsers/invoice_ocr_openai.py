#!/usr/bin/env python3
"""Extract supplier invoices with OpenAI and store their audit results."""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageOps

load_dotenv()

from kaya_kopi.config import AppConfig  # noqa: E402
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (  # noqa: E402
    InvoiceOCRResult,
    LineItemOCR,
    extract_invoice_with_openai,
    validate_invoice_calculations,
)
from kaya_kopi.paths import project_root  # noqa: E402
from kaya_kopi.repositories.chroma_store import SharedChromaRepository  # noqa: E402
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository  # noqa: E402
from kaya_kopi.security.budget_guard import BudgetGuard  # noqa: E402
from kaya_kopi.security.openai_proxy import openai_configured  # noqa: E402
from kaya_kopi.security.supabase_auth import ensure_cli_sign_in  # noqa: E402

SUPPORTED_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def prepare_openai_image(source: Path, raw_bytes: bytes) -> tuple[bytes, str]:
    mime_type = mimetypes.guess_type(source.name)[0]
    if mime_type in {"image/jpeg", "image/png", "image/webp"}:
        return raw_bytes, mime_type
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported image type: {source}")
    with Image.open(io.BytesIO(raw_bytes)) as image:
        prepared = ImageOps.exif_transpose(image).convert("RGB")
        output = io.BytesIO()
        prepared.save(output, format="PNG")
    return output.getvalue(), "image/png"


def write_sidecar(
    source: Path,
    input_dir: Path,
    output_dir: Path,
    cfg: AppConfig,
    file_hash: str,
    invoice: InvoiceOCRResult,
    audit: dict,
    model: str | None = None,
) -> Path:
    sidecar = output_dir / source.relative_to(input_dir)
    sidecar = sidecar.with_name(f"{sidecar.name}.ocr.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source": source.relative_to(input_dir).as_posix(),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "engine": "openai",
                "model": cfg.ocr_model if model is None else model,
                "file_hash": file_hash,
                "text": invoice.raw_markdown_text,
                "invoice": invoice.model_dump(mode="json"),
                "audit": audit,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return sidecar


def index_invoice(
    vectors: SharedChromaRepository,
    file_hash: str,
    stored_name: str,
    image_path: str,
    invoice: InvoiceOCRResult,
    audit: dict,
) -> None:
    status = "PASS" if audit["all_valid"] else "FAIL"
    document = (
        f"Invoice: {invoice.invoice_number} | "
        f"Vendor: {invoice.vendor_name} (GST Reg: {invoice.vendor_gst_reg_no or 'N/A'}) | "
        f"Date: {invoice.invoice_date or 'N/A'}\n"
        f"OCR Totals -> Subtotal: {audit['ocr_subtotal']:.2f}, "
        f"GST: {audit['ocr_gst']:.2f}, Total: {audit['ocr_grand_total']:.2f}\n"
        f"Calc Totals -> Subtotal: {audit['calc_subtotal']:.2f}, "
        f"GST: {audit['calc_gst']:.2f}, Total: {audit['calc_grand_total']:.2f}\n"
        f"Audit Status: {status}\n\n{invoice.raw_markdown_text}"
    )
    chunks = vectors.ingest_document(
        parent_doc_id=file_hash,
        source_type="invoice",
        branch_code=None,
        text=document,
        strategy="single",
        extra_metadata={
            "invoice_number": invoice.invoice_number,
            "filename": stored_name,
            "image_path": image_path,
            "vendor_name": invoice.vendor_name,
            "audit_status": status,
        },
    )
    if not chunks:
        raise RuntimeError(f"No ChromaDB chunks were stored for invoice {invoice.invoice_number}")


def load_stored_invoice(repo: SQLiteOpsRepository, file_hash: str) -> InvoiceOCRResult:
    record = repo.get_invoice_by_hash(file_hash)
    return InvoiceOCRResult(
        vendor_name=record["vendor_name"],
        vendor_address=record["vendor_address"],
        vendor_gst_reg_no=record["vendor_gst_reg_no"],
        bill_to=record["bill_to"],
        invoice_number=record["invoice_number"],
        invoice_date=record["invoice_date"],
        payment_terms=record["payment_terms"],
        currency=record["currency"],
        is_gst_inclusive=bool(record["is_gst_inclusive"]),
        gst_rate_percent=record["gst_rate_percent"],
        line_items=[
            LineItemOCR(
                description=line["description"],
                quantity=line["ocr_quantity"],
                unit_price=line["ocr_unit_price"],
                line_total=line["ocr_line_total"],
            )
            for line in repo.get_line_items(file_hash)
        ],
        subtotal=record["ocr_subtotal"],
        gst_amount=record["ocr_gst_amount"],
        grand_total=record["ocr_grand_total"],
        raw_markdown_text=record["raw_markdown"],
    )


def ingest_invoice(
    source: Path,
    input_dir: Path,
    output_dir: Path,
    cfg: AppConfig,
    repo: SQLiteOpsRepository,
    vectors: SharedChromaRepository,
    budget: BudgetGuard,
) -> bool:
    """Ingest one image; return False when it is already stored."""
    raw_bytes = source.read_bytes()
    file_hash = repo.compute_hash(raw_bytes)
    if repo.hash_exists(file_hash):
        print(f"SKIP {source.relative_to(input_dir)}: image already stored")
        return False

    image_bytes, mime_type = prepare_openai_image(source, raw_bytes)

    invoice = extract_invoice_with_openai(
        image_bytes,
        mime_type=mime_type,
        ocr_model=cfg.ocr_model,
        api_key=cfg.api_key,
        budget_guard=budget,
        branch_code=None,
    )
    if not invoice.invoice_number.strip():
        raise ValueError(f"No invoice number extracted from {source}")
    if repo.invoice_number_exists(invoice.invoice_number):
        audit = validate_invoice_calculations(invoice)
        write_sidecar(source, input_dir, output_dir, cfg, file_hash, invoice, audit)
        print(f"SKIP {source.relative_to(input_dir)}: invoice number already stored")
        return False
    repo.check_sync_identity(file_hash, invoice.invoice_number, invoice.vendor_name)
    audit = validate_invoice_calculations(invoice)

    stem = re.sub(r"[^a-zA-Z0-9_-]", "", invoice.invoice_number.strip())
    if not stem:
        stem = f"INV-{file_hash[:12]}"
    if source.parent.resolve() == cfg.supplier_invoice_dir.resolve():
        stored_name = source.name
    else:
        stored_name = f"{stem}-{file_hash[:12]}{source.suffix.lower()}"
        target = cfg.supplier_invoice_dir / stored_name
        try:
            with target.open("xb") as image_file:
                image_file.write(raw_bytes)
        except FileExistsError:
            if repo.compute_hash(target.read_bytes()) != file_hash:
                raise FileExistsError(f"Refusing to overwrite an existing image: {target}")
    image_path = f"data/images/supplier_invoices/{stored_name}"

    index_invoice(vectors, file_hash, stored_name, image_path, invoice, audit)

    write_sidecar(source, input_dir, output_dir, cfg, file_hash, invoice, audit)
    repo.save_invoice(file_hash, None, stored_name, image_path, invoice, audit)
    status = "PASS" if audit["all_valid"] else "FAIL"
    print(f"OK   {source.relative_to(input_dir)}: {invoice.invoice_number} ({status})")
    return True


def main() -> int:
    cfg = AppConfig()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=cfg.supplier_invoice_dir)
    parser.add_argument("--output-dir", type=Path, default=project_root() / "runtime" / "ocr_openai")
    parser.add_argument("--limit", type=int, default=0, help="maximum invoices to process; 0 means all")
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"input directory does not exist: {input_dir}")
    if output_dir == input_dir or input_dir in output_dir.parents:
        parser.error("output directory must not be inside the input directory")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if not openai_configured(cfg):
        parser.error("OpenAI access is not configured.")
    if cfg.llm_proxy_url:
        ensure_cli_sign_in(cfg)

    images = [
        path for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)
    vectors = SharedChromaRepository(cfg, budget_guard=budget)
    processed = skipped = 0
    for source in images:
        if args.limit and processed >= args.limit:
            break
        if ingest_invoice(source, input_dir, output_dir, cfg, repo, vectors, budget):
            processed += 1
            repo.sync_master_csv_in_image_folder()
        else:
            skipped += 1

    # Also repairs a master CSV write interrupted after a successful SQLite insert.
    if not processed and skipped:
        repo.sync_master_csv_in_image_folder()
    print(f"Processed: {processed}; skipped: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
