"""Allowlisted, read-only execution for model-generated database queries."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from kaya_kopi.paths import project_root
from kaya_kopi.repositories.mysql import MySQLRepository, _json_safe


def allowed_tables(database: str, extra: Iterable[str] = ()) -> set[str]:
    value = os.getenv(f"KAYA_LLM_{database.upper()}_TABLES", "")
    names = {name.strip() for name in value.split(",") if name.strip()}
    names.update(str(name).strip() for name in extra if str(name).strip())
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
        raise ValueError("Table allowlists must contain comma-separated, unqualified table names.")
    return names


def validate_query(sql: str, database: str, extra: Iterable[str] = ()) -> tuple[exp.Query, set[str]]:
    allowed = allowed_tables(database, extra)
    if not allowed:
        raise ValueError(f"No {database} tables are enabled in the environment allowlist.")
    if len(sql) > 20000:
        raise ValueError("Query is too long.")
    try:
        statements = sqlglot.parse(sql, read=database)
    except sqlglot.errors.SqlglotError as error:
        raise ValueError("Invalid SQL; inspect the enabled schema and retry.") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Only one SELECT query (including read-only CTEs) is permitted.")
    query = statements[0]
    forbidden = {"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Into", "Lock", "Command"}
    for node in query.walk():
        if type(node).__name__ in forbidden:
            raise ValueError("Only read-only SELECT queries are permitted.")
        if isinstance(node, exp.Func) and node.sql_name().upper() in {
            "ANONYMOUS", "LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK",
        }:
            raise ValueError("Custom or server-side functions are not permitted.")
    tables = set()
    for scope in traverse_scope(query):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                if source.db or source.catalog or source.name not in allowed:
                    raise ValueError(f"Access to table {source.sql()} is not permitted.")
                tables.add(source.name)
    if not tables:
        raise ValueError("The query must read an enabled table.")
    return query, tables


class RestrictedMySQLRepository(MySQLRepository):
    def _query(self, sql: str, params=()) -> list[dict[str, Any]]:
        if os.getenv("KAYA_LLM_MYSQL_TABLES") is not None:
            validate_query(sql.replace("%s", "?"), "mysql")
        return super()._query(sql, params)


def file_citations(rows: list[dict[str, Any]]) -> list[str]:
    root = project_root().resolve()
    data = (root / "data").resolve()
    citation_keys = {
        "source_uri", "source_uris", "source_file", "source_files",
        "image_path", "source_path", "file_path",
    }
    citations = set()
    for row in rows:
        for key, value in row.items():
            if key not in citation_keys or not isinstance(value, str):
                continue
            for candidate in value.split(","):
                candidate = candidate.strip()
                if not candidate:
                    continue
                path = Path(candidate.split("#", 1)[0])
                path = (path if path.is_absolute() else root / path).resolve()
                if path.is_relative_to(data) and path.is_file():
                    citations.add(path.relative_to(root).as_posix())
    return sorted(citations)


def citation_note(tables: set[str], citations: list[str]) -> str:
    """Explain provenance without treating client tables as uploaded files."""
    if citations:
        return (
            "Original-file references were returned. The application renders download and "
            "preview controls; use the returned filenames in text, not local paths."
        )
    if any(table.startswith("uploaded_") for table in tables):
        return (
            "No uploaded original-file provenance was returned. Query the same uploaded_* rows "
            "with source_file before describing a file. Do not invent filenames or describe "
            "database tables as uploaded files."
        )
    return (
        "Client database result; no original uploaded file is required. Describe figures as a "
        "query result from the Kaya MySQL database, not as an uploaded document."
    )


def sqlite_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def execute_query(
    database: str, sql: str, sqlite_path: Path, limit: int,
    extra: Iterable[str] = (),
) -> dict[str, Any]:
    query, tables = validate_query(sql, database, extra)
    bounded = f"SELECT * FROM ({query.sql(dialect=database)}) AS kaya_result LIMIT {limit + 1}"
    if database == "sqlite":
        connection = sqlite_connection(sqlite_path)
        allowed = allowed_tables(database, extra)

        def authorize(action, table, column, schema, trigger):
            if action == sqlite3.SQLITE_READ and (schema != "main" or table not in allowed):
                return sqlite3.SQLITE_DENY
            if action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and column in {"load_extension", "readfile", "writefile"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        steps = 0

        def stop_expensive_query():
            nonlocal steps
            steps += 1
            return steps > 1000

        connection.set_progress_handler(stop_expensive_query, 1000)
        try:
            rows = [dict(row) for row in connection.execute(bounded).fetchall()]
        finally:
            connection.close()
    else:
        with MySQLRepository()._connect() as connection:
            with connection.cursor() as cursor:
                for table in tables:
                    cursor.execute(
                        "SELECT TABLE_TYPE FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,)
                    )
                    record = cursor.fetchone()
                    if not record or record["TABLE_TYPE"] != "BASE TABLE":
                        raise ValueError("Only existing base tables are permitted, not views.")
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
                cursor.execute("START TRANSACTION READ ONLY")
                try:
                    cursor.execute(bounded)
                    rows = list(cursor.fetchmany(limit + 1))
                finally:
                    connection.rollback()
    visible = _json_safe(rows[:limit])
    citations = file_citations(visible)
    return {
        "ok": True, "database": database, "tables": sorted(tables), "rows": visible,
        "truncated": len(rows) > limit, "citations": citations,
        "citation_note": citation_note(tables, citations),
    }
