import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from kaya_kopi.config import AppConfig
from kaya_kopi.repositories.harness_chat_store import HarnessChatStore
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


def test_threads_are_saved_redacted_renamed_and_reopened(tmp_path):
    repo = SQLiteOpsRepository(
        AppConfig(sqlite_db_path=tmp_path / "invoices.db", supplier_invoice_dir=tmp_path / "images")
    )
    store = HarnessChatStore(repo)
    history = [
        {"role": "user", "content": "Contact test@example.com"},
        {"role": "assistant", "content": "Here is the result", "retrieved": [({}, "test@example.com")]},
    ]
    messages = [
        HumanMessage(content="Contact test@example.com"),
        AIMessage(content="", tool_calls=[{"id": "call", "name": "lookup", "args": {}}]),
        ToolMessage(content="tool result", tool_call_id="call"),
        AIMessage(content="Here is the result"),
    ]
    store.save("first", history, messages, [{"name": "lookup", "result": "test@example.com"}])
    store.rename("first", "Supplier review")
    store.save("first", history, messages, [])
    store.save("second", [{"role": "user", "content": "Second"}], [], [])

    reopened = HarnessChatStore(repo)
    assert {thread["name"] for thread in reopened.list_threads()} == {"Supplier review", "Second"}
    loaded = reopened.load("first")
    assert loaded["name"] == "Supplier review"
    assert len(loaded["messages"]) == 2
    assert isinstance(loaded["messages"][0], HumanMessage)
    assert isinstance(loaded["messages"][1], AIMessage)
    with sqlite3.connect(repo.cfg.sqlite_db_path) as conn:
        values = conn.execute(
            "SELECT history_json, context_json FROM harness_chat_threads WHERE thread_id='first'"
        ).fetchone()
    assert all("test@example.com" not in value for value in values)
    with pytest.raises(ValueError, match="cannot be empty"):
        store.rename("first", "  ")
    with pytest.raises(ValueError, match="not found"):
        store.rename("missing", "Name")


def test_legacy_log_cutoff_does_not_include_new_logs(tmp_path):
    repo = SQLiteOpsRepository(
        AppConfig(sqlite_db_path=tmp_path / "invoices.db", supplier_invoice_dir=tmp_path / "images")
    )
    repo.log_chat("HQ", "Older question", "Older answer", "sqlite", 0)
    store = HarnessChatStore(repo)
    repo.log_chat("HQ", "Newer question", "Newer answer", "sqlite", 0)
    assert store.has_legacy_log()
    assert [row["redacted_question"] for row in store.legacy_log()] == ["Older question"]
    assert [row["redacted_question"] for row in HarnessChatStore(repo).legacy_log()] == [
        "Older question"
    ]
