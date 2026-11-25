"""Supabase Auth session helpers for local Streamlit clients."""

from __future__ import annotations

import getpass
import os
import sys
import time
from typing import Any

import httpx
import streamlit as st

from kaya_kopi.config import AppConfig
from kaya_kopi.security.openai_proxy import set_proxy_access_token

SESSION_KEY = "supabase_auth_session"
REMEMBER_ME_KEY = "supabase_remember_me"
SESSION_TIMEOUT_MINS = 10
REMEMBER_ME_DAYS = 14


def _init_session_storage() -> None:
    """Initialize browser localStorage for persistent sessions."""
    if "session_storage_initialized" in st.session_state:
        return
    
    st.session_state["session_storage_initialized"] = True
    
    # JavaScript to load session from localStorage on page load
    st.components.v1.html(
        """
        <script>
        function initSessionStorage() {
            const sessionKey = "supabase_auth_session";
            const rememberMeKey = "supabase_remember_me";
            const sessionData = localStorage.getItem(sessionKey);
            const rememberMe = localStorage.getItem(rememberMeKey);
            
            if (sessionData) {
                // Set session data in browser's sessionStorage for Streamlit
                sessionStorage.setItem(sessionKey, sessionData);
            }
            if (rememberMe) {
                sessionStorage.setItem(rememberMeKey, rememberMe);
            }
        }
        initSessionStorage();
        </script>
        """,
        height=0,
    )


def _persist_to_browser(session_data: dict, remember_me: bool) -> None:
    """Persist session data to browser localStorage."""
    import json
    
    st.components.v1.html(
        f"""
        <script>
        function persistSession() {{
            const sessionData = {json.dumps(session_data)};
            const rememberMe = {str(remember_me).lower()};
            
            if (rememberMe) {{
                localStorage.setItem("supabase_auth_session", JSON.stringify(sessionData));
                localStorage.setItem("supabase_remember_me", "true");
            }} else {{
                localStorage.removeItem("supabase_auth_session");
                localStorage.removeItem("supabase_remember_me");
            }}
        }}
        persistSession();
        </script>
        """,
        height=0,
    )


def _clear_browser_session() -> None:
    """Clear session from browser localStorage."""
    st.components.v1.html(
        """
        <script>
        localStorage.removeItem("supabase_auth_session");
        localStorage.removeItem("supabase_remember_me");
        </script>
        """,
        height=0,
    )


def _auth_request(cfg: AppConfig, endpoint: str, payload: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{cfg.supabase_url}/auth/v1/{endpoint}",
            headers={"apikey": cfg.supabase_anon_key},
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("Could not connect to Supabase Auth.") from exc
    if not response.is_success:
        try:
            error_response = response.json()
            detail = (
                error_response.get("msg") or error_response.get("message")
                if isinstance(error_response, dict)
                else None
            )
        except ValueError:
            detail = None
        raise RuntimeError(str(detail or "Supabase sign-in failed."))
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Supabase Auth returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Supabase Auth returned an invalid session.")
    return result


def _expires_at(session: dict[str, Any], remember_me: bool = False) -> float:
    explicit = session.get("expires_at")
    if isinstance(explicit, (int, float)):
        expires_in = float(explicit) - time.time()
    else:
        expires_in = session.get("expires_in")
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        expires_in = float(expires_in)
    
    # If "Remember me" is checked, extend expiry to 14 days; otherwise use 10 mins
    if remember_me:
        return time.time() + (14 * 24 * 60 * 60)  # 14 days
    else:
        return time.time() + (SESSION_TIMEOUT_MINS * 60)  # 10 minutes


def _save_session(session: dict[str, Any], remember_me: bool = False) -> str:
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise RuntimeError("Supabase Auth did not return valid session tokens.")
    
    session_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _expires_at(session, remember_me),
    }
    
    st.session_state[SESSION_KEY] = session_data
    st.session_state[REMEMBER_ME_KEY] = remember_me
    
    # Persist to browser if remember_me is checked
    if remember_me:
        _persist_to_browser(session_data, True)
    
    return access_token


def sign_in(cfg: AppConfig, email: str, password: str, remember_me: bool = False) -> str:
    session = _auth_request(
        cfg,
        "token?grant_type=password",
        {"email": email, "password": password},
    )
    return _save_session(session, remember_me)


def current_access_token(cfg: AppConfig) -> str | None:
    # First check if we have a session in memory
    session = st.session_state.get(SESSION_KEY)
    if not isinstance(session, dict):
        # Try to initialize from browser storage on first load
        _init_session_storage()
        # On next run, the data will be in sessionStorage
        return None
    
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    expires_at = session.get("expires_at")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        st.session_state.pop(SESSION_KEY, None)
        _clear_browser_session()
        return None
    if isinstance(expires_at, (int, float)) and expires_at <= time.time() + 60:
        try:
            refreshed = _auth_request(
                cfg,
                "token?grant_type=refresh_token",
                {"refresh_token": refresh_token},
            )
            remember_me = st.session_state.get(REMEMBER_ME_KEY, False)
            return _save_session(refreshed, remember_me)
        except RuntimeError:
            st.session_state.pop(SESSION_KEY, None)
            _clear_browser_session()
            st.session_state["supabase_auth_error"] = (
                "Your session expired or could not be refreshed. Sign in again."
            )
            return None
    return access_token


def render_sign_in(cfg: AppConfig) -> str | None:
    if not cfg.supabase_url or not cfg.supabase_anon_key:
        st.error("Set SUPABASE_URL and SUPABASE_ANON_KEY before using the OpenAI proxy.")
        return None
    access_token = current_access_token(cfg)
    if access_token:
        return access_token
    auth_error = st.session_state.pop("supabase_auth_error", None)
    if auth_error:
        st.warning(auth_error)

    st.title("Sign in")
    with st.form("supabase_sign_in"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        remember_me = st.checkbox(f"Remember me for {REMEMBER_ME_DAYS} days")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            access_token = sign_in(cfg, email, password, remember_me)
        except RuntimeError as exc:
            st.error(str(exc))
        else:
            st.rerun()
    return None


def sign_out() -> None:
    st.session_state.pop(SESSION_KEY, None)
    st.session_state.pop(REMEMBER_ME_KEY, None)
    _clear_browser_session()


def ensure_cli_sign_in(cfg: AppConfig) -> None:
    if not cfg.llm_proxy_url:
        return
    access_token = os.getenv("KAYA_PROXY_ACCESS_TOKEN")
    if access_token:
        set_proxy_access_token(access_token)
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Set KAYA_PROXY_ACCESS_TOKEN or run this command in a terminal to sign in."
        )
    email = input("Supabase account email: ").strip()
    password = getpass.getpass("Supabase account password: ")
    session = _auth_request(
        cfg,
        "token?grant_type=password",
        {"email": email, "password": password},
    )
    access_token = session.get("access_token")
    if not isinstance(access_token, str):
        raise RuntimeError("Supabase Auth did not return an access token.")
    set_proxy_access_token(access_token)
