import re

SG_NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
SG_PHONE_RE = re.compile(r"(?<![\w-])(?:\+65[\s.-]?)?[3689]\d{3}[\s.-]?\d{4}(?![\w-])")
INTERNATIONAL_PHONE_RE = re.compile(r"(?<!\w)\+\d{1,3}(?:[\s().-]*\d){7,14}(?!\w)")
LABELED_PHONE_RE = re.compile(
    r"\b(?:phone|mobile|telephone|tel|contact)\s*[:=]\s*\+?\d[\d\s().-]{5,}\d",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def scrub_pdpa(text: str) -> str:
    """Redacts Singapore NRIC/FIN, phone numbers, and personal emails per SG PDPA."""
    if not text:
        return text
    cleaned = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    cleaned = SG_NRIC_RE.sub("[REDACTED_NRIC]", cleaned)
    cleaned = LABELED_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = INTERNATIONAL_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = SG_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    return cleaned