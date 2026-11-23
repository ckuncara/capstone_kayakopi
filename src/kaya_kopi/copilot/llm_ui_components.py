"""Streamlit UI components for LLM usage tracking and spend meter."""

from __future__ import annotations

import streamlit as st

from kaya_kopi.copilot.llm_usage_tracker import LLMUsageTracker


def render_spend_meter(tracker: LLMUsageTracker) -> None:
    """
    Render the spend meter in the sidebar.
    
    Shows:
    - Current LLM model
    - Last token usage (tokens and $ value)
    - Budget left ($ value with % used)
    - Horizontal bar with color indication
    """
    with st.sidebar:
        st.divider()
        
        # Get current data
        budget_info = tracker.get_budget_info()
        latest_usage = tracker.get_latest_usage()
        current_model = tracker.get_current_model()
        
        # Header
        st.markdown("### 💰 LLM Spend Meter")
        
        # Current model
        st.markdown(f"**Model:** `{current_model}`")
        
        # Last token usage
        if latest_usage:
            st.markdown(f"**Last Usage:**")
            st.markdown(f"  • Tokens: `{latest_usage['total_tokens']:,}`")
            st.markdown(f"  • Cost: `${latest_usage['total_cost']:.6f}`")
        else:
            st.markdown("**Last Usage:** No usage yet")
        
        # Budget left
        budget_left = budget_info["budget_left"]
        percentage_used = budget_info["percentage_used"]
        st.markdown(
            f"**Budget Left:** `${budget_left:.2f}` ({percentage_used:.1f}% used)"
        )
        
        # Progress bar with color
        color = tracker.get_color_for_spending(percentage_used)
        progress_value = percentage_used / 100
        
        # Create colored progress bar using plotly
        st.progress(progress_value, text=f"{percentage_used:.1f}%")
        
        # Add color indicator text
        if color == "green":
            st.markdown("✅ **Status:** Healthy spending")
        elif color == "orange":
            st.markdown("⚠️ **Status:** Moderate spending")
        else:
            st.markdown("🔴 **Status:** High spending")


def render_usage_log_panel(tracker: LLMUsageTracker) -> None:
    """
    Render the usage log panel showing historical usage.
    
    Shows:
    - Date, Time, Prompt, Tokens Used, $ Used
    - Sorted descending (latest first)
    - Back button to return to chat
    """
    # Back button
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if st.button("← Back to Chat", use_container_width=True):
            st.session_state.show_usage_log = False
            st.rerun()
    
    st.title("📊 LLM Usage Log")
    
    # Get usage history
    usage_history = tracker.get_all_usage_history()
    
    if not usage_history:
        st.info("No usage history yet. Start a conversation to see token usage.")
        return
    
    # Prepare data for table
    table_data = []
    for entry in usage_history:
        from datetime import datetime
        
        timestamp = entry['timestamp']
        if isinstance(timestamp, str):
            dt = datetime.fromisoformat(timestamp)
        else:
            dt = timestamp
        
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        prompt = entry['prompt']
        
        # Truncate prompt if too long for display
        if len(prompt) > 100:
            prompt_display = prompt[:97] + "..."
        else:
            prompt_display = prompt
        
        tokens = entry['total_tokens']
        cost = entry['total_cost']
        
        table_data.append({
            "Date": date_str,
            "Time": time_str,
            "Prompt": prompt_display,
            "Tokens": f"{tokens:,}",
            "Cost (USD)": f"${cost:.6f}",
        })
    
    # Display table
    st.dataframe(
        table_data,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Prompt": st.column_config.TextColumn(width="large"),
            "Tokens": st.column_config.TextColumn(width="small"),
            "Cost (USD)": st.column_config.TextColumn(width="small"),
        }
    )
    
    # Summary statistics
    st.divider()
    total_entries = len(usage_history)
    total_tokens = sum(entry['total_tokens'] for entry in usage_history)
    total_cost = sum(entry['total_cost'] for entry in usage_history)
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Queries", total_entries)
    with col2:
        st.metric("Total Tokens", f"{total_tokens:,}")
    with col3:
        st.metric("Total Cost", f"${total_cost:.4f}")
