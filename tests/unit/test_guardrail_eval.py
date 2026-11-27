"""Offline checks for the live evaluation and guarded chat path."""

import json
from unittest.mock import Mock

import pytest
import streamlit as st
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from kaya_kopi.copilot import chat_backend
from kaya_kopi.copilot.response_format import format_copilot_answer
from kaya_kopi.security.guardrails import FINANCE_REFUSAL
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa
from scripts import run_guardrail_eval
from scripts.run_guardrail_eval import CASES, Case, check_answer


SYNTHETIC_ANSWERS = [
    "The roster contains 2 baristas in JUR-01.",
    "The latest roster month is June 2026.",
    "AMK-01 has sales of SGD 100.",
    "Recorded wastage is 5 kg.",
    "There are 69 supplier invoice scans.",
    "D'Best Frozen Foods is at 123 Orchard Road.",
    "Kopi costs SGD 2.00 at the mall.",
    "Supplier purchases total SGD 100.",
    "JUR-01 has sales records for June 2026.",
    "There are 11 outlets after resolving the alias.",
    "There are 69 scanned invoices.",
    "BDK-01 has the highest measured wastage at 640.400 kg.",
    "INV-042509-752 was reused for two purchases; one has no scan.",
    "The difference is SGD 9.33; GST is not the cause.",
    "Some roster rates are TBC.",
]


@pytest.mark.parametrize("index", range(len(CASES)))
def test_evaluation_cases_through_chat_entry(index, tmp_path, monkeypatch):
    case = CASES[index]
    if case.refusal:
        chatbot = Mock()
        monkeypatch.setattr(chat_backend, "get_chatbot", chatbot)
        messages = list(chat_backend.stream_chat("eval", case.question))
        assert check_answer(case, messages[0][0].content) == []
        chatbot.assert_not_called()
        return

    answer = SYNTHETIC_ANSWERS[index]

    @tool
    def synthetic_evidence() -> dict:
        """Return synthetic evidence for an offline check."""
        return {"citation": "synthetic-fixture.txt", "fact": answer}

    @tool("get_supplier_addresses_sql")
    def synthetic_supplier_address() -> dict:
        """Return a fictional supplier address."""
        return {
            "ok": True,
            "results": [{
                "supplier_name": "D'Best Frozen Foods",
                "supplier_address": "123 Orchard Road",
                "citation": "synthetic-fixture.txt",
            }],
        }

    evidence_tool = synthetic_supplier_address if index == 5 else synthetic_evidence
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": evidence_tool.name, "args": {}, "id": "1"}]),
        AIMessage(content=answer),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [evidence_tool])
    monkeypatch.setitem(st.session_state, "usage_tracker", Mock())
    chat_backend.get_chatbot.cache_clear()
    try:
        messages = list(chat_backend.stream_chat("eval", case.question))
        final = [
            message.content for message, _ in messages
            if isinstance(message, AIMessage) and not message.tool_calls
        ][-1]
        assert check_answer(case, final) == []
    finally:
        chat_backend.get_chatbot(None).checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()


def test_evaluation_checks_expected_content_and_does_not_require_source_suffix():
    assert len(CASES) >= 20
    assert sum(case.refusal is not None for case in CASES) >= 2
    case = Case("How many outlets?", r"\b11\b")
    assert check_answer(case, "There are 12 outlets.")
    assert check_answer(case, "There are 12 outlets.\n\nSources: source.txt")
    assert check_answer(case, "There are 11 outlets.") == []
    assert check_answer(case, "There are 11 outlets. Sources: roster; alex@example.com")
    assert check_answer(CASES[-1], "I exported the messages.")


@pytest.mark.parametrize(("index", "answer"), [
    (0, "No barista information is available."),
    (0, "There are 2 outlets, but no barista count for JUR-01."),
    (1, "The latest roster month is unavailable."),
    (2, "AMK-01 sales data is unavailable."),
    (4, "Invoice information is available."),
    (9, "There are 12 outlets. Source: report-11.csv."),
    (10, "There are 70 invoice scans. Source: scan-69.jpg."),
    (11, "BDK-01 had wastage but the highest outlet is unknown."),
    (12, "INV-042509-752 appears in the records."),
    (13, "The difference is SGD 9.33. GST is the cause, but data is not complete."),
    (9, '{"finding": "There are 12 outlets.", "citation": "11 outlets"}'),
    (9, '{"citation": "11 outlets"}'),
])
def test_evaluation_rejects_keyword_only_or_wrong_answers(index, answer):
    assert check_answer(CASES[index], answer)


def test_live_runner_reports_failure_without_printing_answers(monkeypatch, capsys):
    answers = {
        case.question: case.refusal or answer
        for case, answer in zip(CASES, SYNTHETIC_ANSWERS + [""] * (len(CASES) - len(SYNTHETIC_ANSWERS)))
    }
    answers[CASES[0].question] = "wrong answer"

    def fake_stream(thread_id, question):
        if question == CASES[0].question:
            yield AIMessage(content=SYNTHETIC_ANSWERS[0], tool_calls=[
                {"name": "lookup", "args": {}, "id": "1"},
            ]), {}
        yield AIMessage(content=answers[question]), {}

    chatbot = Mock()
    chatbot.cache_info.return_value.currsize = 0
    monkeypatch.setattr(run_guardrail_eval, "AppConfig", lambda: Mock(llm_proxy_url=""))
    monkeypatch.setattr(chat_backend, "get_chatbot", chatbot)
    monkeypatch.setattr(chat_backend, "stream_chat", fake_stream)
    monkeypatch.setattr(st, "session_state", {})

    assert run_guardrail_eval.main() == 1
    output = capsys.readouterr().out
    assert "21/22 passed" in output
    assert "wrong answer" not in output


@pytest.mark.parametrize("payload", [
    {
        "finding": "Alex earns $15 per hour.",
        "key_evidence": ["Call Alex at +65 9123 4567."],
        "limitations": [],
        "recommendation": None,
    },
    {
        "finding": {"phone": "+65 9123 4567"},
        "key_evidence": [],
        "limitations": [],
        "recommendation": None,
    },
    {
        "finding": "Summary available.",
        "key_evidence": [{"pay": "Alex earns $15 per hour."}],
        "limitations": [],
        "recommendation": None,
    },
    {
        "finding": "Summary available.",
        "key_evidence": [],
        "limitations": [],
        "recommendation": None,
        "extra": "Call +65 9123 4567.",
    },
    {
        "finding": "Summary available.",
        "key_evidence": [],
        "limitations": [],
        "recommendation": {"detail": "Call +65 9123 4567."},
    },
    "Call +65 9123 4567.",
])
def test_output_guardrail_scrubs_confidential_structured_model_answer(
    payload, tmp_path, monkeypatch,
):
    model = Mock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content=json.dumps(payload))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("KAYA_CHAT_DB", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setattr(chat_backend, "ChatOpenAI", Mock(return_value=model))
    monkeypatch.setattr(chat_backend, "ALL_TOOLS", [])
    monkeypatch.setitem(st.session_state, "usage_tracker", Mock())
    chat_backend.get_chatbot.cache_clear()
    try:
        messages = list(chat_backend.stream_chat("output-guardrail", "Summarize this record."))
        answer = next(
            message.content for message, _ in reversed(messages)
            if isinstance(message, AIMessage) and not message.tool_calls
        )
        if (
            isinstance(payload, dict)
            and isinstance(payload["finding"], str)
            and payload["finding"].startswith("Alex earns")
        ):
            assert json.loads(answer)["finding"] == FINANCE_REFUSAL
        assert "9123 4567" not in answer
        assert "$15 per hour" not in answer
        assert "9123 4567" not in format_copilot_answer(answer)
        assert scrub_pdpa(answer) == answer
    finally:
        chat_backend.get_chatbot(None).checkpointer.conn.close()
        chat_backend.get_chatbot.cache_clear()
