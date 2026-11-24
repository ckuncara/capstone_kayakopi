import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import uuid
from PIL import Image
from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.invoice_vision_audit import InvoiceOCRResult

CSV_FIELDNAMES = [
    "invoice_number",
    "filename",
    "image_path",
    "vendor_name",
    "vendor_address",
    "vendor_gst_reg_no",
    "invoice_date",
    "payment_terms",
    "currency",
    "line_no",
    "item_description",
    "ocr_quantity",
    "ocr_unit_price",
    "ocr_line_total",
    "calc_line_total",
    "line_diff",
    "line_valid",
    "ocr_subtotal",
    "calc_subtotal",
    "subtotal_diff",
    "gst_rate_percent",
    "ocr_gst_amount",
    "calc_gst_amount",
    "gst_diff",
    "ocr_grand_total",
    "calc_grand_total",
    "grand_total_diff",
    "audit_status",
    "audit_issues",
]


class SQLiteOpsRepository:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.cfg.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.supplier_invoice_dir.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cfg.sqlite_db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS supplier_invoices (
                    file_hash TEXT PRIMARY KEY,
                    invoice_number TEXT UNIQUE,
                    branch_code TEXT,
                    filename TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    vendor_name TEXT,
                    vendor_address TEXT,
                    vendor_gst_reg_no TEXT,
                    bill_to TEXT,
                    invoice_date TEXT,
                    payment_terms TEXT,
                    currency TEXT,
                    is_gst_inclusive INTEGER,
                    gst_rate_percent REAL,
                    ocr_subtotal REAL,
                    calc_subtotal REAL,
                    subtotal_diff REAL,
                    ocr_gst_amount REAL,
                    calc_gst_amount REAL,
                    gst_diff REAL,
                    ocr_grand_total REAL,
                    calc_grand_total REAL,
                    grand_total_diff REAL,
                    audit_status TEXT,
                    audit_issues TEXT,
                    raw_markdown TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS supplier_invoice_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT NOT NULL,
                    line_no INTEGER,
                    description TEXT,
                    ocr_quantity REAL,
                    ocr_unit_price REAL,
                    ocr_line_total REAL,
                    calc_line_total REAL,
                    line_diff REAL,
                    is_valid INTEGER,
                    FOREIGN KEY (file_hash) REFERENCES supplier_invoices(file_hash) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS mysql_invoice_sync (
                    upload_id TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL UNIQUE,
                    invoice_number TEXT NOT NULL UNIQUE,
                    pending INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS superseded_invoice_chunks (
                    file_hash TEXT PRIMARY KEY,
                    upload_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    branch_code TEXT DEFAULT 'HQ',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_code TEXT DEFAULT 'HQ',
                    redacted_question TEXT NOT NULL,
                    redacted_answer TEXT NOT NULL,
                    tools_used TEXT,
                    cost_usd REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    @staticmethod
    def compute_hash(file_bytes: bytes) -> str:
        return hashlib.sha256(file_bytes).hexdigest()

    def hash_exists(self, file_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM supplier_invoices WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            return row is not None

    def invoice_number_exists(self, invoice_number: str) -> bool:
        clean_no = (invoice_number or "").strip()
        if not clean_no:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM supplier_invoices WHERE invoice_number = ? COLLATE NOCASE",
                (clean_no,),
            ).fetchone()
            return row is not None

    @staticmethod
    def _sync_identity(conn, file_hash: str, invoice_number: str, vendor_name: str) -> str | None:
        rows = conn.execute(
            """SELECT s.upload_id, s.invoice_number, i.vendor_name
               FROM mysql_invoice_sync s
               LEFT JOIN supplier_invoices i ON i.file_hash=s.file_hash
               WHERE s.file_hash=? OR s.invoice_number=? COLLATE NOCASE""",
            (file_hash, invoice_number.strip()),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("The file and invoice number belong to different MySQL syncs.")
        if not rows:
            return None
        row = rows[0]
        if (row["invoice_number"].strip().upper() != invoice_number.strip().upper()
                or not row["vendor_name"]
                or row["vendor_name"].strip().upper() != vendor_name.strip().upper()):
            raise ValueError("The synchronized invoice identity changed; resolve it before uploading.")
        return row["upload_id"]

    def check_sync_identity(self, file_hash: str, invoice_number: str, vendor_name: str) -> None:
        with self._connect() as conn:
            self._sync_identity(conn, file_hash, invoice_number, vendor_name)

    def save_image_as_invoice_jpg(
        self, file_bytes: bytes, invoice_number: str, file_hash: str,
        overwrite_existing: bool = True,
        include_hash_suffix: bool = False,
    ) -> tuple[str, str]:
        with self._connect() as conn:
            has_pending_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_invoice_deletions'"
            ).fetchone()
            if has_pending_table and conn.execute(
                "SELECT 1 FROM pending_invoice_deletions WHERE file_hash=? OR invoice_number=?",
                (file_hash, invoice_number.strip()),
            ).fetchone():
                raise ValueError("This invoice is pending deletion; finish cleanup before uploading it.")
        stem = re.sub(r"[^a-zA-Z0-9_-]", "", (invoice_number or "").strip())
        if not stem:
            stem = f"INV-{file_hash[:12]}"
        elif include_hash_suffix:
            stem = f"{stem}-{file_hash[:12]}"

        stored_filename = f"{stem}.jpg"
        target_path = self.cfg.supplier_invoice_dir / stored_filename

        with Image.open(io.BytesIO(file_bytes)) as img:
            if overwrite_existing:
                img.convert("RGB").save(target_path, format="JPEG", quality=95)
            else:
                encoded = io.BytesIO()
                img.convert("RGB").save(encoded, format="JPEG", quality=95)
                try:
                    with target_path.open("xb") as target:
                        target.write(encoded.getvalue())
                except FileExistsError:
                    if target_path.read_bytes() != encoded.getvalue():
                        raise FileExistsError(f"Refusing to overwrite an existing image: {target_path}")

        return stored_filename, f"data/images/supplier_invoices/{stored_filename}"

    def save_invoice(
        self,
        file_hash: str,
        branch_code: str | None,
        stored_filename: str,
        image_path: str,
        invoice: InvoiceOCRResult,
        audit: dict,
        sync_mysql: bool = False,
        allow_replace: bool = True,
    ) -> None:
        status = "PASS" if audit["all_valid"] else f"FAIL ({len(audit['issues'])} issues)"
        issues_json = json.dumps(audit["issues"])

        with self._connect() as conn:
            if sync_mysql or not allow_replace:
                conn.execute("BEGIN IMMEDIATE")
            replaced = None
            if not allow_replace:
                if conn.execute(
                    """SELECT 1 FROM supplier_invoices
                       WHERE file_hash=? OR invoice_number=? COLLATE NOCASE""",
                    (file_hash, invoice.invoice_number.strip()),
                ).fetchone():
                    raise ValueError("This invoice image or invoice number is already stored.")
            elif sync_mysql:
                matches = conn.execute(
                    """SELECT file_hash, invoice_number, vendor_name FROM supplier_invoices
                       WHERE file_hash=? OR invoice_number=? COLLATE NOCASE""",
                    (file_hash, invoice.invoice_number.strip()),
                ).fetchall()
                if len(matches) > 1:
                    raise ValueError("The image and invoice number belong to different invoices.")
                if matches:
                    replaced = matches[0]
                    if (not replaced["invoice_number"] or not replaced["vendor_name"]
                            or replaced["invoice_number"].strip().upper() != invoice.invoice_number.strip().upper()
                            or replaced["vendor_name"].strip().upper() != invoice.vendor_name.strip().upper()):
                        raise ValueError("The existing invoice identity changed; resolve it before uploading.")
            if sync_mysql:
                upload_id = self._sync_identity(
                    conn, file_hash, invoice.invoice_number, invoice.vendor_name
                ) or uuid.uuid4().hex
                if replaced is not None and replaced["file_hash"] != file_hash:
                    conn.execute(
                        "INSERT OR REPLACE INTO superseded_invoice_chunks VALUES (?, ?)",
                        (replaced["file_hash"], upload_id),
                    )
                conn.execute("DELETE FROM superseded_invoice_chunks WHERE file_hash=?", (file_hash,))
                conn.execute(
                    """INSERT INTO mysql_invoice_sync (upload_id, file_hash, invoice_number, pending)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT(upload_id) DO UPDATE SET file_hash=excluded.file_hash,
                           invoice_number=excluded.invoice_number, pending=1""",
                    (upload_id, file_hash, invoice.invoice_number.strip()),
                )
                if replaced is not None:
                    conn.execute("DELETE FROM supplier_invoices WHERE file_hash=?", (replaced["file_hash"],))
            conn.execute(
                """
                INSERT OR REPLACE INTO supplier_invoices (
                    file_hash, invoice_number, branch_code, filename, image_path,
                    vendor_name, vendor_address, vendor_gst_reg_no, bill_to,
                    invoice_date, payment_terms, currency, is_gst_inclusive, gst_rate_percent,
                    ocr_subtotal, calc_subtotal, subtotal_diff,
                    ocr_gst_amount, calc_gst_amount, gst_diff,
                    ocr_grand_total, calc_grand_total, grand_total_diff,
                    audit_status, audit_issues, raw_markdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_hash,
                    invoice.invoice_number.strip(),
                    branch_code.upper() if branch_code is not None else None,
                    stored_filename,
                    image_path,
                    invoice.vendor_name,
                    invoice.vendor_address,
                    invoice.vendor_gst_reg_no,
                    invoice.bill_to,
                    invoice.invoice_date,
                    invoice.payment_terms,
                    invoice.currency,
                    int(invoice.is_gst_inclusive),
                    invoice.gst_rate_percent,
                    audit["ocr_subtotal"],
                    audit["calc_subtotal"],
                    audit["subtotal_diff"],
                    audit["ocr_gst"],
                    audit["calc_gst"],
                    audit["gst_diff"],
                    audit["ocr_grand_total"],
                    audit["calc_grand_total"],
                    audit["grand_total_diff"],
                    status,
                    issues_json,
                    invoice.raw_markdown_text,
                ),
            )
            conn.execute("DELETE FROM supplier_invoice_items WHERE file_hash = ?", (file_hash,))
            for lc in audit["line_checks"]:
                conn.execute(
                    """
                    INSERT INTO supplier_invoice_items (
                        file_hash, line_no, description, ocr_quantity, ocr_unit_price,
                        ocr_line_total, calc_line_total, line_diff, is_valid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_hash,
                        lc["line_no"],
                        lc["description"],
                        lc["ocr_qty"],
                        lc["ocr_unit_price"],
                        lc["ocr_line_total"],
                        lc["calc_line_total"],
                        lc["line_diff"],
                        int(lc["is_valid"]),
                    ),
                )

    def save_individual_invoice_csv(
        self,
        stored_filename: str,
        image_path: str,
        branch_code: str | None,
        invoice: InvoiceOCRResult,
        audit: dict,
    ) -> str:
        """Atomically writes data/images/supplier_invoices/<invoice_number>.csv."""
        csv_target = (self.cfg.supplier_invoice_dir / stored_filename).with_suffix(".csv")
        status = "PASS" if audit["all_valid"] else f"FAIL ({len(audit['issues'])} issues)"
        issues_str = "; ".join(audit["issues"])

        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=csv_target.parent, delete=False, suffix=".tmp"
        ) as tmp:
            writer = csv.DictWriter(tmp, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            for lc in audit["line_checks"]:
                writer.writerow(
                    {
                        "invoice_number": invoice.invoice_number.strip(),
                        "filename": stored_filename,
                        "image_path": image_path,
                        "vendor_name": invoice.vendor_name,
                        "vendor_address": invoice.vendor_address or "",
                        "vendor_gst_reg_no": invoice.vendor_gst_reg_no or "",
                        "invoice_date": invoice.invoice_date or "",
                        "payment_terms": invoice.payment_terms or "",
                        "currency": invoice.currency,
                        "line_no": lc["line_no"],
                        "item_description": lc["description"],
                        "ocr_quantity": lc["ocr_qty"],
                        "ocr_unit_price": lc["ocr_unit_price"],
                        "ocr_line_total": lc["ocr_line_total"],
                        "calc_line_total": lc["calc_line_total"],
                        "line_diff": lc["line_diff"],
                        "line_valid": lc["is_valid"],
                        "ocr_subtotal": audit["ocr_subtotal"],
                        "calc_subtotal": audit["calc_subtotal"],
                        "subtotal_diff": audit["subtotal_diff"],
                        "gst_rate_percent": invoice.gst_rate_percent,
                        "ocr_gst_amount": audit["ocr_gst"],
                        "calc_gst_amount": audit["calc_gst"],
                        "gst_diff": audit["gst_diff"],
                        "ocr_grand_total": audit["ocr_grand_total"],
                        "calc_grand_total": audit["calc_grand_total"],
                        "grand_total_diff": audit["grand_total_diff"],
                        "audit_status": status,
                        "audit_issues": issues_str,
                    }
                )
            tmp_path = tmp.name

        os.replace(tmp_path, csv_target)
        return f"data/images/supplier_invoices/{csv_target.name}"

    def export_master_csv_bytes(self) -> bytes:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.invoice_number, i.filename, i.image_path,
                    i.vendor_name, i.vendor_address, i.vendor_gst_reg_no,
                    i.invoice_date, i.payment_terms, i.currency,
                    l.line_no, l.description AS item_description,
                    l.ocr_quantity, l.ocr_unit_price,
                    l.ocr_line_total, l.calc_line_total, l.line_diff, l.is_valid AS line_valid,
                    i.ocr_subtotal, i.calc_subtotal, i.subtotal_diff,
                    i.gst_rate_percent, i.ocr_gst_amount, i.calc_gst_amount, i.gst_diff,
                    i.ocr_grand_total, i.calc_grand_total, i.grand_total_diff,
                    i.audit_status, i.audit_issues
                FROM supplier_invoices i
                LEFT JOIN supplier_invoice_items l ON i.file_hash = l.file_hash
                ORDER BY i.invoice_date DESC, i.invoice_number ASC, l.line_no ASC
                """
            ).fetchall()

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
        return buf.getvalue().encode("utf-8")

    def sync_master_csv_in_image_folder(self) -> str:
        """Atomically writes data/images/supplier_invoices/supplier_invoices_audited.csv."""
        csv_bytes = self.export_master_csv_bytes()
        target = self.cfg.audited_csv_path
        target.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, delete=False, suffix=".tmp"
        ) as tmp:
            tmp.write(csv_bytes)
            tmp_path = tmp.name

        os.replace(tmp_path, target)
        return "data/images/supplier_invoices/supplier_invoices_audited.csv"

    def log_chat(
        self, branch_code: str | None, redacted_q: str, redacted_a: str, tools_used: str, cost_usd: float
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_logs (
                    branch_code, redacted_question, redacted_answer, tools_used, cost_usd
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (branch_code.upper() if branch_code is not None else None,
                 redacted_q, redacted_a, tools_used, cost_usd),
            )

    def get_all_invoices(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_invoices ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_invoice_by_hash(self, file_hash: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM supplier_invoices WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            if row is None:
                raise ValueError("The local invoice no longer exists.")
            return dict(row)

    def get_line_items(self, file_hash: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_invoice_items WHERE file_hash = ? ORDER BY line_no",
                (file_hash,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_invoice(self, invoice_number: str, branch_code: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT file_hash, invoice_number, branch_code, vendor_name, invoice_date,
                       currency, ocr_subtotal, ocr_gst_amount, ocr_grand_total,
                       audit_status, audit_issues
                FROM supplier_invoices
                WHERE invoice_number = ? COLLATE NOCASE
                  AND (? = 'HQ' OR branch_code = ? COLLATE NOCASE)
                """,
                (invoice_number, branch_code.upper(), branch_code),
            ).fetchone()
            if row is None:
                return None
            invoice = dict(row)
            invoice["items"] = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT line_no, description, ocr_quantity, ocr_unit_price,
                           ocr_line_total, is_valid
                    FROM supplier_invoice_items
                    WHERE file_hash = ? ORDER BY line_no
                    """,
                    (invoice["file_hash"],),
                )
            ]
            return invoice

    def get_price_observations(self, year: int, branch_code: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.invoice_number, i.invoice_date, i.vendor_name, i.currency,
                       i.is_gst_inclusive, i.audit_status, l.line_no, l.description,
                       l.ocr_unit_price, l.is_valid
                FROM supplier_invoices i
                JOIN supplier_invoice_items l ON l.file_hash = i.file_hash
                WHERE i.invoice_date >= ? AND i.invoice_date < ?
                  AND (? = 'HQ' OR i.branch_code = ? COLLATE NOCASE)
                ORDER BY i.invoice_date, i.invoice_number, l.line_no
                """,
                (f"{year:04d}-01-01", f"{year + 1:04d}-01-01",
                 branch_code.upper(), branch_code),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_invoice_summaries(self, branch_code: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT invoice_number, branch_code, vendor_name, invoice_date, currency,
                       ocr_grand_total, audit_status
                FROM supplier_invoices
                WHERE (? = 'HQ' OR branch_code = ? COLLATE NOCASE)
                ORDER BY invoice_date, invoice_number
                """,
                (branch_code.upper(), branch_code),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_analytics(self) -> dict:
        with self._connect() as conn:
            kpis = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_invoices,
                    COALESCE(SUM(ocr_grand_total), 0) AS ocr_spend,
                    COALESCE(SUM(calc_grand_total), 0) AS calc_spend,
                    COALESCE(SUM(ocr_gst_amount), 0) AS ocr_gst,
                    COALESCE(SUM(calc_gst_amount), 0) AS calc_gst,
                    SUM(CASE WHEN audit_status = 'PASS' THEN 1 ELSE 0 END) AS passed,
                    SUM(CASE WHEN audit_status != 'PASS' THEN 1 ELSE 0 END) AS failed
                FROM supplier_invoices
                """
            ).fetchone()

            by_vendor = conn.execute(
                """
                SELECT
                    vendor_name,
                    vendor_gst_reg_no,
                    COUNT(*) AS invoices,
                    ROUND(SUM(ocr_grand_total), 2) AS ocr_spend,
                    ROUND(SUM(calc_grand_total), 2) AS calc_spend,
                    ROUND(SUM(grand_total_diff), 2) AS variance
                FROM supplier_invoices
                GROUP BY vendor_name, vendor_gst_reg_no
                ORDER BY ocr_spend DESC
                """
            ).fetchall()

            return {"kpis": dict(kpis), "by_vendor": [dict(r) for r in by_vendor]}