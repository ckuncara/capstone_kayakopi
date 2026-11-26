import streamlit as st
import pymysql
from langchain_core.messages import HumanMessage, SystemMessage
from kaya_kopi.config import AppConfig
from kaya_kopi.ingestion.parsers.invoice_vision_audit import (
    extract_invoice_with_openai,
    validate_invoice_calculations,
)
from kaya_kopi.repositories.chroma_store import SharedChromaRepository
from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository
from kaya_kopi.repositories.mysql_invoice_sync import (
    completed_syncs, pending_syncs, queue_resync, sync_invoice,
)
from kaya_kopi.security.budget_guard import BudgetExceededError, BudgetGuard
from kaya_kopi.security.openai_proxy import (
    create_chat_model,
    openai_configured,
    set_proxy_access_token,
)
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa
from kaya_kopi.security.supabase_auth import render_sign_in

def render_invoice_audit_panel() -> None:
    default_cfg = AppConfig()
    access_token = render_sign_in(default_cfg) if default_cfg.llm_proxy_url else None
    if default_cfg.llm_proxy_url and access_token is None:
        return
    set_proxy_access_token(access_token)

    with st.sidebar:
        st.divider()
        st.subheader("OpenAI Config")
        if default_cfg.llm_proxy_url:
            st.caption("OpenAI calls use your authenticated team proxy.")
            api_key = default_cfg.api_key
        else:
            api_key = st.text_input("OpenAI API Key", type="password", value=default_cfg.api_key)
        with st.expander("OpenAI Models & Budget", expanded=False):
            ocr_model = st.text_input("OCR Model", value=default_cfg.ocr_model)
            chat_model = st.text_input("Chat Model", value=default_cfg.chat_model)
            embed_model = st.text_input("Embedding Model", value=default_cfg.embed_model)
            budget_cap = st.number_input(
                "Monthly Budget Cap (USD)", min_value=1.0, value=default_cfg.monthly_budget_usd
            )
        skip_dups = st.checkbox("Prevent duplicate uploads", value=True)

    cfg = AppConfig(
        api_key=api_key,
        ocr_model=ocr_model,
        chat_model=chat_model,
        embed_model=embed_model,
        monthly_budget_usd=float(budget_cap),
    )
    repo = SQLiteOpsRepository(cfg)
    budget = BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd)

    spend = budget.get_current_month_spend()
    with st.sidebar:
        st.metric("Monthly OpenAI Spend", f"${spend:.4f} / ${cfg.monthly_budget_usd:.2f}")

    tab_up, tab_sql, tab_rag = st.tabs(
        ["Upload & Audit Invoices", "SQLite Analytics & CSV", "Redacted Invoice Q&A"]
    )

    with tab_up:
        sync_notice = st.session_state.pop("mysql_sync_notice", None)
        if sync_notice is not None:
            if sync_notice.purchase_id is not None and not sync_notice.review_required:
                st.success(sync_notice.message)
            else:
                st.warning(sync_notice.message)
        for pending in pending_syncs(repo):
            st.warning(f"MySQL sync pending: {pending['invoice_number']}")
            if st.button("Retry MySQL sync", key=f"retry_mysql_{pending['upload_id']}"):
                try:
                    result = sync_invoice(repo, pending["upload_id"])
                except (pymysql.MySQLError, ValueError, OSError) as exc:
                    st.error("MySQL sync failed; the invoice remains available locally for retry.")
                    st.exception(exc)
                else:
                    st.session_state.mysql_sync_notice = result
                    st.rerun()
        synced_invoices = completed_syncs(repo)
        if synced_invoices:
            with st.expander("Resync invoices to MySQL"):
                for synced in synced_invoices:
                    if st.button(
                        f"Sync again: {synced['invoice_number']}",
                        key=f"resync_mysql_{synced['upload_id']}",
                    ):
                        try:
                            queue_resync(repo, synced["upload_id"])
                            result = sync_invoice(repo, synced["upload_id"])
                        except (pymysql.MySQLError, ValueError, OSError) as exc:
                            st.error("MySQL sync failed; the invoice remains available locally.")
                            st.exception(exc)
                        else:
                            st.session_state.mysql_sync_notice = result
                            st.rerun()
        files = st.file_uploader(
            "Upload supplier invoice images (saved as <invoice_no>.jpg + <invoice_no>.csv)",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
        )
        if files and st.button("Extract, Validate & Save", type="primary"):
            if not openai_configured(cfg):
                st.error("OpenAI access is not configured.")
            else:
                vec_repo = SharedChromaRepository(cfg, budget_guard=budget)
                for f in files:
                    raw_bytes = f.getvalue()
                    f_hash = repo.compute_hash(raw_bytes)
                    if skip_dups and repo.hash_exists(f_hash):
                        st.warning(f"Skipped duplicate file bytes: `{f.name}`")
                        continue

                    try:
                        with st.spinner(f"Running OCR & 9% GST audit on {f.name}..."):
                            inv = extract_invoice_with_openai(
                                raw_bytes,
                                f.type or "image/jpeg",
                                cfg.ocr_model,
                                cfg.api_key,
                                budget_guard=budget,
                                branch_code=None,
                            )
                            if skip_dups and repo.invoice_number_exists(inv.invoice_number):
                                st.warning(
                                    f"Skipped duplicate invoice number: `{inv.invoice_number}`"
                                )
                                continue

                            repo.check_sync_identity(f_hash, inv.invoice_number, inv.vendor_name)
                            stored_name, rel_img_path = repo.save_image_as_invoice_jpg(
                                raw_bytes, inv.invoice_number, f_hash
                            )
                            audit = validate_invoice_calculations(inv)

                            repo.save_invoice(
                                f_hash, None, stored_name, rel_img_path, inv, audit,
                                sync_mysql=True,
                            )
                            indiv_csv = repo.save_individual_invoice_csv(
                                stored_name, rel_img_path, None, inv, audit
                            )
                            master_csv = repo.sync_master_csv_in_image_folder()

                            unified_doc = (
                                f"Invoice: {inv.invoice_number} | "
                                f"Vendor: {inv.vendor_name} (GST Reg: {inv.vendor_gst_reg_no or 'N/A'}) | "
                                f"Date: {inv.invoice_date or 'N/A'}\n"
                                f"OCR Totals -> Subtotal: {audit['ocr_subtotal']:.2f}, "
                                f"GST: {audit['ocr_gst']:.2f}, Total: {audit['ocr_grand_total']:.2f}\n"
                                f"Calc Totals -> Subtotal: {audit['calc_subtotal']:.2f}, "
                                f"GST: {audit['calc_gst']:.2f}, Total: {audit['calc_grand_total']:.2f}\n"
                                f"Audit Status: {'PASS' if audit['all_valid'] else 'FAIL'}\n\n"
                                f"{inv.raw_markdown_text}"
                            )
                            vec_repo.ingest_document(
                                parent_doc_id=f_hash,
                                source_type="invoice",
                                branch_code=None,
                                text=unified_doc,
                                strategy="single",
                                extra_metadata={
                                    "invoice_number": inv.invoice_number,
                                    "filename": stored_name,
                                    "image_path": rel_img_path,
                                    "vendor_name": inv.vendor_name,
                                    "audit_status": "PASS" if audit["all_valid"] else "FAIL",
                                },
                            )
                            sync = next(
                                entry for entry in pending_syncs(repo) if entry["file_hash"] == f_hash
                            )
                            try:
                                result = sync_invoice(repo, sync["upload_id"])
                            except (pymysql.MySQLError, ValueError, OSError) as exc:
                                st.error("MySQL sync failed; the invoice remains available locally for retry.")
                                st.exception(exc)
                            else:
                                if result.purchase_id is not None and not result.review_required:
                                    st.success(result.message)
                                else:
                                    st.warning(result.message)
                    except BudgetExceededError as exc:
                        st.error(str(exc))
                        break
                    except ValueError as exc:
                        st.error(str(exc))
                        continue

                    badge = "✅" if audit["all_valid"] else "⚠️"
                    with st.expander(
                        f"{badge} `{rel_img_path}` | `{indiv_csv}` | {inv.vendor_name}",
                        expanded=True,
                    ):
                        if audit["all_valid"]:
                            st.success(f"Math & GST verified! Synced master CSV -> `{master_csv}`")
                        else:
                            for err in audit["issues"]:
                                st.error(err)
                        st.dataframe(audit["line_checks"], use_container_width=True)

    with tab_sql:
        data = repo.get_analytics()
        kpis = data["kpis"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Invoices", kpis["total_invoices"])
        c2.metric("OCR vs Calc Spend", f"${kpis['ocr_spend']:,.2f} / ${kpis['calc_spend']:,.2f}")
        c3.metric("OCR vs Calc GST", f"${kpis['ocr_gst']:,.2f} / ${kpis['calc_gst']:,.2f}")
        c4.metric("Pass / Fail", f"{kpis['passed'] or 0} / {kpis['failed'] or 0}")

        if data["by_vendor"]:
            st.dataframe(data["by_vendor"], use_container_width=True)
            st.download_button(
                "Download Master Audited CSV",
                data=repo.export_master_csv_bytes(),
                file_name="supplier_invoices_audited.csv",
                mime="text/csv",
            )

    with tab_rag:
        q = st.text_input("Ask a question (auto-scrubbed for SG PDPA):")
        if q and openai_configured(cfg):
            scrubbed_q = scrub_pdpa(q)
            try:
                budget.check_budget_or_raise()
                vec_repo = SharedChromaRepository(cfg, budget_guard=budget)
                hits = vec_repo.semantic_search(scrubbed_q, branch_code=None, n_results=4)
                if not hits:
                    st.info("No matching documents found.")
                else:
                    ctx = "\n\n---\n\n".join(doc for _, doc in hits)
                    chat = create_chat_model(
                        cfg, cfg.chat_model, purpose="chat", temperature=0.1
                    )
                    resp = chat.invoke(
                        [
                            SystemMessage(
                                content="Answer using ONLY the provided context. Cite invoice "
                                "numbers and note any OCR vs Calculated discrepancies."
                            ),
                            HumanMessage(content=f"Context:\n{ctx}\n\nQuestion: {scrubbed_q}"),
                        ]
                    )
                    ans_text = str(resp.content)
                    cost = budget.log_response(
                        thread_id="rag_chat",
                        prompt=scrubbed_q,
                        response=resp,
                        requested_model=cfg.chat_model,
                    )
                    repo.log_chat(None, scrubbed_q, ans_text, "semantic_search", cost)
                    st.markdown(ans_text)
            except BudgetExceededError as exc:
                st.error(str(exc))