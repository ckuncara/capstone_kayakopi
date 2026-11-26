"""Tests for the manual LLM acceptance runners and latest model options."""

import os

import pytest

from kaya_kopi import config
from kaya_kopi.copilot.llm_ui_components import format_model_pricing
from kaya_kopi.copilot.llm_usage_tracker import MODEL_PRICING, pricing_for_model
from kaya_kopi.security.openai_proxy import (
    is_gpt6_model,
    model_temperature_kwargs,
    reasoning_effort_kwargs,
    tool_request_kwargs,
)
from scripts import run_llm_acceptance as acceptance
from scripts import run_upload_acceptance as upload_acceptance


LATEST_MODELS = (
    "gpt-6.2-astra",
    "gpt-6.2-sol",
    "gpt-6.1-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
)

# Standard, short-context USD per 1M tokens from
# https://developers.openai.com/api/docs/pricing (checked 2026-11-26).
EXPECTED_STANDARD_PRICING = {
    "gpt-6.2-astra": {"input": "8.00", "cached_input": "0.40", "output": "40.00"},
    "gpt-6.2-sol": {"input": "2.00", "cached_input": "0.10", "output": "10.00"},
    "gpt-6.1-terra": {"input": "1.00", "cached_input": "0.05", "output": "5.00"},
    "gpt-6.1-sol": {"input": "2.00", "cached_input": "0.10", "output": "10.00"},
    "gpt-6-astra": {"input": "10.00", "cached_input": "1.00", "output": "50.00"},
    "gpt-6-sol": {"input": "2.00", "cached_input": "0.20", "output": "10.00"},
    "gpt-6-luna": {"input": "0.10", "cached_input": "0.01", "output": "0.50"},
}


def test_acceptance_has_more_than_twenty_unique_scenarios():
    assert len(acceptance.CASES) > 20
    assert all(isinstance(question, str) and question.strip() for question, _ in acceptance.CASES)
    assert all(isinstance(check, str) and check.strip() for _, check in acceptance.CASES)
    assert len({question for question, _ in acceptance.CASES}) == len(acceptance.CASES)


def test_latest_models_are_configured_as_suggestions():
    assert set(LATEST_MODELS) == set(config.LATEST_CHAT_MODELS)
    assert set(LATEST_MODELS) <= set(config.suggested_chat_models())


def test_latest_models_have_individual_official_rate_cards():
    assert set(LATEST_MODELS) <= set(MODEL_PRICING)
    for model in LATEST_MODELS:
        assert pricing_for_model(model) == EXPECTED_STANDARD_PRICING[model]
    assert len({id(MODEL_PRICING[model]) for model in LATEST_MODELS}) == len(LATEST_MODELS)


def test_spend_meter_shows_the_configured_model_rate_card():
    text = format_model_pricing("gpt-6.1-terra")

    assert "gpt-6.1-terra" in text
    assert "input $1.00" in text
    assert "cached input $0.05" in text
    assert "output $5.00" in text
    assert "No rate card" in format_model_pricing("custom-model")


@pytest.mark.parametrize("model", LATEST_MODELS)
def test_latest_models_use_gpt6_request_options(model):
    tools = [{"type": "function", "function": {"name": "lookup"}}]

    assert is_gpt6_model(model)
    assert model_temperature_kwargs(model, 0) == {}
    assert reasoning_effort_kwargs(model) == {"reasoning_effort": "none"}
    assert tool_request_kwargs(model, "required", tools) == {"tools": tools}
    assert tool_request_kwargs(model, "none", tools) == {}


def test_parse_args_uses_configured_model_by_default(monkeypatch):
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-6.2-astra")

    args = acceptance.parse_args([])

    assert args.models == ["gpt-6.2-astra"]


@pytest.mark.parametrize("model", LATEST_MODELS)
def test_parse_args_accepts_each_latest_model(model):
    args = acceptance.parse_args(["--model", model])

    assert args.models == [model]


def test_parse_args_runs_all_latest_models():
    args = acceptance.parse_args(["--all-latest-models"])

    assert args.models == list(LATEST_MODELS)


def test_parse_args_selects_unique_case_numbers():
    args = acceptance.parse_args(["--case", "2", "2", "29"])

    assert [number for number, _ in acceptance.selected_cases(args)] == [2, 29]


def test_parse_args_rejects_unsafe_model():
    with pytest.raises(SystemExit):
        acceptance.parse_args(["--model", "gpt-6\nBAD=1"])


def test_list_models_prints_suggestions_without_calling_the_llm(capsys):
    args = acceptance.parse_args(["--list-models"])

    acceptance.run(args)

    output = capsys.readouterr().out
    assert "Suggested chat models:" in output
    for model in LATEST_MODELS:
        assert f"  {model}" in output


def test_upload_acceptance_has_more_than_twenty_unique_scenarios():
    assert len(upload_acceptance.CASES) > 20
    assert all(isinstance(question, str) and question.strip() for question, _ in upload_acceptance.CASES)
    assert all(isinstance(check, str) and check.strip() for _, check in upload_acceptance.CASES)
    assert len({question for question, _ in upload_acceptance.CASES}) == len(upload_acceptance.CASES)


def test_upload_parse_args_accepts_latest_models_and_case_selection():
    args = upload_acceptance.parse_args(["--model", "gpt-6.1-terra", "--case", "1", "31"])

    assert args.models == ["gpt-6.1-terra"]
    assert [number for number, _ in upload_acceptance.selected_cases(args)] == [1, 31]


def test_upload_runner_uses_selected_model_and_prints_evidence(monkeypatch, capsys):
    seen: dict[str, object] = {}

    class FakeAgent:
        def ask(self, history, *, thread_id):
            seen["history"] = history
            seen["thread_id"] = thread_id
            return {
                "content": "Verified answer",
                "sources": ["data/images/supplier_invoices/INV-042509-752.jpg"],
                "tools": [{"name": "query_uploads", "ok": True, "truncated": False}],
            }

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "gpt-6-luna")
    monkeypatch.setenv("KAYA_OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.setattr(upload_acceptance, "build_agent", lambda: FakeAgent())

    args = upload_acceptance.parse_args(["--model", "gpt-6.2-astra", "--case", "1"])
    upload_acceptance.run(args)

    assert os.environ["OPENAI_CHAT_MODEL"] == "gpt-6.2-astra"
    assert os.environ["KAYA_OPENAI_MODEL"] == "gpt-6.2-astra"
    assert seen["history"] == [{"role": "user", "content": upload_acceptance.CASES[0][0]}]
    output = capsys.readouterr().out
    assert "MODEL: gpt-6.2-astra" in output
    assert "SOURCE FILES" in output
    assert "TOOL TRACE" in output
