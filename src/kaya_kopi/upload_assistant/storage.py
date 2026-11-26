"""Shared Kaya storage, original-file provenance and read-only SQL execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pymysql
import sqlglot
from dotenv import load_dotenv
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from kaya_kopi.paths import project_root
from kaya_kopi.repositories.mysql import DatabaseConfig, MySQLRepository, _json_safe
from kaya_kopi.security.guardrails import protect_answer, protect_text
from kaya_kopi.upload_assistant.invoice_audit import audit_invoice, db_flag


@dataclass(frozen=True)
class Settings:
    root: Path
    database: str = DatabaseConfig.database
    collection: str = "kaya_kopi_knowledge"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(project_root() / ".env")
        database = DatabaseConfig.from_env().database
        collection = os.getenv("CHROMA_COLLECTION", "kaya_kopi_knowledge")
        return cls(project_root() / "data" / "upload_assistant", database, collection)


CLIENT_TABLE_GUIDANCE = {
    "outlets": {
        "purpose": "Client outlet master: canonical outlet codes, names, outlet types and opening dates.",
        "rules": "Join pos_transactions.outlet_code to outlets.outlet_code, not outlet_id. Use outlet_alias and its dated evidence when enabled to resolve legacy/re-registered codes; outlet master alone does not establish rename history.",
    },
    "pos_transactions": {
        "purpose": "Client partial POS export: item sales, quantities, observed prices, payment modes and transaction timestamps.",
        "rules": "Filter tx_time for the requested period. Exact duplicate export rows exist: deduplicate all transaction fields before aggregating. Join item_id to menu_items.item_id and outlet_code to outlets.outlet_code when enabled. POS observations are partial, not complete outlet revenue, supplier expense or profit; disclose coverage and do not invent missing costs.",
    },
    "supplier": {
        "purpose": "Canonical supplier master: supplier identifiers, recorded canonical names and validation state.",
        "rules": "Use for supplier identity and validated master records. A supplier master row is not a purchase, invoice, payment or spending total. Obtain financial evidence from enabled invoice/purchase tables, not invented supplier amount columns.",
    },
    "menu_items": {
        "purpose": "Client menu master: item names, categories and listed base prices.",
        "rules": "Join item_id to pos_transactions.item_id when enabled. base_price is a master/list price, not proof of the price charged at a historical date. Use POS unit_price for observed prices; menu prices alone cannot establish margin.",
    },
}


UPLOAD_SCHEMA = {
    "uploaded_invoices": {
        "purpose": "One OCR supplier invoice per file; purchases, not proof of payment or all company expenses.",
        "columns": "file_hash, invoice_number, invoice_date, vendor_name, outlet_name, currency, subtotal, gst_amount, total_amount, review_notes, line_items_json, source_file",
        "rules": "Filter invoice_date for the period. Do not mix currencies. Include source_file in results; retrieve contributing source_file values separately for aggregates. Check review_notes and uploaded_invoice_audits; totals are OCR observations, not audited amounts.",
    },
    "uploaded_invoice_lines": {
        "purpose": "One OCR-extracted item line per uploaded supplier invoice, stored as individual rows for item-level analysis.",
        "columns": "id, file_hash, line_no, description, quantity, unit_price, line_amount, expected_line_amount, line_amount_diff, check_status, source_file",
        "rules": "Join to uploaded_invoices on file_hash. check_status is MATCH, MISMATCH or UNVERIFIED, comparing printed line_amount with quantity x unit_price within 0.02. NULL means missing, never zero. Include source_file and do not mix currencies.",
    },
    "uploaded_invoice_audits": {
        "purpose": "Arithmetic audit of each uploaded invoice against its printed lines, subtotal, GST and total. Not a tax or payment audit.",
        "columns": "file_hash, subtotal_match, gst_match, total_match, expected_subtotal, expected_gst, expected_total, subtotal_diff, gst_diff, total_diff, audit_status, audit_notes, source_file",
        "rules": "audit_status is PASS, REVIEW or UNVERIFIED. A NULL match flag means the check could not be computed; it is not a pass. Read audit_notes for the exact printed-versus-calculated differences. Join to uploaded_invoices on file_hash and include source_file.",
    },
    "uploaded_outlet_csvs": {
        "purpose": "One normalized daily outlet CSV row; NULL means missing, never zero.",
        "columns": "id, file_hash, outlet_name, report_date, cups_sold, revenue_sgd, reported_volume, volume_measure, wastage_kg, weather, milk_wastage_liters, pastry_wastage_pcs, notes, source_row_number, source_file",
        "rules": "Parsed by Python using the client CSV schemas. Filter report_date; group by outlet_name and period. Include source_file and source_row_number for citations. Client daily-summary volume is receipts, not verified cups; read reported_volume with volume_measure, never substitute it for cups_sold. wastage_kg is not liters. Invalid values remain null with details in notes.",
    },
    "uploaded_roster_rows": {
        "purpose": "One nonempty source row from a roster Excel worksheet, normalized with its source location. These are scheduled assignments, not actual attendance.",
        "columns": "id, file_hash, sheet_name, source_row_number, roster_month, shift_date, outlet_name, staff_key, role, shift_window, paid_hours, mon_shift, tue_shift, wed_shift, thu_shift, fri_shift, sat_shift, sun_shift, raw_hourly_rate, hourly_rate_sgd, rate_status, notes, source_file",
        "rules": "Filter roster_month or shift_date as appropriate. mon_shift through sun_shift retain the actual weekday cells; OFF is explicitly not scheduled, NULL is missing. Do not invent dates or infer paid hours from shift codes without an approved rule. hourly_rate_sgd is the parsed rate; raw_hourly_rate preserves the cell and rate_status identifies CONFIRMED, CLIENT_APPROVED_DEFAULT, MISSING or REVIEW_REQUIRED. TBC uses the existing client-approved SGD 13.00 default. Rates are confidential: do not disclose individual pay. For headcount COUNT(DISTINCT staff_key) within the same outlet and month, not SUM of rows across shifts or roles. staff_key is a file-local pseudonym: it cannot establish unique staff across different uploads. Include source_file, sheet_name, source_row_number.",
    },
}


ROSTER_SOURCE_COLUMNS = {
    **{f"{day}_shift": "VARCHAR(255) NULL" for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
    "raw_hourly_rate": "VARCHAR(255) NULL",
    "hourly_rate_sgd": "DECIMAL(12,2) NULL",
    "rate_status": "VARCHAR(30) NULL",
}


CSV_SOURCE_COLUMNS = {
    "reported_volume": "INT NULL", "volume_measure": "VARCHAR(30) NULL",
    "wastage_kg": "DECIMAL(12,3) NULL", "source_row_number": "INT NULL",
}


def _load_line_items(raw: Any) -> list[dict]:
    """Read the legacy line_items_json column without trusting its contents."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def select_sql(sql: str, tables: set[str], dialect: str) -> str:
    if len(sql) > 20000:
        raise ValueError("SQL exceeds 20,000 characters.")
    try:
        statements = sqlglot.parse(sql, read=dialect)
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Only one read-only SELECT/CTE query is allowed.")
        query = statements[0]
        for node in query.walk():
            if type(node).__name__ in {"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Into", "Lock", "Command"}:
                raise ValueError("SQL writes and locks are not allowed.")
            if isinstance(node, exp.Func) and node.sql_name().upper() in {
                "ANONYMOUS", "LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK",
            }:
                raise ValueError("Custom and server-side functions are not allowed.")
        used = set()
        for scope in traverse_scope(query):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    if source.db or source.catalog or source.name not in tables:
                        raise ValueError("Use only the unqualified tables in the supplied schema.")
                    used.add(source.name)
        if not used:
            raise ValueError("Read at least one enabled table.")
        return query.sql(dialect=dialect)
    except SqlglotError as error:
        raise ValueError("Invalid SQL; correct it using the supplied schema.") from error


def upload_subdirectory(kind: str, name: str = "") -> str:
    folders = {
        "invoices": "images/supplier_invoices", "csvs": "tabular", "rosters": "tabular",
        "texts": "docs/emails" if name.lower().endswith(".eml") else "docs/whatsapp_exports",
        "emails": "docs/emails",
    }
    if kind not in folders:
        raise ValueError("Unknown upload type.")
    return folders[kind]


class UploadStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = settings.root / "manifest.sqlite3"
        with self.manifest() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS uploaded_files (
                file_hash TEXT PRIMARY KEY, source_file TEXT NOT NULL, kind TEXT NOT NULL,
                original_name TEXT NOT NULL, record_count INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS upload_conversations (
                thread_id TEXT PRIMARY KEY, name TEXT NOT NULL, messages_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL)""")

    @contextmanager
    def manifest(self):
        connection = sqlite3.connect(self.manifest_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def repository(self) -> MySQLRepository:
        cfg = DatabaseConfig.from_env()
        return MySQLRepository(DatabaseConfig(cfg.host, cfg.port, cfg.user, cfg.password, self.settings.database))

    def init_mysql(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS uploaded_invoices (
                file_hash CHAR(64) PRIMARY KEY, invoice_number VARCHAR(200), invoice_date DATE,
                vendor_name VARCHAR(255), outlet_name VARCHAR(120), currency VARCHAR(10),
                subtotal DECIMAL(14,2), gst_amount DECIMAL(14,2), total_amount DECIMAL(14,2),
                review_notes TEXT, line_items_json JSON, source_file VARCHAR(700) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS uploaded_invoice_lines (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, file_hash CHAR(64) NOT NULL,
                line_no INT NOT NULL, description VARCHAR(500) NOT NULL,
                quantity DECIMAL(14,4) NULL, unit_price DECIMAL(14,4) NULL,
                line_amount DECIMAL(14,2) NULL, expected_line_amount DECIMAL(14,2) NULL,
                line_amount_diff DECIMAL(14,2) NULL, check_status VARCHAR(20) NOT NULL,
                source_file VARCHAR(700) NOT NULL, INDEX (file_hash))""",
            """CREATE TABLE IF NOT EXISTS uploaded_invoice_audits (
                file_hash CHAR(64) PRIMARY KEY, subtotal_match TINYINT(1) NULL,
                gst_match TINYINT(1) NULL, total_match TINYINT(1) NULL,
                expected_subtotal DECIMAL(14,2) NULL, expected_gst DECIMAL(14,2) NULL,
                expected_total DECIMAL(14,2) NULL, subtotal_diff DECIMAL(14,2) NULL,
                gst_diff DECIMAL(14,2) NULL, total_diff DECIMAL(14,2) NULL,
                audit_status VARCHAR(20) NOT NULL, audit_notes TEXT NOT NULL,
                source_file VARCHAR(700) NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS uploaded_outlet_csvs (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, file_hash CHAR(64) NOT NULL,
                outlet_name VARCHAR(120), report_date DATE, cups_sold INT, revenue_sgd DECIMAL(14,2),
                weather VARCHAR(255), milk_wastage_liters DECIMAL(12,3), pastry_wastage_pcs INT,
                reported_volume INT, volume_measure VARCHAR(30), wastage_kg DECIMAL(12,3), source_row_number INT,
                notes TEXT, source_file VARCHAR(700) NOT NULL, INDEX (file_hash))""",
            """CREATE TABLE IF NOT EXISTS uploaded_roster_rows (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, file_hash CHAR(64) NOT NULL,
                sheet_name VARCHAR(255) NOT NULL, source_row_number INT NOT NULL,
                roster_month DATE, shift_date DATE, outlet_name VARCHAR(120), staff_key VARCHAR(64),
                role VARCHAR(120), shift_window VARCHAR(255), paid_hours DECIMAL(8,2), notes TEXT,
                mon_shift VARCHAR(255), tue_shift VARCHAR(255), wed_shift VARCHAR(255),
                thu_shift VARCHAR(255), fri_shift VARCHAR(255), sat_shift VARCHAR(255), sun_shift VARCHAR(255),
                raw_hourly_rate VARCHAR(255), hourly_rate_sgd DECIMAL(12,2), rate_status VARCHAR(30),
                source_file VARCHAR(700) NOT NULL, INDEX (file_hash))""",
        ]
        with self.repository()._connect() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                for table, additions in (("uploaded_roster_rows", ROSTER_SOURCE_COLUMNS), ("uploaded_outlet_csvs", CSV_SOURCE_COLUMNS)):
                    cursor.execute(f"SHOW COLUMNS FROM {table}")
                    columns = cast(list[dict[str, Any]], cursor.fetchall())
                    existing = {row["Field"] for row in columns}
                    for column, definition in additions.items():
                        if column not in existing:
                            try:
                                cursor.execute(f"ALTER TABLE {table} ADD COLUMN `{column}` {definition}")
                            except pymysql.OperationalError as error:
                                if not error.args or error.args[0] != 1060:
                                    raise
                self._backfill_invoice_details(cursor)

    def _backfill_invoice_details(self, cursor: Any) -> None:
        """Populate normalized lines/audits for invoices uploaded before this schema."""
        cursor.execute("""SELECT i.file_hash, i.source_file, i.subtotal, i.gst_amount,
                                 i.total_amount, i.line_items_json
                          FROM uploaded_invoices i
                          LEFT JOIN uploaded_invoice_audits a ON a.file_hash = i.file_hash
                          WHERE a.file_hash IS NULL""")
        rows = cast(list[dict[str, Any]], cursor.fetchall())
        for row in rows:
            if not row.get("file_hash"):
                continue
            lines = _load_line_items(row.get("line_items_json"))
            audit = audit_invoice({
                "subtotal": row.get("subtotal"),
                "gst_amount": row.get("gst_amount"),
                "total_amount": row.get("total_amount"),
                "gst_rate_percent": None,
                "is_gst_inclusive": None,
                "line_items": lines,
            })
            cursor.execute("DELETE FROM uploaded_invoice_lines WHERE file_hash=%s", (row["file_hash"],))
            if lines:
                cursor.executemany("""INSERT INTO uploaded_invoice_lines
                    (file_hash,line_no,description,quantity,unit_price,line_amount,
                     expected_line_amount,line_amount_diff,check_status,source_file)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", [
                    (row["file_hash"], line["line_no"], line["description"], line["quantity"],
                     line["unit_price"], line["line_amount"], line["expected_line_amount"],
                     line["line_amount_diff"], line["check_status"], row["source_file"])
                    for line in audit["line_checks"]
                ])
            cursor.execute("""REPLACE INTO uploaded_invoice_audits
                (file_hash,subtotal_match,gst_match,total_match,expected_subtotal,
                 expected_gst,expected_total,subtotal_diff,gst_diff,total_diff,
                 audit_status,audit_notes,source_file)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                row["file_hash"], db_flag(audit["subtotal_match"]), db_flag(audit["gst_match"]),
                db_flag(audit["total_match"]), audit["expected_subtotal"], audit["expected_gst"],
                audit["expected_total"], audit["subtotal_diff"], audit["gst_diff"],
                audit["total_diff"], audit["status"], audit["notes_text"], row["source_file"],
            ))

    def existing(self, digest: str) -> dict | None:
        with self.manifest() as connection:
            row = connection.execute("SELECT * FROM uploaded_files WHERE file_hash=?", (digest,)).fetchone()
            return dict(row) if row else None

    def archive(self, name: str, content: bytes, kind: str) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:160] or "upload"
        folder = project_root() / "data" / upload_subdirectory(kind, name)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            target = folder / f"{digest}_{safe_name}"
        if not target.exists():
            with target.open("xb") as output:
                output.write(content)
        return digest, target.relative_to(project_root()).as_posix()

    def record(self, digest: str, source: str, name: str, kind: str, count: int) -> None:
        with self.manifest() as connection:
            connection.execute("INSERT OR REPLACE INTO uploaded_files (file_hash,source_file,kind,original_name,record_count) VALUES (?,?,?,?,?)", (digest, source, kind, Path(name).name, count))

    def files(self) -> list[dict]:
        with self.manifest() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM uploaded_files ORDER BY created_at DESC")]

    def save_conversation(self, thread_id: str, messages: list[dict]) -> None:
        protected = []
        for message in messages:
            if message.get("role") not in {"user", "assistant"}:
                continue
            content = str(message.get("content", ""))
            protected.append({
                **message,
                "content": protect_text(content) if message["role"] == "user" else protect_answer(content),
            })
        name = next((message["content"] for message in protected if message["role"] == "user"), "New conversation")
        name = name.replace("\n", " ").strip()[:60] or "New conversation"
        with self.manifest() as connection:
            connection.execute("""INSERT INTO upload_conversations (thread_id,name,messages_json,updated_at)
                VALUES (?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET
                messages_json=excluded.messages_json,updated_at=excluded.updated_at""",
                (thread_id, name, json.dumps(protected), time.time_ns()))

    def list_conversations(self) -> list[dict]:
        with self.manifest() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT thread_id,name FROM upload_conversations ORDER BY updated_at DESC")]

    def load_conversation(self, thread_id: str) -> list[dict] | None:
        with self.manifest() as connection:
            row = connection.execute("SELECT messages_json FROM upload_conversations WHERE thread_id=?", (thread_id,)).fetchone()
        return json.loads(row["messages_json"]) if row else None

    def rename_conversation(self, thread_id: str, name: str) -> None:
        name = protect_text(name.strip())
        if not name or len(name) > 80:
            raise ValueError("Conversation name must contain 1-80 characters.")
        with self.manifest() as connection:
            result = connection.execute("UPDATE upload_conversations SET name=? WHERE thread_id=?", (name, thread_id))
            if result.rowcount != 1:
                raise ValueError("Conversation no longer exists.")

    def delete_conversation(self, thread_id: str) -> None:
        with self.manifest() as connection:
            connection.execute("DELETE FROM upload_conversations WHERE thread_id=?", (thread_id,))

    def query(self, sql: str) -> dict[str, Any]:
        safe = select_sql(sql, set(UPLOAD_SCHEMA), "mysql")
        with self.repository()._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
                cursor.execute("START TRANSACTION READ ONLY")
                try:
                    cursor.execute(f"SELECT * FROM ({safe}) AS upload_results LIMIT 101")
                    rows = list(cursor.fetchmany(101))
                finally:
                    connection.rollback()
        return {"ok": True, "rows": _json_safe(rows[:100]), "truncated": len(rows) > 100}


def citation_path(source: str) -> Path | None:
    if not isinstance(source, str) or not source or len(source) > 1000 or "\n" in source:
        return None
    root = project_root().resolve()
    path = Path(source.split("#", 1)[0])
    try:
        path = (path if path.is_absolute() else root / path).resolve()
        if path.is_relative_to(root / "data") and path.is_file():
            return path
    except (OSError, ValueError):
        pass
    return None


def collect_sources(value: Any) -> list[str]:
    sources = set()

    def visit(item):
        if isinstance(item, dict):
            for key, entry in item.items():
                if key in {"source_file", "source_uri", "image_path", "source_path", "file_path"} and isinstance(entry, str):
                    path = citation_path(entry)
                    if path:
                        sources.add(path.relative_to(project_root()).as_posix())
                elif isinstance(entry, (dict, list)):
                    visit(entry)
        elif isinstance(item, list):
            for entry in item:
                visit(entry)

    visit(value)
    return sorted(sources)