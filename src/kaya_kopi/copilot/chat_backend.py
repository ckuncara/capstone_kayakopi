"""LangGraph tool-calling backend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Iterator, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from kaya_kopi.copilot.tools import ALL_TOOLS
from kaya_kopi.paths import project_root


SYSTEM_PROMPT = """You are the Kaya Kopi operations copilot.

Answer questions only from facts returned by the available tools. Never calculate or invent a
business figure from memory. Use a tool again for follow-up questions when current data is needed.
Preserve source citations returned by tools in the final answer. If a tool fails or the evidence is
missing, say exactly what could not be checked. Scanned supplier invoices are authoritative for
supplier costs when they conflict with the legacy purchase table; if an invoice header is internally
inconsistent, use validated line arithmetic and disclose the review flag. Keep answers concise and use SGD
for monetary amounts. The tools are read-only; do not claim to change client data.

Follow tool status, conclusions, authority, comparison scope, exclusions, and limitations exactly.
Do not rank records marked non-comparable. Do not turn missing evidence into zero, no change, or a
negative finding. MATCHED reconciliation means that a purchase and scan were linked; it does not mean
their amounts agree. A difference between two pre-GST subtotals is not caused by GST. Do not classify
an outlet as overstaffed without an approved threshold. When asked for supplier spending by month,
use supplier_month grouping and report excluded invoices. When asked which supplier increased prices
most, report the largest comparable percentage change and its first/latest invoice-line evidence.
"""


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _chat_database_path() -> Path:
    path = Path(os.getenv("KAYA_CHAT_DB", ".kaya_chat.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=1)
def get_chatbot():
    """Build and cache the persistent tool-calling graph."""
    load_dotenv(project_root() / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add the team API key."
        )

    model = ChatOpenAI(
        model=os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
        timeout=30,
        max_retries=2,
    )
    model_with_tools = model.bind_tools(ALL_TOOLS)

    def chat_node(state: ChatState) -> dict[str, list[BaseMessage]]:
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
        return {"messages": [model_with_tools.invoke(messages)]}

    connection = sqlite3.connect(_chat_database_path(), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    graph = StateGraph(ChatState)
    graph.add_node("chat", chat_node)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "chat")
    return graph.compile(checkpointer=checkpointer)


def retrieve_all_threads() -> list[str]:
    """Return saved conversation IDs, newest checkpoint first."""
    chatbot = get_chatbot()
    seen: set[str] = set()
    thread_ids: list[str] = []
    for checkpoint in chatbot.checkpointer.list(None):
        thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            thread_ids.append(thread_id)
    return thread_ids


def _content_str(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def stream_chat(thread_id: str, message: str) -> Iterator[tuple[BaseMessage, dict]]:
    """Stream one user turn through the LLM/tool loop, with guardrails applied.

    Input guard  - check_input blocks prompt-injection / oversized input before
                   the LLM is called; a blocked turn yields a single refusal.
    Output guard - every AI message (and every tool result) is passed through
                   sanitize_output so PII is stripped. Sanitization is applied
                   to the *complete* message, not per token, because a PII pattern
                   (phone / NRIC / email) can be split across streamed chunks.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from kaya_kopi.guardrails import InputBlockedError, check_input, sanitize_output

    # --- input guard -------------------------------------------------------
    try:
        check_input(message)
    except InputBlockedError as exc:
        yield (
            AIMessage(
                content=(
                    "I can't work with that request as phrased. "
                    "I'm a read-only operations assistant, so I can help with sales, "
                    "invoices, suppliers, documents, and data-quality questions."
                )
            ),
            {},
        )
        return

    config = {"configurable": {"thread_id": thread_id}}
    ai_buf: list[str] = []
    last_meta: dict = {}

    for item in get_chatbot().stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode="messages",
    ):
        msg, meta = item
        last_meta = meta if isinstance(meta, dict) else {}
        if isinstance(msg, AIMessage):
            text = _content_str(msg.content)
            if text:
                ai_buf.append(text)
            continue

        # A non-AI message closes the current AI segment -> flush it sanitized.
        if ai_buf:
            yield (AIMessage(content=sanitize_output("".join(ai_buf))), last_meta)
            ai_buf = []

        if isinstance(msg, ToolMessage):
            yield (
                ToolMessage(
                    content=sanitize_output(_content_str(msg.content)),
                    name=msg.name,
                    tool_call_id=msg.tool_call_id,
                ),
                last_meta,
            )
        else:
            yield (msg, last_meta)

    if ai_buf:
        yield (AIMessage(content=sanitize_output("".join(ai_buf))), last_meta)
