"""Privacy and request checks for the operations copilot."""

from __future__ import annotations

import re
from collections.abc import Iterable

from kaya_kopi.security.pdpa_scrubber import scrub_pdpa

FINANCE_REFUSAL = (
    "I can't provide an individual's pay, performance or disciplinary details, or legal "
    "or tax advice. Please contact the Finance and Admin Manager."
)
ACTION_REFUSAL = (
    "I can't export batches of messages or personal data, or perform external actions. "
    "I can answer read-only questions about the available data."
)
DECISION_REFUSAL = (
    "I can show the figures and evidence, but can't recommend or make a business decision. "
    "Please contact the Finance and Admin Manager."
)
WITHHELD = "This information was withheld because it may contain confidential details."

PAY = re.compile(
    r"\b(?:pay(?:roll|s|ing)?|paid|salary|salaries|wages?|earn(?:s|ed|ings)?|"
    r"hourly\s+rates?|rates?\s+per\s+hour)\b",
    re.I,
)
PERFORMANCE = re.compile(
    r"\b(?:performance|performing|disciplin\w*|misconduct|warning|terminated|fired|"
    r"appraisal|written\s+up|complaint\s+against)\b",
    re.I,
)
DISCIPLINE = re.compile(r"\b(?:disciplin\w*|misconduct|fired|terminated|written\s+up)\b", re.I)
PERSON_PERFORMANCE = re.compile(
    r"\b(?:performance\s+of|how\s+(?:is|was)|is|was|did)\s+"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*(?:perform\w*|doing|fired|terminated)?"
)
PERSON = re.compile(
    r"\b(?:employee|staff|worker|person|individual|him|her|his|their|"
    r"someone|anyone)\b|['’]s\b",
    re.I,
)
INDIVIDUAL = re.compile(
    r"\b(?:one|individual|specific|single|each)\s+(?:person|employee|staff|worker)|"
    r"\b(?:his|her|their|him|someone|anyone)\b|['’]s\b|"
    r"\b(?:for|of)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"
)
AGGREGATE = re.compile(
    r"\b(?:total|aggregate|combined|overall)\b.*\b(?:outlet|branch|stall)s?\b|"
    r"\b(?:by|per|each)\s+(?:outlet|branch|stall)\b",
    re.I,
)
OUTLET_CODE = re.compile(r"\b[A-Z]{2,5}-\d{2}[A-Z]?\b(?:['’]s)?", re.I)
GROUP_PAY = re.compile(r"\b(?:payroll|staff\s+(?:pay|wages?|salaries))\b", re.I)
HOURLY = re.compile(r"\b(?:hourly\s+rates?|rates?\s+per\s+hour|per\s+hour)\b|/hr\b", re.I)
PERSONAL_PAY = re.compile(
    r"(?i:\b(?:earns?|earned|is\s+paid|was\s+paid)\b|"
    r"\bper\s+(?:employee|staff|worker|person)\b)|"
    r"\b(?:pay|paid)\s+[A-Z][a-z]+\b"
)
ADVICE = re.compile(
    r"\b(?:should|recommend\w*|advis\w*|best\s+(?:choice|option)|"
    r"what\s+would\s+you\s+do|decide|decision)\b",
    re.I,
)
TAX_LAW = re.compile(r"\b(?:legal|law|lawful|tax|gst|taxation)\b", re.I)
LEGAL_QUESTION = re.compile(r"\b(?:is\s+it\s+legal|legally|tax\s+advice|legal\s+advice)\b", re.I)
PRICING = re.compile(r"\b(?:price|cost|expense|supplier|margin|spend)\w*\b", re.I)
BULK = re.compile(
    r"\b(?:export|download|dump|bulk)\b.*\b(?:message|chat|document|email|"
    r"staff|personal|record|data)\w*\b|"
    r"\b(?:all|every|entire|complete|full)\b.*\b(?:messages|chats?|documents|"
    r"emails|conversations?|personal\s+data|staff\s+records?)\b",
    re.I,
)
ACTION = re.compile(
    r"\b(?:place|submit|send|create|update|change|modify|delete|cancel)\b"
    r".{0,45}\b(?:orders?|messages?|emails?|records?|database|purchases?)\b",
    re.I,
)
STREET_ADDRESS = re.compile(
    r"\b(?:blk|block|\d+)\s+\d*[A-Za-z]?\s*"
    r"[A-Za-z0-9 .'-]{0,60}?\b(?:road|rd|street|st|avenue|ave|lane|drive|"
    r"crescent|close|walk|way|terrace|boulevard)\b"
    r"(?:[ ,]+(?:#\d{1,3}-\d{1,5}|unit\s+\S+|Singapore\s+\d{6}))*",
    re.I,
)
HOME_ADDRESS = re.compile(
    r"\b(?:home|residential|residence|lives?\s+at)\s*(?:address)?\s*[:=-]?\s*"
    r"[^,\n;]{3,100}",
    re.I,
)
POSTAL_CODE = re.compile(
    r"\b(?:Singapore|postal\s+code|postcode)\s*[:=-]?\s*"
    r"(?<!\d)\d{6}(?!\w)",
    re.I,
)


def _has_private_pay(text: str, require_details: bool = False) -> bool:
    for passage in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not PAY.search(passage):
            continue
        if require_details and not (
            PERSON.search(passage)
            or HOURLY.search(passage)
            or PERSONAL_PAY.search(passage)
            or re.search(r"\$\s*\d|\b(?:SGD\s*\d|dollars)\b", passage, re.I)
        ):
            continue
        without_outlet_code = OUTLET_CODE.sub("outlet", passage)
        if (
            HOURLY.search(passage)
            or INDIVIDUAL.search(without_outlet_code)
            or PERSONAL_PAY.search(passage)
        ):
            return True
        outlet_group_pay = GROUP_PAY.search(passage) and (
            OUTLET_CODE.search(passage)
            or re.search(r"\b(?:outlet|branch|stall)s?\b", passage, re.I)
        )
        if not (AGGREGATE.search(passage) or outlet_group_pay):
            return True
    return False


def refuse_request(question: str) -> str | None:
    """Return a refusal for recognised prohibited requests."""
    if BULK.search(question) or ACTION.search(question):
        return ACTION_REFUSAL
    if _has_private_pay(question):
        return FINANCE_REFUSAL
    if DISCIPLINE.search(question) or (
        PERFORMANCE.search(question) and (PERSON.search(question) or PERSON_PERFORMANCE.search(question))
    ):
        return FINANCE_REFUSAL
    if LEGAL_QUESTION.search(question):
        return FINANCE_REFUSAL
    if ADVICE.search(question):
        if TAX_LAW.search(question):
            return FINANCE_REFUSAL
    return None


def protect_text(text: str, supplier_addresses: Iterable[str] = ()) -> str:
    """Mask contact details and addresses unless an address is a verified supplier address."""
    text = scrub_pdpa(text)
    saved: dict[str, str] = {}
    for index, address in enumerate(supplier_addresses):
        address = scrub_pdpa(address)
        if address and address in text:
            marker = f"__SUPPLIER_ADDRESS_{index}__"
            saved[marker] = address
            text = text.replace(address, marker)
    text = STREET_ADDRESS.sub("[REDACTED_ADDRESS]", text)
    text = POSTAL_CODE.sub("[REDACTED_POSTAL_CODE]", text)
    text = HOME_ADDRESS.sub("[REDACTED_ADDRESS]", text)
    for marker, address in saved.items():
        text = text.replace(marker, address)
    return text


def protect_answer(text: str, supplier_addresses: Iterable[str] = ()) -> str:
    """Withhold recognisable confidential HR details or advice in a model answer."""
    if _has_private_pay(text, require_details=True) or (
        PERFORMANCE.search(text) and PERSON.search(text)
    ):
        return FINANCE_REFUSAL
    if ADVICE.search(text) and (TAX_LAW.search(text) or PRICING.search(text)):
        parts = re.split(r"(?<=[.!?])\s+|\n+", text)
        safe_parts = [
            part for part in parts
            if not (ADVICE.search(part) and (TAX_LAW.search(part) or PRICING.search(part)))
        ]
        text = "\n".join(safe_parts + [DECISION_REFUSAL])
    return protect_text(text, supplier_addresses)
