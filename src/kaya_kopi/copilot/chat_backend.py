"""LangGraph tool-calling backend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Iterator, TypedDict
from zoneinfo import ZoneInfo
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import (
    OpenAIAuthenticationError,
    OpenAIInvalidRequestError,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker  # Added for LLM spend meter (llm_usage_tracker.py)
from kaya_kopi.copilot.tools import ALL_TOOLS
from kaya_kopi.config import AppConfig
from kaya_kopi.security.openai_proxy import (
    message_text,
    model_temperature_kwargs,
    openai_client_options,
    reasoning_effort_kwargs,
)
from kaya_kopi.paths import project_root
from kaya_kopi.security.guardrails import FINANCE_REFUSAL, protect_answer, protect_text, refuse_request


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Kaya Kopi operations copilot.

Answer questions only from facts returned by the available tools. Never calculate or invent a
business figure from memory. Use a tool again for follow-up questions when current data is needed.
Preserve source citations returned by tools in the final answer. If a tool fails or the evidence is
missing, say exactly what could not be checked. Scanned supplier invoices are authoritative for
supplier costs when they conflict with the legacy purchase table; if an invoice header is internally
inconsistent, use validated line arithmetic and disclose the review flag. Keep answers concise and use SGD
for monetary amounts. The tools are read-only; do not claim to change client data.

Do not disclose personal contact details, home addresses, individual pay, performance, or discipline.
Names of people who reported operational issues may be shown.
Do not offer legal or tax advice, price recommendations, or make decisions for the user.

For supplier business-address questions, use get_supplier_addresses_sql with the full recorded
supplier name. Ask for the supplier name if it is missing. The lookup returns at most 10 invoice-backed
records, not a complete address history. If results are empty, say no recorded address matched that
exact name. If addresses differ, report the differences and their invoice references; do not infer
which address is current.

Follow tool status, conclusions, authority, comparison scope, exclusions, and limitations exactly.
Do not rank records marked non-comparable. Do not turn missing evidence into zero, no change, or a
negative finding. MATCHED reconciliation means that a purchase and scan were linked; it does not mean
their amounts agree. A difference between two pre-GST subtotals is not caused by GST. Do not classify
an outlet as overstaffed without an approved threshold. When asked for supplier spending by month,
use supplier_month grouping and report excluded invoices. When asked which supplier increased prices
most, report the largest comparable percentage change and its first/latest invoice-line evidence.
Local uploaded-invoice tools use SQLite and ChromaDB, which are separate from the MySQL data.
Identify the source and do not present one database's results as records from the other.

Use the supplied Singapore date for relative dates. This quarter and this year start at the
beginning of the current calendar quarter and year, respectively, and end today. Keep that
requested period even if the source data ends earlier. No records in a period means no
observations, not zero sales. For Kopi margin, use analyze_pos_sql with analysis="margin".
For mall outlets, pass outlet_type="mall" and let the database resolve the outlet codes.
Outlet-wide takings and product mix cannot establish a product margin. Report the margin
tool's missing inputs when it returns insufficient evidence. If the tool gives a
last_observed_date, explicitly state that the available POS records end on that date.

For staff-count questions (including how many baristas work at an outlet), call analyze_outlets_sql
with analysis="headcount". Use its monthly role counts, not staffing cost data or document search.
Omit both dates for the latest roster month unless the user specifies a period. State the roster
month and distinguish rostered headcount from actual attendance. Missing roster data is not zero.
Do not sum counts across months or roles to infer unique staff.

For labour cost, payroll, or overstaffing questions, call analyze_outlets_sql with analysis="staffing"
in the same turn. Use outlet_type="mall" for mall stalls. When the user gives no date and there
is no prior analysis to continue, omit both dates so the tool selects the latest roster month.
Show the scheduled labour cost and observed POS takings as separate figures, with the coverage
limitation and any missing benchmark. For undated wastage, call the same tool with analysis="wastage"
and omit both dates. Answer from the tool result in the current turn.

For MySQL analysis follow-ups, reuse the last relevant query's effective dates, supplier, item,
and outlets unless the user changes them. Use the dates returned by the tool when it defaulted
the period. If the user names specific outlets, use those codes. For a request to see the invoice
evidence, start with the exact invoice numbers and line citations returned by the price query.

For item-level purchase totals such as "how much teh we purchase in 2025", do not call
analyze_suppliers_sql with analysis="spend": it aggregates whole invoices and ignores its item
filter. Use direct MySQL retrieval: discover_mysql_schema, then one SELECT joining
supplier_invoice_item to supplier_invoice (and source_document for source_uri), matching the
recorded description such as LIKE '%tea%' for teh/tea, with the requested date filter. Report the
matched description, the summed pre-GST amount, the invoices counted, and the source citations.
If a GROUP BY would hide source_uri, add GROUP_CONCAT(DISTINCT source_document.source_uri) AS
source_uris; the app parses comma-separated paths. If the same item also appears in
uploaded_invoice_lines, keep client and uploaded evidence separate and never add them together.

For database questions, especially any request that says "directly from the database" or asks
for a comparison, total, or list across tables, use direct MySQL retrieval first: call
discover_mysql_schema, then query_mysql_direct with one SELECT using only the returned tables and
exact columns. Use a specialist tool instead when it supplies validation or provenance that a
plain query cannot, such as supplier addresses, headcount, margin coverage, invoice evidence, or
price-change analysis; then follow that tool's constraints exactly. The direct tool can read both
the enabled client tables and Upload Desk uploaded_* tables. Keep those sources distinct, do not
double-count the same purchase in both, and never mix currencies. Return source_file or source_uri
before calling an uploaded-file citation; client-table figures are database results, not uploaded
files. A successful empty result means no matching rows, not zero activity. Do not use direct SQL
to infer unit cost, full expense allocation, legal or tax conclusions.
"""


DIRECT_DATABASE_REQUEST = re.compile(
    r"\b(?:directly|straight)\s+from\s+(?:the\s+)?(?:mysql\s+)?database\b"
    r"|\bquery\s+(?:the\s+)?database\b"
    r"|\bdatabase\s+query\b"
    r"|\bhow\s+much\b.{0,80}\b(?:purchase|purchased|buy|bought|spend|spent)\b",
    re.IGNORECASE,
)


def _current_system_message(question: str | None = None) -> SystemMessage:
    today = datetime.now(ZoneInfo("Asia/Singapore")).date().isoformat()
    content = f"Today's date in Asia/Singapore is {today}.\n\n{SYSTEM_PROMPT}"
    if question and DIRECT_DATABASE_REQUEST.search(question):
        content += (
            "\n\nThe user explicitly requested direct database retrieval. Call "
            "discover_mysql_schema first, then query_mysql_direct, before any specialist tool."
        )
    return SystemMessage(content=content)


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _safe_tool_value(value: object, key: str = "", allow_supplier_address: bool = False) -> object:
    if key in {"staff_name", "hourly_rate_sgd", "raw_hourly_rate", "hourly_rate", "pay_rate"}:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            name: _safe_tool_value(item, name, allow_supplier_address)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_tool_value(item, allow_supplier_address=allow_supplier_address) for item in value]
    if isinstance(value, str):
        if key == "supplier_address" and allow_supplier_address:
            return protect_text(value, [value])
        return protect_answer(value)
    return value


def _safe_tool_message(message: ToolMessage) -> ToolMessage:
    content = message.content
    if not isinstance(content, str):
        return message.model_copy(update={"content": "Tool output withheld: unsupported format."})
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        content = json.dumps(
            _safe_tool_value(
                value,
                allow_supplier_address=message.name in {
                    "analyze_suppliers_sql", "get_supplier_addresses_sql",
                },
            ),
            ensure_ascii=False,
        )
    else:
        content = protect_answer(content)
    return message.model_copy(update={"content": content, "artifact": None})


def _safe_ai_message(message: AIMessage, addresses: list[str]) -> AIMessage:
    content = message.content
    if isinstance(content, str):
        content = protect_answer(content, addresses)
    else:
        # Some models return text as content blocks instead of one plain string.
        text = message_text(content)
        content = protect_answer(text, addresses) if text else "No answer was returned."
    calls = [
        {**call, "args": _safe_tool_value(call["args"])}
        for call in message.tool_calls
    ]
    extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
    return message.model_copy(
        update={"content": content, "tool_calls": calls, "additional_kwargs": extra}
    )


def _supplier_addresses(messages: list[BaseMessage]) -> list[str]:
    addresses = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {
            "analyze_suppliers_sql", "get_supplier_addresses_sql",
        }:
            continue
        try:
            result = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        if message.name == "analyze_suppliers_sql":
            records = [result.get("invoice")]
        else:
            records = result.get("results", [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            address = record.get("supplier_address")
            if isinstance(address, str) and address:
                addresses.append(address)
    return addresses


def _has_tool_result(messages: list[BaseMessage]) -> bool:
    for message in reversed(messages):
        if message.type == "human":
            break
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        try:
            result = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            return bool(message.content)
        if isinstance(result, dict) and result.get("ok") is False:
            continue
        if result:
            return True
    return False


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
def get_chatbot(access_token: str | None = None):
    """Build and cache the persistent tool-calling graph."""
    load_dotenv(project_root() / ".env")
    cfg = AppConfig()
    if not cfg.llm_proxy_url and not cfg.api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add the team API key."
        )
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-6-luna")
    model = ChatOpenAI(
        model=model_name,
        **model_temperature_kwargs(model_name, 0),
        **reasoning_effort_kwargs(model_name),
        timeout=30,
        max_retries=2,
        **openai_client_options(cfg, "chat", access_token=access_token),
    )
    model_with_tools = model.bind_tools(ALL_TOOLS)

    tool_node = ToolNode(ALL_TOOLS)

    def safe_tools(state: ChatState, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        result = tool_node.invoke(state, config=config)
        return {
            "messages": [
                _safe_tool_message(message) if isinstance(message, ToolMessage) else message
                for message in result["messages"]
            ]
        }

    def chat_node(state: ChatState) -> dict[str, list[BaseMessage]]:
        from langchain_core.messages import HumanMessage

        messages = []
        question = ""
        for message in _repair_interrupted_tool_calls(state["messages"]):
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, ToolMessage):
                message = _safe_tool_message(message)
            elif isinstance(message, HumanMessage) and isinstance(message.content, str):
                question = message.content
                message = message.model_copy(update={"content": protect_text(message.content)})
            elif isinstance(message, AIMessage):
                message = _safe_ai_message(message, _supplier_addresses(messages))
            messages.append(message)
        messages.insert(0, _current_system_message(question))
        
        # Invoke model and capture response with token usage (llm_usage_tracker.py)
        response = model_with_tools.invoke(messages)
        response = _safe_ai_message(response, _supplier_addresses(messages))
        if (
            not response.tool_calls
            and response.content
            and response.content != FINANCE_REFUSAL
            and not _has_tool_result(messages)
        ):
            response = response.model_copy(
                update={"content": "I couldn't verify an answer from the available data."}
            )
        
        # Store response metadata with tokens in state for later capture (llm_usage_tracker.py)
        if hasattr(response, 'response_metadata') and response.response_metadata:
            state['_last_response_metadata'] = response.response_metadata
        
        return {"messages": [response]}

    connection = sqlite3.connect(_chat_database_path(), check_same_thread=False)
    checkpointer = SqliteSaver(connection)
    graph = StateGraph(ChatState)
    graph.add_node("chat", chat_node)
    graph.add_node("tools", safe_tools)
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "chat")
    return graph.compile(checkpointer=checkpointer)


def retrieve_all_threads(access_token: str | None = None) -> list[str]:
    """Return saved conversation IDs, newest checkpoint first."""
    chatbot = get_chatbot(access_token)
    seen: set[str] = set()
    thread_ids: list[str] = []
    for checkpoint in chatbot.checkpointer.list(None):
        thread_id = checkpoint.config.get("configurable", {}).get("thread_id")
        if thread_id and thread_id not in seen:
            seen.add(thread_id)
            thread_ids.append(thread_id)
    return thread_ids


def get_thread_names(access_token: str | None = None) -> dict[str, str]:
    """Return saved conversation names keyed by thread ID."""
    with get_chatbot(access_token).checkpointer.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS conversation_names (thread_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        return dict(cursor.execute("SELECT thread_id, name FROM conversation_names").fetchall())


def rename_thread(thread_id: str, name: str, access_token: str | None = None) -> None:
    """Save a name for an existing conversation."""
    name = protect_text(name.strip())
    if not name:
        raise ValueError("Conversation name cannot be empty.")
    if len(name) > 80:
        raise ValueError("Conversation name cannot exceed 80 characters.")
    if thread_id not in retrieve_all_threads(access_token):
        raise ValueError("Conversation no longer exists.")
    get_thread_names(access_token)
    with get_chatbot(access_token).checkpointer.cursor() as cursor:
        cursor.execute(
            "INSERT INTO conversation_names (thread_id, name) VALUES (?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET name=excluded.name",
            (thread_id, name),
        )


def delete_thread(thread_id: str, access_token: str | None = None) -> None:
    """Delete one conversation's checkpoints and name."""
    checkpointer = get_chatbot(access_token).checkpointer
    checkpointer.delete_thread(thread_id)
    get_thread_names(access_token)
    with checkpointer.cursor() as cursor:
        cursor.execute("DELETE FROM conversation_names WHERE thread_id=?", (thread_id,))


def stream_chat(
    thread_id: str, message: str, access_token: str | None = None
) -> Iterator[tuple[BaseMessage, dict]]:
    """Stream one user turn through the LLM/tool loop, capturing token usage."""
    from langchain_core.messages import HumanMessage, AIMessage
    import streamlit as st

    refusal = refuse_request(message)
    if refusal:
        yield AIMessage(content=refusal), {}
        return
    message = protect_text(message)
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-6-luna")
    config = {"configurable": {"thread_id": thread_id}}
    turn_id = str(uuid4())

    # Get the shared tracker from Streamlit session state (llm_usage_tracker.py, llm_ui_components.py)
    if "usage_tracker" not in st.session_state:
        st.session_state.usage_tracker = LLMUsageTracker()
    tracker = st.session_state["usage_tracker"]

    # Stream with stream_mode="updates" to capture token data
    # In "updates" mode, we get deltas like {"chat": state} or {"tools": state}
    try:
        for update in get_chatbot(access_token).stream(
            {"messages": [HumanMessage(content=message, id=turn_id)]},
            config=config,
            stream_mode="updates",
        ):
            # In "updates" mode, each update is a dict: {"node_name": state_update}
            for node_name, node_state in update.items():
                if "messages" in node_state:
                    messages = node_state["messages"]
                    if messages:
                        for msg in messages:
                            if isinstance(msg, AIMessage):
                                tracker.log_response(
                                    thread_id, message, msg, model_name, turn_id=turn_id,
                                )

                            # Yield every message for the UI to handle
                            yield (msg, {"turn_id": turn_id})
    except OpenAIInvalidRequestError as error:
        logger.warning("Chat model %s rejected by provider: %s", model_name, error)
        yield AIMessage(content=(
            f"The provider rejected chat model `{model_name}`. Confirm that this model is "
            "available from the configured endpoint and supports tool calling. If you use a "
            "custom provider, set `OPENAI_BASE_URL` or `LLM_PROXY_URL` to its endpoint. "
            f"Technical detail: `{type(error).__name__}`."
        )), {}
        return
    except OpenAIAuthenticationError as error:
        logger.warning("OpenAI authentication failed for chat model %s: %s", model_name, error)
        yield AIMessage(content=(
            "OpenAI authentication failed. Check the team API key or sign-in session. "
            f"Technical detail: `{type(error).__name__}`."
        )), {}
        return
