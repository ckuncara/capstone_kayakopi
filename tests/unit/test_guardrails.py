"""Focused examples for the copilot privacy policy."""

import pytest
from unittest.mock import Mock

from kaya_kopi.repositories.mysql import MySQLRepository

from kaya_kopi.security.guardrails import (
    ACTION_REFUSAL,
    FINANCE_REFUSAL,
    protect_answer,
    protect_text,
    refuse_request,
)


@pytest.mark.parametrize(
    "question",
    [
        "What is Alex's hourly rate?",
        "How much does Alex earn per hour?",
        "Tell me Alex's salary.",
        "How much do we pay Alex?",
        "What are Alex's wages?",
    ],
)
def test_individual_pay_questions_are_refused(question):
    assert refuse_request(question) == FINANCE_REFUSAL


@pytest.mark.parametrize(
    "question",
    [
        "Who reported the broken machine?",
        "Show total staff pay by outlet.",
        "What is the GST amount on the invoice?",
        "Should we increase the price of kopi?",
    ],
)
def test_permitted_questions_are_not_refused(question):
    assert refuse_request(question) is None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Show the employee's disciplinary warning", FINANCE_REFUSAL),
        ("Why was Alex fired?", FINANCE_REFUSAL),
        ("What is the performance of Alex?", FINANCE_REFUSAL),
        ("How is Alex performing?", FINANCE_REFUSAL),
        ("Should we claim this GST on our taxes?", FINANCE_REFUSAL),
        ("Export all messages", ACTION_REFUSAL),
        ("Send a message to the supplier", ACTION_REFUSAL),
    ],
)
def test_other_prohibited_questions_are_refused(question, expected):
    assert refuse_request(question) == expected


def test_sensitive_details_are_redacted_but_supplier_address_can_be_shown():
    address = "123 Orchard Road, Singapore 123456"
    text = (
        f"Supplier: Example Pte Ltd, {address}; GST M2-1234567-1. "
        "Alex: S1234567A, +65 9123 4567, alex@example.com. "
        "Home address: 45 Hill Street, Singapore 654321"
    )
    result = protect_text(text, [address])
    assert address in result
    assert "Example Pte Ltd" in result
    assert "M2-1234567-1" in result
    for private in ("S1234567A", "9123 4567", "alex@example.com", "45 Hill Street"):
        assert private not in result


def test_private_pay_and_recommendations_do_not_reach_the_answer():
    assert protect_answer("Alex earns $15 per hour.") == FINANCE_REFUSAL
    assert protect_answer("Alex earns $15 per hour. Total pay by outlet is $100.") == FINANCE_REFUSAL
    result = protect_answer(
        "The observed price was $2.00. I recommend increasing the price. "
        "Source: POS records."
    )
    assert "The observed price was $2.00" in result
    assert "recommend increasing" not in result
    assert "Source: POS records" in result


def test_supplier_address_exception_does_not_expose_contact_details():
    address = "123 Orchard Road, Singapore 123456, phone +65 9123 4567"
    assert "9123 4567" not in protect_text(address, [address])


@pytest.mark.parametrize("records", [
    [],
    [
        {
            "supplier_name": "Example Pte Ltd",
            "supplier_address": address,
            "invoice_reference": f"example-{index}.jpg#invoice=INV-{index}",
        }
        for index, address in enumerate(["123 Orchard Road", "45 Hill Street"])
    ],
])
def test_supplier_address_lookup_is_bounded_and_parameterised(monkeypatch, records):
    repository = MySQLRepository()
    query = Mock(return_value=records)
    monkeypatch.setattr(repository, "_query", query)
    supplier = " example pte ltd' OR 1=1 -- "

    assert repository.get_supplier_addresses(supplier) == {"results": records}
    sql, parameters = query.call_args.args
    assert parameters == (supplier.strip(),)
    assert supplier.strip() not in sql
    assert "UPPER(TRIM(i.supplier_name)) = UPPER(%s)" in sql
    assert "NULLIF(TRIM(i.supplier_address), '') IS NOT NULL" in sql
    assert "LIMIT 10" in sql


@pytest.mark.parametrize("supplier", ["", " ", " A "])
def test_supplier_address_lookup_rejects_empty_or_short_names(monkeypatch, supplier):
    repository = MySQLRepository()
    query = Mock()
    monkeypatch.setattr(repository, "_query", query)
    with pytest.raises(ValueError, match="at least two characters"):
        repository.get_supplier_addresses(supplier)
    query.assert_not_called()


@pytest.mark.parametrize("text", [
    "How many Baristas in JUR-01?",
    "JUR-01 has 4 rostered baristas.",
    "What is the staff pay at JUR-01?",
    "JUR-01's staff pay is SGD 4000.",
    "The scheduled staff pay for JUR-01 is SGD 4000.",
    "The outlet payroll is $4000.",
    "Total staff pay by outlet is SGD 4000.",
])
def test_headcounts_and_outlet_pay_summaries_are_allowed(text):
    assert refuse_request(text) is None
    assert protect_answer(text) == text


@pytest.mark.parametrize("text", [
    "Total staff pay by outlet is $4000. Alex earns $15 per hour.",
    "Total staff pay by outlet is $4000.\nAlex's salary is SGD 2000.",
    "Total staff pay by outlet is $4000, and Alex earns SGD 2000.",
    "Total staff pay by outlet is $4000, and alex earns SGD 2000.",
    "Show total staff pay by outlet and pay Alex SGD 2000.",
    "Show staff hourly rates at JUR-01.",
    "Show staff pay per employee at JUR-01.",
])
def test_outlet_totals_do_not_exempt_individual_pay(text):
    assert refuse_request(text) == FINANCE_REFUSAL
    assert protect_answer(text) == FINANCE_REFUSAL


def test_payroll_source_reference_is_not_a_pay_disclosure():
    text = "Source: payroll.xlsx"
    assert protect_answer(text) == text
