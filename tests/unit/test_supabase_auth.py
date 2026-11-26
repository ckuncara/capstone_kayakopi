"""Session persistence, token rotation, and browser storage acknowledgement."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kaya_kopi.security import supabase_auth as auth


class StopRun(Exception):
    pass


class Rerun(Exception):
    pass


@pytest.fixture
def ui(monkeypatch):
    stub = SimpleNamespace(
        session_state={},
        query_params={},
        caption=Mock(),
        stop=Mock(side_effect=StopRun),
        rerun=Mock(side_effect=Rerun),
    )
    monkeypatch.setattr(auth, "st", stub)
    monkeypatch.setattr(auth.time, "time", lambda: 1000.0)
    return stub


@pytest.fixture
def cfg():
    return SimpleNamespace(supabase_url="https://example.supabase.co", supabase_anon_key="anon")


def tokens(**overrides):
    return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, **overrides}


def browser(monkeypatch, response):
    def render(**kwargs):
        command = kwargs["data"]
        return SimpleNamespace(result=response(command))

    monkeypatch.setattr(auth, "_storage_component", lambda: render)


def test_remember_uses_real_token_expiry_and_separate_fixed_deadline(ui):
    assert auth._save_session(tokens(), True) == "access"
    session = ui.session_state[auth.SESSION_KEY]
    assert session["expires_at"] == 4600
    assert session["deadline"] == 1000 + 14 * 86400
    command = ui.session_state[auth._STORAGE_COMMAND]
    assert command["value"] == {"refresh_token": "refresh", "deadline": session["deadline"]}
    assert "access_token" not in command["value"]
    assert not ui.query_params


def test_unchecked_remember_is_memory_only_and_clears_previous_login(ui):
    auth._save_session(tokens(expires_at=2000), False)
    assert ui.session_state[auth.SESSION_KEY]["expires_at"] == 2000
    assert ui.session_state[auth.SESSION_KEY]["deadline"] == 1600
    assert ui.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"


def test_refresh_rotates_saved_token_without_extending_deadline(ui, cfg, monkeypatch):
    auth._save_session(tokens(expires_at=1020), True)
    deadline = ui.session_state[auth.SESSION_KEY]["deadline"]
    request = Mock(return_value=tokens(access_token="new-access", refresh_token="new-refresh"))
    monkeypatch.setattr(auth, "_auth_request", request)
    assert auth.current_access_token(cfg) == "new-access"
    assert ui.session_state[auth.SESSION_KEY]["deadline"] == deadline
    assert ui.session_state[auth._STORAGE_COMMAND]["value"]["refresh_token"] == "new-refresh"
    request.assert_called_once_with(
        cfg, "token?grant_type=refresh_token", {"refresh_token": "refresh"}
    )


@pytest.mark.parametrize("remember", [True, False])
def test_session_deadline_signs_out_even_with_valid_access_token(ui, cfg, remember, monkeypatch):
    auth._save_session(tokens(), remember, deadline=1000)
    request = Mock()
    monkeypatch.setattr(auth, "_auth_request", request)
    assert auth.current_access_token(cfg) is None
    assert auth.SESSION_KEY not in ui.session_state
    assert ui.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"
    request.assert_not_called()


def test_browser_read_waits_for_matching_acknowledgement(ui, cfg, monkeypatch):
    browser(monkeypatch, lambda cmd: {"id": "stale", "value": None})
    with pytest.raises(StopRun):
        auth._sync_browser_storage(cfg)
    assert auth._STORAGE_HANDLED not in ui.session_state


def test_empty_browser_storage_shows_login_without_auth_request(ui, cfg, monkeypatch):
    browser(monkeypatch, lambda cmd: {"id": cmd["id"], "value": None})
    auth._sync_browser_storage(cfg)
    assert not auth._storage_pending()
    assert auth.SESSION_KEY not in ui.session_state


def test_new_browser_session_restores_and_saves_rotated_refresh_token(ui, cfg, monkeypatch):
    saved = {"refresh_token": "saved-refresh", "deadline": 2000}
    browser(monkeypatch, lambda cmd: {"id": cmd["id"], "value": saved})
    request = Mock(return_value=tokens(refresh_token="rotated"))
    monkeypatch.setattr(auth, "_auth_request", request)
    with pytest.raises(Rerun):
        auth._sync_browser_storage(cfg)
    assert ui.session_state[auth.SESSION_KEY]["deadline"] == 2000
    assert ui.session_state[auth._STORAGE_COMMAND]["value"]["refresh_token"] == "rotated"
    assert auth._storage_pending()
    browser(monkeypatch, lambda cmd: {"id": cmd["id"], "value": None})
    auth._sync_browser_storage(cfg)
    assert not auth._storage_pending()
    assert auth.current_access_token(cfg) == "access"
    request.assert_called_once_with(
        cfg, "token?grant_type=refresh_token", {"refresh_token": "saved-refresh"}
    )


@pytest.mark.parametrize(
    "saved",
    [
        {"refresh_token": "old", "deadline": 999},
        {"refresh_token": "old", "deadline": float("nan")},
        {"refresh_token": "old", "deadline": 1000 + 15 * 86400},
        {"deadline": 2000},
        [],
        "invalid",
    ],
)
def test_expired_or_malformed_browser_data_is_cleared(ui, cfg, monkeypatch, saved):
    browser(monkeypatch, lambda cmd: {"id": cmd["id"], "value": saved})
    request = Mock()
    monkeypatch.setattr(auth, "_auth_request", request)
    with pytest.raises(Rerun):
        auth._sync_browser_storage(cfg)
    assert ui.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"
    request.assert_not_called()


def test_failed_restore_clears_persisted_login(ui, cfg, monkeypatch):
    browser(
        monkeypatch,
        lambda cmd: {"id": cmd["id"], "value": {"refresh_token": "revoked", "deadline": 2000}},
    )
    monkeypatch.setattr(auth, "_auth_request", Mock(side_effect=RuntimeError("revoked")))
    with pytest.raises(Rerun):
        auth._sync_browser_storage(cfg)
    assert auth.SESSION_KEY not in ui.session_state
    assert ui.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"


def test_browser_storage_failure_does_not_block_manual_login(ui, cfg, monkeypatch):
    browser(monkeypatch, lambda cmd: {"id": cmd["id"], "error": True})
    auth._sync_browser_storage(cfg)
    assert not auth._storage_pending()
    assert "supabase_storage_error" in ui.session_state


def test_sign_out_and_legacy_cleanup_preserve_other_query_parameters(ui):
    auth._save_session(tokens(), True)
    ui.query_params.update(auth="leaked", refresh="leaked", expires="9999", page="invoices")
    auth.sign_out()
    assert auth.SESSION_KEY not in ui.session_state
    assert ui.query_params == {"page": "invoices"}
    assert ui.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"


def test_streamlit_login_and_logout_wait_for_browser_acknowledgement():
    """Exercise the real v2 widget through form submission and reruns."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string("""
from types import SimpleNamespace
import streamlit as st
from kaya_kopi.security import supabase_auth as auth
cfg = SimpleNamespace(supabase_url="https://fake.test", supabase_anon_key="fake")
auth._auth_request = lambda *args: {
    "access_token": "test-access", "refresh_token": "test-refresh", "expires_in": 3600
}
if auth.render_sign_in(cfg):
    st.success("Signed in")
    if st.button("Sign out"):
        auth.sign_out()
        st.rerun()
""")
    # Isolate the fake Auth response so other tests retain the real implementation.
    original_request = auth._auth_request
    try:
        app.run()
        assert not app.exception
        assert app.caption[0].value == "Updating sign-in session…"

        def acknowledge():
            command = app.session_state[auth._STORAGE_COMMAND]
            app.session_state["kaya_auth_browser_storage"] = {
                "result": {"id": command["id"], "value": None, "error": False}
            }
            app.run()
            assert not app.exception

        acknowledge()
        app.text_input[0].set_value("test@example.com")
        app.text_input[1].set_value("fake")
        app.checkbox[0].check()
        app.button[0].click().run()
        assert not app.exception
        assert app.session_state[auth._STORAGE_COMMAND]["operation"] == "write"
        assert not app.success
        acknowledge()
        assert app.success[0].value == "Signed in"
        app.button[0].click().run()
        assert app.session_state[auth._STORAGE_COMMAND]["operation"] == "clear"
        assert not app.text_input
        acknowledge()
        assert app.title[0].value == "Sign in"
    finally:
        auth._auth_request = original_request
