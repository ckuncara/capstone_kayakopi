"""Typed final-answer shape and a small, non-blocking Streamlit formatter."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_STATUS_LABELS = {
    "COMPLETE": "Complete",
    "INSUFFICIENT_EVIDENCE": "Insufficient evidence",
    "MATCHED": "Matched",
    "NO_APPROVED_OVERSTAFFING_THRESHOLD": "No approved overstaffing threshold",
    "NO_COMPARABLE_SERIES": "No comparable series",
    "NO_DAILY_SUMMARY": "No daily summary",
    "NO_INCREASE_AMONG_COMPARABLE_SERIES": "No increase among comparable series",
    "NO_OBSERVATIONS": "No observations",
    "NO_ROSTER_COVERAGE": "No roster coverage",
    "NO_SCAN": "No scan",
    "OPEN": "Open",
    "PARTIAL": "Partial",
    "PARTIAL_OBSERVATIONS": "Partial observations",
    "PARTIAL_POS_EXPORT": "Partial POS export",
    "REVIEW_REQUIRED": "Review required",
    "UNDETERMINED": "Undetermined",
}
_STATUS_PATTERN = re.compile(
    r"(?<![A-Z0-9_])(" + "|".join(sorted(_STATUS_LABELS, key=len, reverse=True)) + r")(?![A-Z0-9_])",
)
_BOLD_STATUS_PATTERN = re.compile(
    r"\*\*\s*((?:(?i:status|coverage|classification)\s*[:：]\s*)?)"
    r"(" + "|".join(sorted(_STATUS_LABELS, key=len, reverse=True)) + r")\s*\*\*",
)


def humanize_status_labels(text: str) -> str:
    """Render known internal status enums as consistent, unbolded labels."""
    def label(match: re.Match[str]) -> str:
        return _STATUS_LABELS[match.group(1)]

    text = _BOLD_STATUS_PATTERN.sub(
        lambda match: match.group(1) + _STATUS_LABELS[match.group(2)], text
    )
    return _STATUS_PATTERN.sub(label, text)


class CopilotAnswer(BaseModel):
    """A concise answer whose optional sections appear only when useful."""

    model_config = ConfigDict(extra="ignore")

    finding: str = Field(min_length=1)
    key_evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommendation: str | None = None

    @field_validator("finding", mode="before")
    @classmethod
    def clean_finding(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("key_evidence", "limitations", mode="before")
    @classmethod
    def clean_bullets(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                continue
            cleaned = item.strip()
            key = re.sub(r"\s+", " ", cleaned).casefold()
            if key not in seen:
                result.append(cleaned)
                seen.add(key)
        return result

    @field_validator("recommendation", mode="before")
    @classmethod
    def clean_recommendation(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value


def parse_copilot_answer(content: str) -> CopilotAnswer | None:
    """Parse the model's response contract; return None for a safe prose fallback."""
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            return None
        return CopilotAnswer.model_validate(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def format_copilot_answer(content: str) -> str:
    """Render validated answer fields once, or preserve unstructured legacy prose."""
    answer = parse_copilot_answer(content)
    if answer is None:
        return humanize_status_labels(content)

    sections = [humanize_status_labels(answer.finding)]
    for title, items in (
        ("Key evidence", answer.key_evidence),
        ("Limitations", answer.limitations),
    ):
        if items:
            sections.extend((
                f"**{title}**",
                "\n".join(f"- {humanize_status_labels(item)}" for item in items),
            ))
    if answer.recommendation:
        sections.extend(("**Recommendation**", humanize_status_labels(answer.recommendation)))
    return "\n\n".join(sections)
