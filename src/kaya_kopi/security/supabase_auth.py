"""Supabase Auth session helpers for local Streamlit clients."""

from __future__ import annotations

import getpass
import hashlib
import math
import os
import sys
import time
import uuid
from typing import Any

import httpx
import streamlit as st

from kaya_kopi.config import AppConfig
from kaya_kopi.security.openai_proxy import set_proxy_access_token

SESSION_KEY = "supabase_auth_session"
REMEMBER_ME_KEY = "supabase_remember_me"
SESSION_TIMEOUT_MINS = 10
REMEMBER_ME_DAYS = 14
_STORAGE_COMMAND = "supabase_storage_command"
_STORAGE_HANDLED = "supabase_storage_handled"
_STORAGE_KEY = "supabase_storage_key"

# A bidirectional component returns browser storage directly to Python, without
# putting credentials into a URL or racing an iframe redirect against st.rerun().
_STORAGE_JS = """
export default function(component) {
    const { data, setStateValue } = component;
    const result = { id: data.id, value: null, error: false };
    try {
        for (const key of ["supabase_auth", "supabase_refresh", "supabase_expires"]) {
            localStorage.removeItem(key);
        }
        if (data.operation === "write") {
            localStorage.setItem(data.key, JSON.stringify(data.value));
        } else if (data.operation === "clear") {
            localStorage.removeItem(data.key);
        } else {
            const raw = localStorage.getItem(data.key);
            if (raw) {
                try {
                    result.value = JSON.parse(raw);
                } catch (_) {
                    localStorage.removeItem(data.key);
                }
            }
        }
    } catch (_) {
        result.error = true;
    }
    setStateValue("result", result);
}
"""


@st.cache_resource
def _storage_component():
    return st.components.v2.component("kaya_auth_storage", js=_STORAGE_JS)


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


def _clear_legacy_query_params() -> None:
    for key in ("auth", "refresh", "expires"):
        if key in st.query_params:
            del st.query_params[key]


def _queue_storage(operation: str, value: dict[str, Any] | None = None) -> None:
    st.session_state[_STORAGE_COMMAND] = {
        "id": uuid.uuid4().hex,
        "operation": operation,
        "key": st.session_state.get(_STORAGE_KEY, "kaya_auth"),
        "value": value,
    }


def _storage_pending() -> bool:
    command = st.session_state.get(_STORAGE_COMMAND, {})
    return command.get("id") != st.session_state.get(_STORAGE_HANDLED)


def _valid_timestamp(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _expires_at(session: dict[str, Any]) -> float:
    explicit = session.get("expires_at")
    if _valid_timestamp(explicit):
        return float(explicit)
    expires_in = session.get("expires_in", 3600)
    if not _valid_timestamp(expires_in):
        raise RuntimeError("Supabase Auth returned an invalid token expiry.")
    return time.time() + float(expires_in)


def _save_session(
    session: dict[str, Any], remember_me: bool = False, *, deadline: float | None = None
) -> str:
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
    ):
        raise RuntimeError("Supabase Auth did not return valid session tokens.")
    if deadline is None:
        duration = REMEMBER_ME_DAYS * 86400 if remember_me else SESSION_TIMEOUT_MINS * 60
        deadline = time.time() + duration
    st.session_state[SESSION_KEY] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _expires_at(session),
        "deadline": deadline,
    }
    st.session_state[REMEMBER_ME_KEY] = remember_me
    if remember_me:
        _queue_storage("write", {"refresh_token": refresh_token, "deadline": deadline})
    else:
        _queue_storage("clear")
    return access_token


def _sync_browser_storage(cfg: AppConfig) -> None:
    if _STORAGE_COMMAND not in st.session_state:
        project = hashlib.sha256(cfg.supabase_url.encode()).hexdigest()[:16]
        st.session_state[_STORAGE_KEY] = f"kaya_auth_{project}"
        _queue_storage("read")
    command = st.session_state[_STORAGE_COMMAND]
    component_result = _storage_component()(
        key="kaya_auth_browser_storage",
        data=command,
        on_result_change=lambda: None,
        height=0,
    )
    result = getattr(component_result, "result", None)
    if not _storage_pending():
        return
    if not isinstance(result, dict) or result.get("id") != command["id"]:
        st.caption("Updating sign-in session…")
        st.stop()
        return
    st.session_state[_STORAGE_HANDLED] = command["id"]
    if result.get("error"):
        st.session_state["supabase_storage_error"] = (
            "Browser storage is unavailable. Remembered sign-in could not be saved or cleared. "
            "Allow site storage, or clear this site's data to remove a previously saved login."
        )
        return
    if command["operation"] == "read" and result.get("value") is not None:
        saved = result["value"]
        deadline = saved.get("deadline") if isinstance(saved, dict) else None
        refresh_token = saved.get("refresh_token") if isinstance(saved, dict) else None
        if (
            _valid_timestamp(deadline)
            and time.time() < deadline <= time.time() + REMEMBER_ME_DAYS * 86400
            and isinstance(refresh_token, str)
            and refresh_token
        ):
            try:
                refreshed = _auth_request(
                    cfg, "token?grant_type=refresh_token", {"refresh_token": refresh_token}
                )
                _save_session(refreshed, True, deadline=deadline)
            except RuntimeError:
                _expire_session()
        else:
            _queue_storage("clear")
        # Wait for rotated tokens (or removal) to reach storage before proceeding.
        st.rerun()


def _expire_session() -> None:
    sign_out()
    st.session_state["supabase_auth_error"] = (
        "Your session expired or could not be refreshed. Sign in again."
    )


def sign_in(cfg: AppConfig, email: str, password: str, remember_me: bool = False) -> str:
    session = _auth_request(
        cfg,
        "token?grant_type=password",
        {"email": email, "password": password},
    )
    return _save_session(session, remember_me)


def current_access_token(cfg: AppConfig) -> str | None:
    session = st.session_state.get(SESSION_KEY)
    if not isinstance(session, dict):
        return None
    deadline = session.get("deadline")
    if not _valid_timestamp(deadline) or deadline <= time.time():
        _expire_session()
        return None
    expires_at = session.get("expires_at")
    if not _valid_timestamp(expires_at) or expires_at <= time.time() + 60:
        try:
            refreshed = _auth_request(
                cfg, "token?grant_type=refresh_token", {"refresh_token": session["refresh_token"]}
            )
            return _save_session(
                refreshed, st.session_state.get(REMEMBER_ME_KEY, False), deadline=deadline
            )
        except RuntimeError:
            _expire_session()
            return None
    return session.get("access_token")


def render_sign_in(cfg: AppConfig) -> str | None:
    if not cfg.supabase_url or not cfg.supabase_anon_key:
        st.error("Set SUPABASE_URL and SUPABASE_ANON_KEY before using the OpenAI proxy.")
        return None

    _clear_legacy_query_params()
    _sync_browser_storage(cfg)
    storage_error = st.session_state.pop("supabase_storage_error", None)
    if storage_error:
        st.warning(storage_error)
    access_token = current_access_token(cfg)
    if _storage_pending():
        st.rerun()
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
    _clear_legacy_query_params()
    _queue_storage("clear")


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
