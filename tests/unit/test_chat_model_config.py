"""Tests for persisting the selected chat model."""

import os

import pytest

from kaya_kopi import config


def test_set_chat_model_updates_both_env_files_and_process(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "original")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "original")
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    env.write_text(
        "KAYA_OPENAI_MODEL=gpt-4o-mini\nOPENAI_CHAT_MODEL=gpt-4o-mini\nOTHER=keep\n",
        encoding="utf-8",
    )
    example.write_text(
        "KAYA_OPENAI_MODEL=gpt-4o-mini\nOPENAI_CHAT_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )

    updated = config.set_chat_model("gpt-4.1-mini")

    assert updated == "gpt-4.1-mini"
    for path in (env, example):
        content = path.read_text(encoding="utf-8")
        assert "KAYA_OPENAI_MODEL=gpt-4.1-mini" in content
        assert "OPENAI_CHAT_MODEL=gpt-4.1-mini" in content
    assert "OTHER=keep" in env.read_text(encoding="utf-8")
    assert os.environ["KAYA_OPENAI_MODEL"] == "gpt-4.1-mini"
    assert config.AppConfig().chat_model == "gpt-4.1-mini"


def test_set_chat_model_appends_missing_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "original")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "original")
    env = tmp_path / ".env"
    env.write_text("OTHER=keep", encoding="utf-8")

    config.set_chat_model("openai/gpt-4.1")

    content = env.read_text(encoding="utf-8")
    assert content.startswith("OTHER=keep\n")
    assert "KAYA_OPENAI_MODEL=openai/gpt-4.1" in content
    assert "OPENAI_CHAT_MODEL=openai/gpt-4.1" in content
    assert (tmp_path / ".env.example").is_file()


def test_set_chat_model_rejects_unsafe_values(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    env = tmp_path / ".env"
    env.write_text("KAYA_OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Model name"):
        config.set_chat_model("gpt-4o\nMONTHLY_BUDGET_USD=999")

    assert env.read_text(encoding="utf-8") == "KAYA_OPENAI_MODEL=gpt-4o-mini\n"


def test_set_chat_model_accepts_custom_model(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    # set_chat_model writes process env directly; seed both keys so monkeypatch
    # restores their original values for later tests.
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    env = tmp_path / ".env"
    env.write_text("KAYA_OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")

    updated = config.set_chat_model("gpt-6-luna")

    assert updated == "gpt-6-luna"
    assert "KAYA_OPENAI_MODEL=gpt-6-luna" in env.read_text(encoding="utf-8")
    assert os.environ["KAYA_OPENAI_MODEL"] == "gpt-6-luna"


def test_suggested_chat_models_includes_env_override(monkeypatch):
    monkeypatch.setenv("KAYA_CHAT_MODELS", "proxy-chat-model, gpt-4o-mini")
    models = config.suggested_chat_models()
    assert "proxy-chat-model" in models
    assert "gpt-4o-mini" in models
    assert "gpt-6-luna" in models
