import re

SG_NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
SG_PHONE_RE = re.compile(r"(?:\+65[\s-]?)?\d{3}[\s-]?\d{4}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def scrub_pdpa(text: str) -> str:
    """Redacts Singapore NRIC/FIN, phone numbers, and personal emails per SG PDPA."""
    if not text:
        return text
    cleaned = SG_NRIC_RE.sub("[REDACTED_NRIC]", text)
    cleaned = SG_PHONE_RE.sub("[REDACTED_PHONE]", cleaned)
    cleaned = EMAIL_RE.sub("[REDACTED_EMAIL]", cleaned)
    return cleaned