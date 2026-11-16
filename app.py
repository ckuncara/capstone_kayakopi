"""Streamlit frontend (course NB 01–02 pattern).

- chat box + thread id in st.session_state
- streams the assistant reply (st.write_stream)
- shows tool calls as st.status blocks
- spend tracker in the sidebar (box 7)
- honest errors from guardrails/tools shown as st.error, never a crash
Run:  streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from kaya_kopi.backend import build_graph
from kaya_kopi.guardrails import InputBlockedError
from kaya_kopi.spend import SpendTracker

st.set_page_config(page_title="Kaya Kopi Copilot", page_icon="☕", layout="wide")

# ---- one compiled graph + tracker per server process ---------------------
if "graph" not in st.session_state:
    tracker = SpendTracker()
    st.session_state.graph = build_graph(tracker=tracker)
    st.session_state.tracker = tracker
    st.session_state.thread = "local-1"


def _reset():
    for k in list(st.session_state):
        st.session_state[k] = None
    st.session_state.thread = "local-1"
    st.session_state.messages = []


def _run(user_text: str) -> None:
    graph = st.session_state.graph
    config = {"configurable": {"thread_id": st.session_state.thread}}

    try:
        with st.status("Thinking…", expanded=False) as status:
            tool_lines: list[str] = []
            answer_parts: list[str] = []
            for chunk in graph.stream(
                {"messages": [("user", user_text)]},
                config=config,
                stream_mode="messages",
            ):
                # each chunk is (message, metadata)
                msg, meta = chunk
                kind = meta.get("langgraph_node")
                if kind == "tools" or getattr(msg, "tool_calls", None):
                    name = getattr(msg, "name", None) or (
                        msg.tool_calls[0].get("name") if msg.tool_calls else "tool"
                    )
                    line = f"⚙︎ {name}"
                    if line not in tool_lines:
                        tool_lines.append(line)
                        status.update(label=f"Thinking… ({len(tool_lines)} tool call(s))")
                elif getattr(msg, "content", ""):
                    answer_parts.append(msg.content)
            status.update(label="Done", state="complete")

        full = "".join(answer_parts).strip()
        if not full:
            raise RuntimeError("The model returned no answer.")
        st.session_state.messages.append({"role": "assistant", "content": full})
    except InputBlockedError as e:
        st.error(f"🛑 Blocked by guardrail: {e}")
    except Exception as e:  # box 4: honest error, never a raw traceback
        st.error(f"⚠️ Could not answer: {e}")


# ---- layout ----------------------------------------------------------------
st.title("☕ Kaya Kopi — Operations Copilot")
st.caption("Answers only from your sales DB, outlet CSVs, invoices and documents — with citations.")

with st.sidebar:
    st.header("Spend tracker")
    t = st.session_state.tracker.totals()
    st.metric("API calls", t["calls"])
    st.metric("Est. cost (USD)", f"${t['cost_usd']:.4f}")
    st.metric("Tokens (in/out)", f"{t['input_tokens']}/{t['output_tokens']}")
    st.divider()
    thread = st.text_input("Thread ID", value=st.session_state.thread)
    if thread:
        st.session_state.thread = thread
    if st.button("🧹 New conversation"):
        _reset()
        st.rerun()

# ---- message history -------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

for m in st.session_state.messages:
    with st.container():
        if m["role"] == "user":
            st.markdown(f"**You:**  {m['content']}")
        else:
            st.markdown(m["content"])

# ---- prompt ----------------------------------------------------------------
if prompt := st.chat_input("Ask about sales, margins, wastage, staffing, suppliers…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.write(f"**You:**  {prompt}")
    _run(prompt)
