"""Tool layer (HARNESS_CHECKLIST box 1 grounding + box 4 honest errors).

Every tool:
  * returns data ONLY from the client's sources (never model memory),
  * returns a JSON string (machine-readable, citable),
  * on failure returns an explicit error JSON — it NEVER raises into the
    LLM and NEVER invents a number.

Scaffold state: the *interfaces* and error-shapes are final. The bodies
are stubs that report "data not loaded yet" — they are swapped for real
queries in Phase B once the data zip arrives (16 Nov briefing).
"""
from __future__ import annotations

import json
import os
from typing import Any

from langchain.tools import tool

DATA_DIR = os.environ.get("KAYA_DATA_DIR", "data")


def _ok(data: Any, source: str) -> str:
    return json.dumps({"status": "ok", "source": source, "data": data}, default=str)


def _err(msg: str, source: str) -> str:
    # Box 4: honest error — the LLM sees a structured "cannot answer" and
    # the guardrail layer turns it into a user-facing honest refusal.
    return json.dumps({"status": "error", "source": source, "error": msg})


def _data_ready() -> bool:
    return os.path.isdir(DATA_DIR) and any(
        os.scandir(DATA_DIR) if os.path.isdir(DATA_DIR) else []
    )


# --------------------------------------------------------------------------
# 1. MySQL — sales / margin / wastage / staffing (the main structured source)
# --------------------------------------------------------------------------
@tool
def query_sales_db(question: str) -> str:
    """Query the Kaya Kopi MySQL sales database (sales, margins, wastage,
    staffing across 12 outlets). Pass a plain-English question; the tool
    answers from the DB only and returns a cited row set, or an explicit
    error if the data is not loaded."""
    if not _data_ready():
        return _err("sales DB not loaded yet (data zip arrives at the 16 Nov briefing)", "mysql")
    # Phase B: connect via PyMySQL (creds from .env), run parameterised SQL,
    # return _ok(rows, "mysql:<table>") or _err(...).
    return _err("not implemented in scaffold", "mysql")


# --------------------------------------------------------------------------
# 2. Outlet CSVs (8 files) — per-outlet daily detail
# --------------------------------------------------------------------------
@tool
def read_outlet_csv(outlet: str | None = None) -> str:
    """Read one (or a summary of all) of the 8 outlet CSV files. Returns the
    raw rows so the model must compute, not recall."""
    if not _data_ready():
        return _err("outlet CSVs not loaded yet", "csv")
    # Phase B: csv.DictReader, filter by outlet, _ok(rows, f"csv:{outlet}")
    return _err("not implemented in scaffold", "csv")


# --------------------------------------------------------------------------
# 3. Scanned invoices (70 images) — OCR path (CP2 requirement)
# --------------------------------------------------------------------------
@tool
def ocr_invoice(invoice_id: str) -> str:
    """OCR a scanned supplier invoice by ID and return the extracted text
    (supplier, date, line items, prices)."""
    if not _data_ready():
        return _err("invoices not loaded yet", "ocr")
    # Phase B: tesseract/pytesseract on data/invoices/<invoice_id>.png,
    # _ok(text, f"ocr:{invoice_id}")
    return _err("not implemented in scaffold", "ocr")


# --------------------------------------------------------------------------
# 4. WhatsApp / email docs — FAISS retrieval (NB 05 pattern)
# --------------------------------------------------------------------------
@tool
def search_docs(query: str, k: int = 5) -> str:
    """Semantic search over WhatsApp/email documents (FAISS). Returns the
    top-k passages WITH their document id so answers can cite them."""
    if not _data_ready():
        return _err("documents not loaded yet", "docs")
    # Phase B: FAISS index + embeddings, _ok([{id, text}], "docs")
    return _err("not implemented in scaffold", "docs")


ALL_TOOLS = [query_sales_db, read_outlet_csv, ocr_invoice, search_docs]
