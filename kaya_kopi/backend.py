"""LangGraph backend (course NB 00–11 pattern).

Graph:  user -> guard_in -> agent (LLM + tools, ReAct loop) -> tool -> ...
        -> guard_out -> END
State is checkpointed with SqliteSaver so a conversation survives a
restart (HARNESS_CHECKLIST box 5).

LangGraph 1.x notes (verified on the laptop):
  * SqliteSaver takes a sqlite3.Connection (check_same_thread=False), not a path.
  * Tools must have a docstring.
  * Mermaid: compiled_graph.get_graph().draw_mermaid()
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

import operator

from dotenv import load_dotenv

# Load the project .env (repo root) if present — OPENAI_API_KEY etc.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END, add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.messages import AIMessage

from kaya_kopi.guardrails import (
    InputBlockedError,
    check_input,
    sanitize_output,
)
from kaya_kopi.spend import SpendTracker
from kaya_kopi.tools import ALL_TOOLS

CHECKPOINT_DB = os.environ.get("KAYA_CHECKPOINTS", "checkpoints.sqlite")


class State(TypedDict):
    messages: Annotated[list, add_messages]
    citations: Annotated[list, operator.add]
    error: str | None


def _llm(model: str | None = None):
    model = model or os.environ.get("KAYA_MODEL", "gpt-4o-mini")
    return ChatOpenAI(model=model, temperature=0)


_LLM_CACHE: dict = {}


def _get_llm(model: str | None):
    """Lazy, cached LLM — the graph compiles and the app boots with no
    API key; the key is only needed when a message is actually sent."""
    key = model or os.environ.get("KAYA_MODEL", "gpt-4o-mini")
    if key not in _LLM_CACHE:
        _LLM_CACHE[key] = _llm(key).bind_tools(ALL_TOOLS)
    return _LLM_CACHE[key]


def _system_prompt() -> str:
    return (
        "You are the Kaya Kopi operations copilot. You answer questions about "
        "sales, margins, wastage, staffing and suppliers for 12 outlets. "
        "RULES: (1) Use the provided tools to get data — never answer from "
        "memory. (2) Cite the source you used in every numeric answer "
        "(e.g. 'per mysql:sales', 'per invoice INV-0042'). "
        "(3) If a tool returns an error or no data, say plainly that the "
        "question cannot be answered with the available data — do not guess. "
        "(4) Keep answers short and operational."
    )


def _msg_text(m) -> str:
    """Content of a state message — handles both Message objects and (role, text) tuples."""
    if isinstance(m, (tuple, list)) and len(m) == 2:
        return str(m[1])
    return str(getattr(m, "content", ""))


def build_graph(checkpointer=None, tracker: SpendTracker | None = None,
                model: str | None = None):
    """Compile the graph.

    If no checkpointer is given, one is created on the CHECKPOINT_DB file
    (set KAYA_CHECKPOINTS, default 'checkpoints.sqlite'). Pass
    `SqliteSaver(sqlite3.connect(':memory:'))` for throwaway runs (evals).
    """
    tracker = tracker or SpendTracker()

    def guard_in(state: State) -> dict:
        # input guardrail (box 3)
        last = _msg_text(state["messages"][-1]) if state["messages"] else ""
        check_input(last)  # raises -> surfaced by frontend
        return {}

    def agent(state: State) -> dict:
        msgs = [_system_prompt()] + list(state["messages"])
        import time
        t0 = time.time()
        resp = _get_llm(model).invoke(msgs)
        usage = getattr(resp, "usage_metadata", None) or {}
        tracker.record(
            model=model or "gpt-4o-mini",
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            duration_s=time.time() - t0,
        )
        return {"messages": [resp]}

    def guard_out(state: State) -> dict:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.content:
            clean = sanitize_output(last.content)
            if clean != last.content:
                return {"messages": [AIMessage(content=clean)]}
        return {}

    g = StateGraph(State)
    g.add_node("guard_in", guard_in)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(ALL_TOOLS))
    g.add_node("guard_out", guard_out)

    g.add_edge(START, "guard_in")
    g.add_edge("guard_in", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "guard_out"})
    g.add_edge("tools", "agent")
    g.add_edge("guard_out", END)

    if checkpointer is None:
        conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    return g.compile(checkpointer=checkpointer)


def mermaid() -> str:
    """Architecture diagram for docs/ (box: 'at least two Mermaid diagrams')."""
    return build_graph(checkpointer=SqliteSaver(sqlite3.connect(":memory:"))).get_graph().draw_mermaid()


if os.environ.get("KAYA_BUILD") == "1":
    chatbot = build_graph()
