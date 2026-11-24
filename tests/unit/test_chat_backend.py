"""Tests for recovery from interrupted tool calls."""

import os
import tempfile
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

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
