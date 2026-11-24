"""Live MySQL smoke tests for the six chatbot query tools."""

from __future__ import annotations

import unittest

from kaya_kopi.copilot.tools.kaya import (
    ALL_TOOLS,
    analyze_pos_sql,
    analyze_outlets_sql,
    analyze_suppliers_sql,
    assess_kopi_price_change_sql,
    compare_outlets_sql,
    get_data_quality_issues_sql,
    get_data_coverage_sql,
    get_invoice_sql,
    get_outlet_sales_sql,
    get_supplier_cost_trend_sql,
    search_documents_sql,
)


class QueryToolIntegrationTest(unittest.TestCase):
    def test_public_tool_set_is_consolidated(self) -> None:
        self.assertEqual(
            [tool.name for tool in ALL_TOOLS],
            ["get_outlet_sales_sql", "analyze_outlets_sql", "analyze_pos_sql", "analyze_suppliers_sql", "get_data_coverage_sql", "search_documents_sql", "assess_kopi_price_change_sql", "get_data_quality_issues_sql", "lookup_invoice_sqlite", "search_invoice_chroma"],
        )

    def test_get_outlet_sales_infers_alias_effective_source_period_when_dates_are_omitted(self) -> None:
        result = get_outlet_sales_sql.invoke(
            {"outlet_code": "AMK-3A", "include_daily": False}
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outlet_code"], "AMK-03")
        self.assertEqual(result["period"], {"start": "2026-01-01", "end": "2026-06-30"})
        self.assertEqual(result["alias_resolution"]["requested_code"], "AMK-3A")
        self.assertEqual(result["alias_resolution"]["canonical_code"], "AMK-03")
        self.assertEqual(
            result["alias_resolution"]["evidence_source_uri"],
            "data/docs/emails/2026-05-18_outlet_rename.txt",
        )
        self.assertGreater(float(result["summary"]["gross_takings_sgd"]), 0)
        self.assertGreater(int(result["summary"]["receipt_count"]), 0)

    def test_analyze_pos_product_mix_is_explicitly_partial(self) -> None:
        result = analyze_pos_sql.invoke({"analysis": "product_mix", "outlet_codes": ["AMK-01"], "start_date": "2025-07-01", "end_date": "2026-06-30", "menu_item": "Kopi"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authority"], "PARTIAL_POS_EXPORT")
        self.assertGreater(sum(int(row["quantity"]) for row in result["results"]), 0)

    def test_mall_kopi_margin_reports_missing_inputs_and_out_of_period_sales(self) -> None:
        result = analyze_pos_sql.invoke({
            "analysis": "margin", "outlet_type": "mall", "menu_item": "Kopi",
            "start_date": "2026-10-01", "end_date": "2026-11-24",
        })

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["outlet_codes"], ["JEM-01", "ORC-01", "PGL-01", "VIV-01"])
        self.assertEqual(result["period"], {"start": "2026-10-01", "end": "2026-11-24"})
        self.assertEqual(result["pos_coverage"]["last_observed_date"], "2026-06-30")
        self.assertEqual(result["pos_coverage"]["status"], "NO_OBSERVATIONS")
        self.assertIsNone(result["margin_percent"])
        self.assertIn("approved recipe-level cost per cup for the requested period", result["missing_for_margin"])

    def test_pos_outlet_type_and_codes_never_widen_an_empty_intersection(self) -> None:
        result = analyze_pos_sql.invoke({
            "analysis": "product_mix", "outlet_type": "mall", "outlet_codes": ["AMK-03"],
            "start_date": "2026-06-01", "end_date": "2026-06-30",
        })

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["results"], [])

    def test_analyze_suppliers_spend_uses_canonical_invoice_cost(self) -> None:
        result = analyze_suppliers_sql.invoke({"analysis": "spend", "group_by": "supplier"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authority"], "SCANNED_INVOICE_CANONICAL_COST")
        self.assertEqual(sum(int(row["invoice_count"]) for row in result["results"]), 68)
        self.assertEqual(result["excluded_review_required_invoices"], 1)

    def test_analyze_suppliers_spend_identifies_excluded_invoice(self) -> None:
        result = analyze_suppliers_sql.invoke(
            {
                "analysis": "spend",
                "start_date": "2025-07-01",
                "end_date": "2026-06-30",
                "group_by": "supplier_month",
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["included_invoice_count"], 68)
        self.assertEqual(result["excluded_invoice_count"], 1)
        self.assertEqual(
            result["excluded_invoices"],
            [
                {
                    "invoice_number": "INV-012601-458",
                    "supplier_name": "HENG HUAT COFFEE ROASTERS",
                    "invoice_date": "2026-01-21",
                    "reason": "REVIEW_REQUIRED",
                    "source_uri": "data/images/supplier_invoices/INV-012601-458.jpg",
                }
            ],
        )

    def test_analyze_suppliers_returns_2025_invoice_totals_by_month(self) -> None:
        result = analyze_suppliers_sql.invoke(
            {
                "analysis": "spend",
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "group_by": "month",
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"])
        self.assertTrue(all(row["month"].startswith("2025-") for row in result["results"]))
        self.assertGreater(
            sum(float(row["total_sgd"]) for row in result["results"]),
            0,
        )

    def test_supplier_price_change_identifies_largest_comparable_increase_with_evidence(self) -> None:
        result = analyze_suppliers_sql.invoke(
            {
                "analysis": "price_change",
                "start_date": "2025-07-01",
                "end_date": "2026-06-30",
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertGreater(result["comparable_series_count"], 0)
        self.assertGreater(result["insufficient_series_count"], 0)
        largest = result["largest_increase"]
        self.assertEqual(largest["supplier_code"], "SUP-04")
        self.assertEqual(largest["supplier_name"], "MEWAH SUGAR TRADING")
        self.assertEqual(largest["description"], "Condensed milk ctn")
        self.assertEqual(largest["first_unit_price_sgd"], "43.8000")
        self.assertEqual(largest["latest_unit_price_sgd"], "50.4700")
        self.assertEqual(largest["percent_change"], "15.23")
        self.assertIn("invoice_number", largest["first_evidence"])
        self.assertIn("citation", largest["first_evidence"])
        self.assertIn("invoice_number", largest["latest_evidence"])
        self.assertIn("citation", largest["latest_evidence"])

    def test_supplier_price_change_ranks_before_paging_and_reports_date_coverage(self) -> None:
        arguments = {
            "analysis": "price_change", "start_date": "2026-01-01", "end_date": "2026-11-24",
            "detail_limit": 1,
        }
        first = analyze_suppliers_sql.invoke(arguments)
        second = analyze_suppliers_sql.invoke({**arguments, "page": 2})

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first["period"]["end"], "2026-11-24")
        self.assertEqual(first["observed_period"]["end"], "2026-06-28")
        self.assertEqual(first["largest_increase"], first["results"][0])
        self.assertEqual(first["detail_page"]["next_page"], 2)
        self.assertEqual(first["comparable_series_count"], second["comparable_series_count"])
        self.assertGreaterEqual(float(first["results"][0]["percent_change"]),
                                float(second["results"][0]["percent_change"]))
        self.assertIsNone(second["largest_increase"])

    def test_supplier_price_change_distinguishes_missing_dates_from_no_increase(self) -> None:
        result = analyze_suppliers_sql.invoke({
            "analysis": "price_change", "start_date": "2023-01-01", "end_date": "2023-12-31",
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "NO_OBSERVATIONS")
        self.assertIsNone(result["largest_increase"])
        self.assertEqual(result["source_period"]["end"], "2026-06-28")

    def test_analyze_suppliers_reconciliation_keeps_reused_invoice_number(self) -> None:
        result = analyze_suppliers_sql.invoke(
            {"analysis": "reconciliation", "invoice_number": "INV-042509-752"}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(
            {row["reconciliation_status"] for row in result["results"]},
            {"MATCHED", "NO_SCAN"},
        )

    def test_reconciliation_distinguishes_identity_match_from_amount_match_and_gst(self) -> None:
        result = analyze_suppliers_sql.invoke(
            {"analysis": "reconciliation", "invoice_number": "INV-012508-182"}
        )

        self.assertTrue(result["ok"], result)
        row = result["results"][0]
        self.assertTrue(row["identity_match"])
        self.assertFalse(row["amount_match"])
        self.assertEqual(row["legacy_subtotal_sgd"], "942.00")
        self.assertEqual(row["scanned_subtotal_sgd"], "951.33")
        self.assertEqual(row["subtotal_difference_sgd"], "9.33")
        self.assertFalse(row["gst_responsible"])
        self.assertEqual(row["cause"], "UNDETERMINED")
        self.assertEqual(
            row["gst_explanation"],
            "GST cannot explain this difference because both compared values are pre-GST subtotals.",
        )
        self.assertTrue(result["invoice_evidence"]["found"])
        self.assertTrue(result["invoice_evidence"]["items"])

    def test_data_coverage_reports_known_source_counts(self) -> None:
        result = get_data_coverage_sql.invoke({"area": "all"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sections"]["sales"]["outlet_count"], 12)
        self.assertEqual(result["sections"]["sales"]["outlets_with_daily_summaries"], 8)
        self.assertEqual(result["sections"]["invoices"]["scanned_invoices"], 69)
        self.assertEqual(result["sections"]["staffing"]["assignments"], 46)

    def test_kopi_price_assessment_refuses_to_overstate_partial_evidence(self) -> None:
        result = assess_kopi_price_change_sql.invoke({"menu_scope": "base_kopi", "start_date": "2025-07-01", "end_date": "2026-06-30", "gross_price_increase_sgd": "0.10", "demand_change_percent": ["0"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recommendation"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertGreater(int(result["observed_partial_pos"]["units"]), 0)
    def test_analyze_outlets_performance_uses_authoritative_daily_summary(self) -> None:
        result = analyze_outlets_sql.invoke(
            {
                "analysis": "performance",
                "outlet_codes": ["AMK-01"],
                "start_date": "2025-07-01",
                "end_date": "2025-07-01",
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["analysis"], "performance")
        self.assertEqual(result["results"][0]["authority"], "DAILY_SUMMARY_Z_REPORT")
        self.assertEqual(result["results"][0]["gross_takings_sgd"], "815.51")
        self.assertEqual(result["results"][0]["receipt_count"], 263)

    def test_analyze_outlets_preserves_alias_resolution_evidence(self) -> None:
        result = analyze_outlets_sql.invoke(
            {
                "analysis": "performance",
                "outlet_codes": ["AMK-3A"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            }
        )

        self.assertTrue(result["ok"], result)
        row = result["results"][0]
        self.assertEqual(row["requested_outlet_code"], "AMK-3A")
        self.assertEqual(row["outlet_code"], "AMK-03")
        self.assertEqual(row["alias_resolution"]["canonical_code"], "AMK-03")
        self.assertEqual(
            row["alias_resolution"]["evidence_source_uri"],
            "data/docs/emails/2026-05-18_outlet_rename.txt",
        )

    def test_analyze_outlets_wastage_discloses_unrecorded_outlets(self) -> None:
        result = analyze_outlets_sql.invoke(
            {
                "analysis": "wastage",
                "outlet_codes": ["BDK-01", "JUR-01"],
                "start_date": "2025-07-01",
                "end_date": "2026-06-30",
            }
        )

        self.assertTrue(result["ok"], result)
        by_outlet = {row["outlet_code"]: row for row in result["results"]}
        self.assertEqual(by_outlet["BDK-01"]["coverage_status"], "MEASURED")
        self.assertGreater(by_outlet["BDK-01"]["measured_days"], 0)
        self.assertEqual(by_outlet["JUR-01"]["coverage_status"], "NOT_RECORDED")
        self.assertIsNone(by_outlet["JUR-01"]["total_wastage_kg"])

    def test_analyze_outlets_wastage_ranks_only_measured_outlets(self) -> None:
        result = analyze_outlets_sql.invoke({"analysis": "wastage"})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["default_period"])
        self.assertEqual(result["period"], {"start": "2025-07-01", "end": "2026-06-30"})
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["all_outlets_comparable"])
        self.assertEqual(result["comparison_scope"], "MEASURED_OUTLETS_ONLY")
        self.assertEqual(result["measured_outlet_count"], 2)
        self.assertEqual(result["highest_measured_wastage"]["outlet_code"], "BDK-01")
        self.assertEqual(result["highest_measured_wastage"]["total_wastage_kg"], "640.400")
        self.assertEqual(
            [row["outlet_code"] for row in result["ranked_measured_results"]],
            ["BDK-01", "WDL-01"],
        )

    def test_analyze_outlets_staffing_expands_weekly_roster_and_labels_revenue_coverage(self) -> None:
        result = analyze_outlets_sql.invoke(
            {
                "analysis": "staffing",
                "outlet_codes": ["AMK-01", "ORC-01"],
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            }
        )

        self.assertTrue(result["ok"], result)
        by_outlet = {row["outlet_code"]: row for row in result["results"]}
        self.assertGreater(float(by_outlet["AMK-01"]["paid_hours"]), 0)
        self.assertGreater(float(by_outlet["AMK-01"]["labour_cost_sgd"]), 0)
        self.assertEqual(by_outlet["AMK-01"]["revenue_authority"], "DAILY_SUMMARY_Z_REPORT")
        self.assertTrue(by_outlet["AMK-01"]["productivity_comparable"])
        self.assertEqual(by_outlet["ORC-01"]["revenue_authority"], "PARTIAL_POS_EXPORT")
        self.assertFalse(by_outlet["ORC-01"]["productivity_comparable"])

    def test_analyze_outlets_staffing_does_not_invent_overstaffing_threshold(self) -> None:
        result = analyze_outlets_sql.invoke(
            {
                "analysis": "staffing",
                "outlet_codes": [],
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["classification"], "NO_APPROVED_OVERSTAFFING_THRESHOLD")
        self.assertEqual(result["lowest_comparable_productivity"]["outlet_code"], "BDK-01")
        self.assertEqual(
            {row["outlet_code"] for row in result["non_comparable_outlets"]},
            {"JEM-01", "ORC-01", "PGL-01", "VIV-01"},
        )
        self.assertTrue(
            all(row["productivity_comparable"] for row in result["comparable_outlets"])
        )

    def test_mall_staffing_returns_partial_sales_with_latest_roster(self) -> None:
        result = analyze_outlets_sql.invoke({"analysis": "staffing", "outlet_type": "mall"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["period"], {"start": "2026-06-01", "end": "2026-06-30"})
        self.assertEqual({row["outlet_code"] for row in result["results"]},
                         {"JEM-01", "ORC-01", "PGL-01", "VIV-01"})
        for row in result["results"]:
            self.assertGreater(float(row["labour_cost_sgd"]), 0)
            self.assertGreater(float(row["observed_pos_takings_sgd"]), 0)
            self.assertIsNone(row["gross_takings_sgd"])
            self.assertFalse(row["productivity_comparable"])

    def test_staffing_does_not_rank_an_outlet_without_roster_hours(self) -> None:
        result = analyze_outlets_sql.invoke({
            "analysis": "staffing", "outlet_codes": ["AMK-01"],
            "start_date": "2026-05-01", "end_date": "2026-05-31",
        })

        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["results"][0]["labour_cost_sgd"])
        self.assertIsNone(result["results"][0]["gross_takings_per_paid_hour_sgd"])
        self.assertEqual(result["comparable_outlets"], [])

    def test_get_outlet_sales_uses_authoritative_daily_summary_when_available(self) -> None:
        result = get_outlet_sales_sql.invoke(
            {
                "outlet_code": "AMK-01",
                "start_date": "2025-07-01",
                "end_date": "2025-07-01",
                "include_daily": False,
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authority"], "DAILY_SUMMARY_Z_REPORT")
        self.assertEqual(result["summary"]["gross_takings_sgd"], "815.51")
        self.assertEqual(result["summary"]["receipt_count"], 263)
        self.assertEqual(result["coverage"]["missing_dates"], [])

    def test_get_outlet_sales(self) -> None:
        result = get_outlet_sales_sql.invoke(
            {
                "outlet_code": "AMK-03",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "include_daily": False,
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outlet_code"], "AMK-03")
        self.assertGreater(result["summary"]["transaction_count"], 0)

    def test_get_outlet_sales_explains_database_backed_alias(self) -> None:
        result = get_outlet_sales_sql.invoke(
            {
                "outlet_code": "AMK-3A",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "include_daily": False,
            }
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outlet_code"], "AMK-03")
        self.assertEqual(
            result["alias_resolution"],
            {
                "requested_code": "AMK-3A",
                "canonical_code": "AMK-03",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "validation_status": "VERIFIED",
                "evidence_source_uri": "data/docs/emails/2026-05-18_outlet_rename.txt",
            },
        )

    def test_compare_outlets(self) -> None:
        result = compare_outlets_sql.invoke(
            {
                "outlet_codes": ["AMK-01", "AMK-03", "JUR-01"],
                "start_date": "2025-07-01",
                "end_date": "2025-07-31",
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["authority"], "DAILY_SUMMARY_Z_REPORT")
        self.assertEqual(len(result["comparison"]), 3)
        self.assertEqual([row["sales_rank"] for row in result["comparison"]], [1, 2, 3])

    def test_get_invoice(self) -> None:
        result = get_invoice_sql.invoke({"invoice_number": "INV-012508-182"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["found"])
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("source_uri", result["citation"])

    def test_get_invoice_uses_validated_lines_when_header_arithmetic_is_invalid(self) -> None:
        result = get_invoice_sql.invoke({"invoice_number": "INV-032603-511"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["canonical_cost"],
            {
                "currency": "SGD",
                "subtotal": "179.07",
                "gst": "16.12",
                "total": "195.19",
                "subtotal_sgd": "179.07",
                "gst_sgd": "16.12",
                "total_sgd": "195.19",
                "cost_basis": "VALIDATED_INVOICE_LINES",
                "review_required": True,
                "header_arithmetic_valid": False,
            },
        )

    def test_get_supplier_cost_trend(self) -> None:
        result = get_supplier_cost_trend_sql.invoke(
            {
                "supplier_name": "Mewah",
                "start_date": "2025-07-01",
                "end_date": "2026-06-30",
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["matched_suppliers"], ["MEWAH SUGAR TRADING"])
        self.assertEqual(result["matched_supplier_codes"], ["SUP-04"])
        self.assertTrue(result["monthly"])
        self.assertTrue(all("citation" in row for row in result["evidence"]))

    def test_search_documents(self) -> None:
        result = search_documents_sql.invoke(
            {"query": "fridge spoil", "outlet_code": "TAM-01", "limit": 3}
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"])
        self.assertTrue(all("citation" in row for row in result["results"]))

    def test_search_documents_with_supplier_filter(self) -> None:
        result = search_documents_sql.invoke(
            {"query": "eggs deliver", "supplier_name": "Ban Seng", "limit": 3}
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"])

    def test_get_data_quality_issues(self) -> None:
        result = get_data_quality_issues_sql.invoke(
            {"status": "OPEN", "severity": "WARNING", "limit": 5}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 5)
        self.assertTrue(all("citation" in row for row in result["issues"]))

    def test_validation_error_is_safe(self) -> None:
        result = get_outlet_sales_sql.invoke(
            {
                "outlet_code": "BAD-99",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "include_daily": False,
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "validation")


if __name__ == "__main__":
    unittest.main()
