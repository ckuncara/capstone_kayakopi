"""Unit tests for the copilot's allowlisted direct MySQL retrieval tools."""

from unittest.mock import Mock

from kaya_kopi.copilot import chat_backend
from kaya_kopi.copilot.tools import mysql_direct
from kaya_kopi.copilot.tools.direct_access import citation_note, file_citations
from kaya_kopi.upload_assistant.storage import CLIENT_TABLE_GUIDANCE, UPLOAD_SCHEMA


def test_discover_mysql_schema_separates_client_and_uploaded_tables(monkeypatch):
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "pos_transactions")
    repository = Mock()
    rows = [{"TABLE_NAME": "pos_transactions", "COLUMN_NAME": "qty"}]
    for name, details in UPLOAD_SCHEMA.items():
        first_column = details["columns"].split(", ")[0].split(" ")[0]
        rows.append({"TABLE_NAME": name, "COLUMN_NAME": first_column})
    repository._query.return_value = rows
    monkeypatch.setattr(mysql_direct, "MySQLRepository", Mock(return_value=repository))

    result = mysql_direct.discover_mysql_schema.invoke({})

    assert result["ok"] is True
    assert result["tables"] == sorted({"pos_transactions", *UPLOAD_SCHEMA})
    assert result["columns"]["uploaded_invoice_lines"][0] == "id"
    assert set(result["upload_schema"]) == set(UPLOAD_SCHEMA)
    assert "purpose" in result["upload_schema"]["uploaded_invoice_lines"]
    assert "pos_transactions" in result["client_guidance"]
    assert "uploaded_invoice_lines" not in result["client_guidance"]


def test_query_mysql_direct_rejects_tables_outside_the_allowlist(monkeypatch):
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "pos_transactions")

    result = mysql_direct.query_mysql_direct.invoke({
        "sql": "SELECT * FROM purchase_invoice_reconciliation",
    })

    assert result["ok"] is False
    assert "not permitted" in result["error"]


def test_query_mysql_direct_enables_upload_tables_even_with_stale_env(monkeypatch):
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "")
    called = {}

    def fake_execute(database, sql, sqlite_path, limit, extra=()):
        called.update(database=database, sql=sql, limit=limit, extra=set(extra))
        return {
            "ok": True,
            "database": database,
            "rows": [{"description": "Tea dust"}],
            "citations": ["data/images/supplier_invoices/example.jpg"],
            "citation_note": "Cite the returned data paths as Markdown file links.",
        }

    monkeypatch.setattr(mysql_direct, "execute_query", fake_execute)

    result = mysql_direct.query_mysql_direct.invoke({
        "sql": "SELECT description, source_file FROM uploaded_invoice_lines",
        "limit": 50,
    })

    assert result["ok"] is True
    assert called["database"] == "mysql"
    assert called["limit"] == 50
    assert called["extra"] == mysql_direct.DIRECT_TABLES
    assert result["citations"] == ["data/images/supplier_invoices/example.jpg"]


def test_query_mysql_direct_enables_client_line_items_even_with_stale_env(monkeypatch):
    monkeypatch.setenv("KAYA_LLM_MYSQL_TABLES", "pos_transactions")
    called = {}

    def fake_execute(database, sql, sqlite_path, limit, extra=()):
        called["extra"] = set(extra)
        return {"ok": True, "rows": [], "citations": [], "citation_note": ""}

    monkeypatch.setattr(mysql_direct, "execute_query", fake_execute)

    result = mysql_direct.query_mysql_direct.invoke({
        "sql": "SELECT amount FROM supplier_invoice_item",
    })

    assert result["ok"] is True
    assert "supplier_invoice_item" in called["extra"]


def test_citation_note_distinguishes_client_from_uploaded_tables():
    client = citation_note({"pos_transactions"}, [])
    uploaded = citation_note({"uploaded_invoice_lines"}, [])
    with_file = citation_note(
        {"uploaded_invoice_lines"}, ["data/images/supplier_invoices/example.jpg"]
    )

    assert "Client database result" in client
    assert "not as an uploaded document" in client
    assert "No original file provenance" not in client
    assert "uploaded original-file provenance" in uploaded
    assert "Do not invent filenames" in uploaded
    assert "Original-file references were returned" in with_file


def test_direct_database_request_gets_priority_hint():
    message = chat_backend._current_system_message(
        "Compare all outlet sales in 2026 directly from database?"
    )

    assert "explicitly requested direct database retrieval" in message.content
    assert "before any specialist tool" in message.content


def test_item_purchase_question_gets_priority_hint():
    message = chat_backend._current_system_message(
        "how much teh we purchase in 2025?"
    )

    assert "explicitly requested direct database retrieval" in message.content
    assert "before any specialist tool" in message.content


def test_supplier_item_guidance_explains_teh_and_item_totals():
    assert "supplier_invoice_item" in CLIENT_TABLE_GUIDANCE
    assert "Tea dust" in CLIENT_TABLE_GUIDANCE["supplier_invoice_item"]["rules"]
    assert "item-level purchase totals" in chat_backend.SYSTEM_PROMPT
    assert "analysis=\"spend\"" in chat_backend.SYSTEM_PROMPT


def test_file_citations_recognize_uploaded_source_file():
    source = "data/tabular/daily_summary_AMK-01.csv"
    assert file_citations([{"source_file": source}]) == [source]


def test_file_citations_parse_group_concat_source_uris():
    first = "data/images/supplier_invoices/INV-012508-224.jpg"
    second = "data/images/supplier_invoices/INV-012512-804.jpg"
    assert file_citations([{"source_uris": f"{first}, {second}"}]) == [first, second]


def test_system_prompt_requires_schema_discovery_before_direct_sql():
    assert "discover_mysql_schema" in chat_backend.SYSTEM_PROMPT
    assert "query_mysql_direct" in chat_backend.SYSTEM_PROMPT
    assert "uploaded_* tables" in chat_backend.SYSTEM_PROMPT
