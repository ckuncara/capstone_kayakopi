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


def _expires_at(session: dict[str, Any]) -> float:
    explicit = session.get("expires_at")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    expires_in = session.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return time.time() + float(expires_in)
    return time.time() + 3600


def _save_session(session: dict[str, Any]) -> str:
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise RuntimeError("Supabase Auth did not return valid session tokens.")
    st.session_state[SESSION_KEY] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _expires_at(session),
    }
    return access_token


def sign_in(cfg: AppConfig, email: str, password: str) -> str:
    session = _auth_request(
        cfg,
        "token?grant_type=password",
        {"email": email, "password": password},
    )
    return _save_session(session)


def current_access_token(cfg: AppConfig) -> str | None:
    session = st.session_state.get(SESSION_KEY)
    if not isinstance(session, dict):
        return None
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    expires_at = session.get("expires_at")
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        st.session_state.pop(SESSION_KEY, None)
        return None
    if isinstance(expires_at, (int, float)) and expires_at <= time.time() + 60:
        try:
            refreshed = _auth_request(
                cfg,
                "token?grant_type=refresh_token",
                {"refresh_token": refresh_token},
            )
            return _save_session(refreshed)
        except RuntimeError:
            st.session_state.pop(SESSION_KEY, None)
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
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        try:
            access_token = sign_in(cfg, email, password)
        except RuntimeError as exc:
            st.error(str(exc))
        else:
            st.rerun()
    return None


def sign_out() -> None:
    st.session_state.pop(SESSION_KEY, None)


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
