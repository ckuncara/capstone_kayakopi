import sqlite3
from datetime import date
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from kaya_kopi.copilot import chat_backend
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard


def test_chat_logs_both_model_calls_and_preserves_messages(tmp_path):
    tracker = LLMUsageTracker(tmp_path / "usage.db")
    first = AIMessage(
        content="", id="first",
        response_metadata={"model_name": "gpt-4o-mini-2024-07-18"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
    )
    tool = ToolMessage(content="Found", tool_call_id="lookup")
    second = AIMessage(
        content="Answer", id="second",
        response_metadata={"model_name": "gpt-4o-mini-2024-07-18"},
        usage_metadata={
            "input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200,
            "input_token_details": {"cache_read": 1000},
            "output_token_details": {"reasoning": 50},
        },
    )
    graph = Mock()
    graph.stream.return_value = iter([
        {"chat": {"messages": [first]}},
        {"tools": {"messages": [tool]}},
        {"chat": {"messages": [second]}},
    ])
    with patch.object(chat_backend, "get_chatbot", return_value=graph), \
         patch("streamlit.session_state", {"usage_tracker": tracker}):
        results = list(chat_backend.stream_chat("thread", "Question"))
    assert [msg for msg, _ in results] == [first, tool, second]
    turn_id = graph.stream.call_args.args[0]["messages"][0].id
    assert all(metadata["turn_id"] == turn_id for _, metadata in results)
    tracker.log_usage("other-thread", "Question", "gpt-4o-mini", 9000, 1000, turn_id=turn_id)
    tracker.log_usage("thread", "Question", "gpt-4o-mini", 9000, 1000, turn_id="other-turn")
    summary = tracker.get_turn_usage("thread", turn_id)
    assert summary["total_tokens"] == 3300
    assert summary["total_cost"] == pytest.approx(0.000555)
    history = tracker.get_all_usage_history()
    assert len(history) == 4
    assert history[2]["model_name"] == "gpt-4o-mini-2024-07-18"
    assert history[2]["cached_input_tokens"] == 1000
    assert history[2]["reasoning_tokens"] == 50


def test_unknown_model_is_saved_but_not_silently_priced(tmp_path):
    tracker = LLMUsageTracker(tmp_path / "usage.db")
    assert tracker.log_usage("t", "q", "unlisted-model", 100, 50) is None
    row = tracker.get_latest_usage()
    assert row["model_name"] == "unlisted-model"
    assert row["total_cost"] is None
    assert row["cost_status"] == "unpriced"
    assert tracker.get_unpriced_count() == 1
    with pytest.raises(BudgetExceededError, match="incomplete"):
        BudgetGuard(tracker.db_path).check_budget_or_raise()


def test_missing_usage_is_not_reported_as_free(tmp_path):
    tracker = LLMUsageTracker(tmp_path / "usage.db")
    tracker.log_response("t", "q", AIMessage(content="Hello"), "gpt-4o-mini")
    row = tracker.get_latest_usage()
    assert row["total_tokens"] is None
    assert row["total_cost"] is None
    assert row["usage_source"] == "missing"


def test_turn_footer_does_not_present_partial_usage_as_complete(tmp_path):
    from kaya_kopi.copilot.llm_ui_components import format_turn_usage

    tracker = LLMUsageTracker(tmp_path / "usage.db")
    tracker.log_usage("t", "q", "gpt-4o-mini", 100, 50, turn_id="turn")
    tracker.log_usage("t", "q", "unknown", None, None, turn_id="turn")
    caption = format_turn_usage(tracker.get_turn_usage("t", "turn"))
    assert "gpt-4o-mini, unknown" in caption
    assert "Tokens: unavailable" in caption
    assert "Estimated cost: unavailable" in caption
    assert format_turn_usage(tracker.get_turn_usage("t", "old-turn")) is None


def test_nonstandard_tier_is_not_priced_at_standard_rates(tmp_path):
    tracker = LLMUsageTracker(tmp_path / "usage.db")
    response = AIMessage(
        content="Hello",
        response_metadata={"model_name": "gpt-4o-mini", "service_tier": "priority"},
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )
    tracker.log_response("t", "q", response, "gpt-4o-mini")
    assert tracker.get_latest_usage()["total_cost"] is None


def test_chat_preserves_first_call_usage_when_later_tool_fails(tmp_path):
    tracker = LLMUsageTracker(tmp_path / "usage.db")
    first = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )

    def failing_stream(*args, **kwargs):
        yield {"chat": {"messages": [first]}}
        raise RuntimeError("Tool failed")

    graph = Mock()
    graph.stream.side_effect = failing_stream
    with patch.object(chat_backend, "get_chatbot", return_value=graph), \
         patch("streamlit.session_state", {"usage_tracker": tracker}):
        with pytest.raises(RuntimeError, match="Tool failed"):
            list(chat_backend.stream_chat("thread", "Question"))
    assert len(tracker.get_all_usage_history()) == 1


def test_migration_preserves_old_costs_and_labels_them(tmp_path):
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            thread_id TEXT, prompt TEXT, model_name TEXT, input_tokens INTEGER,
            output_tokens INTEGER, total_tokens INTEGER, input_cost REAL,
            output_cost REAL, total_cost REAL, created_at TEXT)""")
        conn.execute("INSERT INTO llm_usage (id, total_cost) VALUES (1, 0.25)")
    tracker = LLMUsageTracker(path)
    assert tracker.get_latest_usage()["cost_status"] == "legacy_estimate"
    assert tracker.get_total_spend() == 0.25


def test_billing_reconciliation_persists_utc_period_and_flags_incomplete_costs(tmp_path):
    path = tmp_path / "usage.db"
    tracker = LLMUsageTracker(path)
    tracker.log_usage("t", "q", "gpt-4o-mini", 1000, 0)
    tracker.log_usage("t", "q", "unlisted", 10, 0)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_usage SET timestamp='2026-11-01 12:00:00'")
        conn.execute(
            "INSERT INTO llm_usage (timestamp, total_cost) VALUES ('2026-12-01', 10)"
        )
    tracker.save_reconciliation(
        date(2026, 11, 1), date(2026, 12, 1), 0.002, "Only this app and API key",
    )
    result = LLMUsageTracker(path).get_latest_reconciliation()
    assert result["estimated_cost"] == pytest.approx(0.00015)
    assert result["difference_usd"] == pytest.approx(0.00185)
    assert result["unpriced_calls"] == 1
    assert result["scope"] == "Only this app and API key"
    with pytest.raises(ValueError):
        tracker.save_reconciliation(date(2026, 12, 1), date(2026, 11, 1), 1, "scope")


def test_usage_views_reread_database_and_save_billing_comparison(tmp_path):
    from streamlit.testing.v1 import AppTest

    def usage_app(path):
        from pathlib import Path

        from kaya_kopi.copilot.llm_ui_components import render_spend_meter, render_usage_log_panel
        from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker

        tracker = LLMUsageTracker(Path(path))
        render_spend_meter(tracker)
        render_usage_log_panel(tracker)

    path = tmp_path / "usage.db"
    tracker = LLMUsageTracker(path)
    app = AppTest.from_function(
        usage_app, args=(str(path),), default_timeout=10,
    )
    app.run()
    assert not app.exception
    tracker.log_usage("t", "q", "unlisted", 100, 50)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE llm_usage SET timestamp='2026-11-30 20:30:00'")
    app.run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    assert app.dataframe[0].value["Timestamp (UTC+8)"][0] == "2026-12-01 04:30:00"
    assert tracker.get_latest_usage()["timestamp"] == "2026-11-30 20:30:00"
    assert any("unpriced" in warning.value for warning in app.warning)
    app.date_input[0].set_value(date(2026, 11, 1))
    app.date_input[1].set_value(date(2026, 12, 1))
    app.number_input[0].set_value(0.5)
    app.text_input[0].set_value("Local API key")
    app.checkbox[0].check()
    app.button[1].click().run()
    assert not app.exception
    assert tracker.get_latest_reconciliation()["provider_cost_usd"] == 0.5
