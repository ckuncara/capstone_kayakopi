"""Tests for the harness model/tool loop without network or database access."""

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode

from kaya_kopi.copilot.harness_tools import run_tool_chat
from kaya_kopi.copilot.tools.kaya import (
    ALL_TOOLS,
    InvoiceToolContext,
    lookup_invoice_sqlite,
    search_invoice_chroma,
)
from kaya_kopi.config import AppConfig
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.security.budget_guard import BudgetGuard


@tool
def invoice_search(query: str) -> str:
    """Search invoice text."""
    return f"invoice: {query}"


@tool
def sales_lookup(outlet: str) -> dict:
    """Look up sales."""
    return {"outlet": outlet, "sales": 10}


@tool("analyze_pos_sql")
def analyze_pos(analysis: str, outlet_codes: list[str]) -> dict:
    """Look up POS analytics."""
    return {"analysis": analysis, "outlet_codes": outlet_codes}


@tool("assess_kopi_price_change_sql")
def assess_price(outlet_codes: list[str]) -> dict:
    """Assess price changes."""
    return {"outlet_codes": outlet_codes}


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.inputs.append(list(messages))
        return next(self.responses)


class ToolChatTest(unittest.TestCase):
    def test_multiple_tool_calls_are_returned_to_model_and_logged(self):
        model = FakeModel([
            AIMessage(content="", tool_calls=[
                {"name": "sales_lookup", "args": {"outlet": "AMK-01"}, "id": "one"},
                {"name": "invoice_search", "args": {"query": "coffee"}, "id": "two"},
            ]),
            AIMessage(content="Combined answer"),
        ])
        checks, responses, tools = [], [], []
        answer, turn = run_tool_chat(
            model, [sales_lookup, invoice_search], [HumanMessage(content="Compare")],
            lambda: checks.append(True),
            lambda response, messages: responses.append(response),
            lambda name, result: tools.append((name, result)),
        )
        self.assertEqual(answer.content, "Combined answer")
        self.assertEqual(len(checks), 2)
        self.assertEqual(len(responses), 2)
        self.assertEqual([item[0] for item in tools], ["sales_lookup", "invoice_search"])
        self.assertEqual([message.tool_call_id for message in model.inputs[1]
                          if isinstance(message, ToolMessage)], ["one", "two"])
        self.assertIn("AMK-01", tools[0][1])
        self.assertEqual(tools[1][1], "invoice: coffee")
        self.assertEqual(len(turn), 4)

    def test_budget_is_checked_before_each_model_call(self):
        model = FakeModel([AIMessage(content="", tool_calls=[
            {"name": "invoice_search", "args": {"query": "coffee"}, "id": "one"},
        ])])
        checks = []

        def check():
            checks.append(True)
            if len(checks) == 2:
                raise RuntimeError("budget exceeded")

        with self.assertRaisesRegex(RuntimeError, "budget exceeded"):
            run_tool_chat(model, [invoice_search], [], check, lambda *_: None, lambda *_: None)
        self.assertEqual(len(model.inputs), 1)

    def test_stops_when_model_keeps_calling_tools(self):
        model = FakeModel([AIMessage(content="", tool_calls=[
            {"name": "invoice_search", "args": {"query": "coffee"}, "id": "one"},
        ])])
        with self.assertRaisesRegex(RuntimeError, "tool-calling limit"):
            run_tool_chat(
                model, [invoice_search], [], lambda: None, lambda *_: None, lambda *_: None,
                max_model_calls=1,
            )

    def test_shared_price_scope_does_not_filter_price_observations(self):
        model = FakeModel([
            AIMessage(content="", tool_calls=[
                {"name": "analyze_pos_sql",
                 "args": {"analysis": "observed_prices", "outlet_codes": ["AMK-01"]},
                 "id": "price"},
                {"name": "analyze_pos_sql",
                 "args": {"analysis": "product_mix", "outlet_codes": ["AMK-01"]},
                 "id": "sales"},
                {"name": "assess_kopi_price_change_sql",
                 "args": {"outlet_codes": ["AMK-03"]},
                 "id": "scenario"},
            ]),
            AIMessage(content="Done"),
        ])
        results = []
        run_tool_chat(
            model, [analyze_pos, assess_price], [], lambda: None, lambda *_: None,
            lambda name, result: results.append(result), shared_price_scope=True,
        )
        self.assertIn('"outlet_codes": []', results[0])
        self.assertIn("price analysis covers all outlets", results[0])
        self.assertIn('"outlet_codes": ["AMK-01"]', results[1])
        self.assertIn('"outlet_codes": []', results[2])


class InvoiceToolTest(unittest.TestCase):
    def test_sqlite_tool_uses_shared_scope_and_redacts_text(self):
        repository = Mock(spec=SQLiteOpsRepository)
        context = InvoiceToolContext(repository, AppConfig(api_key="test"), Mock(spec=BudgetGuard))
        config = {"configurable": {"kaya_invoice_context": context}}
        with patch("kaya_kopi.invoice_chat.sqlite_invoice_answer") as answer:
            answer.return_value = "Contact test@example.com"
            result = lookup_invoice_sqlite.invoke({"question": "Find test@example.com"}, config=config)
            self.assertNotIn("test@example.com", result)
            self.assertNotIn("test@example.com", answer.call_args.args[0])
            self.assertEqual(answer.call_args.args[1:], ("HQ", repository))
            self.assertEqual(set(lookup_invoice_sqlite.tool_call_schema.model_json_schema()["properties"]), {"question"})
            self.assertEqual(set(search_invoice_chroma.tool_call_schema.model_json_schema()["properties"]), {"query"})
            self.assertIn(lookup_invoice_sqlite, ALL_TOOLS)
            self.assertIn(search_invoice_chroma, ALL_TOOLS)
            answer.return_value = None
            self.assertIn(
                "does not support", lookup_invoice_sqlite.invoke({"question": "Other"}, config=config)
            )

    def test_chroma_tool_uses_session_settings_and_collects_only_its_hits(self):
        cfg = AppConfig(api_key="test")
        budget = Mock(spec=BudgetGuard)
        hits = [({"invoice_number": "INV-1", "audit_status": "PASS"}, "Invoice text")]
        first_hits, second_hits = [], []
        first = InvoiceToolContext(Mock(spec=SQLiteOpsRepository), cfg, budget, 5, first_hits)
        second = InvoiceToolContext(Mock(spec=SQLiteOpsRepository), cfg, budget, 2, second_hits)
        with patch("kaya_kopi.repositories.chroma_store.SharedChromaRepository") as factory:
            factory.return_value.semantic_search.side_effect = [hits, []]
            self.assertIn(
                "Invoice text",
                search_invoice_chroma.invoke(
                    {"query": "Find test@example.com"},
                    config={"configurable": {"kaya_invoice_context": first}},
                ),
            )
            factory.assert_called_with(cfg, budget_guard=budget)
            first_search = factory.return_value.semantic_search.call_args.kwargs
            self.assertNotIn("test@example.com", first_search["query_text"])
            self.assertEqual(first_search["branch_code"], None)
            self.assertEqual(first_search["source_type"], "invoice")
            self.assertEqual(first_search["n_results"], 5)
            self.assertEqual(first_hits, hits)
            self.assertIn(
                "No invoice chunks found",
                search_invoice_chroma.invoke(
                    {"query": "Missing"}, config={"configurable": {"kaya_invoice_context": second}}
                ),
            )
            self.assertEqual(factory.return_value.semantic_search.call_args.kwargs["n_results"], 2)
            self.assertEqual(second_hits, [])

    def test_default_configuration_and_invalid_context(self):
        repository = Mock(spec=SQLiteOpsRepository)
        with patch("kaya_kopi.repositories.sqlite_ops.SQLiteOpsRepository", return_value=repository), \
             patch("kaya_kopi.invoice_chat.sqlite_invoice_answer", return_value="invoice") as answer:
            self.assertIn("invoice", lookup_invoice_sqlite.invoke({"question": "Find invoice"}))
            self.assertIs(answer.call_args.args[2], repository)
        with self.assertRaisesRegex(TypeError, "InvoiceToolContext"):
            lookup_invoice_sqlite.invoke(
                {"question": "Find invoice"}, config={"configurable": {"kaya_invoice_context": "bad"}}
            )

    def test_graph_tool_node_receives_per_invocation_context(self):
        repository = Mock(spec=SQLiteOpsRepository)
        context = InvoiceToolContext(
            repository, AppConfig(api_key="sensitive-placeholder-key"), Mock(spec=BudgetGuard)
        )
        builder = StateGraph(MessagesState)
        builder.add_node("tools", ToolNode([lookup_invoice_sqlite]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(connection.close)
        graph = builder.compile(checkpointer=SqliteSaver(connection))
        message = AIMessage(
            content="",
            tool_calls=[{"id": "call", "name": "lookup_invoice_sqlite", "args": {"question": "Find invoice"}}],
        )
        with patch("kaya_kopi.invoice_chat.sqlite_invoice_answer", return_value="invoice") as answer:
            result = graph.invoke(
                {"messages": [message]},
                config={"configurable": {"thread_id": "test", "kaya_invoice_context": context}},
            )
        self.assertIn("invoice", result["messages"][-1].content)
        self.assertIs(answer.call_args.args[2], repository)
        for (metadata,) in connection.execute("SELECT metadata FROM checkpoints"):
            self.assertNotIn(b"sensitive-placeholder-key", metadata)
            self.assertNotIn(b"kaya_invoice_context", metadata)


def test_mysql_tools_import_without_optional_ocr_dependencies():
    src = Path(__file__).resolve().parents[2] / "src"
    code = """
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "PIL" or name.startswith("PIL."):
        raise ModuleNotFoundError("PIL is unavailable")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import kaya_kopi.copilot.tools.kaya
assert "kaya_kopi.config" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(src)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    unittest.main()
