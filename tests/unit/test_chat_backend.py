"""Tests for recovery from interrupted tool calls."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from streamlit.testing.v1 import AppTest

from kaya_kopi.copilot import chat_backend


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
        self.addCleanup(chat_backend.get_chatbot.cache_clear)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "test", "KAYA_CHAT_DB": f"{directory}/chat.sqlite3"}), \
             patch.object(chat_backend, "ChatOpenAI", return_value=model), \
             patch.object(chat_backend, "ALL_TOOLS", []):
            graph = chat_backend.get_chatbot()
            for thread_id in ("first", "second"):
                graph.invoke({"messages": [HumanMessage(content="Hi")]},
                             {"configurable": {"thread_id": thread_id}})
            chat_backend.rename_thread("first", "  Supplier review  ")
            self.assertEqual(chat_backend.get_thread_names(), {"first": "Supplier review"})
            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                chat_backend.rename_thread("first", "  ")
            with self.assertRaisesRegex(ValueError, "no longer exists"):
                chat_backend.rename_thread("missing", "Name")
            chat_backend.get_chatbot.cache_clear()
            self.assertEqual(chat_backend.get_thread_names(), {"first": "Supplier review"})

            chat_backend.delete_thread("first")

            self.assertEqual(chat_backend.retrieve_all_threads(), ["second"])
            self.assertEqual(chat_backend.get_thread_names(), {})
            with sqlite3.connect(f"{directory}/chat.sqlite3") as connection:
                for table in ("checkpoints", "writes"):
                    self.assertEqual(connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE thread_id=?", ("first",)
                    ).fetchone()[0], 0)
            state = chat_backend.get_chatbot().get_state({"configurable": {"thread_id": "second"}})
            self.assertTrue(state.values["messages"])

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
        self.addCleanup(chat_backend.get_chatbot.cache_clear)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "test", "KAYA_CHAT_DB": f"{directory}/chat.sqlite3"}), \
             patch.object(chat_backend, "ChatOpenAI", return_value=model), \
             patch.object(chat_backend, "ALL_TOOLS", [interrupted_search]):
            graph = chat_backend.get_chatbot()
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
