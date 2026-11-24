"""Read-only, parameterized business queries for the Kaya Kopi MySQL schema."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence

import pymysql
from pymysql.cursors import DictCursor


POS_CTE = """
WITH deduplicated_pos AS (
    SELECT DISTINCT tx_id, outlet_code, tx_time, item_id, qty, unit_price, total, payment_mode
    FROM pos_transactions
),
canonical_pos AS (
    SELECT d.*,
           COALESCE(canonical.outlet_code, d.outlet_code) AS canonical_outlet_code
    FROM deduplicated_pos d
    LEFT JOIN outlet_alias a
      ON a.alias_code = d.outlet_code
     AND a.validation_status = 'VERIFIED'
     AND DATE(d.tx_time) >= a.effective_from
     AND (a.effective_to IS NULL OR DATE(d.tx_time) <= a.effective_to)
    LEFT JOIN outlets canonical ON canonical.outlet_id = a.outlet_id
)
"""

CANONICAL_INVOICE_COST_CTE = """
WITH invoice_line_totals AS (
    SELECT supplier_invoice_id,
           ROUND(SUM(amount), 2) AS line_subtotal,
           SUM(amount IS NULL OR line_total_valid = FALSE OR extraction_status <> 'OK')
               AS invalid_line_count
    FROM supplier_invoice_item
    GROUP BY supplier_invoice_id
),
canonical_invoice_cost AS (
    SELECT i.supplier_invoice_id, i.invoice_number, i.supplier_name, i.invoice_date, i.currency,
           CASE
               WHEN i.currency <> 'SGD' THEN NULL
               WHEN i.arithmetic_valid = TRUE THEN i.subtotal
               WHEN l.invalid_line_count = 0 AND i.is_gst_inclusive = TRUE
                   THEN ROUND(l.line_subtotal / (1 + i.gst_rate_percent / 100), 2)
               WHEN l.invalid_line_count = 0 THEN l.line_subtotal
               ELSE NULL
           END AS canonical_subtotal,
           CASE
               WHEN i.currency <> 'SGD' THEN NULL
               WHEN i.arithmetic_valid = TRUE THEN i.gst_amount
               WHEN l.invalid_line_count = 0 AND i.is_gst_inclusive = TRUE
                   THEN ROUND(l.line_subtotal - l.line_subtotal / (1 + i.gst_rate_percent / 100), 2)
               WHEN l.invalid_line_count = 0
                   THEN ROUND(l.line_subtotal * i.gst_rate_percent / 100, 2)
               ELSE NULL
           END AS canonical_gst,
           CASE
               WHEN i.currency <> 'SGD' THEN NULL
               WHEN i.arithmetic_valid = TRUE THEN i.total_amount
               WHEN l.invalid_line_count = 0 AND i.is_gst_inclusive = TRUE THEN l.line_subtotal
               WHEN l.invalid_line_count = 0
                   THEN ROUND(l.line_subtotal * (1 + i.gst_rate_percent / 100), 2)
               ELSE NULL
           END AS canonical_total,
           CASE
               WHEN i.currency <> 'SGD' THEN 'NON_SGD'
               WHEN i.arithmetic_valid = TRUE THEN 'INVOICE_HEADER'
               WHEN l.invalid_line_count = 0 THEN 'VALIDATED_INVOICE_LINES'
               ELSE 'REVIEW_REQUIRED'
           END AS cost_basis,
           (i.extraction_status <> 'OK' OR i.arithmetic_valid = FALSE
             OR l.invalid_line_count > 0 OR i.currency <> 'SGD') AS review_required
    FROM supplier_invoice i
    LEFT JOIN invoice_line_totals l
      ON l.supplier_invoice_id=i.supplier_invoice_id
)
"""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "kayakopi"

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            host=os.getenv("KAYA_DB_HOST", cls.host),
            port=int(os.getenv("KAYA_DB_PORT", str(cls.port))),
            user=os.getenv("KAYA_DB_USER", cls.user),
            password=os.getenv("KAYA_DB_PASSWORD", cls.password),
        )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _validate_period(start_date: date, end_date: date, max_days: int = 3660) -> None:
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if (end_date - start_date).days > max_days:
        raise ValueError(f"date range cannot exceed {max_days} days")


class MySQLRepository:
    """Adapter exposing the six read-only query operations used by the chatbot."""

    def __init__(self, config: DatabaseConfig | None = None) -> None:
        self.config = config or DatabaseConfig.from_env()

    def _connect(self) -> pymysql.Connection:
        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            read_timeout=10,
            write_timeout=10,
            connect_timeout=5,
        )

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return _json_safe(list(cursor.fetchall()))

    def _canonical_outlets(self) -> set[str]:
        return {row["outlet_code"] for row in self._query("SELECT outlet_code FROM outlets")}

    def _validate_canonical_outlet(self, outlet_code: str) -> str:
        canonical = outlet_code.strip().upper()
        if canonical not in self._canonical_outlets():
            raise ValueError(f"unknown canonical outlet code: {outlet_code}")
        return canonical

    def _resolve_outlet(
        self, outlet_code: str, start_date: date, end_date: date
    ) -> tuple[str, dict[str, Any] | None]:
        requested = outlet_code.strip().upper()
        if requested in self._canonical_outlets():
            return requested, None

        aliases = self._query(
            """
            SELECT a.alias_code AS requested_code,
                   o.outlet_code AS canonical_code,
                   a.effective_from,
                   a.effective_to,
                   a.validation_status,
                   d.source_uri AS evidence_source_uri
            FROM outlet_alias a
            JOIN outlets o ON o.outlet_id = a.outlet_id
            JOIN source_document d ON d.source_document_id = a.evidence_document_id
            WHERE a.alias_code = %s
              AND a.validation_status = 'VERIFIED'
              AND a.effective_from <= %s
              AND (a.effective_to IS NULL OR a.effective_to >= %s)
            ORDER BY a.effective_from DESC
            LIMIT 1
            """,
            (requested, end_date, start_date),
        )
        if not aliases:
            raise ValueError(f"unknown outlet code: {outlet_code}")
        resolution = aliases[0]
        return resolution["canonical_code"], resolution

    def _infer_alias_period(self, outlet_code: str) -> tuple[date, date]:
        requested = outlet_code.strip().upper()
        aliases = self._query(
            """
            SELECT a.effective_from, a.effective_to
            FROM outlet_alias a
            WHERE a.alias_code = %s
              AND a.validation_status = 'VERIFIED'
            ORDER BY a.effective_from DESC
            LIMIT 1
            """,
            (requested,),
        )
        if not aliases:
            raise ValueError(
                "start_date and end_date are required unless outlet_code is a verified alias"
            )

        alias = aliases[0]
        start_date = date.fromisoformat(alias["effective_from"])
        if alias["effective_to"]:
            return start_date, date.fromisoformat(alias["effective_to"])

        source_period = self._query(
            """
            SELECT MAX(DATE(tx_time)) AS last_date
            FROM pos_transactions
            WHERE UPPER(TRIM(outlet_code)) = %s
              AND DATE(tx_time) >= %s
            """,
            (requested, start_date),
        )[0]
        if not source_period["last_date"]:
            raise ValueError(f"no source period is available for outlet alias: {outlet_code}")
        return start_date, date.fromisoformat(source_period["last_date"])

    def get_outlet_sales(
        self,
        outlet_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
        include_daily: bool = True,
        *, include_report_dates: bool = False,
    ) -> dict[str, Any]:
        if (start_date is None) != (end_date is None):
            raise ValueError("start_date and end_date must either both be provided or both omitted")
        if start_date is None and end_date is None:
            start_date, end_date = self._infer_alias_period(outlet_code)
        _validate_period(start_date, end_date)
        outlet, alias_resolution = self._resolve_outlet(outlet_code, start_date, end_date)
        end_exclusive = end_date + timedelta(days=1)
        daily_source = self._query(
            """
            SELECT MIN(d.business_date) AS first_date, MAX(d.business_date) AS last_date,
                   COUNT(*) AS available_days
            FROM daily_outlet_summary d
            JOIN outlets o ON o.outlet_id=d.outlet_id
            WHERE o.outlet_code=%s
            """,
            (outlet,),
        )[0]
        if daily_source["available_days"]:
            daily_rows = self._query(
                """
                SELECT d.business_date, d.sales_amount_sgd AS gross_takings_sgd,
                       d.reported_volume AS receipt_count, d.wastage_kg,
                       d.validation_status, s.source_uri, d.source_locator
                FROM daily_outlet_summary d
                JOIN outlets o ON o.outlet_id=d.outlet_id
                JOIN source_document s ON s.source_document_id=d.source_document_id
                WHERE o.outlet_code=%s
                  AND d.business_date BETWEEN %s AND %s
                ORDER BY d.business_date
                """,
                (outlet, start_date, end_date),
            )
            usable_rows = [row for row in daily_rows if row["validation_status"] == "OK"]
            gross_takings = sum(Decimal(row["gross_takings_sgd"]) for row in usable_rows)
            receipt_count = sum(int(row["receipt_count"]) for row in usable_rows)
            present_dates = {date.fromisoformat(row["business_date"]) for row in daily_rows}
            requested_dates = {
                start_date + timedelta(days=offset)
                for offset in range((end_date - start_date).days + 1)
            }
            missing_dates = sorted(day.isoformat() for day in requested_dates - present_dates)
            citations = [
                {
                    "source_uri": row["source_uri"],
                    "source_locator": row["source_locator"],
                }
                for row in daily_rows
            ]
            observed_period = {
                "start": usable_rows[0]["business_date"] if usable_rows else None,
                "end": usable_rows[-1]["business_date"] if usable_rows else None,
            }
            return {
                "outlet_code": outlet,
                "alias_resolution": alias_resolution,
                "authority": "DAILY_SUMMARY_Z_REPORT",
                "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
                "summary": {
                    "outlet_code": outlet,
                    "gross_takings_sgd": f"{gross_takings:.2f}",
                    "total_sales_sgd": f"{gross_takings:.2f}",
                    "receipt_count": receipt_count,
                    "transaction_count": receipt_count,
                    "reported_days": len(daily_rows),
                    "included_days": len(usable_rows),
                },
                "daily": [
                    {
                        "business_date": row["business_date"],
                        "gross_takings_sgd": row["gross_takings_sgd"],
                        "receipt_count": row["receipt_count"],
                        "wastage_kg": row["wastage_kg"],
                        "validation_status": row["validation_status"],
                        "citation": f"{row['source_uri']}#{row['source_locator']}",
                    }
                    for row in daily_rows
                ] if include_daily else [],
                "coverage": {
                    "observed_period": observed_period,
                    **({"validated_report_dates": [row["business_date"] for row in usable_rows]}
                       if include_report_dates else {}),
                    "source_period": {
                        "start": daily_source["first_date"],
                        "end": daily_source["last_date"],
                    },
                    "missing_dates": missing_dates,
                    "missing_dates_mean": "MISSING_REPORT_NOT_ZERO_OR_CLOSED",
                },
                "method": "Uses manager-entered daily Z-report summaries as the authoritative source for gross takings and receipts; missing dates are excluded and reported as gaps.",
                "citation": {
                    "source_locator": "table=daily_outlet_summary",
                    "source_uris": sorted({row["source_uri"] for row in citations}),
                    **({"records": citations} if include_daily else {}),
                },
            }

        summary = self._query(
            POS_CTE
            + """
            SELECT canonical_outlet_code AS outlet_code,
                   ROUND(COALESCE(SUM(total), 0), 2) AS total_sales_sgd,
                   COUNT(*) AS transaction_count,
                   COALESCE(SUM(qty), 0) AS items_sold,
                   MIN(tx_time) AS first_transaction,
                   MAX(tx_time) AS last_transaction
            FROM canonical_pos
            WHERE canonical_outlet_code=%s AND tx_time >= %s AND tx_time < %s
            GROUP BY canonical_outlet_code
            """,
            (outlet, start_date, end_exclusive),
        )
        daily: list[dict[str, Any]] = []
        if include_daily:
            daily = self._query(
                POS_CTE
                + """
                SELECT DATE(tx_time) AS business_date,
                       ROUND(SUM(total), 2) AS sales_sgd,
                       COUNT(*) AS transaction_count,
                       SUM(qty) AS items_sold
                FROM canonical_pos
                WHERE canonical_outlet_code=%s AND tx_time >= %s AND tx_time < %s
                GROUP BY DATE(tx_time)
                ORDER BY business_date
                """,
                (outlet, start_date, end_exclusive),
            )
        return {
            "outlet_code": outlet,
            "alias_resolution": alias_resolution,
            "authority": "PARTIAL_POS_EXPORT",
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "summary": summary[0] if summary else {
                "outlet_code": outlet,
                "total_sales_sgd": "0.00",
                "transaction_count": 0,
                "items_sold": 0,
                "first_transaction": None,
                "last_transaction": None,
            },
            "daily": daily,
            "coverage": {
                "observed_period": {
                    "start": summary[0]["first_transaction"][:10] if summary else None,
                    "end": summary[0]["last_transaction"][:10] if summary else None,
                },
                "warning": "No daily summary exists for this outlet. POS is a partial export and must not be treated as complete revenue or volume.",
                "missing_dates": [],
            },
            "method": "Uses the partial POS export only because no daily summary exists; exact duplicates are removed and verified aliases resolve by effective date.",
            "citation": {"source_uri": "data/sql/kayakopi_pos.sql", "source_locator": "table=pos_transactions"},
        }

    def analyze_outlets(
        self, analysis: str, outlet_codes: list[str], start_date: date | None,
        end_date: date | None, outlet_type: str | None = None,
    ) -> dict[str, Any]:
        if (start_date is None) != (end_date is None):
            raise ValueError("provide both dates or omit both")
        default_period = start_date is None
        if default_period:
            if analysis == "wastage":
                bounds = self._query("""SELECT MIN(business_date) AS start, MAX(business_date) AS end
                    FROM daily_outlet_summary WHERE wastage_kg IS NOT NULL""")[0]
            elif analysis == "staffing":
                bounds = self._query("""SELECT MAX(roster_month) AS start,
                    LAST_DAY(MAX(roster_month)) AS end FROM staff_roster""")[0]
            else:
                raise ValueError("start_date and end_date are required for outlet performance")
            if not bounds["start"]:
                return {"analysis": analysis, "status": "NO_OBSERVATIONS", "results": []}
            start_date, end_date = date.fromisoformat(bounds["start"]), date.fromisoformat(bounds["end"])

        _validate_period(start_date, end_date)
        selected = outlet_codes or sorted(self._canonical_outlets())
        if outlet_type is not None:
            matching = {row["outlet_code"] for row in self._query(
                "SELECT outlet_code FROM outlets WHERE outlet_type = %s", (outlet_type,)
            )}
            selected = [code for code in selected if self._resolve_outlet(code, start_date, end_date)[0] in matching]
        period = {"start": start_date.isoformat(), "end": end_date.isoformat()}
        scope = {"period": period, "default_period": default_period, "outlet_type": outlet_type}
        if analysis == "wastage":
            results = []
            for requested_code in selected:
                outlet, _alias = self._resolve_outlet(requested_code, start_date, end_date)
                row = self._query(
                    """
                    SELECT COUNT(d.daily_summary_id) AS reported_days,
                           COUNT(d.wastage_kg) AS measured_days,
                           ROUND(SUM(d.wastage_kg), 3) AS total_wastage_kg,
                           ROUND(AVG(d.wastage_kg), 3) AS average_wastage_kg,
                           MIN(CASE WHEN d.wastage_kg IS NOT NULL THEN d.business_date END) AS first_measured,
                           MAX(CASE WHEN d.wastage_kg IS NOT NULL THEN d.business_date END) AS last_measured
                    FROM outlets o
                    LEFT JOIN daily_outlet_summary d
                      ON d.outlet_id=o.outlet_id
                     AND d.business_date BETWEEN %s AND %s
                    WHERE o.outlet_code=%s
                    """,
                    (start_date, end_date, outlet),
                )[0]
                if not row["reported_days"]:
                    coverage_status = "NO_DAILY_SUMMARY"
                elif not row["measured_days"]:
                    coverage_status = "NOT_RECORDED"
                else:
                    coverage_status = "MEASURED"
                sources = self._query(
                    """
                    SELECT DISTINCT s.source_uri
                    FROM daily_outlet_summary d
                    JOIN outlets o ON o.outlet_id=d.outlet_id
                    JOIN source_document s ON s.source_document_id=d.source_document_id
                    WHERE o.outlet_code=%s AND d.business_date BETWEEN %s AND %s
                      AND d.wastage_kg IS NOT NULL
                    ORDER BY s.source_uri
                    """,
                    (outlet, start_date, end_date),
                ) if row["measured_days"] else []
                results.append(
                    {
                        "outlet_code": outlet,
                        "coverage_status": coverage_status,
                        "reported_days": row["reported_days"],
                        "measured_days": row["measured_days"],
                        "total_wastage_kg": row["total_wastage_kg"],
                        "average_wastage_kg": row["average_wastage_kg"],
                        "observed_period": {"start": row["first_measured"], "end": row["last_measured"]},
                        "citation": {
                            "source_locator": "table=daily_outlet_summary;field=wastage_kg",
                            "source_uris": [source["source_uri"] for source in sources],
                        },
                    }
                )
            measured = sorted(
                (row for row in results if row["coverage_status"] == "MEASURED"),
                key=lambda row: Decimal(row["total_wastage_kg"]),
                reverse=True,
            )
            all_comparable = bool(measured) and len(measured) == len(results)
            return {
                "status": "COMPLETE" if all_comparable else "PARTIAL" if measured else "NO_OBSERVATIONS",
                "analysis": analysis,
                **scope,
                "definition": "Total weight of all food discarded at the outlet; not product-specific.",
                "results": results,
                "comparison_scope": "ALL_REQUESTED_OUTLETS" if all_comparable else "MEASURED_OUTLETS_ONLY",
                "all_outlets_comparable": all_comparable,
                "measured_outlet_count": len(measured),
                "unmeasured_outlet_count": len(results) - len(measured),
                "highest_measured_wastage": measured[0] if measured else None,
                "ranked_measured_results": measured,
                "limitation": "Only outlets with recorded measurements can be ranked. The ranking uses total measured kilograms, and measured-day counts can differ.",
            }
        if analysis == "staffing":
            weekday_names = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
            rows = self._query(
                """
                SELECT o.outlet_code, r.roster_month, a.staff_assignment_id,
                       a.hourly_rate_sgd, a.rate_status, sh.day_of_week,
                       sh.paid_hours, s.source_uri, a.source_locator
                FROM staff_assignment a
                JOIN staff_roster r ON r.staff_roster_id=a.staff_roster_id
                JOIN staff_shift sh ON sh.staff_assignment_id=a.staff_assignment_id
                JOIN outlets o ON o.outlet_id=a.outlet_id
                JOIN source_document s ON s.source_document_id=r.source_document_id
                WHERE r.roster_month <= %s
                  AND LAST_DAY(r.roster_month) >= %s
                  AND sh.validation_status='OK'
                """,
                (end_date, start_date),
            )
            roster_by_outlet: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                roster_by_outlet.setdefault(row["outlet_code"], []).append(row)
            period_days = (start_date + timedelta(days=offset)
                           for offset in range((end_date - start_date).days + 1))
            period_counts = Counter((day.isoformat()[:7], day.weekday()) for day in period_days)
            requested_months = {month for month, _weekday in period_counts}
            results = []
            for requested_code in selected:
                outlet, _alias = self._resolve_outlet(requested_code, start_date, end_date)
                outlet_rows = roster_by_outlet.get(outlet, [])
                sales = self.get_outlet_sales(
                    outlet, start_date, end_date, include_daily=False, include_report_dates=True,
                )
                matched_days = set(sales["coverage"].get("validated_report_dates", []))
                matched_counts = Counter((day[:7], date.fromisoformat(day).weekday()) for day in matched_days)
                paid_hours = Decimal("0")
                labour_cost = Decimal("0")
                matched_hours = Decimal("0")
                matched_labour_cost = Decimal("0")
                defaulted_assignments: set[int] = set()
                covered_months = {row["roster_month"][:7] for row in outlet_rows}
                for row in outlet_rows:
                    key = (row["roster_month"][:7], weekday_names.index(row["day_of_week"]))
                    shift_hours = Decimal(row["paid_hours"] or 0)
                    hourly_rate = Decimal(row["hourly_rate_sgd"] or 0)
                    hours = shift_hours * period_counts[key]
                    paid_hours += hours
                    labour_cost += hours * hourly_rate
                    hours_with_sales = shift_hours * matched_counts[key]
                    matched_hours += hours_with_sales
                    matched_labour_cost += hours_with_sales * hourly_rate
                    if row["rate_status"] == "CLIENT_APPROVED_DEFAULT":
                        defaulted_assignments.add(row["staff_assignment_id"])
                summary = sales["summary"]
                authoritative = sales["authority"] == "DAILY_SUMMARY_Z_REPORT"
                observations = summary["included_days"] if authoritative else summary["transaction_count"]
                gross = Decimal(summary["total_sales_sgd"]) if observations else None
                comparable = bool(
                    authoritative and gross is not None and matched_hours > 0
                    and requested_months <= covered_months
                )
                results.append(
                    {
                        "outlet_code": outlet,
                        "roster_available": bool(outlet_rows),
                        "paid_hours": f"{paid_hours:.2f}" if outlet_rows else None,
                        "labour_cost_sgd": f"{labour_cost:.2f}" if outlet_rows else None,
                        "roster_months": sorted(covered_months),
                        "defaulted_rate_assignments": len(defaulted_assignments),
                        "gross_takings_sgd": f"{gross:.2f}" if gross is not None and authoritative else None,
                        "observed_pos_takings_sgd": f"{gross:.2f}" if gross is not None and not authoritative else None,
                        "revenue_authority": sales["authority"],
                        "productivity_comparable": comparable,
                        "matched_paid_hours": f"{matched_hours:.2f}" if comparable else None,
                        "matched_labour_cost_sgd": f"{matched_labour_cost:.2f}" if comparable else None,
                        "gross_takings_per_paid_hour_sgd": (
                            f"{gross / matched_hours:.2f}" if comparable else None
                        ),
                        "labour_cost_percent_of_takings": (
                            f"{matched_labour_cost / gross * 100:.2f}" if gross and comparable else None
                        ),
                        "coverage": {
                            "sales_observations": observations,
                            "observed_period": sales["coverage"].get("observed_period"),
                            "missing_report_days": len(sales["coverage"].get("missing_dates", [])) if authoritative else None,
                            "warning": sales["coverage"].get("warning"),
                        },
                        "citation": [sales["citation"], {
                            "source_locator": "tables=staff_roster,staff_assignment,staff_shift",
                            "source_uris": sorted({row["source_uri"] for row in outlet_rows}),
                        }],
                    }
                )
            comparable_outlets = sorted(
                (row for row in results if row["productivity_comparable"]),
                key=lambda row: Decimal(row["gross_takings_per_paid_hour_sgd"]),
            )
            non_comparable_outlets = [
                row for row in results if not row["productivity_comparable"]
            ]
            return {
                "analysis": analysis,
                **scope,
                "results": results,
                "classification": "NO_APPROVED_OVERSTAFFING_THRESHOLD",
                "comparable_outlets": comparable_outlets,
                "non_comparable_outlets": non_comparable_outlets,
                "lowest_comparable_productivity": (
                    comparable_outlets[0] if comparable_outlets else None
                ),
                "conclusion": "Show scheduled labour cost and available takings separately. An overstaffing verdict needs an approved benchmark and reliable sales for the roster period. Partial POS takings cannot support a productivity ranking.",
                "method": "Scheduled totals cover the requested roster period. Productivity ratios compare takings and rostered labour on the same validated Z-report dates, which may differ by outlet. TBC rates use the client-approved S$13/hour default.",
                "privacy": "Individual staff names are not returned.",
            }
        if analysis != "performance":
            raise ValueError(f"unsupported outlet analysis: {analysis}")
        results = []
        for requested_code in selected:
            result = self.get_outlet_sales(
                requested_code, start_date, end_date, include_daily=False
            )
            summary = result["summary"]
            observed = result["coverage"]["observed_period"]
            results.append(
                {
                    "requested_outlet_code": requested_code.strip().upper(),
                    "outlet_code": result["outlet_code"],
                    "alias_resolution": result["alias_resolution"],
                    "authority": result["authority"],
                    "gross_takings_sgd": summary["total_sales_sgd"] if observed["start"] else None,
                    "receipt_count": summary.get("receipt_count") if observed["start"] else None,
                    "coverage": {
                        "observed_period": observed,
                        "missing_report_days": len(result["coverage"]["missing_dates"])
                        if result["authority"] == "DAILY_SUMMARY_Z_REPORT" else None,
                        "warning": result["coverage"].get("warning"),
                    },
                    "citation": result["citation"],
                }
            )
        return {
            "analysis": analysis,
            **scope,
            "results": results,
        }

    def compare_outlets(
        self, outlet_codes: list[str], start_date: date, end_date: date
    ) -> dict[str, Any]:
        _validate_period(start_date, end_date)
        if len(outlet_codes) < 2:
            raise ValueError("provide at least two outlet codes")
        if len(outlet_codes) > 12:
            raise ValueError("at most 12 outlet codes may be compared")
        outlets = list(
            dict.fromkeys(
                self._resolve_outlet(code, start_date, end_date)[0] for code in outlet_codes
            )
        )
        results = [
            self.get_outlet_sales(outlet, start_date, end_date, include_daily=False)
            for outlet in outlets
        ]
        authorities = {result["authority"] for result in results}
        rows = []
        for result in results:
            summary = result["summary"]
            gross_takings = summary.get("gross_takings_sgd", summary["total_sales_sgd"])
            receipts = summary.get("receipt_count")
            rows.append(
                {
                    "outlet_code": result["outlet_code"],
                    "gross_takings_sgd": gross_takings,
                    "total_sales_sgd": gross_takings,
                    "receipt_count": receipts,
                    "average_receipt_sgd": (
                        f"{Decimal(gross_takings) / receipts:.2f}" if receipts else None
                    ),
                    "reported_days": summary.get("reported_days"),
                    "authority": result["authority"],
                    "coverage": result["coverage"],
                }
            )
        rows.sort(key=lambda row: Decimal(row["gross_takings_sgd"]), reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["sales_rank"] = rank
        authority = authorities.pop() if len(authorities) == 1 else "MIXED_SOURCES"
        return {
            "authority": authority,
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "requested_outlets": outlets,
            "comparison": rows,
            "comparable": authority == "DAILY_SUMMARY_Z_REPORT",
            "method": "Uses authoritative daily Z-report summaries where available. Outlets with only the partial POS export are labelled and are not directly comparable for complete revenue.",
            "citation": [result["citation"] for result in results],
        }

    def analyze_pos(
        self, analysis: str, outlet_codes: list[str], start_date: date, end_date: date,
        menu_item: str | None = None, outlet_type: str | None = None,
    ) -> dict[str, Any]:
        _validate_period(start_date, end_date)
        selected = [self._resolve_outlet(code, start_date, end_date)[0] for code in outlet_codes]
        if outlet_type is not None:
            matching_outlets = {
                row["outlet_code"] for row in self._query(
                    "SELECT outlet_code FROM outlets WHERE outlet_type = %s", (outlet_type,)
                )
            }
            if outlet_codes:
                matching_outlets.intersection_update(selected)
            selected = sorted(matching_outlets)

        outlet_filter = bool(outlet_codes) or outlet_type is not None
        outlet_condition = "FALSE"
        if selected:
            placeholders = ",".join(["%s"] * len(selected))
            outlet_condition = f"p.canonical_outlet_code IN ({placeholders})"

        period = {"start": start_date.isoformat(), "end": end_date.isoformat()}
        if analysis == "margin":
            if menu_item is not None and menu_item.strip().casefold() != "kopi":
                raise ValueError("margin coverage is currently available only for the base Kopi item")

            coverage = self._query(
                POS_CTE + f"""
                SELECT DATE(MIN(p.tx_time)) AS first_observed_date,
                       DATE(MAX(p.tx_time)) AS last_observed_date,
                       COUNT(CASE WHEN p.tx_time >= %s AND p.tx_time < %s THEN 1 END)
                           AS observed_rows_in_period
                FROM canonical_pos p JOIN menu_items m ON m.item_id = p.item_id
                WHERE m.item_name = 'Kopi' AND {outlet_condition if outlet_filter else 'TRUE'}
                """,
                (start_date, end_date + timedelta(days=1), *selected),
            )[0]
            return {
                "analysis": analysis,
                "status": "INSUFFICIENT_EVIDENCE",
                "margin_percent": None,
                "menu_item": "Kopi",
                "period": period,
                "outlet_type": outlet_type,
                "outlet_codes": selected if outlet_filter else sorted(self._canonical_outlets()),
                "pos_coverage": {
                    **coverage,
                    "status": (
                        "PARTIAL_OBSERVATIONS" if coverage["observed_rows_in_period"]
                        else "NO_OBSERVATIONS"
                    ),
                },
                "missing_for_margin": [
                    "complete Kopi item sales for these outlets and dates",
                    "approved recipe-level cost per cup for the requested period",
                ],
                "citation": {
                    "source_uri": "data/sql/kayakopi_pos.sql",
                    "source_locator": "tables=outlets,pos_transactions,menu_items",
                },
            }

        where = ["p.tx_time >= %s", "p.tx_time < %s"]
        params: list[Any] = [start_date, end_date + timedelta(days=1)]
        if outlet_filter:
            where.append(outlet_condition)
            params.extend(selected)
        if menu_item:
            where.append("m.item_name LIKE %s")
            params.append(f"%{menu_item.strip()}%")
        dimensions = {
            "product_mix": ("p.canonical_outlet_code, m.item_name", "SUM(p.qty) AS quantity, ROUND(SUM(p.total),2) AS observed_sales_sgd"),
            "observed_prices": ("p.canonical_outlet_code, m.item_name", "MIN(p.unit_price) AS minimum_price_sgd, MAX(p.unit_price) AS maximum_price_sgd, ROUND(AVG(p.unit_price),2) AS average_price_sgd, COUNT(*) AS observations"),
            "payment_modes": ("p.payment_mode", "COUNT(*) AS transaction_rows, ROUND(SUM(p.total),2) AS observed_sales_sgd"),
            "time_of_day": ("HOUR(p.tx_time)", "COUNT(*) AS transaction_rows, SUM(p.qty) AS quantity, ROUND(SUM(p.total),2) AS observed_sales_sgd"),
        }
        if analysis not in dimensions:
            raise ValueError(f"unsupported POS analysis: {analysis}")
        group, measures = dimensions[analysis]
        label = " AS hour_of_day" if analysis == "time_of_day" else ""
        rows = self._query(
            POS_CTE + f"""
            SELECT {group}{label}, {measures}
            FROM canonical_pos p JOIN menu_items m ON m.item_id=p.item_id
            WHERE {' AND '.join(where)}
            GROUP BY {group}
            ORDER BY {group}
            """, params,
        )
        return {
            "analysis": analysis,
            "authority": "PARTIAL_POS_EXPORT",
            "period": period,
            "results": rows,
            "coverage_warning": "The POS export is incomplete. Results describe observed records only and must not be presented as complete revenue or volume.",
            "citation": {"source_uri": "data/sql/kayakopi_pos.sql", "source_locator": "tables=pos_transactions,menu_items"},
        }

    def analyze_suppliers(
        self, analysis: str, supplier_query: str | None, invoice_number: str | None,
        start_date: date | None, end_date: date | None, group_by: str,
        item: str | None = None, page: int = 1, detail_limit: int = 10,
    ) -> dict[str, Any]:
        if analysis == "invoice":
            if not invoice_number:
                raise ValueError("invoice_number is required for invoice analysis")
            return {"analysis": analysis, **self.get_invoice(invoice_number)}
        if analysis == "cost_trend":
            if not supplier_query or not start_date or not end_date:
                raise ValueError("supplier_query, start_date, and end_date are required for cost_trend")
            return {"analysis": analysis, **self.get_supplier_cost_trend(supplier_query, start_date, end_date)}
        if analysis == "reconciliation":
            where, params = ["1=1"], []
            if invoice_number:
                where.append("UPPER(COALESCE(i.invoice_number, sp.invoice_no))=%s")
                params.append(invoice_number.strip().upper())
            if supplier_query:
                where.append("(sp.supplier_code=%s OR sp.supplier_name LIKE %s)")
                params.extend([supplier_query.upper(), f"%{supplier_query}%"])
            rows = self._query(
                f"""SELECT r.supplier_purchase_id,
                           COALESCE(i.invoice_number, sp.invoice_no) AS invoice_number,
                           sp.supplier_name,
                           r.reconciliation_status, r.match_method, r.legacy_subtotal_sgd,
                           r.scanned_subtotal_sgd, r.subtotal_difference_sgd, r.validation_status
                    FROM purchase_invoice_reconciliation r
                    JOIN supplier_purchases sp ON sp.purchase_id=r.supplier_purchase_id
                    LEFT JOIN supplier_invoice i ON i.supplier_invoice_id=r.supplier_invoice_id
                    WHERE {' AND '.join(where)} ORDER BY sp.purchase_date, r.supplier_purchase_id""",
                params,
            )
            for row in rows:
                identity_match = row["reconciliation_status"] == "MATCHED"
                difference = row["subtotal_difference_sgd"]
                needs_review = row["validation_status"] == "REVIEW_REQUIRED"
                amount_match = None if needs_review else (
                    identity_match and difference is not None and Decimal(difference) == 0
                )
                row.update(
                    {
                        "identity_match": identity_match,
                        "amount_match": amount_match,
                        "gst_responsible": False,
                        "gst_explanation": (
                            "Reconciliation requires review after the invoice was updated."
                            if needs_review else
                            "GST cannot explain this difference because both compared values are pre-GST subtotals."
                            if identity_match and not amount_match
                            else "GST is not part of this subtotal comparison."
                        ),
                        "cause": (
                            "REVIEW_REQUIRED" if needs_review else "NO_SCAN"
                            if not identity_match
                            else "AMOUNTS_AGREE" if amount_match else "UNDETERMINED"
                        ),
                    }
                )
            invoice_evidence = self.get_invoice(invoice_number) if invoice_number else None
            return {
                "analysis": analysis,
                "results": rows,
                "invoice_evidence": invoice_evidence,
                "interpretation": "MATCHED identifies the linked purchase and scan; it does not mean their amounts agree. A subtotal difference does not establish its cause.",
                "citation": {"source_locator": "table=purchase_invoice_reconciliation"},
            }
        if analysis == "spend":
            if start_date and end_date:
                _validate_period(start_date, end_date)
            groups = {
                "all": ("'all' AS scope", "'all'"),
                "supplier": (
                    "s.supplier_code, c.supplier_name",
                    "s.supplier_code, c.supplier_name",
                ),
                "month": (
                    "DATE_FORMAT(c.invoice_date, '%%Y-%%m') AS month",
                    "DATE_FORMAT(c.invoice_date, '%%Y-%%m')",
                ),
                "supplier_month": (
                    "s.supplier_code, c.supplier_name, "
                    "DATE_FORMAT(c.invoice_date, '%%Y-%%m') AS month",
                    "s.supplier_code, c.supplier_name, "
                    "DATE_FORMAT(c.invoice_date, '%%Y-%%m')",
                ),
            }
            if group_by not in groups:
                raise ValueError(f"unsupported supplier grouping: {group_by}")
            select_expr, group_expr = groups[group_by]
            filters, params = [], []
            if supplier_query:
                filters.append("(s.supplier_code=%s OR c.supplier_name LIKE %s)")
                params.extend([supplier_query.upper(), f"%{supplier_query}%"])
            if start_date:
                filters.append("c.invoice_date >= %s")
                params.append(start_date)
            if end_date:
                filters.append("c.invoice_date <= %s")
                params.append(end_date)
            where = ["c.canonical_total IS NOT NULL", *filters]
            rows = self._query(
                CANONICAL_INVOICE_COST_CTE + f"""
                SELECT {select_expr}, COUNT(*) AS invoice_count,
                       MIN(c.invoice_date) AS first_invoice_date, MAX(c.invoice_date) AS last_invoice_date,
                       ROUND(SUM(c.canonical_subtotal),2) AS subtotal_sgd,
                       ROUND(SUM(c.canonical_gst),2) AS gst_sgd,
                       ROUND(SUM(c.canonical_total),2) AS total_sgd,
                       SUM(c.review_required) AS review_required_count
                FROM canonical_invoice_cost c LEFT JOIN supplier s ON UPPER(s.canonical_name)=UPPER(c.supplier_name)
                WHERE {' AND '.join(where)} GROUP BY {group_expr} ORDER BY {group_expr}""", params,
            )
            review_where = ["(c.review_required OR c.canonical_total IS NULL)", *filters]
            flagged = self._query(
                CANONICAL_INVOICE_COST_CTE + f"""
                SELECT c.invoice_number, c.supplier_name, c.invoice_date,
                       c.cost_basis AS reason, d.source_uri, c.canonical_total IS NULL AS excluded,
                       i.review_notes
                FROM canonical_invoice_cost c
                LEFT JOIN supplier s ON UPPER(s.canonical_name)=UPPER(c.supplier_name)
                JOIN supplier_invoice i ON i.supplier_invoice_id=c.supplier_invoice_id
                JOIN source_document d ON d.source_document_id=i.source_document_id
                WHERE {' AND '.join(review_where)}
                ORDER BY c.invoice_date, c.invoice_number""",
                params,
            )
            excluded = [
                {key: row[key] for key in ("invoice_number", "supplier_name", "invoice_date", "reason", "source_uri")}
                for row in flagged if row["excluded"]
            ]
            observed = {
                "start": min((row["first_invoice_date"] for row in rows if row["first_invoice_date"]), default=None),
                "end": max((row["last_invoice_date"] for row in rows if row["last_invoice_date"]), default=None),
            }
            native_filters = [part.replace("c.", "i.") for part in filters]
            foreign = self._query(
                f"""
                SELECT i.currency, {select_expr.replace("c.", "i.")},
                       COUNT(*) AS invoice_count, ROUND(SUM(i.subtotal),2) AS subtotal,
                       ROUND(SUM(i.gst_amount),2) AS gst, ROUND(SUM(i.total_amount),2) AS total
                FROM supplier_invoice i
                LEFT JOIN supplier s ON UPPER(s.canonical_name)=UPPER(i.supplier_name)
                WHERE {' AND '.join(["i.currency <> 'SGD'", "i.arithmetic_valid=TRUE", *native_filters])}
                GROUP BY i.currency, {group_expr.replace("c.", "i.")}
                ORDER BY i.currency, {group_expr.replace("c.", "i.")}
                """, params,
            )
            return {
                "analysis": analysis,
                "group_by": group_by,
                "period": {
                    "start": start_date.isoformat() if start_date else None,
                    "end": end_date.isoformat() if end_date else None,
                },
                "observed_period": observed,
                "supplier_query": supplier_query,
                "results": rows,
                "authority": "SCANNED_INVOICE_CANONICAL_COST",
                "included_invoice_count": sum(int(row["invoice_count"]) for row in rows),
                "excluded_invoice_count": len(excluded),
                "excluded_invoices": excluded,
                "reviewed_invoice_evidence": flagged,
                "excluded_review_required_invoices": sum(
                    row["reason"] != "NON_SGD" for row in excluded
                ),
                "excluded_non_sgd_invoices": sum(row["reason"] == "NON_SGD" for row in excluded),
                "foreign_currency_results": foreign,
                "foreign_currency_note": (
                    "Separate native-currency totals include only arithmetically valid headers; "
                    "no exchange rates are applied."
                ),
                "citation": {"source_locator": "tables=supplier_invoice,supplier_invoice_item"},
            }
        if analysis == "price_change":
            return self._supplier_price_change(supplier_query, start_date, end_date, item, page, detail_limit)
        raise ValueError(f"unsupported supplier analysis: {analysis}")

    def _supplier_price_change(
        self, supplier_query: str | None, start_date: date | None, end_date: date | None,
        item: str | None, page: int, detail_limit: int,
    ) -> dict[str, Any]:
        if start_date and end_date:
            _validate_period(start_date, end_date)
        if not 1 <= detail_limit <= 50 or not 1 <= page <= 1000:
            raise ValueError("page must be 1–1000 and detail_limit must be 1–50")
        filters = [
            "li.line_total_valid=TRUE", "li.extraction_status='OK'", "li.unit_price IS NOT NULL",
            "i.currency='SGD'", "i.is_gst_inclusive=FALSE", "i.invoice_date IS NOT NULL",
        ]
        params: list[Any] = []
        if supplier_query:
            filters.append("(s.supplier_code=%s OR i.supplier_name LIKE %s)")
            params.extend([supplier_query.upper(), f"%{supplier_query}%"])
        if item:
            filters.append("li.description LIKE %s")
            params.append(f"%{item.strip()}%")

        # Rank the full eligible population before paging. Contradictory endpoint
        # prices on the same invoice date make the series ineligible for comparison.
        rows = self._query(
            f"""WITH source_lines AS (
                    SELECT s.supplier_code, i.supplier_name, li.description,
                           CAST(i.supplier_name AS BINARY) AS supplier_name_key,
                           CAST(li.description AS BINARY) AS description_key,
                           i.invoice_number, i.invoice_date, li.unit_price,
                           d.source_uri, li.line_number, i.supplier_invoice_id
                    FROM supplier_invoice_item li
                    JOIN supplier_invoice i USING(supplier_invoice_id)
                    LEFT JOIN supplier s ON UPPER(s.canonical_name)=UPPER(i.supplier_name)
                    JOIN source_document d ON d.source_document_id=i.source_document_id
                    WHERE {' AND '.join(filters)}
                ), requested AS (
                    SELECT * FROM source_lines
                    WHERE (%s IS NULL OR invoice_date >= %s)
                      AND (%s IS NULL OR invoice_date <= %s)
                ), ranked AS (
                    SELECT requested.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY supplier_code, supplier_name_key, description_key
                               ORDER BY invoice_date, supplier_invoice_id, line_number
                           ) AS first_rank,
                           ROW_NUMBER() OVER (
                               PARTITION BY supplier_code, supplier_name_key, description_key
                               ORDER BY invoice_date DESC, supplier_invoice_id DESC, line_number DESC
                           ) AS last_rank,
                           MIN(unit_price) OVER date_prices AS min_price_on_date,
                           MAX(unit_price) OVER date_prices AS max_price_on_date
                    FROM requested
                    WINDOW date_prices AS (
                        PARTITION BY supplier_code, supplier_name_key, description_key, invoice_date
                    )
                ), series AS (
                    SELECT supplier_code, supplier_name_key, description_key,
                           MAX(supplier_name) AS supplier_name, MAX(description) AS description,
                           COUNT(*) AS observations,
                           MIN(invoice_date) AS first_date, MAX(invoice_date) AS latest_date,
                           MAX(CASE WHEN first_rank=1 THEN unit_price END) AS first_price,
                           MAX(CASE WHEN last_rank=1 THEN unit_price END) AS latest_price,
                           MAX(CASE WHEN first_rank=1 THEN invoice_number END) AS first_invoice,
                           MAX(CASE WHEN last_rank=1 THEN invoice_number END) AS latest_invoice,
                           MAX(CASE WHEN first_rank=1 THEN source_uri END) AS first_uri,
                           MAX(CASE WHEN last_rank=1 THEN source_uri END) AS latest_uri,
                           MAX(CASE WHEN first_rank=1 THEN line_number END) AS first_line,
                           MAX(CASE WHEN last_rank=1 THEN line_number END) AS latest_line,
                           MAX(CASE WHEN first_rank=1 OR last_rank=1
                               THEN min_price_on_date <> max_price_on_date END) AS ambiguous_endpoint
                    FROM ranked GROUP BY supplier_code, supplier_name_key, description_key
                ), comparable AS (
                    SELECT *, (latest_price - first_price) / first_price * 100 AS percent_change
                    FROM series
                    WHERE first_date < latest_date AND first_price > 0 AND ambiguous_endpoint=0
                ), detail_page AS (
                    SELECT * FROM comparable
                    ORDER BY percent_change DESC, supplier_code, supplier_name_key, description_key
                    LIMIT %s OFFSET %s
                ), coverage AS (
                    SELECT (SELECT MIN(invoice_date) FROM source_lines) AS source_start,
                           (SELECT MAX(invoice_date) FROM source_lines) AS source_end,
                           (SELECT MIN(invoice_date) FROM requested) AS observed_start,
                           (SELECT MAX(invoice_date) FROM requested) AS observed_end,
                           (SELECT COUNT(*) FROM series) AS series_count,
                           (SELECT COUNT(*) FROM comparable) AS comparable_count,
                           (SELECT MAX(percent_change) FROM comparable) AS max_percent_change
                )
                SELECT coverage.*, detail_page.* FROM coverage LEFT JOIN detail_page ON TRUE
                ORDER BY percent_change DESC, supplier_code, supplier_name_key, description_key""",
            [*params, start_date, start_date, end_date, end_date, detail_limit, (page - 1) * detail_limit],
        )
        totals = rows[0]
        results = []
        for row in rows:
            if row["observations"] is None:
                continue
            evidence = {
                f"{label}_evidence": {
                    "invoice_number": row[f"{label}_invoice"],
                    "citation": f"{row[f'{label}_uri']}#invoice={row[f'{label}_invoice']};line={row[f'{label}_line']}",
                }
                for label in ("first", "latest")
            }
            results.append({
                **{key: row[key] for key in (
                    "supplier_code", "supplier_name", "description", "first_date",
                    "latest_date", "observations",
                )},
                "first_unit_price_sgd": f"{Decimal(row['first_price']):.4f}",
                "latest_unit_price_sgd": f"{Decimal(row['latest_price']):.4f}",
                "absolute_change_sgd": f"{Decimal(row['latest_price']) - Decimal(row['first_price']):.4f}",
                "percent_change": f"{Decimal(row['percent_change']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}",
                **evidence,
            })

        comparable_count = totals["comparable_count"]
        insufficient_count = totals["series_count"] - comparable_count
        has_more = page * detail_limit < comparable_count
        increased = comparable_count > 0 and Decimal(totals["max_percent_change"]) > 0
        largest = results[0] if page == 1 and increased else None
        status = "COMPLETE"
        if not totals["series_count"]:
            status = "NO_OBSERVATIONS"
        elif not comparable_count:
            status = "NO_COMPARABLE_SERIES"
        elif not increased:
            status = "NO_INCREASE_AMONG_COMPARABLE_SERIES"
        elif insufficient_count or (
            (start_date and start_date.isoformat() < totals["source_start"])
            or (end_date and end_date.isoformat() > totals["source_end"])
        ):
            status = "PARTIAL"
        return {
            "analysis": "price_change",
            "status": status,
            "period": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None,
            },
            "observed_period": {"start": totals["observed_start"], "end": totals["observed_end"]},
            "source_period": {"start": totals["source_start"], "end": totals["source_end"]},
            "supplier_query": supplier_query,
            "item": item,
            "results": results,
            "comparable_series_count": comparable_count,
            "insufficient_series_count": insufficient_count,
            "largest_increase": largest,
            "detail_page": {"number": page, "next_page": page + 1 if has_more else None},
            "comparison_basis": "Ranks changes in validated, GST-exclusive SGD unit prices for the same supplier and exact item description. Supporting rows are paged after ranking.",
            "authority": "VALIDATED_SCANNED_INVOICE_LINES",
            "citation": {"source_locator": "tables=supplier_invoice,supplier_invoice_item"},
        }

    def get_invoice(self, invoice_number: str) -> dict[str, Any]:
        number = invoice_number.strip().upper()
        headers = self._query(
            """
            SELECT i.supplier_invoice_id, i.invoice_number, sp.supplier_code,
                   i.supplier_name, i.supplier_address,
                   i.gst_registration_number, i.invoice_date, i.bill_to, i.currency,
                   i.is_gst_inclusive, i.subtotal,
                   i.gst_rate_percent, i.gst_amount, i.total_amount, i.payment_terms,
                   i.received_condition, i.mean_ocr_confidence, i.arithmetic_valid,
                   i.extraction_status, i.review_notes, i.supplier_purchase_id,
                   p.total_amount AS legacy_purchase_amount, p.gst_treatment,
                   r.reconciliation_status, r.match_method, r.subtotal_difference_sgd,
                   s.source_uri, s.ocr_json_uri
            FROM supplier_invoice i
            LEFT JOIN supplier sp ON UPPER(sp.canonical_name)=UPPER(i.supplier_name)
            JOIN source_document s ON s.source_document_id=i.source_document_id
            LEFT JOIN supplier_purchases p ON p.purchase_id=i.supplier_purchase_id
            LEFT JOIN purchase_invoice_reconciliation r
              ON r.supplier_invoice_id=i.supplier_invoice_id
            WHERE UPPER(i.invoice_number)=%s
            ORDER BY i.supplier_invoice_id
            """,
            (number,),
        )
        if not headers:
            return {"invoice_number": number, "found": False, "invoice": None, "items": []}
        invoice = headers[0]
        invoice_id = invoice.pop("supplier_invoice_id")
        items = self._query(
            """
            SELECT line_number, description, quantity, unit_price, amount,
                   mean_field_confidence, line_total_valid, extraction_status, review_notes
            FROM supplier_invoice_item
            WHERE supplier_invoice_id=%s
            ORDER BY line_number
            """,
            (invoice_id,),
        )
        lines_are_valid = bool(items) and all(
            item["amount"] is not None
            and item["line_total_valid"]
            and item["extraction_status"] == "OK"
            for item in items
        )
        if invoice["arithmetic_valid"]:
            canonical_subtotal = Decimal(invoice["subtotal"])
            canonical_gst = Decimal(invoice["gst_amount"])
            canonical_total = Decimal(invoice["total_amount"])
            cost_basis = "INVOICE_HEADER"
        elif lines_are_valid:
            line_total = sum(Decimal(item["amount"]) for item in items)
            gst_rate = Decimal(invoice["gst_rate_percent"]) / Decimal("100")
            if invoice["is_gst_inclusive"]:
                canonical_total = line_total
                canonical_subtotal = (line_total / (1 + gst_rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                canonical_gst = canonical_total - canonical_subtotal
            else:
                canonical_subtotal = line_total
                canonical_gst = (canonical_subtotal * gst_rate).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                canonical_total = canonical_subtotal + canonical_gst
            cost_basis = "VALIDATED_INVOICE_LINES"
        else:
            canonical_subtotal = canonical_gst = canonical_total = None
            cost_basis = "REVIEW_REQUIRED"
        return {
            "invoice_number": number,
            "found": True,
            "invoice": invoice,
            "items": items,
            "canonical_cost": {
                "currency": invoice["currency"],
                "subtotal": f"{canonical_subtotal:.2f}" if canonical_subtotal is not None else None,
                "gst": f"{canonical_gst:.2f}" if canonical_gst is not None else None,
                "total": f"{canonical_total:.2f}" if canonical_total is not None else None,
                "subtotal_sgd": f"{canonical_subtotal:.2f}" if canonical_subtotal is not None and invoice["currency"] == "SGD" else None,
                "gst_sgd": f"{canonical_gst:.2f}" if canonical_gst is not None and invoice["currency"] == "SGD" else None,
                "total_sgd": f"{canonical_total:.2f}" if canonical_total is not None and invoice["currency"] == "SGD" else None,
                "cost_basis": cost_basis,
                "review_required": (
                    invoice["extraction_status"] != "OK"
                    or not invoice["arithmetic_valid"]
                    or not lines_are_valid
                ),
                "header_arithmetic_valid": bool(invoice["arithmetic_valid"]),
            },
            "citation": {
                "source_uri": invoice["source_uri"],
                "source_locator": f"invoice={number}",
                "ocr_json_uri": invoice["ocr_json_uri"],
            },
        }

    def get_supplier_cost_trend(
        self, supplier_name: str, start_date: date, end_date: date
    ) -> dict[str, Any]:
        _validate_period(start_date, end_date)
        supplier = supplier_name.strip()
        if len(supplier) < 2:
            raise ValueError("supplier_name must contain at least two characters")
        matches = self._query(
            """
            SELECT DISTINCT s.supplier_code, i.supplier_name
            FROM supplier_invoice i
            LEFT JOIN supplier s ON UPPER(s.canonical_name)=UPPER(i.supplier_name)
            WHERE i.supplier_name LIKE %s OR s.supplier_code=%s
            ORDER BY i.supplier_name
            """,
            (f"%{supplier}%", supplier.upper()),
        )
        if not matches:
            return {
                "supplier_query": supplier,
                "matched_supplier_codes": [],
                "matched_suppliers": [],
                "monthly": [],
                "item_trends": [],
            }
        matched_names = [row["supplier_name"] for row in matches]
        matched_codes = [row["supplier_code"] for row in matches]
        placeholders = ",".join(["%s"] * len(matched_names))
        monthly = self._query(
            CANONICAL_INVOICE_COST_CTE
            + f"""
            SELECT DATE_FORMAT(invoice_date, '%%Y-%%m') AS month,
                   COUNT(*) AS invoice_count,
                   ROUND(SUM(canonical_subtotal), 2) AS subtotal_sgd,
                   ROUND(SUM(canonical_gst), 2) AS gst_sgd,
                   ROUND(SUM(canonical_total), 2) AS total_sgd,
                   SUM(review_required) AS review_required_count,
                   SUM(cost_basis='VALIDATED_INVOICE_LINES') AS line_derived_count
            FROM canonical_invoice_cost
            WHERE supplier_name IN ({placeholders})
              AND invoice_date BETWEEN %s AND %s
              AND currency = 'SGD'
            GROUP BY DATE_FORMAT(invoice_date, '%%Y-%%m')
            ORDER BY month
            """,
            (*matched_names, start_date, end_date),
        )
        item_trends = self._query(
            f"""
            SELECT DATE_FORMAT(i.invoice_date, '%%Y-%%m') AS month,
                   li.description,
                   ROUND(AVG(li.unit_price), 4) AS average_unit_price_sgd,
                   ROUND(SUM(li.quantity), 3) AS total_quantity,
                   ROUND(SUM(li.amount), 2) AS total_amount_sgd,
                   COUNT(*) AS observations
            FROM supplier_invoice i
            JOIN supplier_invoice_item li ON li.supplier_invoice_id=i.supplier_invoice_id
            WHERE i.supplier_name IN ({placeholders})
              AND i.invoice_date BETWEEN %s AND %s
              AND li.description IS NOT NULL
              AND li.line_total_valid = TRUE
              AND li.extraction_status = 'OK'
              AND i.currency = 'SGD'
              AND i.is_gst_inclusive = FALSE
            GROUP BY DATE_FORMAT(i.invoice_date, '%%Y-%%m'), li.description
            ORDER BY month, li.description
            """,
            (*matched_names, start_date, end_date),
        )
        evidence = self._query(
            f"""
            SELECT i.invoice_number, i.invoice_date, i.extraction_status, i.currency,
                   s.source_uri, s.ocr_json_uri
            FROM supplier_invoice i
            JOIN source_document s ON s.source_document_id=i.source_document_id
            WHERE i.supplier_name IN ({placeholders})
              AND i.invoice_date BETWEEN %s AND %s
            ORDER BY i.invoice_date, i.invoice_number
            """,
            (*matched_names, start_date, end_date),
        )
        for source in evidence:
            source["citation"] = f"{source['source_uri']}#invoice={source['invoice_number']}"
        return {
            "supplier_query": supplier,
            "matched_supplier_codes": matched_codes,
            "matched_suppliers": matched_names,
            "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "monthly": monthly,
            "item_trends": item_trends,
            "evidence": evidence,
            "currency_note": (
                "SGD totals exclude invoices in other currencies; no exchange rates are applied. "
                "Item trends exclude GST-inclusive prices."
            ),
            "method": "Uses scanned supplier invoices; rows marked REVIEW_REQUIRED remain disclosed in monthly counts.",
            "citation": {"source_locator": "tables=supplier_invoice,supplier_invoice_item"},
        }

    def search_documents(
        self,
        query: str,
        limit: int = 5,
        outlet_code: str | None = None,
        supplier_name: str | None = None,
    ) -> dict[str, Any]:
        text = query.strip()
        if len(text) < 2:
            raise ValueError("query must contain at least two characters")
        if not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        outlet = self._validate_canonical_outlet(outlet_code) if outlet_code else None
        supplier = supplier_name.strip() if supplier_name else None
        filters = []
        params: list[Any] = [text]
        if outlet:
            filters.append(
                "EXISTS (SELECT 1 FROM document_entity_mention m "
                "WHERE m.document_chunk_id=c.document_chunk_id "
                "AND m.entity_type='outlet' AND m.canonical_key=%s)"
            )
            params.append(outlet)
        if supplier:
            filters.append(
                "EXISTS (SELECT 1 FROM document_entity_mention m "
                "WHERE m.document_chunk_id=c.document_chunk_id "
                "AND m.entity_type='supplier' AND m.canonical_key LIKE %s)"
            )
            params.append(f"%{supplier}%")
        where_filters = " AND " + " AND ".join(filters) if filters else ""
        fulltext_sql = f"""
            SELECT c.chunk_import_key AS chunk_id, c.source_type, c.occurred_at, c.author,
                   c.title, c.chunk_text, c.source_locator, s.source_uri,
                   MATCH(c.title, c.chunk_text) AGAINST (%s IN NATURAL LANGUAGE MODE) AS relevance
            FROM document_chunk c
            JOIN source_document s ON s.source_document_id=c.source_document_id
            WHERE MATCH(c.title, c.chunk_text) AGAINST (%s IN NATURAL LANGUAGE MODE) > 0
            {where_filters}
            ORDER BY relevance DESC, c.occurred_at DESC
            LIMIT %s
        """
        results = self._query(fulltext_sql, (text, *params, limit))
        strategy = "mysql_fulltext"
        if not results:
            like_params: list[Any] = [f"%{text}%", f"%{text}%"]
            if outlet:
                like_params.append(outlet)
            if supplier:
                like_params.append(f"%{supplier}%")
            results = self._query(
                f"""
                SELECT c.chunk_import_key AS chunk_id, c.source_type, c.occurred_at, c.author,
                       c.title, c.chunk_text, c.source_locator, s.source_uri, NULL AS relevance
                FROM document_chunk c
                JOIN source_document s ON s.source_document_id=c.source_document_id
                WHERE (c.chunk_text LIKE %s OR c.title LIKE %s)
                {where_filters}
                ORDER BY c.occurred_at DESC
                LIMIT %s
                """,
                (*like_params, limit),
            )
            strategy = "substring_fallback"
        for result in results:
            result["citation"] = f"{result['source_uri']}#{result['source_locator']}"
        return {
            "query": text,
            "filters": {"outlet_code": outlet, "supplier_name": supplier},
            "strategy": strategy,
            "results": results,
        }

    def get_data_coverage(self, area: str = "all") -> dict[str, Any]:
        valid = {"all", "sales", "invoices", "wastage", "staffing"}
        if area not in valid:
            raise ValueError(f"unsupported coverage area: {area}")
        sections: dict[str, Any] = {}
        if area in {"all", "sales"}:
            counts = self._query("""
                SELECT (SELECT COUNT(*) FROM outlets) AS outlet_count,
                       COUNT(DISTINCT d.outlet_id) AS outlets_with_daily_summaries,
                       COUNT(*) AS daily_summary_rows
                FROM daily_outlet_summary d
            """)[0]
            missing = self._query("""SELECT outlet_code FROM outlets WHERE outlet_id NOT IN
                (SELECT DISTINCT outlet_id FROM daily_outlet_summary) ORDER BY outlet_code""")
            sections["sales"] = {**counts, "outlets_without_daily_summaries": [r["outlet_code"] for r in missing], "pos_status": "PARTIAL_EXPORT", "authority": "Daily Z-report summaries are authoritative where available; POS is fallback/behavioural evidence only."}
        if area in {"all", "invoices"}:
            sections["invoices"] = self._query("""
                SELECT (SELECT COUNT(*) FROM supplier_invoice) AS scanned_invoices,
                       (SELECT COUNT(*) FROM supplier_purchases) AS purchase_rows,
                       (SELECT COUNT(DISTINCT invoice_no) FROM supplier_purchases) AS distinct_purchase_invoice_numbers,
                       SUM(reconciliation_status='MATCHED') AS matched_scans,
                       SUM(reconciliation_status='NO_SCAN') AS purchases_without_scan
                FROM purchase_invoice_reconciliation
            """)[0]
            sections["invoices"]["authority"] = "Scanned invoice header, or validated scanned lines when header arithmetic fails; legacy purchase totals are stale-system values."
        if area in {"all", "wastage"}:
            rows = self._query("""SELECT o.outlet_code, COUNT(d.wastage_kg) AS measured_days
                FROM outlets o JOIN daily_outlet_summary d ON d.outlet_id=o.outlet_id
                GROUP BY o.outlet_id HAVING COUNT(d.wastage_kg)>0 ORDER BY o.outlet_code""")
            sections["wastage"] = {"recording_outlets": rows, "limitation": "Only BDK-01 and WDL-01 record wastage; missing values mean not recorded, not zero."}
        if area in {"all", "staffing"}:
            sections["staffing"] = self._query("""
                SELECT (SELECT COUNT(*) FROM staff_roster) AS roster_files,
                       COUNT(*) AS assignments, COUNT(DISTINCT outlet_id) AS covered_outlets,
                       SUM(rate_status='CLIENT_APPROVED_DEFAULT') AS defaulted_rate_assignments,
                       (SELECT COUNT(*) FROM staff_shift) AS shift_patterns
                FROM staff_assignment
            """)[0]
            sections["staffing"]["limitation"] = "Only June 2026 is supplied; the roster is a weekly pattern, not attendance evidence."
        return {"area": area, "sections": sections, "citation": {"source_locator": "database coverage counts"}}

    def assess_kopi_price_change(
        self, menu_scope: str, start_date: date, end_date: date,
        gross_price_increase_sgd: Decimal, demand_change_percent: list[Decimal],
        outlet_codes: list[str],
    ) -> dict[str, Any]:
        _validate_period(start_date, end_date)
        if menu_scope not in {"base_kopi", "all_kopi"}:
            raise ValueError(f"unsupported menu scope: {menu_scope}")
        if any(Decimal(change) < -100 or Decimal(change) > 100 for change in demand_change_percent):
            raise ValueError("demand changes must be between -100 and 100 percent")
        selected = [self._resolve_outlet(code, start_date, end_date)[0] for code in outlet_codes]
        where = ["p.tx_time >= %s", "p.tx_time < %s", ("m.item_name='Kopi'" if menu_scope == "base_kopi" else "m.item_name LIKE 'Kopi%'")]
        params: list[Any] = [start_date, end_date + timedelta(days=1)]
        if selected:
            where.append(f"p.canonical_outlet_code IN ({','.join(['%s'] * len(selected))})")
            params.extend(selected)
        observed = self._query(POS_CTE + f"""
            SELECT SUM(p.qty) AS units, ROUND(SUM(p.total),2) AS gross_sales_sgd,
                   MIN(p.unit_price) AS minimum_observed_price_sgd,
                   MAX(p.unit_price) AS maximum_observed_price_sgd
            FROM canonical_pos p JOIN menu_items m ON m.item_id=p.item_id
            WHERE {' AND '.join(where)}""", params)[0]
        units = Decimal(observed["units"] or 0)
        current_sales = Decimal(observed["gross_sales_sgd"] or 0)
        increase = Decimal(gross_price_increase_sgd)
        scenarios = []
        for change in demand_change_percent:
            change = Decimal(change)
            factor = Decimal("1") + change / Decimal("100")
            proposed_units = units * factor
            proposed_sales = current_sales * factor + proposed_units * increase
            scenarios.append({"demand_change_percent": change, "projected_units": proposed_units.quantize(Decimal("0.01")), "projected_gross_sales_sgd": proposed_sales.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "incremental_gross_sales_sgd": (proposed_sales-current_sales).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "incremental_ex_gst_sales_sgd": ((proposed_sales-current_sales)/Decimal("1.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)})
        # --- Assumption-based ingredient cost (client gave no recipe; we state ours) ---
        base_price = Decimal(observed["minimum_observed_price_sgd"] or 1.60)
        base_cpc = (Decimal("5")/Decimal("1000")*Decimal("13.54")
                    + Decimal("25")/Decimal("1000")*Decimal("5.01")
                    + Decimal("15")/Decimal("1000")*Decimal("4.68")
                    + Decimal("0.05"))
        ingredient = base_price - base_cpc
        ingredient_pct = (ingredient / base_price * 100).quantize(Decimal("1"))
        be_pct = (Decimal(gross_price_increase_sgd)
                  / (base_price + Decimal(gross_price_increase_sgd)) * 100).quantize(Decimal("0.1"))
        cost_assumption = {
            "basis": "assumption-based ingredient cost (no recipe/portion spec provided by client)",
            "assumptions": [
                "5 g roasted kopi per cup",
                "25 ml condensed milk per cup",
                "15 ml evaporated milk per cup",
                "S$0.05 cup + lid",
                "milk carton = 24 x 400 ml (12/48 tins also tested in sensitivity)",
                "labour, rent, equipment EXCLUDED (contribution margin, not full cost)",
            ],
            "evidence": {
                "roasted_kopi_sgd_per_kg": [13.18, 13.99],
                "roasted_kopi_median_sgd_per_kg": 13.54,
                "milk_carton_size_assumed": "24 x 400 ml (net mass not legible on scans)",
                "condensed_milk_carton_sgd": 48.13,  # 44.99-50.71
                "evaporated_milk_carton_sgd": 44.92,  # 41.78-46.05
                "source": "69-scan invoice set (Heng Huat, D'Best, etc.); latest legible 12 Jun 2026",
            },
            "cost_per_cup_sgd": base_cpc.quantize(Decimal("0.01")),
            "ingredient_margin_sgd": ingredient.quantize(Decimal("0.01")),
            "ingredient_margin_pct": str(ingredient_pct) + "%",
            "incremental_per_cup_sgd": Decimal(gross_price_increase_sgd).quantize(Decimal("0.01")),
            "break_even_demand_loss_pct": str(be_pct) + "% (cost-independent; = 0.10 / 1.70)",
            "cost_sensitivity_by_milk_carton": [
                {"carton_tins": 12, "cost_per_cup_sgd": "0.51"},
                {"carton_tins": 24, "cost_per_cup_sgd": "0.31"},
                {"carton_tins": 48, "cost_per_cup_sgd": "0.22"},
            ],
            "interpretation": ("The +10c is a pure +10c to every cup sold (cost-independent). "
                               "Break-even is a 5.9% demand loss. Above that, the raise loses money. "
                               "The only decision-critical unknown is the demand response, which the data cannot answer."),
        }
        return {
            "cost_assumption": cost_assumption,
            "status": "PARTIAL",
            "recommendation": "INSUFFICIENT_EVIDENCE",
            "menu_scope": menu_scope,
            "observed_partial_pos": observed,
            "gross_price_increase_sgd": increase,
            "scenarios": scenarios,
            "missing_for_decision": ["complete kopi unit volume", "recipe-level ingredient cost per cup", "target margin", "evidence of demand response to a price change"],
            "explanation": "This is a sensitivity analysis, not a pricing recommendation. POS volume is incomplete and invoice trends do not identify recipe consumption per cup. The ingredient cost is an ASSUMPTION-based estimate (no recipe provided); the +10c margin effect and the 5.9% break-even demand loss are cost-independent. See cost_assumption for the stated recipe, evidence, and sensitivity.",
            "citation": [{"source_uri": "data/sql/kayakopi_pos.sql", "source_locator": "tables=pos_transactions,menu_items"}, {"source_locator": "tables=supplier_invoice,supplier_invoice_item"}],
        }

    def get_data_quality_issues(
        self,
        status: str = "OPEN",
        severity: str | None = None,
        rule_code: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        where = ["q.resolution_status=%s"]
        params: list[Any] = [status.strip().upper()]
        if severity:
            where.append("q.severity=%s")
            params.append(severity.strip().upper())
        if rule_code:
            where.append("q.rule_code=%s")
            params.append(rule_code.strip().upper())
        params.append(limit)
        rows = self._query(
            f"""
            SELECT q.issue_import_key AS issue_id, q.record_type, q.record_key,
                   q.source_locator, q.rule_code, q.severity, q.details,
                   q.resolution_status, q.detected_at, s.source_uri
            FROM data_quality_issue q
            JOIN source_document s ON s.source_document_id=q.source_document_id
            WHERE {' AND '.join(where)}
            ORDER BY FIELD(q.severity, 'ERROR', 'WARNING', 'INFO'), q.detected_at DESC
            LIMIT %s
            """,
            params,
        )
        for row in rows:
            row["citation"] = (
                f"{row['source_uri']}#{row['source_locator']}"
                if row["source_locator"] else row["source_uri"]
            )
        return {
            "filters": {
                "status": status.strip().upper(),
                "severity": severity.strip().upper() if severity else None,
                "rule_code": rule_code.strip().upper() if rule_code else None,
            },
            "count": len(rows),
            "issues": rows,
        }
