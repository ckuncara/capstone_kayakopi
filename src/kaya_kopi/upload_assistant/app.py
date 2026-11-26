"""Standalone Streamlit entry point for upload-first operational analysis."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from uuid import uuid4

import streamlit as st
from openpyxl import load_workbook

from kaya_kopi.config import AppConfig
from kaya_kopi.paths import project_root
from kaya_kopi.copilot.llm_ui_components import (
    format_turn_usage, render_spend_meter, render_team_spend_meter, render_usage_log_panel,
)
from kaya_kopi.security.guardrails import protect_answer, protect_text
from kaya_kopi.upload_assistant.agent import UploadAgent
from kaya_kopi.upload_assistant.copilot_bridge import (
    delete_thread as delete_copilot_thread,
    ensure_thread,
    list_threads,
    load_messages,
    rename_thread as rename_copilot_thread,
    save_thread,
)
from kaya_kopi.security.guardrails import protect_answer, protect_text
from kaya_kopi.security.openai_proxy import set_proxy_access_token
from kaya_kopi.security.supabase_auth import render_sign_in, sign_out

from kaya_kopi.upload_assistant.ingestion import UploadProcessor
from kaya_kopi.upload_assistant.storage import Settings, UploadStore, citation_path, upload_subdirectory


st.set_page_config(page_title="Kaya Kopi Ops Copilot", page_icon="☕", layout="wide")
cfg = AppConfig()
access_token = render_sign_in(cfg) if cfg.llm_proxy_url else None
if cfg.llm_proxy_url and access_token is None:
    st.stop()
set_proxy_access_token(access_token)


st.markdown("""<style>
.stApp {background: #f5f7f6;}
h1,h2,h3 {font-family: Georgia, serif; letter-spacing: 0;}
[data-testid="stSidebar"] {background: #e8eeea;}
.stButton button,.stDownloadButton button {border-radius: 6px;}
[data-testid="stTabs"] {overflow: visible;}
[data-testid="stTabs"] [role="tablist"] {
    position: sticky;
    top: 3.75rem;
    z-index: 99;
    background: #f5f7f6;
    border-bottom: 1px solid #d5dfd9;
}
.stApp:has([data-testid="stTabs"] [role="tab"]:first-child[aria-selected="false"]) [data-testid="stBottom"] {
    display: none;
}
[data-testid="stBottom"] {background: #f5f7f6;}
[data-testid="stChatInput"] {max-width: 100%;}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def services():
    store = UploadStore(Settings.from_env())
    processor = UploadProcessor(store)
    store.init_mysql()
    return store, processor, UploadAgent(processor)


def _new_thread() -> str:
    return str(uuid4())


def _load_history(store: UploadStore, thread_id: str) -> list[dict]:
    """Restore one saved chat, with the same guard used by the copilot app."""
    try:
        return store.load_conversation(thread_id) or []
    except sqlite3.Error as error:
        st.error(f"Could not load that conversation: {error}")
        return []


def _safe_copilot_threads() -> list[dict]:
    try:
        return list_threads()
    except sqlite3.Error as error:
        st.error(f"Could not load Ops Copilot chats: {error}")
        return []


def _history_for(store: UploadStore, thread_id: str) -> list[dict]:
    """Load a chat from either store; the copilot uses its native checkpoints."""
    return _load_history(store, thread_id) or load_messages(thread_id)


def initialize_conversations(store: UploadStore, session) -> None:
    """Restore the newest saved chat in a fresh browser, like the copilot sidebar.

    Each key is checked separately so a partially initialised session cannot skip
    restore, and a chat deleted in either app no longer leaves stale messages.
    """
    if "upload_desk_thread_id" not in session:
        try:
            own = store.list_conversations()
        except sqlite3.Error as error:
            st.error(f"Could not load saved conversations: {error}")
            own = []
        copilot = _safe_copilot_threads()
        if own:
            thread_id = own[0]["thread_id"]
        elif copilot:
            thread_id = copilot[0]["thread_id"]
        else:
            thread_id = _new_thread()
        session["upload_desk_thread_id"] = thread_id
    if "upload_desk_messages" not in session:
        session["upload_desk_messages"] = _history_for(
            store, session["upload_desk_thread_id"]
        )
    if session["upload_desk_messages"]:
        try:
            saved_ids = {row["thread_id"] for row in store.list_conversations()}
            saved_ids.update(row["thread_id"] for row in _safe_copilot_threads())
            if session["upload_desk_thread_id"] not in saved_ids:
                session["upload_desk_thread_id"] = _new_thread()
                session["upload_desk_messages"] = []
        except sqlite3.Error as error:
            st.error(f"Could not verify saved conversations: {error}")


def sync_shared_history(store: UploadStore) -> None:
    """Backfill Upload Desk chats so the copilot app can list and open them."""
    try:
        for row in store.list_conversations():
            thread_id = row["thread_id"]
            ensure_thread(thread_id, row["name"], store.load_conversation(thread_id) or [])
    except (ValueError, sqlite3.Error) as error:
        st.error(f"Could not sync shared chat history: {error}")


def render_sources(sources: list[str], key: str) -> None:
    if not sources:
        return
    st.caption("Source Files")
    for index, source in enumerate(sources):
        path = citation_path(source)
        if path is None:
            st.caption(f"Unavailable: {Path(source).name}")
            continue
        with st.expander(path.name):
            st.caption(source)
            st.download_button("Download original", path.read_bytes(), file_name=path.name, key=f"{key}_{index}", icon=":material/download:")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                st.image(str(path), width=450)
            elif path.suffix.lower() == ".csv":
                st.code(path.read_text(encoding="utf-8-sig", errors="replace")[:12000], language="csv")
            elif path.suffix.lower() in {".txt", ".md", ".eml"}:
                st.text(path.read_text(encoding="utf-8-sig", errors="replace")[:12000])
            elif path.suffix.lower() == ".xlsx":
                workbook = load_workbook(path, read_only=True, data_only=True)
                try:
                    st.dataframe([{"Worksheet": sheet.title, "Rows": sheet.max_row, "Columns": sheet.max_column} for sheet in workbook], hide_index=True)
                finally:
                    workbook.close()


def render_invoice_audit(audit: dict) -> None:
    st.caption("Invoice arithmetic audit")
    status = audit.get("status")
    notes = [protect_text(str(note)) for note in audit.get("issues", [])]
    if status == "REVIEW":
        st.warning("Printed invoice amounts do not agree; see the differences below.")
        for note in notes:
            st.write(f"- {note}")
    elif status == "PASS":
        st.success("Printed lines, subtotal, GST and total agree within 0.02.")
    else:
        st.info("Some arithmetic could not be verified from the printed invoice.")
        for note in [protect_text(str(note)) for note in audit.get("notes", [])]:
            st.caption(note)
    st.caption(f"{len(audit.get('line_checks', []))} item line(s) stored for analysis.")


def process_upload(name: str, content: bytes, kind: str, key: str) -> None:
    status = st.status(f"Processing {name}...", expanded=True)
    with status:
        progress = st.progress(0.0, text="Preparing file...")

    def report_progress(value: float, message: str) -> None:
        progress.progress(value, text=message)

    try:
        result = processor.process(name, content, kind, on_progress=report_progress)
        status.update(label=f"Finished: {name}", state="complete", expanded=False)
        if result.get("refreshed"):
            st.success(f"Refreshed {name}: {result['record_count']} records using Python")
        elif result["duplicate"]:
            st.info(f"Already processed: {name}")
        else:
            st.success(f"{name}: {result['record_count']} records")
        render_sources([result["source_file"]], key)
        if result.get("audit"):
            render_invoice_audit(result["audit"])
    except (ValueError, UnicodeError) as error:
        status.update(label=f"Processing stopped: {name}", state="error", expanded=True)
        st.error(f"{name}: {error}")
    except Exception as error:
        status.update(label=f"Processing failed: {name}", state="error", expanded=True)
        logging.exception("Upload Desk ingestion failed")
        st.error(f"{name}: processing failed ({type(error).__name__}); retry after checking service availability.")


st.title("☕ Kaya Kopi Ops Copilot")
st.caption("Grounded answers from POS, invoices, operational documents, and data-quality records")
try:
    store, processor, agent = services()
    # One shared tracker for the spend meter, audit log and per-answer captions.
    # processor.budget now points at AppConfig().sqlite_db_path, the same
    # database the unmodified Ops Copilot reads.
    st.session_state.usage_tracker = processor.budget.usage_tracker
except Exception as error:
    logging.exception("Upload Desk startup failed")
    st.error(f"Upload Desk could not start ({type(error).__name__}). Check MySQL permissions for uploaded_* tables and OpenAI/Chroma configuration.")
    st.stop()

initialize_conversations(store, st.session_state)
sync_shared_history(store)

with st.sidebar:
    st.subheader("Workspace")
    st.caption("Upload Desk")
    if st.button("New conversation", icon=":material/add:", use_container_width=True):
        st.session_state.upload_desk_thread_id = _new_thread()
        st.session_state.upload_desk_messages = []
        st.rerun()
    st.subheader("Conversations")
    st.caption("Shared with the Ops Copilot app")
    try:
        own = {
            row["thread_id"]: {**row, "source": "upload"}
            for row in store.list_conversations()
        }
    except sqlite3.Error as error:
        st.error(f"Could not load saved conversations: {error}")
        own = {}
    copilot = {
        row["thread_id"]: {**row, "source": "copilot"}
        for row in _safe_copilot_threads()
    }
    conversations = list({**copilot, **own}.values())
    if not conversations:
        st.caption("No saved chats yet. Ask a question to start one.")
    for index, conversation in enumerate(conversations, start=1):
        thread_id = conversation["thread_id"]
        title = protect_text(conversation["name"] or f"Conversation {index}")
        source = conversation["source"]
        navigation, actions = st.columns([0.83, 0.17], vertical_alignment="center")
        with navigation:
            if st.button(title, key=f"open_{thread_id}", use_container_width=True, help=title):
                st.session_state.upload_desk_thread_id = thread_id
                st.session_state.upload_desk_messages = _history_for(store, thread_id)
                st.rerun()
        with actions:
            with st.popover(":material/more_vert:", help=f"Manage {title} ({source})"):
                name = st.text_input("Conversation name", value=title, max_chars=80, key=f"rename_{thread_id}")
                if st.button("Save name", icon=":material/save:", key=f"save_{thread_id}"):
                    try:
                        if thread_id in own:
                            store.rename_conversation(thread_id, name)
                        rename_copilot_thread(thread_id, name)
                    except (ValueError, sqlite3.Error) as error:
                        st.error(str(error))
                    else:
                        st.rerun()
                confirmed = st.checkbox("Confirm deletion", key=f"confirm_{thread_id}", help="API usage records are retained.")
                if st.button("Delete conversation", icon=":material/delete:", key=f"delete_{thread_id}", disabled=not confirmed):
                    try:
                        store.delete_conversation(thread_id)
                        delete_copilot_thread(thread_id)
                    except (ValueError, sqlite3.Error) as error:
                        st.error(str(error))
                    else:
                        if st.session_state.upload_desk_thread_id == thread_id:
                            st.session_state.upload_desk_thread_id = _new_thread()
                            st.session_state.upload_desk_messages = []
                        st.rerun()
    # st.divider()
    # st.metric("Archived files", len(store.files()))
    # st.metric("Monthly API spend", f"${processor.budget.get_current_month_spend():.4f}")
    # Added spend meter at bottom of sidebar (llm_ui_components.py, llm_usage_tracker.py)
    if cfg.llm_proxy_url and access_token:
        render_team_spend_meter(cfg)
    else:
        render_spend_meter(st.session_state.usage_tracker)


chat_tab, upload_tab, usage_tab, files_tab = st.tabs(["Assistant", "Upload", "Usage Log", "Files"])
question = st.chat_input("Ask about invoices, outlet reports or uploaded documents", key="upload_desk_chat")

with chat_tab:
    for index, message in enumerate(st.session_state.upload_desk_messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("usage_caption"):
                st.caption(message["usage_caption"])
            if message.get("source_note"):
                st.caption(message["source_note"])
            render_sources(message.get("sources", []), f"history_{index}")
            if message.get("tools"):
                with st.expander("Query activity"):
                    st.json(message["tools"])
    if question:
        question = protect_text(question)
        turn_id = str(uuid4())
        st.session_state.upload_desk_messages.append({"role": "user", "content": question, "turn_id": turn_id})
        store.save_conversation(st.session_state.upload_desk_thread_id, st.session_state.upload_desk_messages)
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            try:
                with st.spinner("Checking records and original files..."):
                    result = agent.ask(st.session_state.upload_desk_messages, thread_id=st.session_state.upload_desk_thread_id, turn_id=turn_id)
            except Exception as error:
                logging.exception("Upload Desk lookup failed")
                detail = str(getattr(error, "message", error)).replace("\n", " ").strip()[:300]
                suffix = f" Detail: {detail}" if detail else ""
                result = {"content": f"The lookup could not complete ({type(error).__name__}). Check service availability and API configuration.{suffix}", "sources": [], "tools": []}
            result["content"] = protect_answer(result["content"])
            result["turn_id"] = turn_id
            if not result.get("usage_caption"):
                summary = processor.budget.usage_tracker.get_turn_usage(st.session_state.upload_desk_thread_id, turn_id)
                result["usage_caption"] = format_turn_usage(summary) or "No API usage record is available for this answer."
            st.markdown(result["content"])
            st.caption(result["usage_caption"])
            if result.get("source_note"):
                st.caption(result["source_note"])
            render_sources(result["sources"], f"live_{len(st.session_state.upload_desk_messages)}")
            if result["tools"]:
                with st.expander("Query activity"):
                    st.json(result["tools"])
            st.session_state.upload_desk_messages.append({"role": "assistant", **result})
            store.save_conversation(st.session_state.upload_desk_thread_id, st.session_state.upload_desk_messages)
            try:
                save_thread(
                    st.session_state.upload_desk_thread_id,
                    "",
                    st.session_state.upload_desk_messages,
                )
            except (ValueError, sqlite3.Error) as error:
                st.error(f"Could not sync this answer to the Ops Copilot chats: {error}")
            st.rerun()

with upload_tab:
    kinds = {"Invoice images": ("invoices", ["jpg", "jpeg", "png", "webp"]), "Outlet CSVs": ("csvs", ["csv"]), "Roster Excel workbooks": ("rosters", ["xlsx"]), "WhatsApp and text": ("texts", ["txt", "md"]), "Emails": ("emails", ["eml", "txt", "md"])}
    label = st.selectbox("Document type", list(kinds))
    kind, extensions = kinds[label]
    files = st.file_uploader("Files", type=extensions, accept_multiple_files=True, key=f"files_{kind}")
    if st.button("Process uploads", icon=":material/upload_file:", disabled=not files, type="primary"):
        for index, file in enumerate(files):
            process_upload(file.name, file.getvalue(), kind, f"upload_{kind}_{index}")
    folder = project_root() / "data" / upload_subdirectory(kind)
    candidates = sorted(path.relative_to(project_root()).as_posix() for path in folder.glob("*") if path.suffix.lower().lstrip(".") in extensions and citation_path(path.as_posix()))
    selected_files = st.multiselect("Existing data files", candidates, key=f"existing_{kind}")
    if st.button("Process selected files", icon=":material/folder_open:", disabled=not selected_files):
        for index, source in enumerate(selected_files):
            selected_path = citation_path(source)
            if selected_path:
                process_upload(selected_path.name, selected_path.read_bytes(), kind, f"existing_{kind}_{index}")

with usage_tab:
    # Same audit panel as the Ops Copilot (usage source, pricing version,
    # legacy warnings and provider-billing reconciliation).
    render_usage_log_panel(st.session_state.usage_tracker)

with files_tab:
    records = store.files()
    st.subheader("File Register")
    if records:
        st.dataframe([{key: row[key] for key in ("original_name", "kind", "record_count", "created_at")} for row in records], hide_index=True, use_container_width=True)
        selected = st.selectbox("Original file", range(len(records)), format_func=lambda index: f"{records[index]['original_name']} ({records[index]['kind']})")
        render_sources([records[selected]["source_file"]], "register")
    else:
        st.caption("No files archived.")