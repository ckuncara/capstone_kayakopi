from pathlib import Path
import sys
import streamlit as st
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kaya_kopi.config import AppConfig  # noqa: E402
from kaya_kopi.invoice_chat import sqlite_invoice_answer  # noqa: E402
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (  # noqa: E402
    extract_invoice_with_openai,
    validate_invoice_calculations,
)
from kaya_kopi.repositories.chroma_store import SharedChromaRepository  # noqa: E402
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository  # noqa: E402
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard  # noqa: E402
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa  # noqa: E402

st.set_page_config(page_title="Kaya Kopi OCR + ChromaDB Test Harness", layout="wide")
st.title("Kaya Kopi — Supplier Invoice OCR & ChromaDB Test Harness")

default_cfg = AppConfig()
BRANCHES = ["HQ", "AMK-01", "AMK-03", "BDK-01", "JUR-01", "TAM-01", "TPY-02", "WDL-01", "YIS-02"]

# --- Sidebar Configuration ---
with st.sidebar:
    st.header("Test Configuration")
    branch_code = st.selectbox("Branch Tag", BRANCHES, index=0)
    api_key = st.text_input("OpenAI API Key", type="password", value=default_cfg.api_key)

    with st.expander("Model & Budget Overrides", expanded=False):
        ocr_model = st.text_input("OCR Model", value=default_cfg.ocr_model)
        chat_model = st.text_input("Chat Model", value=default_cfg.chat_model)
        embed_model = st.text_input("Embedding Model", value=default_cfg.embed_model)
        budget_limit = st.number_input(
            "Monthly Budget Cap (USD)", min_value=1.0, value=default_cfg.monthly_budget_usd
        )

    skip_duplicates = st.checkbox("Skip duplicate files / invoice numbers", value=True)
    top_k = st.slider("Top chunks for RAG Q&A", 1, 5, 3)

cfg = AppConfig(
    api_key=api_key,
    ocr_model=ocr_model,
    chat_model=chat_model,
    embed_model=embed_model,
    monthly_budget_usd=float(budget_limit),
)

sql_repo = SQLiteOpsRepository(cfg)
budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)

with st.sidebar:
    st.divider()
    spend = budget.get_current_month_spend()
    st.metric("Monthly Spend", f"${spend:.4f} / ${cfg.monthly_budget_usd:.2f}")
    st.caption(f"SQLite: `{cfg.sqlite_db_path}`")
    st.caption(f"ChromaDB: `{cfg.chroma_dir}`")

# --- Tabs ---
tab_ocr, tab_chat, tab_inspect = st.tabs(
    ["1. Test OCR -> ChromaDB", "2. Chat with Invoice Data", "3. Inspect Stored Chunks"]
)

# ==============================================================================
# TAB 1: Test OpenAI Vision OCR + 9% GST Audit + Single-Chunk Chroma Insertion
# ==============================================================================
with tab_ocr:
    uploaded_files = st.file_uploader(
        "Upload supplier invoice images (e.g., INV-012508-182.jpg)",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
    )

    if uploaded_files and st.button("Run OCR, Math Audit & Upsert to ChromaDB", type="primary"):
        if not cfg.api_key:
            st.error("Please enter your OpenAI API key in the sidebar.")
        else:
            vec_repo = SharedChromaRepository(cfg, budget_guard=budget)

            for file in uploaded_files:
                raw_bytes = file.getvalue()
                file_hash = sql_repo.compute_hash(raw_bytes)

                if skip_duplicates and sql_repo.hash_exists(file_hash):
                    st.warning(f"Skipped duplicate file bytes: `{file.name}`")
                    continue

                try:
                    with st.spinner(f"Running `{cfg.ocr_model}` OCR & math audit on `{file.name}`..."):
                        invoice = extract_invoice_with_openai(
                            file_bytes=raw_bytes,
                            mime_type=file.type or "image/jpeg",
                            ocr_model=cfg.ocr_model,
                            api_key=cfg.api_key,
                            budget_guard=budget,
                            branch_code=branch_code,
                        )

                        if skip_duplicates and sql_repo.invoice_number_exists(invoice.invoice_number):
                            st.warning(f"Skipped duplicate invoice number: `{invoice.invoice_number}`")
                            continue

                        # 1. Save image as <invoice_number>.jpg
                        stored_name, rel_img_path = sql_repo.save_image_as_invoice_jpg(
                            raw_bytes, invoice.invoice_number, file_hash
                        )

                        # 2. Run deterministic Python math & 9% GST check
                        audit = validate_invoice_calculations(invoice)

                        # 3. Persist to SQLite + individual CSV + master CSV
                        sql_repo.save_invoice(
                            file_hash, branch_code, stored_name, rel_img_path, invoice, audit
                        )
                        indiv_csv = sql_repo.save_individual_invoice_csv(
                            stored_name, rel_img_path, branch_code, invoice, audit
                        )
                        master_csv = sql_repo.sync_master_csv_in_image_folder()

                        # 4. Build unified single-chunk text and upsert to ChromaDB
                        status_str = "PASS" if audit["all_valid"] else "FAIL"
                        unified_doc = (
                            f"Invoice: {invoice.invoice_number} | Branch: {branch_code} | "
                            f"Vendor: {invoice.vendor_name} (GST Reg: {invoice.vendor_gst_reg_no or 'N/A'}) | "
                            f"Date: {invoice.invoice_date or 'N/A'}\n"
                            f"OCR Totals -> Subtotal: {audit['ocr_subtotal']:.2f}, "
                            f"GST: {audit['ocr_gst']:.2f}, Total: {audit['ocr_grand_total']:.2f}\n"
                            f"Calc Totals -> Subtotal: {audit['calc_subtotal']:.2f}, "
                            f"GST: {audit['calc_gst']:.2f}, Total: {audit['calc_grand_total']:.2f}\n"
                            f"Audit Status: {status_str}\n\n"
                            f"{invoice.raw_markdown_text}"
                        )

                        chunks_added = vec_repo.ingest_document(
                            parent_doc_id=file_hash,
                            source_type="invoice",
                            branch_code=branch_code,
                            text=unified_doc,
                            strategy="single",
                            extra_metadata={
                                "invoice_number": invoice.invoice_number,
                                "filename": stored_name,
                                "image_path": rel_img_path,
                                "vendor_name": invoice.vendor_name,
                                "audit_status": status_str,
                            },
                        )

                    badge = "✅" if audit["all_valid"] else "⚠️"
                    with st.expander(
                        f"{badge} `{stored_name}` | Vendor: {invoice.vendor_name} | "
                        f"Chunks Stored: {chunks_added}",
                        expanded=True,
                    ):
                        if audit["all_valid"]:
                            st.success(
                                f"Math & GST verified! Saved `{rel_img_path}`, `{indiv_csv}`, "
                                f"and synced `{master_csv}`."
                            )
                        else:
                            for err in audit["issues"]:
                                st.error(err)

                        st.markdown("**Line-Item Comparison (OCR vs. Calculated)**")
                        st.dataframe(audit["line_checks"], use_container_width=True)

                        st.markdown("**Single Chunk Text Indexed in ChromaDB**")
                        st.code(unified_doc, language="markdown")

                except BudgetExceededError as exc:
                    st.error(str(exc))
                    break

# ==============================================================================
# TAB 2: Chat & Ask Questions Against ChromaDB OCR Data
# ==============================================================================
with tab_chat:
    st.caption(
        "SQLite supports invoice lookup, spending, monthly trends, audit counts and yearly "
        "supplier/product price changes for the selected branch. Other questions use ChromaDB. "
        "SQLite answers include failed audits and flag them."
    )
    if "test_chat_history" not in st.session_state:
        st.session_state.test_chat_history = []

    for msg in st.session_state.test_chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask a question about uploaded invoices (e.g., unit prices, GST, totals)..."):
        scrubbed_q = scrub_pdpa(prompt)
        st.session_state.test_chat_history.append({"role": "user", "content": scrubbed_q})
        with st.chat_message("user"):
            st.markdown(scrubbed_q)

        with st.chat_message("assistant"):
            answer = sqlite_invoice_answer(prompt, branch_code, sql_repo)
            if answer is not None:
                st.markdown(answer)
                st.caption("Source: SQLite invoice records")
                sql_repo.log_chat(branch_code, scrubbed_q, scrub_pdpa(answer), "sqlite", 0.0)
                st.session_state.test_chat_history.append({"role": "assistant", "content": answer})
            elif not cfg.api_key:
                st.error("Please enter your OpenAI API key in the sidebar for semantic search.")
            else:
                try:
                    budget.check_budget_or_raise()
                    vec_repo = SharedChromaRepository(cfg, budget_guard=budget)
                    hits = vec_repo.semantic_search(
                        query_text=scrubbed_q,
                        branch_code=branch_code,
                        source_type="invoice",
                        n_results=top_k,
                    )

                    if not hits:
                        answer = "No invoice chunks found in ChromaDB for the selected branch."
                        st.info(answer)
                    else:
                        context_str = "\n\n---\n\n".join(
                            f"[Invoice: {m.get('invoice_number')} | Branch: {m.get('branch_code')} | "
                            f"Audit: {m.get('audit_status')}]\n{doc}"
                            for m, doc in hits
                        )

                        llm = ChatOpenAI(
                            model=cfg.chat_model,
                            temperature=0.1,
                            api_key=cfg.api_key,
                        )
                        response = llm.invoke(
                            [
                                SystemMessage(
                                    content=(
                                        "You are an invoice auditing assistant. Answer using ONLY "
                                        "the provided ChromaDB OCR context. Always cite invoice "
                                        "numbers and highlight any OCR vs. Calculated discrepancies."
                                    )
                                ),
                                HumanMessage(
                                    content=f"Context:\n{context_str}\n\nQuestion: {scrubbed_q}"
                                ),
                            ]
                        )
                        answer = str(response.content)

                        cost = budget.log_response(
                            thread_id=f"rag_chat_test:{branch_code}",
                            prompt=scrubbed_q,
                            response=response,
                            requested_model=cfg.chat_model,
                        )
                        sql_repo.log_chat(branch_code, scrubbed_q, answer, "semantic_search", cost)

                        st.markdown(answer)
                        with st.expander("Retrieved ChromaDB Chunks & Metadata"):
                            for meta, doc in hits:
                                st.json(meta)
                                st.text(doc)
                                st.divider()

                    st.session_state.test_chat_history.append(
                        {"role": "assistant", "content": answer}
                    )
                except BudgetExceededError as exc:
                    st.error(str(exc))

# ==============================================================================
# TAB 3: Inspect Raw ChromaDB Collection State
# ==============================================================================
with tab_inspect:
    st.subheader("Stored ChromaDB Invoice Chunks")
    if not cfg.api_key:
        st.info("Enter your OpenAI API key in the sidebar to inspect ChromaDB.")
    else:
        vec_repo = SharedChromaRepository(cfg)
        col = vec_repo.get_collection()
        st.caption(f"Collection: `{vec_repo.collection_name}` | Total Chunks: **{col.count()}**")

        records = col.get(where={"source_type": "invoice"}, include=["documents", "metadatas"])
        if not records["ids"]:
            st.info("No invoice chunks stored yet.")
        else:
            for cid, meta, doc in zip(records["ids"], records["metadatas"], records["documents"]):
                with st.expander(
                    f"Chunk ID: `{cid[:16]}...` | Invoice: `{meta.get('invoice_number')}` | "
                    f"Branch: `{meta.get('branch_code')}` | Tokens: `{meta.get('token_count')}`"
                ):
                    st.json(meta)
                    st.code(doc, language="markdown")
