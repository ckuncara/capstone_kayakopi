#!/usr/bin/env python3
"""OCR all images and generate MySQL invoice staging data using OpenAI."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from kaya_kopi.ingestion.parsers import invoice_ocr_openai as pipeline
from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.invoice import write_csv
from kaya_kopi.ingestion.parsers.invoice_vision_audit import InvoiceOCRResult
from kaya_kopi.ingestion.parsers.vector_chunking import estimate_tokens
from kaya_kopi.paths import project_root
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetGuard


def sidecar_path(source: Path, image_root: Path, output_dir: Path) -> Path:
    path = output_dir / source.relative_to(image_root)
    return path.with_name(f"{path.name}.ocr.json")


def cached_sidecar(path: Path, file_hash: str) -> dict | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid OCR sidecar: {path}")
    return payload if payload.get("file_hash") == file_hash else None


def ocr_other_image(
    source: Path, image_root: Path, output_dir: Path, cfg: AppConfig, budget: BudgetGuard
) -> None:
    raw_bytes = source.read_bytes()
    file_hash = SQLiteOpsRepository.compute_hash(raw_bytes)
    sidecar = sidecar_path(source, image_root, output_dir)
    cached = cached_sidecar(sidecar, file_hash)
    if cached is not None:
        if cached.get("engine") != "openai" or not isinstance(cached.get("text"), str):
            raise ValueError(f"Invalid OpenAI OCR sidecar: {sidecar}")
        print(f"SKIP {source.relative_to(image_root)}: OCR sidecar already exists")
        return

    image_bytes, mime_type = pipeline.prepare_openai_image(source, raw_bytes)
    budget.check_budget_or_raise()
    response = ChatOpenAI(model=cfg.ocr_model, temperature=0.0, api_key=cfg.api_key).invoke(
        [
            SystemMessage(content="Transcribe all visible text in this image faithfully."),
            HumanMessage(
                content=[
                    {"type": "text", "text": "Transcribe the image text."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,"
                            f"{base64.b64encode(image_bytes).decode('ascii')}"
                        },
                    },
                ]
            ),
        ]
    )
    if not isinstance(response.content, str):
        raise ValueError(f"OpenAI returned non-text OCR output for {source}")
    budget.log_usage(
        operation="ocr_vision",
        model=cfg.ocr_model,
        prompt_tokens=1200,
        completion_tokens=estimate_tokens(response.content),
        branch_code=None,
    )
    with Image.open(source) as image:
        width, height = image.size
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source": source.relative_to(image_root).as_posix(),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "engine": "openai",
                "model": cfg.ocr_model,
                "file_hash": file_hash,
                "image": {"width": width, "height": height},
                "text": response.content,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"OK   {source.relative_to(image_root)}")


def stage_invoice(
    source: Path,
    image_root: Path,
    output_dir: Path,
    cfg: AppConfig,
    repo: SQLiteOpsRepository,
    vectors: SharedChromaRepository,
    budget: BudgetGuard,
    root: Path,
) -> tuple[dict, list[dict]]:
    raw_bytes = source.read_bytes()
    file_hash = repo.compute_hash(raw_bytes)
    sidecar = sidecar_path(source, image_root, output_dir)
    payload = cached_sidecar(sidecar, file_hash)
    already_stored = repo.hash_exists(file_hash)
    if not already_stored:
        pipeline.ingest_invoice(source, image_root, output_dir, cfg, repo, vectors, budget)
        if not repo.hash_exists(file_hash):
            raise ValueError(
                f"Invoice in {source} conflicts with an existing SQLite invoice number"
            )
        payload = cached_sidecar(sidecar, file_hash)
    elif payload is None:
        invoice = pipeline.load_stored_invoice(repo, file_hash)
        audit = pipeline.validate_invoice_calculations(invoice)
        pipeline.write_sidecar(
            source, image_root, output_dir, cfg, file_hash, invoice, audit, model=""
        )
        payload = cached_sidecar(sidecar, file_hash)
    if payload is None or payload.get("engine") != "openai":
        raise ValueError(f"Missing or invalid invoice OCR sidecar: {sidecar}")
    invoice = InvoiceOCRResult.model_validate(payload["invoice"])
    audit = pipeline.validate_invoice_calculations(invoice)
    existing = repo.get_invoice_by_hash(file_hash)
    if (existing["invoice_number"].strip().upper() != invoice.invoice_number.strip().upper()
            or existing["vendor_name"].strip().upper() != invoice.vendor_name.strip().upper()):
        raise ValueError(f"OCR identity differs from the stored SQLite invoice for {source}")

    if not vectors.get_collection().get(ids=[file_hash])["ids"]:
        pipeline.index_invoice(
            vectors, file_hash, existing["filename"], existing["image_path"], invoice, audit
        )
    if payload.get("text") != invoice.raw_markdown_text:
        pipeline.write_sidecar(source, image_root, output_dir, cfg, file_hash, invoice, audit)
        payload = cached_sidecar(sidecar, file_hash)
        if payload is None:
            raise RuntimeError(f"Unable to read invoice OCR sidecar: {sidecar}")

    source_file = source.relative_to(image_root).as_posix()
    review_notes = list(audit["issues"])
    for label, value in (
        ("invoice number", invoice.invoice_number),
        ("supplier", invoice.vendor_name),
        ("invoice date", invoice.invoice_date),
    ):
        if not value:
            review_notes.append(f"missing {label}")
    if not invoice.line_items:
        review_notes.append("no line items extracted")
    items = []
    for check in audit["line_checks"]:
        valid = check["is_valid"]
        items.append({
            "source_file": source_file,
            "invoice_number": invoice.invoice_number,
            "line_number": check["line_no"],
            "description": check["description"],
            "quantity": check["ocr_qty"],
            "unit_price": check["ocr_unit_price"],
            "amount": check["ocr_line_total"],
            "mean_field_confidence": "",
            "line_total_valid": str(valid).lower(),
            "extraction_status": "OK" if valid else "REVIEW_REQUIRED",
            "review_notes": "" if valid else "quantity x unit price does not equal amount",
        })
    header = {
        "source_file": source_file,
        "invoice_number": invoice.invoice_number,
        "ocr_engine": "openai",
        "ocr_language": "",
        "ocr_processed_at": payload["processed_at"],
        "image_width": "",
        "image_height": "",
        "supplier_name": invoice.vendor_name,
        "supplier_address": invoice.vendor_address or "",
        "gst_registration_number": invoice.vendor_gst_reg_no or "",
        "invoice_date": invoice.invoice_date or "",
        "bill_to": invoice.bill_to or "",
        "subtotal": invoice.subtotal,
        "gst_rate_percent": invoice.gst_rate_percent,
        "gst_amount": invoice.gst_amount,
        "total": invoice.grand_total,
        "payment_terms": invoice.payment_terms or "",
        "received_condition": "",
        "mean_ocr_confidence": "",
        "line_item_count": len(items),
        "arithmetic_valid": str(audit["all_valid"]).lower(),
        "extraction_status": "REVIEW_REQUIRED" if review_notes else "OK",
        "review_notes": "; ".join(review_notes),
        "ocr_json": sidecar.relative_to(root).as_posix(),
    }
    return header, items


def main() -> int:
    root = project_root()
    image_root = root / "data" / "images"
    cfg = AppConfig()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=image_root)
    parser.add_argument("--output-dir", type=Path, default=root / "runtime" / "ocr_openai_all")
    parser.add_argument("--staging-dir", type=Path, default=root / "runtime" / "structured")
    args = parser.parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    staging_dir = args.staging_dir.expanduser().resolve()
    if not input_dir.is_dir() or not input_dir.is_relative_to(image_root):
        parser.error(f"input directory must exist under {image_root}")
    if not output_dir.is_relative_to(root) or output_dir.is_relative_to(input_dir):
        parser.error("output directory must be under the project root and outside the input")
    if input_dir != image_root and staging_dir == root / "runtime" / "structured":
        parser.error("a partial input directory requires a separate --staging-dir")
    if staging_dir.is_relative_to(input_dir):
        parser.error("staging directory must be outside the input directory")
    if not cfg.api_key:
        parser.error("OPENAI_API_KEY is not set in the environment or .env")

    images = [
        path for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in pipeline.SUPPORTED_EXTENSIONS
    ]
    supplier_images = {
        path for path in images if path.relative_to(image_root).parts[0] == "supplier_invoices"
    }
    if not supplier_images:
        parser.error("no supplier invoice images found; refusing to replace staging CSVs")
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)
    vectors = SharedChromaRepository(cfg, budget_guard=budget)
    headers: list[dict] = []
    items: list[dict] = []
    invoice_numbers: set[str] = set()
    invoice_hashes: set[str] = set()
    ordered_images = sorted(images, key=lambda path: (-len(path.relative_to(image_root).parts), path))
    for source in ordered_images:
        if source in supplier_images:
            file_hash = repo.compute_hash(source.read_bytes())
            if file_hash in invoice_hashes:
                print(f"SKIP {source.relative_to(image_root)}: duplicate invoice image")
                continue
            invoice_hashes.add(file_hash)
            header, rows = stage_invoice(
                source, image_root, output_dir, cfg, repo, vectors, budget, root
            )
            number = header["invoice_number"].strip().upper()
            if number in invoice_numbers:
                raise ValueError(f"Duplicate invoice number in input images: {number}")
            invoice_numbers.add(number)
            headers.append(header)
            items.extend(rows)
        else:
            ocr_other_image(source, image_root, output_dir, cfg, budget)

    staging_dir.mkdir(parents=True, exist_ok=True)
    write_csv(staging_dir / "invoices.csv", headers, list(headers[0]))
    write_csv(
        staging_dir / "invoice_items.csv",
        items,
        [
            "source_file", "invoice_number", "line_number", "description", "quantity",
            "unit_price", "amount", "mean_field_confidence", "line_total_valid",
            "extraction_status", "review_notes",
        ],
    )
    repo.sync_master_csv_in_image_folder()
    print(f"OCR images: {len(images)}; staged invoices: {len(headers)}; items: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
