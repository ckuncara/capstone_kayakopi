"""Streamlit frontend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openpyxl import load_workbook

from kaya_kopi.config import AppConfig
from kaya_kopi.copilot.chat_backend import (
    _supplier_addresses, delete_thread, get_chatbot, get_thread_names, rename_thread,
    retrieve_all_threads, stream_chat,
)
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker  # Added for LLM spend meter (llm_usage_tracker.py, llm_ui_components.py)
from kaya_kopi.copilot.llm_ui_components import (
    format_turn_usage, render_spend_meter, render_team_spend_meter, render_usage_log_panel,
)
from kaya_kopi.copilot.result_renderer import (
    render_business_results,
    render_scope_alignment,
    render_specialist_reports,
)
from kaya_kopi.copilot.response_format import format_copilot_answer
from kaya_kopi.security.guardrails import protect_answer, protect_text
from kaya_kopi.security.openai_proxy import set_proxy_access_token
from kaya_kopi.security.supabase_auth import render_sign_in, sign_out
from kaya_kopi.upload_assistant.storage import citation_path, collect_sources


st.set_page_config(page_title="Kaya Kopi Ops Copilot", page_icon="☕", layout="wide")
cfg = AppConfig()
access_token = render_sign_in(cfg) if cfg.llm_proxy_url else None
if cfg.llm_proxy_url and access_token is None:
    st.stop()
set_proxy_access_token(access_token)


def _new_thread() -> str:
    return str(uuid.uuid4())

def _tool_evidence(message: ToolMessage) -> tuple[list[str], str | None]:
    """Collect citable files and provenance notes from a tool result."""
    try:
        value = json.loads(_text_content(message.content))
    except (json.JSONDecodeError, TypeError):
        return [], None
    sources = collect_sources(value) if isinstance(value, (dict, list)) else []
    note = None
    if message.name == "query_mysql_direct" and isinstance(value, dict):
        candidate = value.get("citation_note")
        if isinstance(candidate, str):
            note = candidate
    return sources, note

def render_sources(sources: list[str], key: str) -> None:
    if not sources:
        return
    st.caption("Source Files")
    for index, source in enumerate(sources):
        path = citation_path(source)
        if path is None:
            st.caption(f"Unavailable: {Path(source).name}")
            continue
        with st.expander(protect_text(path.name)):
            st.caption(protect_text(source))
            st.download_button(
                "Download original", path.read_bytes(), file_name=path.name,
                key=f"{key}_{index}", icon=":material/download:",
            )
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                st.image(str(path), width=450)
            elif path.suffix.lower() == ".csv":
                st.code(path.read_text(encoding="utf-8-sig", errors="replace")[:12000], language="csv")
            elif path.suffix.lower() in {".txt", ".md", ".eml"}:
                st.text(path.read_text(encoding="utf-8-sig", errors="replace")[:12000])
            elif path.suffix.lower() == ".xlsx":
                workbook = load_workbook(path, read_only=True, data_only=True)
                try:
                    st.dataframe([
                        {"Worksheet": sheet.title, "Rows": sheet.max_row, "Columns": sheet.max_column}
                        for sheet in workbook
                    ], hide_index=True)
                finally:
                    workbook.close()

def _load_history(thread_id: str, token: str | None = None) -> list[dict[str, object]]:
    state = get_chatbot(token).get_state({"configurable": {"thread_id": thread_id}})
    history: list[dict[str, object]] = []
    turn_id: str | None = None
    turn_question = ""
    turn_sources: set[str] = set()
    turn_source_note: str | None = None
    last_answers: dict[str, dict[str, object]] = {}
    pending_results: list[dict] = []
    messages = state.values.get("messages", [])
    addresses = _supplier_addresses(messages)
    for message in messages:
        if isinstance(message, HumanMessage):
            turn_id = message.id
            turn_question = protect_text(str(message.content))
            turn_sources = set()
            turn_source_note = None
            history.append({"role": "user", "content": turn_question})
        elif isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
            if message.artifact.get("result_type") in {"business_data", "specialist_report"}:
                pending_results.append(message.artifact)
        elif isinstance(message, ToolMessage):
            sources, note = _tool_evidence(message)
            turn_sources.update(sources)
            if note:
                turn_source_note = note
        elif isinstance(message, AIMessage) and message.content:
            answer = {"role": "assistant", "content": protect_answer(str(message.content), addresses)}
            if turn_sources:
                answer["sources"] = sorted(turn_sources)
            if turn_source_note:
                answer["source_note"] = turn_source_note
            history.append(answer)
            if turn_id:
                last_answers[turn_id] = answer
    for saved_turn_id, answer in last_answers.items():
        caption = format_turn_usage(
            st.session_state.usage_tracker.get_turn_usage(thread_id, saved_turn_id)
        )
        if caption:
            answer["usage_caption"] = caption
    return history

def _text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)

def _render_structured_results(results: list[dict], question: str | None = None) -> None:
    business_results = [item for item in results if item.get("result_type") == "business_data"]
    specialist_results = [item for item in results if item.get("result_type") == "specialist_report"]
    render_scope_alignment(st, results)
    if business_results:
        render_business_results(st, business_results, question=question)
    if specialist_results:
        render_specialist_reports(st, specialist_results, question=question)


if "thread_id" not in st.session_state:
    st.session_state.thread_id = _new_thread()
if "message_history" not in st.session_state:
    st.session_state.message_history = []
if "tool_log" not in st.session_state:
    st.session_state.tool_log = []
# Added for LLM spend meter (llm_ui_components.py)
if "show_usage_log" not in st.session_state:
    st.session_state.show_usage_log = False
if "show_invoice_upload" not in st.session_state:
    st.session_state.show_invoice_upload = False
# Initialize tracker for spend meter (llm_usage_tracker.py)
if (
    "usage_tracker" not in st.session_state
    or st.session_state.usage_tracker.db_path != AppConfig().sqlite_db_path
):
    st.session_state.usage_tracker = LLMUsageTracker()

st.title("☕ Kaya Kopi Ops Copilot")
st.caption("Grounded answers from POS, invoices, operational documents, and data-quality records")

# Check if usage log should be displayed (llm_ui_components.py)
if st.session_state.show_usage_log:
    render_usage_log_panel(st.session_state.usage_tracker)
elif st.session_state.show_invoice_upload:
    from kaya_kopi.copilot.invoice_upload import render_invoice_upload_page

    render_invoice_upload_page()
else:
    with st.sidebar:
        st.header("Conversations")
        if cfg.llm_proxy_url and st.button("Sign out", use_container_width=True):
            sign_out()
            get_chatbot.cache_clear()
            set_proxy_access_token(None)
            st.rerun()
        # Added menu button for LLM Usage Log (llm_ui_components.py)
        if st.button("📊 LLM Usage Log", use_container_width=True):
            st.session_state.show_usage_log = True
            st.rerun()

        if st.button("Upload supplier invoices", use_container_width=True):
            st.session_state.show_invoice_upload = True
            st.rerun()
        
        if st.button("＋ New chat", use_container_width=True):
            st.session_state.thread_id = _new_thread()
            st.session_state.message_history = []
            st.session_state.tool_log = []
            st.rerun()

        try:
            saved_threads = (
                retrieve_all_threads(access_token)
                if access_token
                else retrieve_all_threads()
            )
            if saved_threads and access_token:
                thread_names = get_thread_names(access_token)
            elif saved_threads:
                thread_names = get_thread_names()
            else:
                thread_names = {}
        except RuntimeError as error:
            st.error(str(error))
            st.stop()

        for index, thread_id in enumerate(saved_threads, start=1):
            name = protect_text(thread_names.get(thread_id, f"Conversation {index}"))
            conversation, actions = st.columns([0.82, 0.18], vertical_alignment="center")
            with conversation:
                if st.button(name, key=thread_id, use_container_width=True, help=name):
                    st.session_state.thread_id = thread_id
                    st.session_state.message_history = _load_history(thread_id, access_token)
                    st.session_state.tool_log = []
                    st.rerun()
            with actions:
                with st.popover("⋮", help=f"Manage {name}"):
                    new_name = st.text_input(
                        "Conversation name", value=name, max_chars=80, key=f"name_{thread_id}"
                    )
                    if st.button("Save name", key=f"save_{thread_id}", icon=":material/save:"):
                        try:
                            rename_thread(thread_id, new_name, access_token)
                        except (ValueError, sqlite3.Error) as error:
                            st.error(str(error))
                        else:
                            st.rerun()
                    confirmed = st.checkbox(
                        "Confirm deletion", key=f"confirm_{thread_id}",
                        help="Usage records are retained for spend tracking.",
                    )
                    if st.button(
                        "Delete", key=f"delete_{thread_id}", icon=":material/delete:",
                        disabled=not confirmed,
                    ):
                        try:
                            delete_thread(thread_id, access_token)
                        except (ValueError, sqlite3.Error) as error:
                            st.error(str(error))
                        else:
                            if st.session_state.thread_id == thread_id:
                                st.session_state.thread_id = _new_thread()
                                st.session_state.message_history = []
                                st.session_state.tool_log = []
                            st.rerun()

        with st.expander("Tool activity", expanded=False):
            if not st.session_state.tool_log:
                st.caption("No tools used in this browser session yet.")
            for entry in st.session_state.tool_log:
                st.markdown(f"**{entry['name']}**")
                st.code(entry["result"], language="json")

        st.divider()
        st.caption("Chat is read-only. The invoice upload page saves invoice data.")
        
        # Added spend meter at bottom of sidebar (llm_ui_components.py, llm_usage_tracker.py)
        if cfg.llm_proxy_url and access_token:
            render_team_spend_meter(cfg)
        else:
            render_spend_meter(st.session_state.usage_tracker)

    if not st.session_state.message_history:
        st.info(
            "Try: **Compare AMK-01, AMK-03 and JUR-01 sales for July 2025.**"
        )

    for index, item in enumerate(st.session_state.message_history):
        with st.chat_message(item["role"]):
            content = item["content"]
            if item["role"] == "assistant":
                content = format_copilot_answer(content)
            st.markdown(content)
            if item.get("structured_results"):
                _render_structured_results(item["structured_results"], item.get("question"))
            if item.get("usage_caption"):
                st.caption(item["usage_caption"])
            if item.get("source_note"):
                st.caption(item["source_note"])
            render_sources(item.get("sources", []), f"history_{index}")

    prompt = st.chat_input("Ask about sales, invoices, suppliers, documents, or data quality")
    if prompt:
        displayed_prompt = protect_text(prompt)
        st.session_state.message_history.append({"role": "user", "content": displayed_prompt})
        with st.chat_message("user"):
            st.markdown(displayed_prompt)

        with st.chat_message("assistant"):
            activity = {"status": None, "last_tool": "database tool"}
            turn_metadata: dict[str, str] = {}
            structured_results: list[dict] = []            
            answer_sources: set[str] = set()
            answer_meta: dict[str, str | None] = {"note": None}

            answer_parts: list[str] = []
            def response_tokens():
                try:
                    with st.spinner("Working…"):
                        for message, metadata in stream_chat(
                            st.session_state.thread_id, prompt, access_token
                        ):
                            if isinstance(metadata.get("turn_id"), str):
                                turn_metadata["turn_id"] = metadata["turn_id"]
                            if isinstance(message, ToolMessage):
                                activity["last_tool"] = message.name or "database tool"
                                result = _text_content(message.content)
                                sources, note = _tool_evidence(message)
                                answer_sources.update(sources)
                                if note:
                                    answer_meta["note"] = note
                                if (
                                    isinstance(message.artifact, dict)
                                    and message.artifact.get("result_type")
                                    in {"business_data", "specialist_report"}
                                ):
                                    structured_results.append(message.artifact)
                                
                                st.session_state.tool_log.append(
                                    {"name": activity["last_tool"], "result": result}
                                )
                                if activity["status"] is None:
                                    activity["status"] = st.status(
                                        f"Using {activity['last_tool']}…", expanded=False
                                    )
                                else:
                                    activity["status"].update(
                                        label=f"Using {activity['last_tool']}…"
                                    )
                            elif (
                                isinstance(message, AIMessage)
                                and message.content
                                and not message.tool_calls
                            ):
                                answer_parts.append(_text_content(message.content))
                except Exception as error:
                    answer_parts[:] = [
                        "I couldn't complete that check. Confirm the database is available and "
                        f"the LLM configuration is valid. Technical detail: `{type(error).__name__}`."
                    ]

            response_tokens()
            answer = "".join(answer_parts).strip()
            if not answer:
                answer = "I couldn't complete that check because no final answer was returned."
            st.markdown(format_copilot_answer(answer))
            if structured_results:
                _render_structured_results(structured_results, displayed_prompt)
            if activity["status"] is not None:
                activity["status"].update(
                    label=f"✅ {activity['last_tool']} finished",
                    state="complete",
                    expanded=False,
                )
            usage_caption = None
            if turn_metadata.get("turn_id"):
                usage_caption = format_turn_usage(
                    st.session_state.usage_tracker.get_turn_usage(
                        st.session_state.thread_id, turn_metadata["turn_id"],
                    )
                )
                if usage_caption:
                    st.caption(usage_caption)
            if answer_meta["note"]:
                st.caption(answer_meta["note"])
            render_sources(sorted(answer_sources), f"live_{len(st.session_state.message_history)}")

        saved_answer = {
            "role": "assistant", "content": answer,
            "structured_results": structured_results,
            "question": displayed_prompt,
        }
        if usage_caption:
            saved_answer["usage_caption"] = usage_caption
        if answer_meta["note"]:
            saved_answer["source_note"] = answer_meta["note"]
        if answer_sources:
            saved_answer["sources"] = sorted(answer_sources)
        st.session_state.message_history.append(saved_answer)
