from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest

from kaya_kopi.copilot.text_to_sql import (
    BUSINESS_SCHEMA,
    BusinessSQLInput,
    ReadOnlySQLValidator,
    QueryIntent,
    UnsafeSQLQuery,
    execute_business_query,
    query_business_data_sql,
)
from kaya_kopi.copilot.result_renderer import (
    render_business_results,
    render_scope_alignment,
    render_specialist_reports,
)


class ReadOnlySQLValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = ReadOnlySQLValidator(BUSINESS_SCHEMA, max_rows=200)

    def test_accepts_parameterized_join_and_keeps_bound_values_separate(self) -> None:
        query = self.validator.validate(
            """SELECT o.outlet_code, SUM(d.sales_amount_sgd) AS gross_takings_sgd
               FROM outlets AS o
               JOIN daily_outlet_summary AS d ON d.outlet_id = o.outlet_id
               WHERE d.business_date >= ? AND d.business_date < ?
               GROUP BY o.outlet_code ORDER BY gross_takings_sgd DESC""",
            ["2026-06-01", "2026-07-01"],
        )

        self.assertEqual(query.parameters, ("2026-06-01", "2026-07-01"))
        self.assertTrue(query.truncated_by_limit)
        self.assertIn("LIMIT 201", query.sql.upper())

    def test_accepts_and_records_declared_date_scope_when_sql_uses_exact_bounds(self) -> None:
        query = self.validator.validate(
            "SELECT business_date, sales_amount_sgd FROM daily_outlet_summary "
            "WHERE business_date >= ? AND business_date < ?",
            ["2026-07-01", "2026-10-01"],
            date_scope={
                "column": "daily_outlet_summary.business_date",
                "start": "2026-07-01",
                "end_exclusive": "2026-10-01",
            },
        )

        self.assertEqual(
            query.date_scope,
            {
                "column": "daily_outlet_summary.business_date",
                "start": "2026-07-01",
                "end_exclusive": "2026-10-01",
            },
        )

    def test_tool_schema_requires_explicit_date_scope_decision(self) -> None:
        required = BusinessSQLInput.model_json_schema()["required"]
        self.assertIn("query_intent", required)
        self.assertEqual(
            set(QueryIntent.model_json_schema()["required"]),
            {"metric_alias", "group_by", "filters", "date_scope"},
        )

    def test_validates_metric_grouping_filters_and_date_scope_together(self) -> None:
        query = self.validator.validate(
            "SELECT o.outlet_code, SUM(d.sales_amount_sgd) AS gross_takings "
            "FROM outlets o JOIN daily_outlet_summary d ON d.outlet_id = o.outlet_id "
            "WHERE d.business_date >= ? AND d.business_date < ? AND o.outlet_code = ? "
            "GROUP BY o.outlet_code",
            ["2026-07-01", "2026-10-01", "AMK-03"],
            query_intent={
                "metric_alias": "gross_takings",
                "group_by": ["outlet_code"],
                "filters": [{"column": "outlet_code", "operator": "=", "values": ["AMK-03"]}],
                "date_scope": {
                    "column": "business_date",
                    "start": "2026-07-01",
                    "end_exclusive": "2026-10-01",
                },
            },
        )

        self.assertEqual(query.date_scope["start"], "2026-07-01")

    def test_rejects_intent_metric_grouping_or_filter_that_disagrees_with_sql(self) -> None:
        query = "SELECT outlet_code, COUNT(*) AS total FROM outlets GROUP BY outlet_code"
        cases = (
            ({"metric_alias": "profit", "group_by": ["outlet_code"], "filters": [], "date_scope": None}, "metric"),
            ({"metric_alias": "total", "group_by": ["outlet_type"], "filters": [], "date_scope": None}, "grouping"),
            ({"metric_alias": "total", "group_by": ["outlet_code"], "filters": [
                {"column": "outlet_code", "operator": "=", "values": ["AMK-03"]}
            ], "date_scope": None}, "filter"),
        )
        for intent, expected_message in cases:
            with self.subTest(expected_message=expected_message), self.assertRaisesRegex(
                UnsafeSQLQuery, expected_message
            ):
                self.validator.validate(query, [], query_intent=intent)

    def test_rejects_filter_values_that_do_not_match_bound_sql_values(self) -> None:
        with self.assertRaisesRegex(UnsafeSQLQuery, "do not match query_intent"):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = ?",
                ["JUR-01"],
                query_intent={
                    "metric_alias": None,
                    "group_by": [],
                    "filters": [{"column": "outlet_code", "operator": "=", "values": ["AMK-03"]}],
                    "date_scope": None,
                },
            )

    def test_validates_in_filter_values_and_rejects_undeclared_sql_filters(self) -> None:
        self.validator.validate(
            "SELECT outlet_code FROM outlets WHERE outlet_code IN (?, ?)",
            ["AMK-03", "JUR-01"],
            query_intent={
                "metric_alias": None,
                "group_by": [],
                "filters": [{
                    "column": "outlet_code", "operator": "IN", "values": ["AMK-03", "JUR-01"]
                }],
                "date_scope": None,
            },
        )

        with self.assertRaisesRegex(UnsafeSQLQuery, "undeclared: outlet_code ="):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = ?",
                ["AMK-03"],
                query_intent={
                    "metric_alias": None,
                    "group_by": [],
                    "filters": [],
                    "date_scope": None,
                },
            )

    def test_rejects_unmodeled_between_or_boolean_filter_expressions(self) -> None:
        intent = {"metric_alias": None, "group_by": [], "filters": [], "date_scope": None}
        for sql, parameters in (
            ("SELECT qty FROM pos_transactions WHERE qty BETWEEN ? AND ?", [1, 5]),
            ("SELECT outlet_code FROM outlets WHERE outlet_code = ? OR outlet_code = ?", ["AMK-03", "JUR-01"]),
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(UnsafeSQLQuery, "Declare filters as bound"):
                self.validator.validate(sql, parameters, query_intent=intent)

    def test_rejects_declared_date_scope_missing_query_bounds_or_bound_values(self) -> None:
        scope = {"column": "business_date", "start": "2026-07-01", "end_exclusive": "2026-10-01"}
        with self.assertRaisesRegex(UnsafeSQLQuery, "constrain business_date"):
            self.validator.validate(
                "SELECT business_date FROM daily_outlet_summary WHERE business_date >= ?",
                ["2026-07-01"],
                date_scope=scope,
            )

        with self.assertRaisesRegex(UnsafeSQLQuery, "matching SQL filters"):
            self.validator.validate(
                "SELECT business_date FROM daily_outlet_summary "
                "WHERE business_date >= ? AND business_date < ?",
                ["2026-07-01", "2026-09-30"],
                date_scope=scope,
            )

        with self.assertRaisesRegex(UnsafeSQLQuery, "matching SQL filters"):
            self.validator.validate(
                "SELECT business_date FROM daily_outlet_summary "
                "WHERE business_date >= ? AND business_date < ? AND outlet_id = ?",
                ["2026-06-01", "2026-10-01", "2026-07-01"],
                date_scope=scope,
            )

    def test_rejects_date_scope_for_non_date_column_or_empty_interval(self) -> None:
        with self.assertRaisesRegex(UnsafeSQLQuery, "approved date column"):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = ?",
                ["2026-01-01"],
                date_scope={"column": "outlet_code", "start": "2026-01-01", "end_exclusive": "2026-02-01"},
            )

        with self.assertRaisesRegex(UnsafeSQLQuery, "earlier than its exclusive end"):
            self.validator.validate(
                "SELECT business_date FROM daily_outlet_summary "
                "WHERE business_date >= ? AND business_date < ?",
                ["2026-01-01", "2026-01-01"],
                date_scope={"column": "business_date", "start": "2026-01-01", "end_exclusive": "2026-01-01"},
            )

    def test_accepts_count_star_and_ordering_by_a_selected_alias(self) -> None:
        query = self.validator.validate(
            "SELECT outlet_code, COUNT(*) AS receipt_count FROM outlets "
            "GROUP BY outlet_code ORDER BY receipt_count DESC",
            [],
        )
        self.assertIn("COUNT(*)", query.sql.upper())
        self.assertIn("ORDER BY RECEIPT_COUNT", query.sql.upper())

    def test_accepts_rank_then_detail_cte_join_on_canonical_invoice_id(self) -> None:
        query = self.validator.validate(
            """WITH ranked AS (
                   SELECT supplier_invoice_id, SUM(total_amount) AS invoice_total
                   FROM supplier_invoice GROUP BY supplier_invoice_id
                   ORDER BY invoice_total DESC LIMIT 3
               )
               SELECT i.invoice_number, r.invoice_total, li.description
               FROM ranked AS r
               JOIN supplier_invoice AS i ON i.supplier_invoice_id = r.supplier_invoice_id
               JOIN supplier_invoice_item AS li ON li.supplier_invoice_id = i.supplier_invoice_id
               ORDER BY r.invoice_total DESC""",
            [],
            result_mode="bounded",
        )
        self.assertIn("with ranked", query.sql.lower())
        self.assertEqual(
            query.source_tables,
            ("supplier_invoice", "supplier_invoice_item"),
        )

    def test_rejects_mutations_and_multiple_statements(self) -> None:
        for sql in (
            "DELETE FROM outlets",
            "SELECT * FROM outlets; DELETE FROM outlets",
            "UPDATE outlets SET outlet_name = 'x'",
        ):
            with self.subTest(sql=sql), self.assertRaises(UnsafeSQLQuery):
                self.validator.validate(sql, [])

    def test_rejects_unapproved_objects_and_private_columns(self) -> None:
        for sql in (
            "SELECT user FROM mysql.user",
            "SELECT staff_name FROM staff_assignment",
            "SELECT raw_text FROM source_document",
        ):
            with self.subTest(sql=sql), self.assertRaises(UnsafeSQLQuery):
                self.validator.validate(sql, [])

    def test_rejects_inline_filter_literals_and_parameter_count_mismatch(self) -> None:
        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = 'AMK-01'", []
            )

        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = ?", []
            )

    def test_rejects_read_functions_with_side_effects_or_unbounded_runtime(self) -> None:
        for sql, values in (
            ("SELECT SLEEP(?) FROM outlets", [10]),
            ("SELECT GET_LOCK(?, 1) FROM outlets", ["report"]),
            ("SELECT LOAD_FILE(?) FROM outlets", ["/etc/passwd"]),
        ):
            with self.subTest(sql=sql), self.assertRaises(UnsafeSQLQuery):
                self.validator.validate(sql, values)

    def test_enforces_one_bounded_select_and_validates_parameter_values(self) -> None:
        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate(
                "SELECT outlet_code FROM outlets LIMIT 1000", [], result_mode="bounded"
            )

        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate("SELECT outlet_code FROM outlets LIMIT 3", [])

        bounded = self.validator.validate(
            "SELECT outlet_code FROM outlets ORDER BY outlet_code LIMIT 3", [], result_mode="bounded"
        )
        self.assertIn("LIMIT 3", bounded.sql.upper())

        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate("SELECT outlet_code FROM outlets", [], result_mode="bounded")

        with self.assertRaises(UnsafeSQLQuery):
            self.validator.validate(
                "SELECT outlet_code FROM outlets WHERE outlet_code = ?", [{"nested": "value"}]
            )


class BusinessQueryToolTest(unittest.TestCase):
    def test_all_outlet_sales_query_reports_daily_summary_entity_coverage(self) -> None:
        outlets = [f"OUT-{index:02d}" for index in range(1, 13)]
        coverage_rows = [
            {"outlet_code": code, "source_rows": 5 if index <= 8 else 0}
            for index, code in enumerate(outlets, start=1)
        ]
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [("outlet_code",), ("gross_takings_sgd",)]
        cursor.fetchmany.return_value = [
            {"outlet_code": code, "gross_takings_sgd": Decimal("100.00")}
            for code in outlets[:8]
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        intent = {
            "metric_alias": "gross_takings_sgd",
            "group_by": ["outlet_code"],
            "filters": [],
            "date_scope": {
                "column": "business_date",
                "start": "2026-01-01",
                "end_exclusive": "2026-07-01",
            },
        }

        with (
            patch("kaya_kopi.copilot.text_to_sql.MySQLRepository._connect", return_value=connection),
            patch("kaya_kopi.copilot.text_to_sql.MySQLRepository._query", return_value=coverage_rows) as coverage_query,
        ):
            result = execute_business_query(
                "SELECT o.outlet_code, SUM(d.sales_amount_sgd) AS gross_takings_sgd "
                "FROM outlets o JOIN daily_outlet_summary d ON d.outlet_id=o.outlet_id "
                "WHERE d.business_date >= ? AND d.business_date < ? GROUP BY o.outlet_code",
                ["2026-01-01", "2026-07-01"],
                query_intent=intent,
            )

        coverage = result["outlet_source_coverage"]
        self.assertEqual(coverage["expected_outlet_count"], 12)
        self.assertEqual(coverage["outlets_with_source_records_count"], 8)
        self.assertEqual(coverage["outlets_without_source_records"], outlets[8:])
        self.assertIn("2026-01-01", coverage_query.call_args.args[1])
        self.assertIn("2026-07-01", coverage_query.call_args.args[1])

    def test_filtered_outlet_query_does_not_claim_businesswide_coverage(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [("outlet_code",), ("sales_sgd",)]
        cursor.fetchmany.return_value = [{"outlet_code": "JEM-01", "sales_sgd": Decimal("10.00")}]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        intent = {
            "metric_alias": "sales_sgd",
            "group_by": ["outlet_code"],
            "filters": [{"column": "outlet_type", "operator": "=", "values": ["mall"]}],
            "date_scope": None,
        }

        with (
            patch("kaya_kopi.copilot.text_to_sql.MySQLRepository._connect", return_value=connection),
            patch("kaya_kopi.copilot.text_to_sql.MySQLRepository._query") as coverage_query,
        ):
            result = execute_business_query(
                "SELECT o.outlet_code, SUM(d.sales_amount_sgd) AS sales_sgd "
                "FROM outlets o JOIN daily_outlet_summary d ON d.outlet_id=o.outlet_id "
                "WHERE o.outlet_type = ? GROUP BY o.outlet_code",
                ["mall"], query_intent=intent,
            )

        self.assertIsNone(result["outlet_source_coverage"])
        coverage_query.assert_not_called()

    def test_large_complete_query_is_capped_without_losing_truncation_metadata(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [("outlet_id",), ("sales_amount_sgd",)]
        cursor.fetchmany.return_value = [
            {"outlet_id": index, "sales_amount_sgd": Decimal("12.34")}
            for index in range(201)
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor

        with patch("kaya_kopi.copilot.text_to_sql.MySQLRepository._connect", return_value=connection):
            result = execute_business_query(
                "SELECT outlet_id, sales_amount_sgd FROM daily_outlet_summary", []
            )

        self.assertEqual(len(result["rows"]), 200)
        self.assertEqual(result["retrieved_record_count"], 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows"][0]["sales_amount_sgd"], "12.34")
        self.assertIn("LIMIT 201", cursor.execute.call_args_list[1].args[0].upper())

    def test_model_gets_bounded_preview_while_artifact_keeps_all_rows(self) -> None:
        rows = [{"outlet_code": f"OUT-{index:02d}", "sales_sgd": index * 100} for index in range(12)]
        result = {
            "result_type": "business_data",
            "columns": ["outlet_code", "sales_sgd"],
            "rows": rows,
            "retrieved_record_count": 12,
            "expected_record_count": None,
            "truncated": False,
            "requested_result_mode": "complete",
            "sources": ["daily_outlet_summary"],
            "source_coverage_notes": ["Daily outlet reports can have missing dates."],
            "outlet_source_coverage": {
                "expected_outlet_count": 12,
                "outlets_with_source_records_count": 8,
                "outlets_without_source_records": ["KOP-01", "KOP-02", "KOP-03", "KOP-04"],
                "complete": False,
            },
            "query_intent": {"metric_alias": "sales_sgd", "group_by": [], "filters": [], "date_scope": None},
        }
        with patch("kaya_kopi.copilot.text_to_sql.execute_business_query", return_value=result):
            content, artifact = query_business_data_sql.func(
                "SELECT outlet_code, sales_amount_sgd FROM daily_outlet_summary", [],
                query_intent={"metric_alias": "sales_sgd", "group_by": [], "filters": [], "date_scope": None},
            )

        self.assertEqual(len(artifact["rows"]), 12)
        self.assertIn("12 rows", content)
        self.assertIn("8 of 12 canonical outlets", content)
        self.assertIn("No report rows means missing evidence, not zero sales", content)
        self.assertNotIn("OUT-11", content)
        self.assertEqual(query_business_data_sql.response_format, "content_and_artifact")

    def test_renderer_displays_all_rows_even_when_summary_preview_is_short(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.rows = None
                self.captions: list[str] = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.rows = rows

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append(("bar", data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append(("line", data, kwargs))

        ui = FakeUI()
        result = {
            "columns": ["outlet_code", "sales_sgd"],
            "rows": [{"outlet_code": f"OUT-{index:02d}", "sales_sgd": index} for index in range(12)],
            "retrieved_record_count": 12,
            "truncated": False,
            "sources": ["daily_outlet_summary"],
            "outlet_source_coverage": {
                "expected_outlet_count": 12,
                "outlets_with_source_records_count": 8,
                "outlets_without_source_records": ["KOP-01", "KOP-02", "KOP-03", "KOP-04"],
                "complete": False,
            },
            "date_scope": {"column": "business_date", "start": "2026-01-01", "end_exclusive": "2026-02-01"},
            "query_intent": {
                "metric_alias": "sales_sgd", "group_by": ["outlet_code"],
                "filters": [],
                "date_scope": None,
            },
        }
        render_business_results(ui, [result])
        self.assertEqual(len(ui.rows), 12)
        self.assertEqual(ui.rows[-1]["outlet_code"], "OUT-11")
        self.assertIn("12 retrieved · 12 displayed", ui.captions)
        self.assertTrue(any("Applied date scope: business_date" in line for line in ui.captions))
        self.assertTrue(any("Query intent: metric sales_sgd; grouped by outlet_code" in line for line in ui.captions))
        self.assertTrue(any("Daily Z-report source coverage: 8 of 12" in line for line in ui.captions))
        self.assertTrue(any("KOP-01, KOP-02, KOP-03, KOP-04" in line for line in ui.captions))
        self.assertEqual(len(ui.charts), 1)
        self.assertEqual(ui.charts[0][0], "bar")
        self.assertEqual(len(ui.charts[0][1]), 12)
        self.assertEqual(ui.charts[0][2]["x"], "outlet_code")
        self.assertEqual(ui.charts[0][2]["y"], "sales_sgd")

    def test_renderer_uses_line_chart_for_monthly_series_and_keeps_full_table(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.rows = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.rows = rows

            def caption(self, _value: str) -> None:
                pass

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def bar_chart(self, data, **kwargs) -> None:
                self.fail("monthly trend should use a line chart")

        ui = FakeUI()
        rows = [
            {"month": "2026-01", "sales_sgd": 1000},
            {"month": "2026-02", "sales_sgd": 1200},
        ]
        render_business_results(ui, [{
            "columns": ["month", "sales_sgd"], "rows": rows,
            "retrieved_record_count": 2, "query_intent": {
                "metric_alias": "sales_sgd", "group_by": ["month"], "filters": [],
            },
        }])
        self.assertEqual(ui.rows, rows)
        self.assertEqual(len(ui.charts), 1)
        self.assertEqual(ui.charts[0][1]["x"], "month")
        self.assertEqual(ui.charts[0][1]["y"], "sales_sgd")

    def test_renderer_omits_chart_when_rows_conflict_with_requested_item_scope(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.rows = []
                self.captions = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.rows = rows

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        rows = [
            {"item_name": "Kopi", "outlet_code": "AMK-03", "sales_sgd": 100},
            {"item_name": "Kaya Toast", "outlet_code": "AMK-03", "sales_sgd": 80},
        ]
        render_business_results(ui, [{
            "columns": ["item_name", "outlet_code", "sales_sgd"],
            "rows": rows,
            "retrieved_record_count": 2,
            "query_intent": {
                "metric_alias": "sales_sgd",
                "group_by": ["item_name"],
                "filters": [{
                    "column": "menu_items.item_name", "operator": "=", "values": ["Kopi"],
                }],
            },
        }])

        self.assertEqual(ui.rows, rows, "the complete tool result remains visible")
        self.assertEqual(ui.charts, [], "a chart must not visualize rows outside the declared scope")
        self.assertTrue(any("Chart omitted" in caption for caption in ui.captions))

    def test_renderer_keeps_chart_when_all_rows_match_declared_item_scope(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_business_results(ui, [{
            "columns": ["item_name", "sales_sgd"],
            "rows": [
                {"item_name": "Kopi", "sales_sgd": 100},
                {"item_name": "Kopi", "sales_sgd": 80},
            ],
            "retrieved_record_count": 2,
            "query_intent": {
                "metric_alias": "sales_sgd",
                "group_by": ["item_name"],
                "filters": [{
                    "column": "menu_items.item_name", "operator": "=", "values": ["Kopi"],
                }],
            },
        }])

        self.assertEqual(len(ui.charts), 1)
        self.assertEqual(ui.charts[0][0][0]["item_name"], "Kopi")

    def test_renderer_omits_unrequested_product_breakdown(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.rows = []
                self.captions = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.rows = rows

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        rows = [
            {"item_name": "Kopi", "sales_sgd": 100},
            {"item_name": "Kaya Toast", "sales_sgd": 80},
        ]
        render_business_results(ui, [{
            "columns": ["item_name", "sales_sgd"],
            "rows": rows,
            "retrieved_record_count": 2,
            "query_intent": {
                "metric_alias": "sales_sgd", "group_by": ["item_name"], "filters": [],
            },
        }], question="What is the margin on Kopi at mall outlets this quarter?")

        self.assertEqual(ui.charts, [])
        self.assertEqual(ui.rows, rows)
        self.assertTrue(any("Chart omitted" in caption for caption in ui.captions))

    def test_renderer_keeps_product_chart_when_user_requests_item_breakdown(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_business_results(ui, [{
            "columns": ["item_name", "sales_sgd"],
            "rows": [
                {"item_name": "Kopi", "sales_sgd": 100},
                {"item_name": "Kaya Toast", "sales_sgd": 80},
            ],
            "retrieved_record_count": 2,
            "query_intent": {"metric_alias": "sales_sgd", "group_by": ["item_name"], "filters": []},
        }], question="Show the product mix by item.")

        self.assertEqual(len(ui.charts), 1)


class SpecialistReportRendererTest(unittest.TestCase):
    def test_period_mismatch_for_same_outlet_scope_is_explicit(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.warnings = []

            def warning(self, value: str) -> None:
                self.warnings.append(value)

        ui = FakeUI()
        render_scope_alignment(ui, [
            {
                "result_type": "business_data",
                "date_scope": {
                    "column": "daily_outlet_summary.business_date",
                    "start": "2026-01-01",
                    "end_exclusive": "2026-11-27",
                },
                "query_intent": {
                    "metric_alias": "sales_sgd",
                    "filters": [{
                        "column": "outlets.outlet_type", "operator": "=", "values": ["mall"],
                    }],
                },
            },
            {
                "result_type": "specialist_report",
                "tool_name": "analyze_staffing_sql",
                "report": {
                    "analysis": "staffing",
                    "period": {"start": "2026-06-01", "end": "2026-06-30"},
                    "scope": {"outlet_type": "mall"},
                    "results": [],
                },
            },
        ])

        self.assertEqual(len(ui.warnings), 1)
        self.assertIn("different reporting periods", ui.warnings[0])
        self.assertIn("2026-01-01", ui.warnings[0])
        self.assertIn("2026-06-01", ui.warnings[0])

    def test_scope_alignment_does_not_warn_for_matching_periods_or_different_entities(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.warnings = []

            def warning(self, value: str) -> None:
                self.warnings.append(value)

        query = {
            "result_type": "business_data",
            "date_scope": {"start": "2026-06-01", "end_exclusive": "2026-07-01"},
            "query_intent": {"filters": [{
                "column": "outlets.outlet_type", "operator": "=", "values": ["mall"],
            }]},
        }
        matching_report = {
            "result_type": "specialist_report",
            "tool_name": "analyze_staffing_sql",
            "report": {
                "period": {"start": "2026-06-01", "end": "2026-06-30"},
                "scope": {"outlet_type": "mall"},
            },
        }
        different_entity_report = {
            **matching_report,
            "report": {
                "period": {"start": "2026-01-01", "end": "2026-01-31"},
                "scope": {"outlet_type": "kopitiam"},
            },
        }

        matching_ui = FakeUI()
        render_scope_alignment(matching_ui, [query, matching_report])
        different_entity_ui = FakeUI()
        render_scope_alignment(different_entity_ui, [query, different_entity_report])

        self.assertEqual(matching_ui.warnings, [])
        self.assertEqual(different_entity_ui.warnings, [])

    def test_specialist_report_displays_tool_period_and_authoritative_findings(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.captions = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def bar_chart(self, _data, **_kwargs) -> None:
                pass

            def line_chart(self, _data, **_kwargs) -> None:
                pass

        ui = FakeUI()
        render_specialist_reports(ui, [{
            "tool_name": "analyze_staffing_sql",
            "report": {
                "analysis": "staffing",
                "period": {"start": "2026-06-01", "end": "2026-06-30"},
                "classification": "NO_APPROVED_OVERSTAFFING_THRESHOLD",
                "results": [{
                    "outlet_code": "JEM-01",
                    "cause": "UNDETERMINED",
                    "coverage": {
                        "requested_roster_months": ["2026-06"],
                        "roster_months": ["2026-06"],
                        "missing_roster_months": [],
                    },
                }],
            },
        }])

        self.assertTrue(any("Requested period: 2026-06-01 through 2026-06-30" in item for item in ui.captions))
        self.assertTrue(any("Tool classification: NO_APPROVED_OVERSTAFFING_THRESHOLD" in item for item in ui.captions))
        self.assertTrue(any("Tool-recorded cause: UNDETERMINED" in item for item in ui.captions))

    def test_incomplete_source_months_warn_even_when_requested_dates_match(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.warnings = []

            def warning(self, value: str) -> None:
                self.warnings.append(value)

        requested_months = [f"2026-{month:02d}" for month in range(1, 12)]
        ui = FakeUI()
        render_scope_alignment(ui, [{
            "result_type": "specialist_report",
            "tool_name": "analyze_staffing_sql",
            "report": {
                "period": {"start": "2026-01-01", "end": "2026-11-26"},
                "results": [{
                    "outlet_code": "JEM-01",
                    "coverage": {
                        "requested_roster_months": requested_months,
                        "roster_months": ["2026-06"],
                        "missing_roster_months": [
                            month for month in requested_months if month != "2026-06"
                        ],
                    },
                }],
            },
        }])

        self.assertEqual(len(ui.warnings), 1)
        self.assertIn("incomplete roster-month coverage", ui.warnings[0])
        self.assertIn("JEM-01: 1 of 11 roster months (2026-06)", ui.warnings[0])

    def test_product_mix_chart_is_omitted_when_question_is_about_one_item(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.tables = []
                self.captions = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.tables.append(rows)

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        rows = [
            {"item_name": "Kopi", "quantity_sold": 20},
            {"item_name": "Kaya Toast", "quantity_sold": 12},
        ]
        render_specialist_reports(ui, [{
            "tool_name": "analyze_pos_sql",
            "report": {"analysis": "product_mix", "results": rows},
        }], question="What is the margin on Kopi at mall outlets?")

        self.assertEqual(ui.charts, [])
        self.assertEqual(ui.tables[0], rows)
        self.assertTrue(any("Chart omitted" in caption for caption in ui.captions))

    def test_product_mix_chart_is_kept_when_user_requests_mix(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_specialist_reports(ui, [{
            "tool_name": "analyze_pos_sql",
            "report": {"analysis": "product_mix", "results": [
                {"item_name": "Kopi", "quantity_sold": 20},
                {"item_name": "Kaya Toast", "quantity_sold": 12},
            ]},
        }], question="Show the product mix by item.")

        self.assertEqual(len(ui.charts), 1)

    def test_renders_all_specialist_rows_and_citations(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.tables = []
                self.captions = []
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, rows, **_kwargs) -> None:
                self.tables.append(rows)

            def caption(self, value: str) -> None:
                self.captions.append(value)

            def json(self, _value, **_kwargs) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, _data, **_kwargs) -> None:
                self.fail("price comparison should use a bar chart")

        ui = FakeUI()
        report = {
            "analysis": "wastage",
            "results": [
                {
                    "outlet_code": f"OUT-{index:02d}",
                    "measured_days": index + 1,
                    "citation": f"daily-summary.csv#row={index}",
                }
                for index in range(12)
            ],
        }
        render_specialist_reports(ui, [{
            "result_type": "specialist_report",
            "tool_name": "analyze_outlets_sql",
            "report": report,
        }])

        self.assertEqual(len(ui.tables), 1)
        self.assertEqual(len(ui.tables[0]), 12)
        self.assertEqual(ui.tables[0][-1]["outlet_code"], "OUT-11")
        self.assertIn("12 retrieved · 12 displayed", ui.captions)
        self.assertTrue(any("daily-summary.csv#row=0" in line for line in ui.captions))

    def test_specialist_price_change_chart_uses_percentage_metric(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def json(self, _value, **_kwargs) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_specialist_reports(ui, [{
            "tool_name": "analyze_suppliers_sql",
            "report": {"analysis": "price_change", "results": [
                {"item_description": "Coffee", "percentage_change": 7.2},
                {"item_description": "Tea", "percentage_change": 4.1},
            ]},
        }])
        self.assertEqual(len(ui.charts), 1)
        self.assertEqual(ui.charts[0][1]["x"], "item_description")
        self.assertEqual(ui.charts[0][1]["y"], "percentage_change")

    def test_supplier_price_change_chart_keeps_each_supplier_item_series_distinct(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

            def line_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_specialist_reports(ui, [{
            "tool_name": "analyze_suppliers_sql",
            "report": {"analysis": "price_change", "results": [
                {
                    "supplier_name": "Mewah Sugar Trading",
                    "description": "Condensed milk ctn",
                    "percentage_change": 8.03,
                },
                {
                    "supplier_name": "Mewah Sugar Trading",
                    "description": "Sugar 25kg",
                    "percentage_change": 2.50,
                },
            ]},
        }])

        self.assertEqual(len(ui.charts), 1)
        chart_rows, options = ui.charts[0]
        self.assertEqual(options["x"], "supplier_item")
        self.assertEqual(
            [row["supplier_item"] for row in chart_rows],
            [
                "Mewah Sugar Trading — Condensed milk ctn",
                "Mewah Sugar Trading — Sugar 25kg",
            ],
        )
        self.assertEqual([row["percentage_change"] for row in chart_rows], [8.03, 2.5])

    def test_kopi_scenario_chart_compares_revenue_impact_by_demand_change(self) -> None:
        class FakeUI:
            def __init__(self) -> None:
                self.charts = []

            def markdown(self, _value: str) -> None:
                pass

            def dataframe(self, _rows, **_kwargs) -> None:
                pass

            def caption(self, _value: str) -> None:
                pass

            def json(self, _value, **_kwargs) -> None:
                pass

            def bar_chart(self, data, **kwargs) -> None:
                self.charts.append((data, kwargs))

        ui = FakeUI()
        render_specialist_reports(ui, [{
            "tool_name": "assess_kopi_price_change_sql",
            "report": {"scenarios": [
                {"demand_change_percent": 0, "incremental_gross_sales_sgd": 110},
                {"demand_change_percent": -5, "incremental_gross_sales_sgd": 2.78},
                {"demand_change_percent": -10, "incremental_gross_sales_sgd": -104.44},
            ]},
        }])
        self.assertEqual(len(ui.charts), 1)
        self.assertEqual(ui.charts[0][1]["x"], "demand_change_percent")
        self.assertEqual(ui.charts[0][1]["y"], "incremental_gross_sales_sgd")


def test_sql_rejection_reason_is_logged_without_parameter_values(caplog):
    caplog.set_level("INFO", logger="kaya_kopi.copilot.timing")
    intent = {
        "metric_alias": None,
        "group_by": [],
        "filters": [{
            "column": "daily_outlet_summary.validation_status",
            "operator": "=",
            "values": ["VALID"],
        }],
        "date_scope": None,
    }

    with pytest.raises(UnsafeSQLQuery):
        execute_business_query(
            "SELECT d.validation_status FROM daily_outlet_summary d "
            "WHERE d.validation_status = ?",
            ["VALID"],
            query_intent=intent,
            timing_id="rejection-test",
        )

    logs = "\n".join(record.message for record in caplog.records)
    assert "stage=sql_validation" in logs
    assert "rejection_reason=Unsupported value for daily_outlet_summary.validation_status." in logs
    assert "rejection-test" in logs
    assert "'VALID'" not in logs


if __name__ == "__main__":
    unittest.main()
