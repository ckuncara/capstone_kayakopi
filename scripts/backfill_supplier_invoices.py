#!/usr/bin/env python3
"""Batch-ingest existing supplier invoice images from data/images/supplier_invoices/
into SQLite, MySQL, ChromaDB, individual CSVs, and the master CSV.

Example usage:
# Preview pending images without calling OpenAI
python scripts/backfill_supplier_invoices.py --dry-run

# Test on the first 5 unprocessed invoices
python scripts/backfill_supplier_invoices.py --limit 5

# Process all remaining supplier invoices in data/images/supplier_invoices/
python scripts/backfill_supplier_invoices.py
"""
import argparse
import mimetypes
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
load_dotenv()

from kaya_kopi.config import AppConfig  # noqa: E402
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (  # noqa: E402
    extract_invoice_with_openai,
    validate_invoice_calculations,
)
from kaya_kopi.repositories.mysql_invoice_sync import pending_syncs, sync_invoice  # noqa: E402
from kaya_kopi.repositories.chroma_store import SharedChromaRepository  # noqa: E402
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository  # noqa: E402
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard  # noqa: E402
from kaya_kopi.security.openai_proxy import openai_configured  # noqa: E402
from kaya_kopi.security.supabase_auth import ensure_cli_sign_in  # noqa: E402

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill supplier invoice images.")
    parser.add_argument("--limit", type=int, default=0, help="Max images to process (0 = all)")
    parser.add_argument("--force", action="store_true", help="Re-process already stored invoices")
    parser.add_argument("--dry-run", action="store_true", help="List pending images without API calls")
    args = parser.parse_args()

    cfg = AppConfig()
    if not args.dry_run and not openai_configured(cfg):
        raise SystemExit("Error: OpenAI access is not configured.")
    if not args.dry_run and cfg.llm_proxy_url:
        ensure_cli_sign_in(cfg)

    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)
    vec_repo = None if args.dry_run else SharedChromaRepository(cfg, budget_guard=budget)

    image_files = sorted(
        p for p in cfg.supplier_invoice_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )
    print(f"Found {len(image_files)} image(s) in {cfg.supplier_invoice_dir}")

    processed = 0
    skipped = 0
    mysql_failures = 0

    for img_path in image_files:
        if args.limit and processed >= args.limit:
            break

        raw_bytes = img_path.read_bytes()
        f_hash = repo.compute_hash(raw_bytes)
        stem_invoice_no = img_path.stem

        if not args.force and (repo.hash_exists(f_hash) or repo.invoice_number_exists(stem_invoice_no)):
            skipped += 1
            continue

        if args.dry_run:
            print(f"[DRY-RUN] Pending: {img_path.name}")
            processed += 1
            continue

        mime = mimetypes.guess_type(img_path.name)[0] or "image/jpeg"
        try:
            print(f"Processing {img_path.name} with {cfg.ocr_model}...")
            inv = extract_invoice_with_openai(
                raw_bytes,
                mime_type=mime,
                ocr_model=cfg.ocr_model,
                api_key=cfg.api_key,
                budget_guard=budget,
                branch_code=None,
            )
            repo.check_sync_identity(f_hash, inv.invoice_number, inv.vendor_name)
            stored_name, rel_img_path = repo.save_image_as_invoice_jpg(
                raw_bytes, inv.invoice_number or stem_invoice_no, f_hash
            )
            audit = validate_invoice_calculations(inv)

            repo.save_invoice(f_hash, None, stored_name, rel_img_path, inv, audit, sync_mysql=True)
            indiv_csv = repo.save_individual_invoice_csv(
                stored_name, rel_img_path, None, inv, audit
            )

            unified_doc = (
                f"Invoice: {inv.invoice_number} | "
                f"Vendor: {inv.vendor_name} (GST Reg: {inv.vendor_gst_reg_no or 'N/A'}) | "
                f"Date: {inv.invoice_date or 'N/A'}\n"
                f"OCR Totals -> Subtotal: {audit['ocr_subtotal']:.2f}, "
                f"GST: {audit['ocr_gst']:.2f}, Total: {audit['ocr_grand_total']:.2f}\n"
                f"Calc Totals -> Subtotal: {audit['calc_subtotal']:.2f}, "
                f"GST: {audit['calc_gst']:.2f}, Total: {audit['calc_grand_total']:.2f}\n"
                f"Audit Status: {'PASS' if audit['all_valid'] else 'FAIL'}\n\n"
                f"{inv.raw_markdown_text}"
            )
            assert vec_repo is not None
            vec_repo.ingest_document(
                parent_doc_id=f_hash,
                source_type="invoice",
                branch_code=None,
                text=unified_doc,
                strategy="single",
                extra_metadata={
                    "invoice_number": inv.invoice_number,
                    "filename": stored_name,
                    "image_path": rel_img_path,
                    "vendor_name": inv.vendor_name,
                    "audit_status": "PASS" if audit["all_valid"] else "FAIL",
                },
            )
            sync = next(entry for entry in pending_syncs(repo) if entry["file_hash"] == f_hash)
            try:
                result = sync_invoice(repo, sync["upload_id"])
            except (pymysql.MySQLError, ValueError, OSError) as exc:
                mysql_failures += 1
                print(
                    f"  -> MySQL sync failed for {inv.invoice_number}: {exc}. "
                    "The local invoice remains pending for retry in Settings.",
                    file=sys.stderr,
                )
            else:
                print(f"  -> {result.message}")
            status_tag = "PASS" if audit["all_valid"] else f"FAIL ({len(audit['issues'])} issues)"
            print(f"  -> {status_tag} | Saved {rel_img_path} & {indiv_csv}")
            processed += 1

        except BudgetExceededError as exc:
            print(f"Halted: {exc}")
            break

    if not args.dry_run and processed > 0:
        master_csv = repo.sync_master_csv_in_image_folder()
        print(f"Synced master CSV -> {master_csv}")

    print(f"Done. Processed: {processed} | Skipped existing: {skipped}")
    if mysql_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()