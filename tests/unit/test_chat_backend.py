"""Tests for recovery from interrupted tool calls."""

import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from datetime import date
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai.chat_models.base import OpenAIInvalidRequestError
from streamlit.testing.v1 import AppTest

from kaya_kopi.copilot import chat_backend
from kaya_kopi.copilot.tools import kaya
from kaya_kopi.repositories.mysql import MySQLRepository


def test_safe_tool_message_preserves_and_redacts_structured_result_artifact():
    message = ToolMessage(
        content="8 rows, use artifact",
        tool_call_id="sql-1",
        name="query_business_data_sql",
        artifact={
            "result_type": "business_data",
            "rows": [{"outlet_code": "AMK-01", "staff_name": "Private Name"}],
        },
    )
    safe = chat_backend._safe_tool_message(message)
    assert safe.artifact["result_type"] == "business_data"
    assert safe.artifact["rows"][0]["outlet_code"] == "AMK-01"
    assert safe.artifact["rows"][0]["staff_name"] == "[REDACTED]"


def test_safe_ai_message_accepts_text_content_blocks_with_json_answer():
    answer = {
        "finding": "Margin cannot be calculated from the available evidence.",
        "key_evidence": ["No matching Kopi sales rows were returned."],
        "limitations": ["Recipe cost per cup is unavailable."],
        "recommendation": None,
    }
    message = AIMessage(content=[{"type": "text", "text": json.dumps(answer)}])

    safe = chat_backend._safe_ai_message(message, [])

    assert json.loads(safe.content) == answer


def test_specialist_sql_report_keeps_complete_rows_as_artifact_and_bounds_model_content():
    report = {
        "ok": True,
        "analysis": "performance",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "results": [
            {
                "outlet_code": f"OUT-{index:02d}",
                "gross_takings_sgd": f"{index * 100}.00",
                "citation": {"source_uri": "daily-summary.csv", "source_locator": f"row={index}"},
            }
            for index in range(12)
        ],
    }
    message = ToolMessage(
        content=json.dumps(report),
        tool_call_id="outlets",
        name="analyze_outlets_sql",
    )
    normalized = chat_backend._normalize_specialist_tool_result(message)
    compact = json.loads(normalized.content)
    assert compact["summary"]["results"]["record_count"] == 12
    assert len(compact["summary"]["results"]["preview"]) == 3
    assert normalized.artifact["report"]["results"][-1]["outlet_code"] == "OUT-11"
    assert "OUT-11" not in normalized.content


def test_followup_context_keeps_query_scope_and_all_returned_outlet_ids_only():
    messages = [
        HumanMessage(content="Show this year's sales by outlet"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "query_business_data_sql",
                "args": {"sql": "SELECT outlet_code, SUM(total) AS sales FROM pos_transactions WHERE tx_time >= ?", "parameters": ["2026-01-01"]},
                "id": "sql-1",
            }],
        ),
        ToolMessage(
            content="12 rows returned",
            tool_call_id="sql-1",
            name="query_business_data_sql",
            artifact={
                "result_type": "business_data",
                "sources": ["pos_transactions"],
                "columns": ["outlet_code", "sales"],
                "retrieved_record_count": 12,
                "truncated": False,
                "rows": [{"outlet_code": f"OUT-{number:02d}", "sales": 1000} for number in range(12)],
            },
        ),
    ]
    context = chat_backend._latest_business_report_context(messages)
    assert "2026-01-01" in context
    assert "OUT-11" in context
    assert "1000" not in context


def test_followup_context_uses_resolved_scope_and_canonical_ids_from_specialist_report():
    messages = [
        HumanMessage(content="Show mall outlet sales this quarter"),
        AIMessage(content="", tool_calls=[{
            "name": "analyze_outlets_sql",
            "args": {
                "analysis": "performance", "outlet_codes": ["AMK-3A"],
                "outlet_type": "mall", "start_date": "2026-10-01", "end_date": "2026-11-25",
            },
            "id": "specialist-1",
        }]),
        ToolMessage(
            content='{"ok":true,"result_type":"specialist_report"}',
            tool_call_id="specialist-1",
            name="analyze_outlets_sql",
            artifact={
                "result_type": "specialist_report",
                "tool_name": "analyze_outlets_sql",
                "report": {
                    "analysis": "performance",
                    "scope": {
                        "period": {"start": "2026-10-01", "end": "2026-11-25"},
                        "outlet_type": "mall",
                    },
                    "status": "PARTIAL",
                    "authority": "DAILY_SUMMARY_Z_REPORT",
                    "results": [
                        {"outlet_code": "AMK-03", "gross_takings_sgd": "1200.00"},
                        {"outlet_code": "JUR-01", "gross_takings_sgd": "900.00"},
                    ],
                },
            },
        ),
    ]

    context = chat_backend._latest_business_report_context(messages)
    assert '"report_type": "specialist"' in context
    assert '"analysis": "performance"' in context
    assert '"outlet_type": "mall"' in context
    assert '"start": "2026-10-01"' in context
    assert "AMK-03" in context and "JUR-01" in context
    assert "AMK-3A" not in context
    assert "1200.00" not in context


def test_followup_context_survives_failed_and_unrelated_later_turns_but_is_chat_local():
    prior_report = ToolMessage(
        content="{}", tool_call_id="report", name="get_outlet_sales_sql",
        artifact={
            "result_type": "specialist_report", "tool_name": "get_outlet_sales_sql",
            "report": {
                "outlet_code": "WDL-01",
                "period": {"start": "2025-07-01", "end": "2025-07-31"},
                "summary": {"outlet_code": "WDL-01", "gross_takings_sgd": "500.00"},
            },
        },
    )
    messages = [
        HumanMessage(content="Show WDL-01 sales in July 2025"),
        AIMessage(content="", tool_calls=[{"name": "get_outlet_sales_sql", "args": {}, "id": "report"}]),
        prior_report,
        HumanMessage(content="What is today's date?"),
        ToolMessage(content="database error", tool_call_id="failed", status="error"),
    ]

    context = chat_backend._latest_business_report_context(messages)
    assert "WDL-01" in context
    assert "2025-07-01" in context
    assert chat_backend._latest_business_report_context([]) == ""


def test_graph_passes_specialist_scope_to_followup_and_keeps_threads_isolated(tmp_path, monkeypatch):
    @tool("analyze_outlets_sql")
    def outlet_report() -> dict:
        """Return synthetic outlet analysis with resolved IDs and effective scope."""
        return {
            "ok": True,
            "analysis": "performance",
            "scope": {
                "period": {"start": "2026-10-01", "end": "2026-11-25"},
                "outlet_type": "mall",
            },
            "status": "PARTIAL",
            "results": [
                {"outlet_code": "AMK-03", "gross_takings_sgd": "1200.00"},
                {"outlet_code": "JUR-01", "gross_takings_sgd": "900.00"},
            ],
        }

    model = Mock()
    model.bind_tools.return_value = model
    prompts = []
    responses = iter([
        AIMessage(content="", tool_calls=[{
            "name": "analyze_outlets_sql",
            "args": {
                "analysis": "performance", "outlet_codes": ["AMK-3A"],
                "outlet_type": "mall", "start_date": "2026-10-01", "end_date": "2026-11-25",
            },
            "id": "specialist-1",
        }]),
        AIMessage(content="The report covers the mall outlets."),
        AIMessage(content="I will show monthly sales for the same outlets and period."),
        AIMessage(content="This is a fresh conversation."),
    ])
    model.invoke.side_effect = lambda messages: (prompts.append(messages), next(responses))[1]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "specialist-followup.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [outlet_report])
    chat_backend.get_chatbot.cache_clear()
    try:
        graph = chat_backend.get_chatbot()
        graph.invoke(
            {"messages": [HumanMessage(content="Show mall outlet sales this quarter")]},
            {"configurable": {"thread_id": "specialist-followup"}},
        )
        graph.invoke(
            {"messages": [HumanMessage(content="Show their monthly sales")]},
            {"configurable": {"thread_id": "specialist-followup"}},
        )
        graph.invoke(
            {"messages": [HumanMessage(content="What data is available?")]},
            {"configurable": {"thread_id": "fresh-thread"}},
        )

        followup_system = prompts[2][0].content
        assert "Recent successful report context" in followup_system
        assert "2026-10-01" in followup_system
        assert "AMK-03" in followup_system and "JUR-01" in followup_system
        assert "AMK-3A" not in followup_system
        assert "result_was_partial" in followup_system
        assert "Recent successful report context" not in prompts[3][0].content
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_singapore_relative_calendar_period_bounds_are_exact_and_injectable():
    bounds = chat_backend._relative_date_bounds(date(2026, 11, 25))
    assert bounds["this quarter"] == (date(2026, 10, 1), date(2026, 11, 25))
    assert bounds["last quarter"] == (date(2026, 7, 1), date(2026, 9, 30))
    assert bounds["this year"] == (date(2026, 1, 1), date(2026, 11, 25))
    assert bounds["last year"] == (date(2025, 1, 1), date(2025, 12, 31))
    prompt = chat_backend._current_system_message(today=date(2026, 11, 25)).content
    assert "last quarter: 2026-07-01 through 2026-09-30" in prompt


def test_system_prompt_combines_response_contract_and_project_guardrails():
    prompt = chat_backend.SYSTEM_PROMPT

    assert "## Tool capabilities and evidence limits" in prompt
    assert "## Evidence and reasoning" in prompt
    assert "## Response composition" in prompt
    assert (
        '{"finding":"direct answer","key_evidence":[],"limitations":[],"recommendation":null}'
        in prompt
    )
    assert "item-level purchase totals" in prompt
    assert 'analysis="spend"' in prompt
    assert "uploaded_* tables" in prompt
    assert "source_uri and source_locator" in prompt
    assert "Never set it to a" in prompt
    assert "price recommendation, business decision, legal advice, or tax advice" in prompt


def test_query_business_data_only_mode_keeps_response_contract(monkeypatch):
    monkeypatch.setenv("KAYA_QUERY_BUSINESS_DATA_ONLY", "1")

    prompt = chat_backend._current_system_message(today=date(2026, 11, 25)).content

    assert "query_business_data_sql`: the only MySQL/SQL tool" in prompt
    assert "## Response composition" in prompt
    assert '"finding":"direct answer"' in prompt


def test_current_system_message_includes_current_turn_period_conflict():
    results = [
        {
            "result_type": "business_data",
            "date_scope": {"start": "2026-01-01", "end_exclusive": "2026-11-27"},
            "query_intent": {"filters": [{
                "column": "outlets.outlet_type", "operator": "=", "values": ["mall"],
            }]},
        },
        {
            "result_type": "specialist_report",
            "tool_name": "analyze_staffing_sql",
            "report": {
                "period": {"start": "2026-06-01", "end": "2026-06-30"},
                "scope": {"outlet_type": "mall"},
            },
        },
    ]
    warnings = chat_backend.report_scope_alignment_warnings(results)

    prompt = chat_backend._current_system_message(
        today=date(2026, 11, 26), scope_warnings=warnings,
    ).content

    assert "Current-turn evidence has scope mismatches" in prompt
    assert "2026-01-01 through 2026-11-26" in prompt
    assert "2026-06-01 through 2026-06-30" in prompt
    assert "do not combine their figures" in prompt


def test_current_system_message_includes_source_coverage_gap_for_matching_periods():
    result = {
        "result_type": "specialist_report",
        "tool_name": "analyze_staffing_sql",
        "report": {
            "period": {"start": "2026-01-01", "end": "2026-11-26"},
            "results": [{
                "outlet_code": "JEM-01",
                "coverage": {
                    "requested_roster_months": [f"2026-{month:02d}" for month in range(1, 12)],
                    "roster_months": ["2026-06"],
                    "missing_roster_months": [
                        f"2026-{month:02d}" for month in range(1, 12) if month != 6
                    ],
                },
            }],
        },
    }
    warnings = chat_backend.report_scope_alignment_warnings([result])

    prompt = chat_backend._current_system_message(
        today=date(2026, 11, 26), scope_warnings=warnings,
    ).content

    assert "incomplete roster-month coverage" in prompt
    assert "JEM-01: 1 of 11 roster months (2026-06)" in prompt
    assert "include only months with roster records" in prompt


@pytest.mark.parametrize("question", [
    "What is Alex's hourly rate?",
    "How much does Alex earn per hour?",
    "Tell me Alex's salary.",
    "How much do we pay Alex?",
    "What are Alex's wages?",
])
def test_refused_question_does_not_construct_or_call_chatbot(monkeypatch, question):
    get_chatbot = Mock()
    monkeypatch.setattr(chat_backend, "get_chatbot", get_chatbot)
    result = list(chat_backend.stream_chat("thread", question))
    assert "Finance and Admin Manager" in result[0][0].content
    get_chatbot.assert_not_called()


def test_safe_ai_message_normalises_block_content():
    message = AIMessage(content=[
        {"type": "text", "text": "Our suppliers are A and B."},
    ])

    safe = chat_backend._safe_ai_message(message, [])

    assert safe.content == "Our suppliers are A and B."


def test_delivery_issue_question_detects_operational_reports():
    assert chat_backend.delivery_issue_question("any eggs delivery delayed in any outlet?")
    assert chat_backend.delivery_issue_question("eggs never arrive, driver MC")
    assert not chat_backend.delivery_issue_question("how much teh did we purchase in 2025?")


def test_delivery_system_message_requires_document_search():
    message = chat_backend._current_system_message(
        question="any eggs delivery delayed in any outlet?"
    )

    assert "search_documents_sql" in message.content
    assert "invoices alone" in message.content


def test_delivery_question_prefetches_chat_evidence(tmp_path, monkeypatch):
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="Yes, chat reports show egg delivery problems.")
    search = Mock()
    search.invoke = Mock(return_value={
        "ok": True,
        "results": [{
            "source_type": "whatsapp_message",
            "chunk_text": "eggs today never deliver leh, called Ban Seng they say driver MC",
            "source_uri": "data/docs/whatsapp_exports/ops_chat_export.txt",
            "source_locator": "lines=47-47",
        }],
    })
    monkeypatch.setattr(kaya, "search_documents_sql", search)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="any eggs delivery delayed in any outlet?")]},
            {"configurable": {"thread_id": "delivery-test"}},
        )
    finally:
        chat_backend.get_chatbot.cache_clear()

    assert search.invoke.call_count == 1
    sent = model.invoke.call_args.args[0]
    assert any(
        "Operational chat/email evidence retrieved" in str(message.content)
        for message in sent
    )
    assert "chat reports show egg delivery problems" in result["messages"][-1].content
    assert any(
        isinstance(message, SystemMessage)
        and "Operational chat/email evidence retrieved" in str(message.content)
        for message in result["messages"]
    )


def test_tool_call_turn_does_not_print_a_placeholder_answer():
    tool_turn = AIMessage(
        content=[{"type": "text", "text": ""}],
        tool_calls=[{"name": "get_data_coverage_sql", "args": {}, "id": "1"}],
    )
    final_turn = AIMessage(content=[{"type": "text", "text": ""}])

    assert chat_backend._safe_ai_message(tool_turn, []).content == ""
    assert chat_backend._safe_ai_message(final_turn, []).content == "No answer was returned."


def test_price_scenario_detection_and_amount():
    question = (
        "Show a S$0.10 kopi price scenario and the public recipe assumptions. "
        "What can and cannot be concluded?"
    )

    assert chat_backend.price_scenario_question(question)
    assert chat_backend.price_scenario_amount(question) == Decimal("0.10")
    assert not chat_backend.price_scenario_question(
        "How much can we increase the Teh price to improve margin?"
    )


def test_price_scenario_prefetches_numbers_and_missing_recipe_note(tmp_path, monkeypatch):
    question = (
        "Show a S$0.10 kopi price scenario and the public recipe assumptions. "
        "What can and cannot be concluded?"
    )
    scenario = Mock()
    scenario.invoke = Mock(return_value={
        "ok": True,
        "status": "PARTIAL",
        "observed_partial_pos": {
            "units": "1100", "gross_sales_sgd": "2034.40",
            "minimum_observed_price_sgd": "1.60", "maximum_observed_price_sgd": "2.16",
        },
        "gross_price_increase_sgd": "0.10",
        "scenarios": [{
            "demand_change_percent": "0", "projected_units": "1100.00",
            "projected_gross_sales_sgd": "2144.40",
            "incremental_gross_sales_sgd": "110.00",
            "incremental_ex_gst_sales_sgd": "100.92",
        }],
        "missing_for_decision": ["recipe-level ingredient cost per cup"],
        "citation": [{"source_uri": "data/sql/kayakopi_pos.sql"}],
    })
    recipe = Mock()
    recipe.invoke = Mock(return_value={"ok": True, "results": []})
    monkeypatch.setattr(kaya, "assess_kopi_price_change_sql", scenario)
    monkeypatch.setattr(kaya, "search_documents_sql", recipe)

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="At unchanged volume, +SGD 110.00 gross sales.")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content=question)]},
            {"configurable": {"thread_id": "scenario-test"}},
        )
    finally:
        chat_backend.get_chatbot.cache_clear()

    assert scenario.invoke.call_count == 1
    assert recipe.invoke.call_count == 1
    sent = model.invoke.call_args.args[0]
    context = " ".join(str(message.content) for message in sent)
    assert "2144.40" in context
    assert "110.00" in context
    assert "No indexed recipe card" in context
    assert result["messages"][-1].content == "At unchanged volume, +SGD 110.00 gross sales."


def test_custom_chat_model_is_sent_to_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    graph = Mock()
    graph.stream.return_value = iter([{"chat": {"messages": [AIMessage(content="ok")]}}])
    get_chatbot = Mock(return_value=graph)
    monkeypatch.setattr(chat_backend, "get_chatbot", get_chatbot)

    result = list(chat_backend.stream_chat(
        "thread", "how much teh we purchase in 2025?"
    ))

    assert result[-1][0].content == "ok"
    get_chatbot.assert_called_once()


def test_get_chatbot_builds_gpt6_with_compatible_options(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    constructor = Mock(return_value=model)
    monkeypatch.setattr(chat_backend, "ChatOpenAI", constructor)
    chat_backend.get_chatbot.cache_clear()
    graph = None
    try:
        graph = chat_backend.get_chatbot()
        kwargs = constructor.call_args.kwargs
        assert kwargs["model"] == "gpt-6-luna"
        assert "temperature" not in kwargs
        assert kwargs["reasoning_effort"] == "none"
    finally:
        if graph is not None:
            graph.checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()


def test_invalid_request_error_names_configured_model(monkeypatch, tmp_path):
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    graph = Mock()
    graph.stream.side_effect = OpenAIInvalidRequestError(
        "invalid model", response=Mock(), body=None,
    )
    get_chatbot = Mock(return_value=graph)
    monkeypatch.setattr(chat_backend, "get_chatbot", get_chatbot)

    result = list(chat_backend.stream_chat("thread", "how much teh?"))

    content = result[-1][0].content
    assert "gpt-4o-mini" in content
    assert "provider rejected" in content
    get_chatbot.assert_called_once()


def test_tool_evidence_and_answer_are_protected_before_checkpoint(tmp_path, monkeypatch):
    @tool
    def search_example() -> dict:
        """Return synthetic document evidence."""
        return {
            "citation": "example.txt#L1",
            "message": "Alex reported the machine. Contact +65 9123 4567.",
            "staff_name": "Alex",
            "hourly_rate_sgd": "15",
        }

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "search_example", "args": {}, "id": "1"}]),
        AIMessage(content="Alex earns $15 per hour."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [search_example])
    chat_backend.get_chatbot.cache_clear()
    try:
        graph = chat_backend.get_chatbot()
        result = graph.invoke(
            {"messages": [HumanMessage(content="Who reported the machine?")]},
            {"configurable": {"thread_id": "test"}},
        )
        tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        assert "9123 4567" not in tool_message.content
        assert '"hourly_rate_sgd": "[REDACTED]"' in tool_message.content
        assert "$15" not in result["messages"][-1].content
        assert "Finance and Admin Manager" in result["messages"][-1].content
        state = graph.get_state({"configurable": {"thread_id": "test"}})
        assert "9123 4567" not in str(state.values["messages"])
        assert "$15" not in str(state.values["messages"])
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_graph_checkpoints_content_and_artifact_tool_results(tmp_path, monkeypatch):
    @tool(response_format="content_and_artifact")
    def sql_report() -> tuple[str, dict]:
        """Return a bounded summary plus rows for the app."""
        return "12 rows returned", {
            "result_type": "business_data",
            "columns": ["outlet_code"],
            "rows": [{"outlet_code": f"OUT-{number:02d}"} for number in range(12)],
            "retrieved_record_count": 12,
            "truncated": False,
            "sources": ["daily_outlet_summary"],
        }

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "sql_report", "args": {}, "id": "sql-1"}]),
        AIMessage(content="The query returned 12 outlets."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [sql_report])
    chat_backend.get_chatbot.cache_clear()
    try:
        graph = chat_backend.get_chatbot()
        graph.invoke(
            {"messages": [HumanMessage(content="List outlet sales")]},
            {"configurable": {"thread_id": "artifact"}},
        )
        state = graph.get_state({"configurable": {"thread_id": "artifact"}})
        tool_message = next(item for item in state.values["messages"] if isinstance(item, ToolMessage))
        assert len(tool_message.artifact["rows"]) == 12
        assert tool_message.artifact["rows"][-1]["outlet_code"] == "OUT-11"
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_graph_turns_specialist_dict_result_into_complete_report_artifact(tmp_path, monkeypatch):
    @tool("analyze_outlets_sql")
    def outlet_analysis() -> dict:
        """Return a synthetic complete outlet report."""
        return {
            "ok": True,
            "analysis": "performance",
            "results": [
                {"outlet_code": f"OUT-{index:02d}", "gross_takings_sgd": str(index * 100)}
                for index in range(12)
            ],
        }

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "analyze_outlets_sql", "args": {}, "id": "outlet-report",
        }]),
        AIMessage(content="The report covers 12 outlets."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "specialist.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [outlet_analysis])
    chat_backend.get_chatbot.cache_clear()
    try:
        graph = chat_backend.get_chatbot()
        graph.invoke(
            {"messages": [HumanMessage(content="List all outlet sales")]},
            {"configurable": {"thread_id": "specialist"}},
        )
        state = graph.get_state({"configurable": {"thread_id": "specialist"}})
        tool_message = next(item for item in state.values["messages"] if isinstance(item, ToolMessage))
        assert tool_message.artifact["result_type"] == "specialist_report"
        assert len(tool_message.artifact["report"]["results"]) == 12
        assert "OUT-11" not in tool_message.content
        compact = json.loads(tool_message.content)
        assert compact["summary"]["results"]["record_count"] == 12
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_graph_runs_independent_tool_calls_in_parallel_group(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="kaya_kopi.copilot.timing")
    @tool
    def sales_source() -> str:
        """Return sales evidence."""
        return "Sales total: 100"

    @tool
    def email_source() -> str:
        """Return email evidence."""
        return "Supplier email: delivery changed"

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[
            {"name": "sales_source", "args": {}, "id": "sales"},
            {"name": "email_source", "args": {}, "id": "email"},
        ]),
        AIMessage(content="Sales were 100; the email mentioned a delivery change."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "parallel.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [sales_source, email_source])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="Check sales and the related email")]},
            {"configurable": {"thread_id": "parallel", "copilot_timing_id": "timing-test"}},
        )
        tool_results = [item for item in result["messages"] if isinstance(item, ToolMessage)]
        assert {item.name for item in tool_results} == {"sales_source", "email_source"}
        assert "100" in next(item.content for item in tool_results if item.name == "sales_source")
        assert "delivery changed" in next(item.content for item in tool_results if item.name == "email_source")
        timing_logs = "\n".join(record.message for record in caplog.records)
        assert "[COPILOT_TIMING]" in timing_logs
        assert "stage=model_call" in timing_logs
        assert "stage=tool_batch" in timing_logs
        assert "turn_id=timing-test" in timing_logs
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_graph_waits_for_first_tool_before_dependent_lookup(tmp_path, monkeypatch):
    calls: list[str] = []

    @tool
    def rank_invoices() -> str:
        """Return the exact invoice selected by a ranking."""
        calls.append("rank")
        return "Selected invoice ID: 481"

    @tool
    def invoice_details(invoice_id: str) -> str:
        """Retrieve details for a specific selected invoice ID."""
        calls.append(f"details:{invoice_id}")
        return "Invoice 481 has two lines"

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "rank_invoices", "args": {}, "id": "rank",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "invoice_details", "args": {"invoice_id": "481"}, "id": "details",
        }]),
        AIMessage(content="Invoice 481 has two lines."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chain.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [rank_invoices, invoice_details])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="Show the top invoice and its details")]},
            {"configurable": {"thread_id": "chain"}},
        )
        assert calls == ["rank", "details:481"]
        assert result["messages"][-1].content == "Invoice 481 has two lines."
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_direct_answer_from_supplied_context_is_not_blocked_by_reviewer(tmp_path, monkeypatch):
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="Today is 2026-11-25.")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="What is today's date?")]},
            {"configurable": {"thread_id": "test"}},
        )
        assert result["messages"][-1].content == "Today is 2026-11-25."
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_result_without_citation_can_be_used(tmp_path, monkeypatch):
    @tool
    def lookup_total() -> str:
        """Return a known total."""
        return "Total: 42"

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "lookup_total", "args": {}, "id": "1"}]),
        AIMessage(content="The total is 42."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [lookup_total])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="What is the total?")]},
            {"configurable": {"thread_id": "test"}},
        )
        assert result["messages"][-1].content == "The total is 42."
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_supplier_business_address_is_preserved(tmp_path, monkeypatch):
    @tool("analyze_suppliers_sql")
    def supplier_example() -> dict:
        """Return synthetic supplier evidence."""
        return {
            "ok": True,
            "invoice": {
                "supplier_name": "Example Pte Ltd",
                "supplier_address": "123 Orchard Road, Singapore 123456",
                "gst_registration_number": "M2-1234567-1",
            },
            "citation": {"source_uri": "invoice-example.jpg"},
        }

    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "analyze_suppliers_sql", "args": {}, "id": "supplier",
        }]),
        AIMessage(content="Example Pte Ltd is at 123 Orchard Road, Singapore 123456."),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [supplier_example])
    chat_backend.get_chatbot.cache_clear()
    try:
        result = chat_backend.get_chatbot().invoke(
            {"messages": [HumanMessage(content="What is the supplier's address?")]},
            {"configurable": {"thread_id": "test"}},
        )
        answer = result["messages"][-1].content
        assert "123 Orchard Road, Singapore 123456" in answer
        assert "M2-1234567-1" in next(
            message.content for message in result["messages"] if isinstance(message, ToolMessage)
        )
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_supplier_lookup_preserves_business_address_in_chat_and_history(tmp_path, monkeypatch):
    address = "123 Orchard Road, Singapore 123456"
    reference = "example.jpg#invoice=INV-1"
    repository = MySQLRepository()
    monkeypatch.setattr(repository, "_query", Mock(return_value=[{
        "supplier_name": "Example Pte Ltd",
        "supplier_address": address,
        "invoice_reference": reference,
    }]))
    monkeypatch.setattr(kaya, "_repository", lambda: repository)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "get_supplier_addresses_sql",
            "args": {"supplier_name": "example pte ltd"},
            "id": "address",
        }]),
        AIMessage(content=(
            f"Example Pte Ltd: {address}. Invoice: {reference}.\n"
            "Alex lives at 45 Hill Street."
        )),
    ]
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [kaya.get_supplier_addresses_sql])
    chat_backend.get_chatbot.cache_clear()
    graph = chat_backend.get_chatbot()
    try:
        result = graph.invoke(
            {"messages": [HumanMessage(content="What is Example Pte Ltd's supplier address?")]},
            {"configurable": {"thread_id": "address-test"}},
        )
        tool_message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert address in tool_message.content
        assert address in str(model.invoke.call_args.args[0])
        answer = result["messages"][-1].content
        assert address in answer
        assert reference in answer
        assert "45 Hill Street" not in answer

        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        app.button(key="address-test").click().run()
        assert not app.exception
        saved_answer = app.session_state["message_history"][-1]["content"]
        assert address in saved_answer
        assert reference in saved_answer
        assert "45 Hill Street" not in saved_answer
    finally:
        graph.checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()


def test_headcount_answer_is_displayed(tmp_path, monkeypatch):
    answer = "JUR-01 has 2 rostered baristas in June 2026."
    repository = Mock(spec=MySQLRepository)
    repository.analyze_outlets.return_value = {
        "analysis": "headcount",
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "results": [{
            "outlet_code": "JUR-01",
            "counts_by_month_and_role": [
                {"roster_month": "2026-06-01", "role": "Barista", "headcount": 2},
            ],
            "citation": {"source_uri": "example-roster.xlsx"},
        }],
    }
    monkeypatch.setattr(kaya, "_repository", lambda: repository)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "analyze_outlets_sql",
            "args": {"analysis": "headcount", "outlet_codes": ["JUR-01"]},
            "id": "headcount",
        }]),
        AIMessage(content=answer),
    ]
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [kaya.analyze_outlets_sql])
    chat_backend.get_chatbot.cache_clear()
    graph = chat_backend.get_chatbot()
    try:
        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        app.chat_input[0].set_value("How many Baristas in JUR-01?").run()
        assert not app.exception
        assert app.session_state["message_history"][-1]["content"] == answer
        repository.analyze_outlets.assert_called_once_with(
            "headcount", ["JUR-01"], None, None, None,
        )
    finally:
        graph.checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()


def test_other_tools_cannot_enable_supplier_address_exception():
    message = ToolMessage(
        name="search_documents_sql",
        tool_call_id="untrusted",
        content='{"results": [{"supplier_address": "123 Orchard Road"}]}',
    )
    protected = chat_backend._safe_tool_message(message)
    assert "123 Orchard Road" not in protected.content
    assert chat_backend._supplier_addresses([protected]) == []


@tool
def interrupted_search(query: str) -> str:
    """Search for an invoice."""
    raise RuntimeError("Search interrupted")


class FakeModel:
    def __init__(self):
        self.calls = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "interrupted_search", "args": {"query": "coffee"}, "id": "1"}],
            )
        return AIMessage(content="Please try the search again.")


class ChatBackendTest(unittest.TestCase):
    def test_names_and_deletes_only_selected_thread(self):
        model = Mock()
        model.bind_tools.return_value = model
        model.invoke.return_value = AIMessage(content="Hello")
        chat_backend.get_chatbot.cache_clear()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "test", "KAYA_CHAT_DB": f"{directory}/chat.sqlite3"}), \
             patch.object(chat_backend, "ChatOpenAI", return_value=model), \
             patch.object(chat_backend, "ALL_TOOLS", []):
            graph = chat_backend.get_chatbot()
            try:
                for thread_id in ("first", "second"):
                    graph.invoke({"messages": [HumanMessage(content="Hi")]},
                                 {"configurable": {"thread_id": thread_id}})
                chat_backend.rename_thread("first", "  Supplier review  ")
                self.assertEqual(chat_backend.get_thread_names(), {"first": "Supplier review"})
                with self.assertRaisesRegex(ValueError, "cannot be empty"):
                    chat_backend.rename_thread("first", "  ")
                with self.assertRaisesRegex(ValueError, "no longer exists"):
                    chat_backend.rename_thread("missing", "Name")
            finally:
                graph.checkpointer.conn.close()
                chat_backend.get_chatbot.cache_clear()

            reopened_graph = chat_backend.get_chatbot()
            try:
                self.assertEqual(chat_backend.get_thread_names(), {"first": "Supplier review"})

                chat_backend.delete_thread("first")

                self.assertEqual(chat_backend.retrieve_all_threads(), ["second"])
                self.assertEqual(chat_backend.get_thread_names(), {})
                with closing(sqlite3.connect(f"{directory}/chat.sqlite3")) as connection:
                    for table in ("checkpoints", "writes"):
                        self.assertEqual(connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", ("first",)
                        ).fetchone()[0], 0)
                state = reopened_graph.get_state({"configurable": {"thread_id": "second"}})
                self.assertTrue(state.values["messages"])
            finally:
                reopened_graph.checkpointer.conn.close()
                chat_backend.get_chatbot.cache_clear()

    def test_repairs_only_missing_results_in_parallel_tool_calls(self):
        call = AIMessage(content="", tool_calls=[
            {"name": "first", "args": {}, "id": "1"},
            {"name": "second", "args": {}, "id": "2"},
        ])
        result = ToolMessage(content="Found", tool_call_id="2", name="second")
        messages = [HumanMessage(content="Search"), call, result]

        repaired = chat_backend._repair_interrupted_tool_calls(messages)

        self.assertEqual(repaired[:3], messages)
        self.assertEqual(repaired[3].tool_call_id, "1")
        self.assertEqual(repaired[3].status, "error")
        self.assertEqual(chat_backend._repair_interrupted_tool_calls(repaired), repaired)

    def test_next_turn_repairs_interrupted_tool_call(self):
        model = FakeModel()
        chat_backend.get_chatbot.cache_clear()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "test", "KAYA_CHAT_DB": f"{directory}/chat.sqlite3"}), \
             patch.object(chat_backend, "ChatOpenAI", return_value=model), \
             patch.object(chat_backend, "ALL_TOOLS", [interrupted_search]):
            graph = chat_backend.get_chatbot()
            try:
                config = {"configurable": {"thread_id": "test"}}
                with self.assertRaisesRegex(RuntimeError, "Search interrupted"):
                    graph.invoke({"messages": [HumanMessage(content="Find coffee")]}, config)
                graph.invoke({"messages": [HumanMessage(content="Try again")]}, config)

                messages = model.calls[1]
                call_index = next(i for i, message in enumerate(messages) if isinstance(message, AIMessage))
                result = messages[call_index + 1]
                self.assertIsInstance(result, ToolMessage)
                self.assertEqual(result.tool_call_id, "1")
                self.assertIn("interrupt", result.content.lower())
                self.assertIsInstance(messages[call_index + 2], HumanMessage)
            finally:
                graph.checkpointer.conn.close()
                chat_backend.get_chatbot.cache_clear()


def test_conversation_sidebar_rename_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="Hello")
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [])
    chat_backend.get_chatbot.cache_clear()
    try:
        graph = chat_backend.get_chatbot()
        for thread_id in ("first", "second"):
            graph.invoke({"messages": [HumanMessage(content="Hi")]},
                         {"configurable": {"thread_id": thread_id}})
        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        assert not app.exception
        app.button(key="first").click().run()
        assert app.session_state["thread_id"] == "first"
        app.text_input(key="name_first").set_value("Supplier review").run()
        app.button(key="save_first").click().run()
        assert app.button(key="first").label == "Supplier review"
        assert app.button(key="delete_first").disabled

        app.checkbox(key="confirm_first").check().run()
        app.button(key="delete_first").click().run()

        assert not app.exception
        assert app.session_state["thread_id"] not in ("first", "second")
        assert app.session_state["message_history"] == []
        assert chat_backend.retrieve_all_threads() == ["second"]
        assert chat_backend.get_thread_names() == {}
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_answer_usage_footer_sums_tool_calls_and_survives_reopening(tmp_path, monkeypatch):
    @tool
    def lookup_total() -> str:
        """Return a known total."""
        return "Total: 42"

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(
            content="",
            tool_calls=[{"name": "lookup_total", "args": {}, "id": "lookup"}],
            response_metadata={"model_name": "gpt-4o-mini-2024-07-18"},
            usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        ),
        AIMessage(
            content="The total is 42.",
            response_metadata={"model_name": "gpt-4o-mini-2024-07-18"},
            usage_metadata={
                "input_tokens": 2000, "output_tokens": 200, "total_tokens": 2200,
                "input_token_details": {"cache_read": 1000},
            },
        ),
    ]
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [lookup_total])
    chat_backend.get_chatbot.cache_clear()
    try:
        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        app.chat_input[0].set_value("What is the total?").run()
        assert not app.exception
        expected = (
            "Model: gpt-4o-mini-2024-07-18 | Tokens: 3,300 (3,000 input + 300 output) | "
            "Estimated cost: $0.000555 USD"
        )
        assert expected in [caption.value for caption in app.caption]
        thread_id = app.session_state["thread_id"]
        app.run()
        assert expected in [caption.value for caption in app.caption]
        next(button for button in app.button if button.label == "＋ New chat").click().run()
        app.button(key=thread_id).click().run()
        assert not app.exception
        assert expected in [caption.value for caption in app.caption]
        assert model.invoke.call_count == 2
    finally:
        chat_backend.get_chatbot.cache_clear()


def test_direct_mysql_sources_are_saved_and_rendered(tmp_path, monkeypatch):
    source = "data/tabular/daily_summary_AMK-01.csv"
    note = "Cite the returned data paths as Markdown file links."

    @tool("query_mysql_direct")
    def direct_example(sql: str) -> dict:
        """Return synthetic direct MySQL evidence."""
        return {
            "ok": True,
            "rows": [{"source_file": source}],
            "citations": [source],
            "citation_note": note,
        }

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("LOCAL_SQLITE_PATH", str(tmp_path / "usage.sqlite3"))
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{
            "name": "query_mysql_direct", "args": {"sql": "SELECT * FROM daily_outlet_summary"},
            "id": "direct",
        }]),
        AIMessage(content=f"Observed source: {source}."),
    ]
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [direct_example])
    chat_backend.get_chatbot.cache_clear()
    graph = chat_backend.get_chatbot()
    try:
        app_path = Path(__file__).resolve().parents[2] / "src/kaya_kopi/copilot/streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=30).run()
        app.chat_input[0].set_value("Show the daily summary source.").run()
        assert not app.exception
        saved = app.session_state["message_history"][-1]
        assert saved["sources"] == [source]
        assert saved["source_note"] == note
        assert "Source Files" in [caption.value for caption in app.caption]
        assert source in [caption.value for caption in app.caption]
    finally:
        graph.checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()
