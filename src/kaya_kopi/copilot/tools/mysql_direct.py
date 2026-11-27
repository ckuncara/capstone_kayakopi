"""Allowlisted direct MySQL retrieval for the Ops Copilot.

These tools expose the same read-only, schema-checked MySQL access used by Upload
Desk to the LangGraph tool loop. A curated client-table baseline is always enabled
so a stale shell ``KAYA_LLM_MYSQL_TABLES`` cannot hide invoice-line evidence.
Uploaded tables use their stored schema guidance so the model does not confuse
uploads with legacy invoices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymysql
from langchain_core.tools import tool

from kaya_kopi.copilot.tools.direct_access import allowed_tables, execute_query
from kaya_kopi.repositories.mysql import MySQLRepository
from kaya_kopi.security.guardrails import protect_text
from kaya_kopi.upload_assistant.storage import CLIENT_TABLE_GUIDANCE, UPLOAD_SCHEMA


UPLOAD_TABLES = frozenset(UPLOAD_SCHEMA)
CLIENT_TABLES = frozenset({
    "daily_outlet_summary",
    "menu_items",
    "outlet_alias",
    "outlets",
    "pos_transactions",
    "source_document",
    "staff_assignment",
    "staff_roster",
    "staff_shift",
    "supplier",
    "supplier_invoice",
    "supplier_invoice_item",
})
DIRECT_TABLES = CLIENT_TABLES | UPLOAD_TABLES


def _client_schema() -> dict[str, list[str]]:
    """Return columns for every enabled MySQL table, including uploads."""
    tables = sorted(allowed_tables("mysql", DIRECT_TABLES))
    if not tables:
        return {}
    placeholders = ", ".join(["%s"] * len(tables))
    rows = MySQLRepository()._query(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders}) "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        tuple(tables),
    )
    schema: dict[str, list[str]] = {}
    for row in rows:
        schema.setdefault(str(row["TABLE_NAME"]), []).append(str(row["COLUMN_NAME"]))
    return schema


@tool
def discover_mysql_schema() -> dict[str, Any]:
    """Discover enabled MySQL tables and their columns before writing a direct SQL query.

    Call this before query_mysql_direct whenever a specialist tool does not cover
    the question. The result separates client table guidance from Upload Desk
    tables and lists the exact columns that may be used.
    """
    try:
        schema = _client_schema()
        enabled = allowed_tables("mysql", DIRECT_TABLES)
    except (ValueError, pymysql.MySQLError, OSError) as error:
        return {"ok": False, "error": protect_text(str(error))}
    upload_schema = {
        name: details for name, details in UPLOAD_SCHEMA.items() if name in enabled
    }
    client_guidance = {
        name: details for name, details in CLIENT_TABLE_GUIDANCE.items() if name in schema
    }
    return {
        "ok": True,
        "database": "mysql",
        "tables": sorted(schema),
        "columns": schema,
        "upload_schema": upload_schema,
        "client_guidance": client_guidance,
        "limitations": [
            "Only the listed tables are enabled; do not qualify table names with a database.",
            "uploaded_* tables are Upload Desk records and must not be mixed with legacy invoices.",
            "NULL means missing, never zero. Return source_file or source_uri before citing a file.",
        ],
    }


@tool
def query_mysql_direct(sql: str, limit: int = 100) -> dict[str, Any]:
    """Run one read-only SELECT against enabled client and uploaded MySQL tables.

    Use exact tables and columns returned by discover_mysql_schema. Include
    source_file or source_uri for original-file citations, and never combine
    currencies or present purchases as payments, profit, or all expenses.
    """
    try:
        bounded = int(limit)
        if bounded < 1 or bounded > 200:
            return {"ok": False, "error": "Limit must be between 1 and 200."}
        return execute_query("mysql", sql, Path(), bounded, extra=DIRECT_TABLES)
    except (ValueError, pymysql.MySQLError, OSError) as error:
        return {"ok": False, "error": protect_text(str(error))}


DIRECT_MYSQL_TOOLS = [discover_mysql_schema, query_mysql_direct]
