"""Allowlisted access helpers for the Upload Desk's existing-data tools.

The main copilot tool module no longer exposes ``discover_database_schema`` or
``query_database``. Keeping small equivalents here avoids changing that shared
module while preserving the ``.invoke({...})`` interface used by UploadAgent.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pymysql

from kaya_kopi.copilot.tools.direct_access import allowed_tables, execute_query
from kaya_kopi.paths import project_root
from kaya_kopi.repositories.mysql import MySQLRepository
from kaya_kopi.security.guardrails import protect_text


class _InvokableTool:
    """Minimal LangChain-like tool wrapper for ``.invoke(dict)`` callers."""

    def __init__(self, function: Callable[..., dict[str, Any]]) -> None:
        self._function = function

    def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        return self._function(**arguments)


def _sqlite_path() -> Path:
    return Path(os.getenv("LOCAL_SQLITE_PATH", str(project_root() / "data" / "kaya_ops.db")))


def _mysql_schema() -> dict[str, list[str]]:
    tables = sorted(allowed_tables("mysql"))
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


def _sqlite_schema() -> dict[str, list[str]]:
    tables = sorted(allowed_tables("sqlite"))
    path = _sqlite_path()
    if not tables or not path.is_file():
        return {}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        schema: dict[str, list[str]] = {}
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            if columns:
                schema[table] = columns
        return schema
    finally:
        connection.close()


def _discover_database_schema(database: str) -> dict[str, Any]:
    if database not in {"mysql", "sqlite"}:
        return {"ok": False, "error": "Database must be mysql or sqlite."}
    try:
        schema = _mysql_schema() if database == "mysql" else _sqlite_schema()
    except (ValueError, pymysql.MySQLError, sqlite3.Error, OSError) as error:
        return {"ok": False, "error": protect_text(str(error))}
    return {"ok": True, "database": database, "schema": schema}


def _query_database(database: str, sql: str, limit: int = 100) -> dict[str, Any]:
    if database not in {"mysql", "sqlite"}:
        return {"ok": False, "error": "Database must be mysql or sqlite."}
    try:
        return execute_query(database, sql, _sqlite_path(), int(limit))
    except (ValueError, pymysql.MySQLError, sqlite3.Error, OSError) as error:
        return {"ok": False, "error": protect_text(str(error))}


discover_database_schema = _InvokableTool(_discover_database_schema)
query_database = _InvokableTool(_query_database)
