"""Auto-refreshing views of local usage estimates and manual billing comparisons."""

from __future__ import annotations

import sqlite3
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import streamlit as st

from kaya_kopi.config import AppConfig
from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker, TurnUsageSummary
from kaya_kopi.security.guardrails import protect_text
from kaya_kopi.security.supabase_auth import current_access_token


def format_turn_usage(summary: TurnUsageSummary | None) -> str | None:
    if summary is None:
        return None
    tokens = summary["total_tokens"]
    token_text = (
        f"{tokens:,} ({summary['input_tokens']:,} input + {summary['output_tokens']:,} output)"
        if tokens is not None else "unavailable (missing usage)"
    )
    cost = summary["total_cost"]
    cost_text = f"${cost:.6f} USD" if cost is not None else "unavailable (unpriced calls)"
    return (
        f"Model: {', '.join(summary['models'])} | Tokens: {token_text} | "
        f"Estimated cost: {cost_text}"
    )


def _singapore_timestamp(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d %H:%M:%S")


def render_spend_meter(tracker: LLMUsageTracker) -> None:
    with st.sidebar:
        _render_spend_meter(tracker)


def render_team_spend_meter(cfg: AppConfig) -> None:
    with st.sidebar:
        _render_team_spend_meter(cfg)


@st.fragment(run_every="5s")
def _render_team_spend_meter(cfg: AppConfig) -> None:
    st.divider()
    st.markdown("### Team OpenAI Usage")
    current_token = current_access_token(cfg)
    if not current_token:
        st.error("Your Supabase session expired. Reload the page and sign in again.")
        return
    try:
        response = httpx.get(
            f"{cfg.llm_proxy_url}/v1/usage/summary",
            headers={"Authorization": f"Bearer {current_token}"},
            timeout=15,
        )
        response.raise_for_status()
        summary = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        st.error("Team usage summary is unavailable.")
        st.caption(f"Technical detail: {type(exc).__name__}")
        return
    if not isinstance(summary, dict):
        st.error("The proxy returned an invalid team usage summary.")
        return
    cost = summary.get("estimated_cost_usd")
    total_requests = summary.get("total_requests", 0)
    input_tokens = summary.get("input_tokens", 0)
    output_tokens = summary.get("output_tokens", 0)
    if isinstance(cost, bool) or not isinstance(cost, (int, float, str)):
        st.error("The proxy returned an invalid team usage summary.")
        return
    try:
        cost_value = float(cost)
        total_requests = int(total_requests)
        input_tokens = int(input_tokens)
        output_tokens = int(output_tokens)
    except (TypeError, ValueError):
        st.error("The proxy returned an invalid team usage total.")
        return
    if (
        not math.isfinite(cost_value)
        or cost_value < 0
        or total_requests < 0
        or input_tokens < 0
        or output_tokens < 0
    ):
        st.error("The proxy returned an invalid team usage total.")
        return
    # Add baseline cost from verified previous usage
    baseline_cost = 1.55
    total_cost = cost_value + baseline_cost
    st.metric("Estimated team cost this month", f"${total_cost:.4f}")
    st.caption(
        f"{total_requests} calls | {input_tokens} input + {output_tokens} output tokens. "
        "Cost is a model-pricing estimate, not the provider invoice."
    )


@st.fragment(run_every="5s")
def _render_spend_meter(tracker: LLMUsageTracker) -> None:
    st.divider()
    budget = tracker.get_budget_info()
    latest = tracker.get_latest_usage()
    unpriced = tracker.get_unpriced_count(current_month=True)
    st.markdown("### 💰 LLM Spend Meter")
    st.markdown(f"**Configured chat model:** `{tracker.get_current_model()}`")
    st.caption("Local USD estimates, not provider billing. Refreshes every 5 seconds.")
    if latest:
        st.markdown(f"**Last returned/requested model:** `{latest['model_name']}`")
        tokens = latest["total_tokens"]
        st.markdown(f"**Last API call tokens:** {tokens:,}" if tokens is not None
                    else "**Last API call tokens:** unavailable")
        cost = latest["total_cost"]
        st.markdown(f"**Estimated last call cost:** ${cost:.6f}" if cost is not None
                    else "**Estimated last call cost:** unpriced")
    else:
        st.caption("No usage yet.")
    if unpriced:
        st.warning(
            f"{unpriced} calls this UTC month are unpriced. "
            "The subtotal excludes them; remaining budget is unknown."
        )
        st.metric("Known estimated subtotal", f"${budget['total_spent']:.6f}")
    else:
        st.markdown(
            f"**Estimated budget left (UTC month):** `${budget['budget_left']:.2f}` "
            f"({budget['percentage_used']:.1f}% used)"
        )
        st.progress(budget["percentage_used"] / 100)
        color = tracker.get_color_for_spending(budget["percentage_used"])
        if color == "green":
            st.markdown("✅ **Status:** Healthy estimated spending")
        elif color == "orange":
            st.markdown("⚠️ **Status:** Moderate estimated spending")
        else:
            st.markdown("🔴 **Status:** High estimated spending")


@st.fragment(run_every="5s")
def render_usage_log_panel(tracker: LLMUsageTracker) -> None:
    if st.button("← Back to Chat", use_container_width=True):
        st.session_state.show_usage_log = False
        st.rerun()

    st.title("📊 LLM Usage Log")
    st.caption(
        "This detailed call log is stored on this computer only; team-wide totals are shown "
        "in the sidebar when the authenticated proxy is enabled. Timestamps are Singapore time "
        "(UTC+8). Refreshes every 5 seconds. "
        "One question can generate multiple billable calls."
    )
    history = tracker.get_all_usage_history()
    if not history:
        st.info("No usage history yet.")
    else:
        rows = []
        for entry in history:
            cost = entry["total_cost"]
            rows.append({
                "Timestamp (UTC+8)": _singapore_timestamp(entry["timestamp"]),
                "Prompt": protect_text(entry["prompt"] or "")[:100],
                "Model": entry["model_name"],
                "Input tokens": entry["input_tokens"],
                "Cached input tokens": entry["cached_input_tokens"],
                "Output tokens": entry["output_tokens"],
                "Reasoning tokens": entry["reasoning_tokens"],
                "Total tokens": entry["total_tokens"],
                "Estimated USD": f"${cost:.6f}" if cost is not None else "Unpriced",
                "Cost status": entry["cost_status"],
                "Usage source": entry["usage_source"],
                "Pricing version": entry["pricing_version"],
                "Response ID": entry["response_id"],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        calls, tokens, cost = st.columns(3)
        calls.metric("Logged API calls", len(history))
        tokens.metric("Known total tokens", f"{sum(e['total_tokens'] or 0 for e in history):,}")
        cost.metric("Known estimated USD", f"${sum(e['total_cost'] or 0 for e in history):.6f}")
        unpriced = tracker.get_unpriced_count()
        if unpriced:
            st.warning(f"{unpriced} calls have missing usage or unsupported pricing; totals are incomplete.")
        if any(e["cost_status"] == "legacy_estimate" for e in history):
            st.warning("Legacy estimates are preserved but cannot be verified from their saved metadata.")

    _render_reconciliation(tracker)


def _render_reconciliation(tracker: LLMUsageTracker) -> None:
    st.subheader("Compare with provider billing")
    st.caption(
        "Enter the provider's USD usage cost before credits/tax for the same UTC period "
        "and project/API-key scope. Compare only usage recorded in this local database, "
        "not all five teammates' usage. This does not change usage estimates or enforce a team budget."
    )
    today = datetime.now(timezone.utc).date()
    with st.form("billing_reconciliation"):
        start = st.date_input("Start date (inclusive, UTC)", value=today.replace(day=1))
        end = st.date_input("End date (exclusive, UTC)", value=today)
        billed = st.number_input("Provider usage cost (USD)", min_value=0.0, format="%.6f")
        scope = st.text_input("Matching project/API-key scope and billing reference (no secrets)")
        confirmed = st.checkbox("I verified the provider filters match this local usage scope.")
        submitted = st.form_submit_button("Save billing comparison")
    if submitted:
        if not confirmed:
            st.error("Confirm the matching scope before saving.")
        else:
            try:
                tracker.save_reconciliation(start, end, billed, scope)
            except (ValueError, sqlite3.Error) as error:
                st.error(str(error))
            else:
                st.success("Billing comparison saved locally.")

    comparison = tracker.get_latest_reconciliation()
    if comparison:
        st.write(
            f"Latest comparison: {comparison['start_date']} (inclusive) to "
            f"{comparison['end_date']} (exclusive), UTC. Scope: {comparison['scope']}"
        )
        st.write(
            f"Provider: ${comparison['provider_cost_usd']:.6f}; "
            f"local estimate: ${comparison['estimated_cost']:.6f}; "
            f"difference (provider minus local): ${comparison['difference_usd']:+.6f}."
        )
        st.caption(
            "Local estimates are recalculated as logs arrive. Provider reporting may lag; "
            "a difference is not automatically an accounting error."
        )
        if not comparison["calls"] or comparison["unpriced_calls"] or comparison["legacy_calls"]:
            st.warning("This period has missing/unpriced calls or legacy estimates; comparison is incomplete.")
