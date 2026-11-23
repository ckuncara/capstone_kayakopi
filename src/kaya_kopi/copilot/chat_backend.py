def stream_chat(thread_id: str, message: str) -> Iterator[tuple[BaseMessage, dict]]:
    """Stream one user turn through the LLM/tool loop.

    - Guardrails (client policy Q14-Q23): check_input blocks prompt-injection
      before the LLM is called; sanitize_output strips PII from every AI
      message and tool result. Sanitization runs on the *complete* message,
      not per token, because a PII pattern can be split across streamed chunks.
    - LLM usage tracking (llm_usage_tracker.py): token usage is captured from
      the first AIMessage of the turn and logged via the shared tracker when
      one is present in the session (Streamlit app); absent otherwise, so the
      backend stays usable outside the UI.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from kaya_kopi.guardrails import InputBlockedError, check_input, sanitize_output

    # --- input guard -------------------------------------------------------
    try:
        check_input(message)
    except InputBlockedError:
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

    # --- optional usage tracker (present under the Streamlit app) ----------
    tracker = None
    try:
        import streamlit as st

        if "usage_tracker" not in st.session_state:
            st.session_state.usage_tracker = LLMUsageTracker()
        tracker = st.session_state.usage_tracker
    except Exception:
        tracker = None
    model_name = os.getenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    tokens_logged = False

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
            if not tokens_logged and getattr(msg, "usage_metadata", None):
                u = msg.usage_metadata
                if isinstance(u, dict):
                    itok = int(u.get("input_tokens") or 0)
                    otok = int(u.get("output_tokens") or 0)
                    if (itok or otok) and tracker is not None:
                        tracker.log_usage(thread_id, message, model_name, itok, otok)
                        tokens_logged = True
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
