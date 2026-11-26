"""Validated LangChain interfaces for Kaya Kopi business and retrieval queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

import pymysql
from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from kaya_kopi.paths import project_root
from kaya_kopi.repositories.mysql import MySQLRepository
from kaya_kopi.copilot.text_to_sql import query_business_data_sql
from kaya_kopi.security.budget_guard import BudgetGuard
from kaya_kopi.security.pdpa_scrubber import scrub_pdpa

if TYPE_CHECKING:
    from kaya_kopi.config import AppConfig
    from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository


class OutletSalesInput(BaseModel):
    outlet_code: str = Field(description="Canonical outlet code, for example AMK-03 or JUR-01")
    start_date: date | None = Field(
        default=None,
        description="Inclusive start date; omit both dates to use a verified alias source period",
    )
    end_date: date | None = Field(
        default=None,
        description="Inclusive end date; omit both dates to use a verified alias source period",
    )
    include_daily: bool = Field(default=True, description="Include daily results as well as totals")


class CompareOutletsInput(BaseModel):
    outlet_codes: list[str] = Field(
        min_length=2, max_length=12, description="Two or more canonical outlet codes to compare"
    )
    start_date: date = Field(description="Inclusive start date in YYYY-MM-DD format")
    end_date: date = Field(description="Inclusive end date in YYYY-MM-DD format")


class InvoiceInput(BaseModel):
    invoice_number: str = Field(description="Invoice number such as INV-012508-182")


class SupplierAddressInput(BaseModel):
    supplier_name: str = Field(
        min_length=2,
        description="Full supplier name exactly as recorded on invoices; case is ignored",
    )


class SupplierTrendInput(BaseModel):
    supplier_name: str = Field(description="Full or partial supplier name")
    start_date: date = Field(description="Inclusive invoice start date in YYYY-MM-DD format")
    end_date: date = Field(description="Inclusive invoice end date in YYYY-MM-DD format")


class DocumentSearchInput(BaseModel):
    query: str = Field(description="Natural-language or keyword query")
    limit: int = Field(default=5, ge=1, le=10)
    outlet_code: str | None = Field(default=None, description="Optional canonical outlet filter")
    supplier_name: str | None = Field(default=None, description="Optional supplier-name filter")


class QualityIssuesInput(BaseModel):
    status: str = Field(default="OPEN", description="Resolution status, usually OPEN")
    severity: str | None = Field(default=None, description="Optional ERROR, WARNING, or INFO filter")
    rule_code: str | None = Field(default=None, description="Optional exact quality rule code")
    limit: int = Field(default=50, ge=1, le=100)


class AnalyzeOutletsInput(BaseModel):
    analysis: Literal["performance", "wastage", "staffing", "headcount"]
    outlet_codes: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="Canonical, legacy, or re-registered outlet codes; verified aliases are resolved automatically. Leave empty for all outlets.",
    )
    outlet_type: Literal["mall", "kopitiam"] | None = Field(default=None, description="Outlet master type")
    start_date: date | None = Field(default=None, description="Omit both dates for the measured wastage period or latest roster month")
    end_date: date | None = None


class AnalyzePosInput(BaseModel):
    analysis: Literal["product_mix", "observed_prices", "payment_modes", "time_of_day", "margin"] = Field(
        description=(
            "POS product mix, observed prices, payments, time patterns, or Kopi margin evidence. "
            "The margin mode checks POS coverage and required cost inputs; it does not invent recipe costs."
        )
    )
    outlet_codes: list[str] = Field(default_factory=list, max_length=12)
    outlet_type: Literal["mall", "kopitiam"] | None = Field(
        default=None, description="Filter using the outlet master; use mall for mall outlets."
    )
    start_date: date = Field(description="Inclusive start date in YYYY-MM-DD format")
    end_date: date = Field(description="Inclusive end date in YYYY-MM-DD format")
    menu_item: str | None = None


class StaffingInput(BaseModel):
    outlet_codes: list[str] = Field(default_factory=list, max_length=12)
    outlet_type: Literal["mall", "kopitiam"] | None = None
    start_date: date | None = Field(default=None, description="Inclusive roster month/date range start")
    end_date: date | None = Field(default=None, description="Inclusive roster month/date range end")


class AnalyzeSuppliersInput(BaseModel):
    analysis: Literal[
        "invoice", "spend", "cost_trend", "price_change", "item_price_summary", "reconciliation"
    ] = Field(description=(
        "Use item_price_summary for scanned SGD invoice average unit prices grouped by supplier and "
        "exact item description, including valid-line counts and review-excluded lines."
    ))
    supplier_query: str | None = None
    invoice_number: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    group_by: Literal["all", "supplier", "month", "supplier_month"] = "supplier"
    item: str | None = Field(default=None, description="Item description filter for price changes")
    page: int = Field(default=1, ge=1, le=1000, description="Detail page for price-change or excluded-line records")
    detail_limit: int = Field(default=10, ge=1, le=50, description="Rows per price-change or excluded-line detail page")


class DataCoverageInput(BaseModel):
    area: Literal["all", "sales", "invoices", "wastage", "staffing"] = "all"


class KopiPriceChangeInput(BaseModel):
    menu_scope: Literal["base_kopi", "all_kopi"] = "base_kopi"
    start_date: date
    end_date: date
    gross_price_increase_sgd: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    demand_change_percent: list[Decimal] = Field(
        default_factory=lambda: [Decimal("0"), Decimal("-5"), Decimal("-10")],
        min_length=1,
        max_length=10,
    )
    outlet_codes: list[str] = Field(default_factory=list, max_length=12)


@lru_cache(maxsize=1)
def _repository() -> MySQLRepository:
    return MySQLRepository()


def _call(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = getattr(_repository(), method)(*args, **kwargs)
        return {"ok": True, **result}
    except ValueError as error:
        return {"ok": False, "error": str(error), "error_type": "validation"}
    except pymysql.MySQLError:
        return {
            "ok": False,
            "error": "The database query failed. Check database availability and configuration.",
            "error_type": "database",
        }


@tool("analyze_outlets_sql", args_schema=AnalyzeOutletsInput)
def analyze_outlets_sql(
    analysis: str,
    start_date: date | None = None,
    end_date: date | None = None,
    outlet_codes: list[str] | None = None,
    outlet_type: str | None = None,
) -> dict[str, Any]:
    """Return outlet performance, measured wastage, or roster headcounts.

    Wastage results cover measured records only and include coverage information; outlets
    without measurements must not be treated as having zero wastage. Use
    ``analyze_staffing_sql`` for paid-hour and labour-cost analysis.
    """
    return _call("analyze_outlets", analysis, outlet_codes or [], start_date, end_date, outlet_type)


@tool("analyze_staffing_sql", args_schema=StaffingInput)
def analyze_staffing_sql(
    outlet_codes: list[str] | None = None,
    outlet_type: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Return aggregated planned staffing hours and labour costs by outlet.

    This calculation is retained because individual pay fields are excluded from the approved SQL
    catalog. Roster patterns are not attendance and do not establish overstaffing.
    """
    return _call("analyze_outlets", "staffing", outlet_codes or [], start_date, end_date, outlet_type)


@tool("analyze_pos_sql", args_schema=AnalyzePosInput)
def analyze_pos_sql(
    analysis: str, start_date: date, end_date: date,
    outlet_codes: list[str] | None = None, menu_item: str | None = None,
    outlet_type: str | None = None,
) -> dict[str, Any]:
    """Analyze observed POS product evidence, including Kopi margin coverage and missing inputs.

    POS exports are partial and missing rows do not establish zero sales. Margin requires verified
    recipe cost per cup; scanned supplier unit prices alone are not per-cup costs.
    """
    return _call("analyze_pos", analysis, outlet_codes or [], start_date, end_date, menu_item, outlet_type)


@tool("analyze_suppliers_sql", args_schema=AnalyzeSuppliersInput)
def analyze_suppliers_sql(
    analysis: str, supplier_query: str | None = None, invoice_number: str | None = None,
    start_date: date | None = None, end_date: date | None = None,
    group_by: str = "supplier",
    item: str | None = None, page: int = 1, detail_limit: int = 10,
) -> dict[str, Any]:
    """Return scanned-invoice totals, item price summaries, price comparisons, or reconciliation.

    The analysis and group_by arguments select the report. Price changes compare validated
    lines for the same supplier and exact item description; result rows include endpoint
    invoice evidence and comparison coverage. item_price_summary averages valid SGD line prices
    by supplier and exact item description, and reports lines excluded for review.
    """
    return _call("analyze_suppliers", analysis, supplier_query, invoice_number, start_date, end_date, group_by, item, page, detail_limit)


@tool("get_supplier_addresses_sql", args_schema=SupplierAddressInput)
def get_supplier_addresses_sql(supplier_name: str) -> dict[str, Any]:
    """Look up recorded supplier business addresses by exact supplier name, ignoring case.

    Returns up to 10 invoice-backed records. Empty results mean no recorded address
    matched that exact name. Report differing addresses with their invoice references;
    do not infer which address is current.
    """
    return _call("get_supplier_addresses", supplier_name)


@tool("get_data_coverage_sql", args_schema=DataCoverageInput)
def get_data_coverage_sql(area: str = "all") -> dict[str, Any]:
    """Report objective source coverage and known completeness limitations."""
    return _call("get_data_coverage", area)


@tool("assess_kopi_price_change_sql", args_schema=KopiPriceChangeInput)
def assess_kopi_price_change_sql(
    start_date: date, end_date: date, menu_scope: str = "base_kopi",
    gross_price_increase_sgd: Decimal = Decimal("0.10"),
    demand_change_percent: list[Decimal] | None = None,
    outlet_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Run a transparent price scenario from observed partial-POS volumes without overstating certainty."""
    return _call(
        "assess_kopi_price_change", menu_scope, start_date, end_date,
        gross_price_increase_sgd,
        demand_change_percent or [Decimal("0"), Decimal("-5"), Decimal("-10")],
        outlet_codes or [],
    )


@tool("get_outlet_sales_sql", args_schema=OutletSalesInput)
def get_outlet_sales_sql(
    outlet_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
    include_daily: bool = True,
) -> dict[str, Any]:
    """Get outlet sales; omitted dates infer the verified alias's available source period."""
    return _call("get_outlet_sales", outlet_code, start_date, end_date, include_daily)


@tool("compare_outlets_sql", args_schema=CompareOutletsInput)
def compare_outlets_sql(
    outlet_codes: list[str], start_date: date, end_date: date
) -> dict[str, Any]:
    """Compare deduplicated sales, transaction counts, and activity across selected outlets."""
    return _call("compare_outlets", outlet_codes, start_date, end_date)


@tool("get_invoice_sql", args_schema=InvoiceInput)
def get_invoice_sql(invoice_number: str) -> dict[str, Any]:
    """Get one scanned supplier invoice, its item rows, review state, reconciliation, and citation."""
    return _call("get_invoice", invoice_number)


@tool("get_supplier_cost_trend_sql", args_schema=SupplierTrendInput)
def get_supplier_cost_trend_sql(
    supplier_name: str, start_date: date, end_date: date
) -> dict[str, Any]:
    """Get monthly scanned-invoice totals and item unit-price trends for a supplier."""
    return _call("get_supplier_cost_trend", supplier_name, start_date, end_date)


@tool("search_documents_sql", args_schema=DocumentSearchInput)
def search_documents_sql(
    query: str,
    limit: int = 5,
    outlet_code: str | None = None,
    supplier_name: str | None = None,
) -> dict[str, Any]:
    """Search citable email and WhatsApp chunks, optionally filtered by explicit entity mentions."""
    return _call("search_documents", query, limit, outlet_code, supplier_name)


@tool("get_data_quality_issues_sql", args_schema=QualityIssuesInput)
def get_data_quality_issues_sql(
    status: str = "OPEN",
    severity: str | None = None,
    rule_code: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List citable data-quality issues by status, severity, and rule code."""
    return _call("get_data_quality_issues", status, severity, rule_code, limit)


@dataclass(frozen=True)
class InvoiceToolContext:
    """Per-invocation settings for local invoice tools."""

    sql_repo: SQLiteOpsRepository
    cfg: AppConfig
    budget: BudgetGuard
    top_k: int = 3
    retrieved_hits: list[tuple[dict, str]] | None = None


def _invoice_context(config: RunnableConfig) -> InvoiceToolContext:
    context = config.get("configurable", {}).get("kaya_invoice_context")
    if context is not None:
        if not isinstance(context, InvoiceToolContext):
            raise TypeError("kaya_invoice_context must be an InvoiceToolContext.")
        return context

    # AppConfig evaluates environment defaults when its module is first imported.
    load_dotenv(project_root() / ".env")
    from kaya_kopi.config import AppConfig
    from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository

    cfg = AppConfig()
    return InvoiceToolContext(
        sql_repo=SQLiteOpsRepository(cfg),
        cfg=cfg,
        budget=BudgetGuard(cfg.sqlite_db_path, cfg.monthly_budget_usd),
    )


@tool
def lookup_invoice_sqlite(question: str, config: RunnableConfig) -> str:
    """Look up uploaded invoices or analyze local invoice spending, audits and price trends.

    Provide a self-contained question with invoice numbers, supplier names and dates
    where available. Results cover all branches and may include failed audits.
    """
    from kaya_kopi.invoice_chat import sqlite_invoice_answer  # Requires the optional OCR dependencies.

    context = _invoice_context(config)
    answer = sqlite_invoice_answer(scrub_pdpa(question), "HQ", context.sql_repo)
    if answer is None:
        return (
            "SQLite invoice lookup does not support this question. "
            "Try searching the invoice OCR chunks or another appropriate tool."
        )
    return f"Source: local SQLite invoice records.\n\n{scrub_pdpa(answer)}"


@tool
def search_invoice_chroma(query: str, config: RunnableConfig) -> str:
    """Search shared invoice OCR chunks across all branches."""
    from kaya_kopi.repositories.chroma_store import SharedChromaRepository

    context = _invoice_context(config)
    if not (context.cfg.llm_proxy_url or context.cfg.api_key):
        raise RuntimeError("OpenAI access is not configured to search invoice chunks.")
    vec_repo = SharedChromaRepository(context.cfg, budget_guard=context.budget)
    hits = vec_repo.semantic_search(
        query_text=scrub_pdpa(query),
        branch_code=None,
        source_type="invoice",
        n_results=context.top_k,
    )
    if context.retrieved_hits is not None:
        context.retrieved_hits.extend(hits)
    if not hits:
        return "No invoice chunks found across all branches."
    return "\n\n---\n\n".join(
        f"Invoice: {meta.get('invoice_number')} | "
        f"Audit: {meta.get('audit_status')}\n{document}"
        for meta, document in hits
    )


ALL_TOOLS = [
    analyze_outlets_sql,
    analyze_staffing_sql,
    analyze_pos_sql,
    assess_kopi_price_change_sql,
    get_outlet_sales_sql,
    get_data_coverage_sql,
    analyze_suppliers_sql,
    get_supplier_addresses_sql,
    get_invoice_sql,
    get_data_quality_issues_sql,
    search_documents_sql,
    lookup_invoice_sqlite,
    search_invoice_chroma,
    query_business_data_sql,
]
