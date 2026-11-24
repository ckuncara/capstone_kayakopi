"""Streamlit frontend for the Kaya Kopi operations copilot."""

from __future__ import annotations

import json
import sqlite3
import uuid

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from kaya_kopi.config import AppConfig
from kaya_kopi.copilot.chat_backend import (
    delete_thread, get_chatbot, get_thread_names, rename_thread, retrieve_all_threads, stream_chat,
)
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker  # Added for LLM spend meter (llm_usage_tracker.py, llm_ui_components.py)
from kaya_kopi.copilot.llm_ui_components import render_spend_meter, render_usage_log_panel  # Added for LLM spend meter (llm_ui_components.py)


st.set_page_config(page_title="Kaya Kopi Ops Copilot", page_icon="☕", layout="wide")


def _new_thread() -> str:
    return str(uuid.uuid4())


def _load_history(thread_id: str) -> list[dict[str, str]]:
    state = get_chatbot().get_state({"configurable": {"thread_id": thread_id}})
    history: list[dict[str, str]] = []
    for message in state.values.get("messages", []):
        if isinstance(message, HumanMessage):
            history.append({"role": "user", "content": str(message.content)})
        elif isinstance(message, AIMessage) and message.content:
            history.append({"role": "assistant", "content": str(message.content)})
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
            saved_threads = retrieve_all_threads()
            thread_names = get_thread_names() if saved_threads else {}
        except RuntimeError as error:
            st.error(str(error))
            st.stop()

        for index, thread_id in enumerate(saved_threads, start=1):
            name = thread_names.get(thread_id, f"Conversation {index}")
            conversation, actions = st.columns([0.82, 0.18], vertical_alignment="center")
            with conversation:
                if st.button(name, key=thread_id, use_container_width=True, help=name):
                    st.session_state.thread_id = thread_id
                    st.session_state.message_history = _load_history(thread_id)
                    st.session_state.tool_log = []
                    st.rerun()
            with actions:
                with st.popover("⋮", help=f"Manage {name}"):
                    new_name = st.text_input(
                        "Conversation name", value=name, max_chars=80, key=f"name_{thread_id}"
                    )
                    if st.button("Save name", key=f"save_{thread_id}", icon=":material/save:"):
                        try:
                            rename_thread(thread_id, new_name)
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
                            delete_thread(thread_id)
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
        render_spend_meter(st.session_state.usage_tracker)

    if not st.session_state.message_history:
        st.info(
            "Try: **Compare AMK-01, AMK-03 and JUR-01 sales for July 2025.**"
        )

    for item in st.session_state.message_history:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])

    prompt = st.chat_input("Ask about sales, invoices, suppliers, documents, or data quality")
    if prompt:
        st.session_state.message_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            activity = {"status": None, "last_tool": "database tool"}

            def response_tokens():
                try:
                    for message, _metadata in stream_chat(st.session_state.thread_id, prompt):
                        if isinstance(message, ToolMessage):
                            activity["last_tool"] = message.name or "database tool"
                            result = _text_content(message.content)
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
                        elif isinstance(message, AIMessage) and message.content:
                            yield _text_content(message.content)
                except Exception as error:
                    yield (
                        "I couldn't complete that check. Confirm the database is available and "
                        f"the LLM configuration is valid. Technical detail: `{type(error).__name__}`."
                    )

            answer = st.write_stream(response_tokens())
            if activity["status"] is not None:
                activity["status"].update(
                    label=f"✅ {activity['last_tool']} finished",
                    state="complete",
                    expanded=False,
                )

        if not isinstance(answer, str):
            answer = json.dumps(answer, default=str)
        st.session_state.message_history.append({"role": "assistant", "content": answer})