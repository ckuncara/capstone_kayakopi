"""LangGraph tool-calling backend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Iterator, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker  # Added for LLM spend meter (llm_usage_tracker.py)
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
Local uploaded-invoice tools use SQLite and ChromaDB, which are separate from the MySQL data.
Identify the source and do not present one database's results as records from the other.
"""


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _repair_interrupted_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Supply error results for calls interrupted before their tool results were saved."""
    repaired: list[BaseMessage] = []
    pending: dict[str, str] = {}

    def finish_pending() -> None:
        for call_id, name in pending.items():
            repaired.append(ToolMessage(
                content="This tool call was interrupted; no result is available.",
                tool_call_id=call_id,
                name=name,
                status="error",
            ))
        pending.clear()

    for message in messages:
        if not isinstance(message, ToolMessage):
            finish_pending()
        repaired.append(message)
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if not call["id"]:
                    raise ValueError("A saved tool call is missing its ID.")
                pending[call["id"]] = call["name"]
        elif isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
    finish_pending()
    return repaired


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
        messages = _repair_interrupted_tool_calls(state["messages"])
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
        
        # Invoke model and capture response with token usage (llm_usage_tracker.py)
        response = model_with_tools.invoke(messages)
        
        # Store response metadata with tokens in state for later capture (llm_usage_tracker.py)
        if hasattr(response, 'response_metadata') and response.response_metadata:
            state['_last_response_metadata'] = response.response_metadata
        
        return {"messages": [response]}

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


def stream_chat(thread_id: str, message: str) -> Iterator[tuple[BaseMessage, dict]]:
    """Stream one user turn through the LLM/tool loop, capturing token usage."""
    from langchain_core.messages import HumanMessage, AIMessage
    import streamlit as st

    config = {"configurable": {"thread_id": thread_id}}
    
    # Get the shared tracker from Streamlit session state (llm_usage_tracker.py, llm_ui_components.py)
    if "usage_tracker" not in st.session_state:
        st.session_state.usage_tracker = LLMUsageTracker()
    tracker = st.session_state.usage_tracker
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    
    # Stream with stream_mode="updates" to capture token data
    # In "updates" mode, we get deltas like {"chat": state} or {"tools": state}
    tokens_logged = False
    
    for update in get_chatbot().stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode="updates",
    ):
        # In "updates" mode, each update is a dict: {"node_name": state_update}
        for node_name, node_state in update.items():
            if "messages" in node_state:
                messages = node_state["messages"]
                if messages:
                    for msg in messages:
                        # Log tokens from the first AIMessage we encounter (after initial prompt)
                        if isinstance(msg, AIMessage) and not tokens_logged:
                            if hasattr(msg, 'usage_metadata') and msg.usage_metadata:
                                usage_meta = msg.usage_metadata
                                if isinstance(usage_meta, dict):
                                    input_tokens = usage_meta.get('input_tokens', 0)
                                    output_tokens = usage_meta.get('output_tokens', 0)
                                    
                                    # Log if we have tokens (llm_usage_tracker.py)
                                    if input_tokens > 0 or output_tokens > 0:
                                        tracker.log_usage(thread_id, message, model_name, input_tokens, output_tokens)
                                        tokens_logged = True
                        
                        # Yield every message for the UI to handle
                        yield (msg, {})
