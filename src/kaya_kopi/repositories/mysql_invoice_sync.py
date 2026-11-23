"""Synchronize locally saved OCR invoices to MySQL."""

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

from kaya_kopi.repositories.mysql import DatabaseConfig
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.paths import project_root
from kaya_kopi.ingestion.reconciliation import select_purchase


@dataclass(frozen=True)
class InvoiceSyncResult:
    purchase_id: int | None
    review_required: bool = False
    reason: str | None = None

    @property
    def message(self) -> str:
        if self.purchase_id is not None:
            text = f"Invoice synced to MySQL and matched purchase {self.purchase_id}."
            if self.review_required:
                text += " The reconciliation requires review."
            return text
        return f"Invoice synced to MySQL, but no reconciliation was created: {self.reason}"


def _reconcile_invoice(
    cursor, invoice_id: int, supplier: str, number: str, invoice_date: date | None,
    subtotal: Decimal, currency: str, extraction_status: str, unchanged: bool,
) -> InvoiceSyncResult:
    cursor.execute(
        """SELECT i.supplier_purchase_id, r.supplier_purchase_id AS reconciled_purchase_id
           FROM supplier_invoice i LEFT JOIN purchase_invoice_reconciliation r
             ON r.supplier_invoice_id=i.supplier_invoice_id
           WHERE i.supplier_invoice_id=%s FOR UPDATE""", (invoice_id,)
    )
    link = cursor.fetchone()
    linked_purchase = link["supplier_purchase_id"]
    reconciled_purchase = link["reconciled_purchase_id"]
    if linked_purchase is not None and reconciled_purchase is not None and linked_purchase != reconciled_purchase:
        raise ValueError("The invoice has conflicting purchase and reconciliation links.")
    purchase_id = linked_purchase if linked_purchase is not None else reconciled_purchase
    if purchase_id is None and currency != "SGD":
        return InvoiceSyncResult(None, reason="non-SGD amounts cannot be compared to SGD purchases.")

    purchase_sql = """
        SELECT p.purchase_id, p.purchase_date, p.total_amount,
               r.supplier_invoice_id AS reconciled_invoice_id, r.reconciliation_status,
               r.validation_status, r.match_method,
               i.supplier_invoice_id AS assigned_invoice_id
        FROM supplier_purchases p
        LEFT JOIN purchase_invoice_reconciliation r ON r.supplier_purchase_id=p.purchase_id
        LEFT JOIN supplier_invoice i ON i.supplier_purchase_id=p.purchase_id
    """
    if purchase_id is not None:
        cursor.execute(purchase_sql + " WHERE p.purchase_id=%s FOR UPDATE", (purchase_id,))
        purchase = cursor.fetchone()
        if purchase is None:
            raise ValueError("The linked purchase no longer exists.")
        candidates = [purchase]
    else:
        cursor.execute(
            purchase_sql + """
            JOIN supplier s ON UPPER(TRIM(s.supplier_code))=UPPER(TRIM(p.supplier_code))
            WHERE UPPER(TRIM(s.canonical_name))=%s AND UPPER(TRIM(p.invoice_no))=%s
            FOR UPDATE""",
            (supplier.upper(), number.upper()),
        )
        candidates = list(cursor.fetchall())

    available = [
        row for row in candidates
        if row["assigned_invoice_id"] in (None, invoice_id)
        and row["reconciled_invoice_id"] in (None, invoice_id)
        and (row["reconciled_invoice_id"] == invoice_id
             or row["reconciliation_status"] in (None, "NO_SCAN"))
    ]
    if not available:
        if purchase_id is not None:
            raise ValueError("The linked purchase is assigned to another invoice or requires review.")
        return InvoiceSyncResult(None, reason="no available purchase matches the supplier and invoice number.")

    if currency != "SGD":
        purchase, method = available[0], "EXISTING_LINK"
    else:
        purchase, method = select_purchase(available, invoice_date, subtotal)
    purchase_id = purchase["purchase_id"]
    already_matched = purchase["reconciled_invoice_id"] == invoice_id
    if already_matched:
        method = purchase["match_method"] or method
    needs_review = (
        currency != "SGD" or extraction_status != "OK"
        or (already_matched and not unchanged)
        or (linked_purchase is not None and not already_matched)
    )
    validation = (
        "REVIEW_REQUIRED" if needs_review else
        purchase["validation_status"] if already_matched else "CLIENT_RULE_APPLIED"
    )
    scanned = subtotal if currency == "SGD" else None
    legacy = Decimal(purchase["total_amount"])
    difference = scanned - legacy if scanned is not None else None
    if purchase["reconciliation_status"] is None:
        cursor.execute(
            """INSERT INTO purchase_invoice_reconciliation
               (supplier_purchase_id, supplier_invoice_id, reconciliation_status, match_method,
                legacy_subtotal_sgd, scanned_subtotal_sgd, subtotal_difference_sgd, validation_status)
               VALUES (%s,%s,'MATCHED',%s,%s,%s,%s,%s)""",
            (purchase_id, invoice_id, method, legacy, scanned, difference, validation),
        )
    else:
        cursor.execute(
            """UPDATE purchase_invoice_reconciliation SET supplier_invoice_id=%s,
               reconciliation_status='MATCHED', match_method=%s, legacy_subtotal_sgd=%s,
               scanned_subtotal_sgd=%s, subtotal_difference_sgd=%s, validation_status=%s,
               reconciled_at=CURRENT_TIMESTAMP WHERE supplier_purchase_id=%s""",
            (invoice_id, method, legacy, scanned, difference, validation, purchase_id),
        )
    cursor.execute(
        "UPDATE supplier_invoice SET supplier_purchase_id=%s WHERE supplier_invoice_id=%s",
        (purchase_id, invoice_id),
    )
    return InvoiceSyncResult(purchase_id, review_required=validation == "REVIEW_REQUIRED")


def _connect():
    load_dotenv(project_root() / ".env")
    cfg = DatabaseConfig.from_env()
    return pymysql.connect(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
        database=cfg.database, charset="utf8mb4", cursorclass=DictCursor,
        autocommit=False, connect_timeout=5, read_timeout=10, write_timeout=10,
        init_command="SET SESSION sql_mode=CONCAT_WS(',', NULLIF(@@sql_mode,''), 'STRICT_ALL_TABLES')",
    )


def pending_syncs(repo: SQLiteOpsRepository) -> list[dict]:
    with closing(repo._connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT upload_id, file_hash, invoice_number FROM mysql_invoice_sync WHERE pending=1"
        )]


def completed_syncs(repo: SQLiteOpsRepository) -> list[dict]:
    with closing(repo._connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT upload_id, file_hash, invoice_number FROM mysql_invoice_sync WHERE pending=0"
        )]


def queue_resync(repo: SQLiteOpsRepository, upload_id: str) -> None:
    with closing(repo._connect()) as conn, conn:
        result = conn.execute(
            "UPDATE mysql_invoice_sync SET pending=1 WHERE upload_id=?", (upload_id,)
        )
        if result.rowcount != 1:
            raise ValueError("The invoice sync no longer exists.")


def _quality_prefix(upload_id: str) -> str:
    return "ocr" + hashlib.sha256(upload_id.encode()).hexdigest()[:20]


def _sync_quality_issues(cursor, upload_id: str, source_id: int, number: str,
                         issues: list[str], items: list[dict]) -> None:
    prefix = _quality_prefix(upload_id)
    rows = []
    if issues:
        rows.append((
            prefix + "h", source_id, "supplier_invoice", number,
            "INVOICE_REVIEW_REQUIRED", "; ".join(issues),
        ))
    for item in items:
        if not item["is_valid"]:
            suffix = hashlib.sha256(str(item["line_no"]).encode()).hexdigest()[:8]
            rows.append((
                prefix + "i" + suffix, source_id, "supplier_invoice_item",
                f"{number}:{item['line_no']}", "INVOICE_ITEM_REVIEW_REQUIRED",
                f"Line difference: {item['line_diff']}",
            ))
    cursor.execute(
        """SELECT issue_import_key FROM data_quality_issue
           WHERE source_document_id=%s AND LEFT(issue_import_key,%s)=%s""",
        (source_id, len(prefix), prefix),
    )
    expected = {row[0] for row in rows}
    for existing in cursor.fetchall():
        if existing["issue_import_key"] not in expected:
            cursor.execute(
                "DELETE FROM data_quality_issue WHERE issue_import_key=%s",
                (existing["issue_import_key"],),
            )
    if rows:
        cursor.executemany(
            """INSERT INTO data_quality_issue
               (issue_import_key, source_document_id, record_type, record_key, rule_code,
                severity, details, resolution_status)
               VALUES (%s,%s,%s,%s,%s,'WARNING',%s,'OPEN')
               ON DUPLICATE KEY UPDATE source_document_id=VALUES(source_document_id),
                   record_type=VALUES(record_type), record_key=VALUES(record_key),
                   rule_code=VALUES(rule_code), severity=VALUES(severity), details=VALUES(details)""",
            rows,
        )


def sync_invoice(repo: SQLiteOpsRepository, upload_id: str) -> InvoiceSyncResult:
    with closing(repo._connect()) as local:
        local.execute("BEGIN IMMEDIATE")
        row = local.execute(
            """SELECT i.*, s.upload_id FROM supplier_invoices i
               JOIN mysql_invoice_sync s ON s.file_hash=i.file_hash
               WHERE s.upload_id=?""", (upload_id,)
        ).fetchone()
        if row is None:
            raise ValueError("The local invoice for this sync no longer exists.")
        invoice = dict(row)
        items = [dict(item) for item in local.execute(
            "SELECT * FROM supplier_invoice_items WHERE file_hash=? ORDER BY line_no",
            (invoice["file_hash"],),
        )]
        number = (invoice["invoice_number"] or "").strip()
        supplier = (invoice["vendor_name"] or "").strip()
        currency = (invoice["currency"] or "").strip().upper()
        if not number or not supplier:
            raise ValueError("MySQL sync requires an invoice number and supplier name.")
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError("MySQL sync requires a three-letter currency code.")
        if invoice["gst_rate_percent"] is None or invoice["gst_rate_percent"] < 0:
            raise ValueError("MySQL sync requires a non-negative GST rate.")
        invoice_date = date.fromisoformat(invoice["invoice_date"]) if invoice["invoice_date"] else None
        image_path = repo.cfg.supplier_invoice_dir / invoice["filename"]
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        version = hashlib.sha256(f"ocr-upload:{upload_id}:{invoice['file_hash']}".encode()).hexdigest()
        issues = json.loads(invoice["audit_issues"] or "[]")
        arithmetic_valid = invoice["audit_status"] == "PASS"
        if not arithmetic_valid and not issues:
            issues.append("invoice arithmetic requires review")
        if invoice_date is None:
            issues.append("missing invoice date")
        if not items:
            issues.append("no line items extracted")
        status = "OK" if arithmetic_valid and not issues else "REVIEW_REQUIRED"

        with closing(_connect()) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT s.supplier_invoice_id, s.created_invoice, i.invoice_number,
                                  i.supplier_name, i.invoice_date, i.subtotal, i.currency,
                                  i.extraction_status, d.source_version_key
                           FROM ocr_invoice_sync s JOIN supplier_invoice i
                             ON i.supplier_invoice_id=s.supplier_invoice_id
                           JOIN source_document d ON d.source_document_id=i.source_document_id
                           WHERE s.upload_id=%s FOR UPDATE""", (upload_id,)
                    )
                    existing = cursor.fetchone()
                    if existing and (
                        existing["invoice_number"].strip().upper() != number.upper()
                        or existing["supplier_name"].strip().upper() != supplier.upper()
                    ):
                        raise ValueError("The synchronized invoice identity changed; resolve it before retrying.")
                    if existing:
                        invoice_id = existing["supplier_invoice_id"]
                    else:
                        cursor.execute(
                            """SELECT supplier_invoice_id FROM supplier_invoice
                               WHERE UPPER(TRIM(invoice_number))=%s
                                 AND UPPER(TRIM(supplier_name))=%s FOR UPDATE""",
                            (number.upper(), supplier.upper()),
                        )
                        matches = cursor.fetchall()
                        if len(matches) > 1:
                            raise ValueError("Multiple MySQL invoices match this supplier and invoice number.")
                        invoice_id = matches[0]["supplier_invoice_id"] if matches else None
                        if invoice_id is not None:
                            cursor.execute(
                                "SELECT upload_id FROM ocr_invoice_sync WHERE supplier_invoice_id=%s",
                                (invoice_id,),
                            )
                            if cursor.fetchone():
                                raise ValueError("This MySQL invoice belongs to another OCR upload.")

                    cursor.execute(
                        """INSERT INTO source_document
                           (source_version_key, source_uri, content_sha256, document_type,
                            mime_type, raw_text, extraction_status)
                           VALUES (%s,%s,%s,'supplier_invoice','image/jpeg',%s,%s)
                           ON DUPLICATE KEY UPDATE source_document_id=LAST_INSERT_ID(source_document_id),
                               source_uri=VALUES(source_uri), content_sha256=VALUES(content_sha256),
                               raw_text=VALUES(raw_text), extraction_status=VALUES(extraction_status)""",
                        (version, invoice["image_path"], image_hash, invoice["raw_markdown"], status),
                    )
                    source_id = cursor.lastrowid
                    fields = (
                        source_id, number, supplier, invoice["vendor_address"],
                        invoice["vendor_gst_reg_no"], invoice_date, invoice["bill_to"],
                        currency, bool(invoice["is_gst_inclusive"]), invoice["ocr_subtotal"],
                        invoice["gst_rate_percent"], invoice["ocr_gst_amount"],
                        invoice["ocr_grand_total"], invoice["payment_terms"],
                        arithmetic_valid, status, "; ".join(issues) or None,
                    )
                    if invoice_id is None:
                        cursor.execute(
                            """INSERT INTO supplier_invoice
                               (source_document_id, invoice_number, supplier_name, supplier_address,
                                gst_registration_number, invoice_date, bill_to, currency,
                                is_gst_inclusive, subtotal, gst_rate_percent, gst_amount,
                                total_amount, payment_terms, arithmetic_valid, extraction_status,
                                review_notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            fields,
                        )
                        invoice_id = cursor.lastrowid
                        created = True
                    else:
                        cursor.execute(
                            """UPDATE supplier_invoice SET source_document_id=%s, invoice_number=%s,
                               supplier_name=%s, supplier_address=%s, gst_registration_number=%s,
                               invoice_date=%s, bill_to=%s, currency=%s, is_gst_inclusive=%s,
                               subtotal=%s, gst_rate_percent=%s, gst_amount=%s, total_amount=%s,
                               payment_terms=%s, arithmetic_valid=%s, extraction_status=%s,
                               review_notes=%s, mean_ocr_confidence=NULL
                               WHERE supplier_invoice_id=%s""", (*fields, invoice_id),
                        )
                        created = bool(existing["created_invoice"]) if existing else False
                    if not existing:
                        cursor.execute(
                            "INSERT INTO ocr_invoice_sync VALUES (%s,%s,%s)",
                            (upload_id, invoice_id, created),
                        )
                    cursor.execute(
                        "SELECT source_document_id FROM ocr_invoice_source WHERE source_document_id=%s",
                        (source_id,),
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            "INSERT INTO ocr_invoice_source VALUES (%s,%s)", (upload_id, source_id)
                        )
                    cursor.execute("DELETE FROM supplier_invoice_item WHERE supplier_invoice_id=%s", (invoice_id,))
                    cursor.executemany(
                        """INSERT INTO supplier_invoice_item
                           (supplier_invoice_id, line_number, description, quantity, unit_price,
                            amount, line_total_valid, extraction_status, review_notes)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [
                            (invoice_id, item["line_no"], item["description"], item["ocr_quantity"],
                             item["ocr_unit_price"], item["ocr_line_total"], bool(item["is_valid"]),
                             "OK" if item["is_valid"] else "REVIEW_REQUIRED",
                             None if item["is_valid"] else f"Line difference: {item['line_diff']}")
                            for item in items
                        ],
                    )
                    _sync_quality_issues(cursor, upload_id, source_id, number, issues, items)
                    cursor.execute(
                        "SELECT subtotal FROM supplier_invoice WHERE supplier_invoice_id=%s",
                        (invoice_id,),
                    )
                    subtotal = cursor.fetchone()["subtotal"]
                    unchanged = bool(
                        existing and existing["source_version_key"] == version
                        and existing["invoice_date"] == invoice_date
                        and existing["subtotal"] == subtotal
                        and existing["currency"] == currency
                        and existing["extraction_status"] == status
                    )
                    result = _reconcile_invoice(
                        cursor, invoice_id, supplier, number, invoice_date, subtotal, currency,
                        status, unchanged,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        local.execute("UPDATE mysql_invoice_sync SET pending=0 WHERE upload_id=?", (upload_id,))
        local.commit()
        return result


def delete_synced_invoice(upload_id: str) -> None:
    with closing(_connect()) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT s.supplier_invoice_id, s.created_invoice, i.source_document_id,
                              i.supplier_purchase_id
                       FROM ocr_invoice_sync s JOIN supplier_invoice i
                         ON i.supplier_invoice_id=s.supplier_invoice_id
                       WHERE s.upload_id=%s FOR UPDATE""", (upload_id,)
                )
                sync = cursor.fetchone()
                if sync is None:
                    connection.commit()
                    return
                cursor.execute(
                    "SELECT source_document_id FROM ocr_invoice_source WHERE upload_id=%s",
                    (upload_id,),
                )
                source_ids = [row["source_document_id"] for row in cursor.fetchall()]
                if sync["source_document_id"] not in source_ids:
                    raise ValueError("The MySQL invoice source has changed outside this upload.")
                invoice_id = sync["supplier_invoice_id"]
                if sync["created_invoice"]:
                    cursor.execute(
                        "SELECT 1 FROM purchase_invoice_reconciliation WHERE supplier_invoice_id=%s",
                        (invoice_id,),
                    )
                    if sync["supplier_purchase_id"] is not None or cursor.fetchone():
                        raise ValueError("The MySQL invoice is linked to a purchase; resolve the link first.")
                    cursor.execute(
                        "DELETE FROM supplier_invoice_item WHERE supplier_invoice_id=%s", (invoice_id,)
                    )
                cursor.execute("DELETE FROM ocr_invoice_source WHERE upload_id=%s", (upload_id,))
                cursor.execute("DELETE FROM ocr_invoice_sync WHERE upload_id=%s", (upload_id,))
                if sync["created_invoice"]:
                    cursor.execute("DELETE FROM supplier_invoice WHERE supplier_invoice_id=%s", (invoice_id,))
                for source_id in source_ids:
                    if sync["created_invoice"] or source_id != sync["source_document_id"]:
                        prefix = _quality_prefix(upload_id)
                        cursor.execute(
                            """DELETE FROM data_quality_issue WHERE source_document_id=%s
                               AND LEFT(issue_import_key,%s)=%s""",
                            (source_id, len(prefix), prefix),
                        )
                        cursor.execute("DELETE FROM source_document WHERE source_document_id=%s", (source_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
