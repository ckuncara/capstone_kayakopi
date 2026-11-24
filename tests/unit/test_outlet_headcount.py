"""Roster headcount queries and their read-only chat interface."""

import calendar
import sqlite3
from datetime import date

import pytest

from kaya_kopi.copilot.tools import kaya
from kaya_kopi.repositories.mysql import MySQLRepository


@pytest.fixture
def headcount_repository(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    def last_day(value):
        if value is None:
            return None
        day = date.fromisoformat(value)
        return day.replace(day=calendar.monthrange(day.year, day.month)[1]).isoformat()

    connection.create_function("LAST_DAY", 1, last_day)
    connection.executescript("""
        CREATE TABLE outlets (outlet_id INTEGER, outlet_code TEXT, outlet_type TEXT);
        CREATE TABLE source_document (source_document_id INTEGER, source_uri TEXT);
        CREATE TABLE staff_roster (
            staff_roster_id INTEGER, source_document_id INTEGER, roster_month TEXT
        );
        CREATE TABLE staff_assignment (
            staff_roster_id INTEGER, outlet_id INTEGER, staff_key TEXT, role TEXT
        );
        INSERT INTO outlets VALUES (1, 'JUR-01', 'kopitiam'), (2, 'JEM-01', 'mall');
        INSERT INTO source_document VALUES
            (1, 'may.xlsx'), (2, 'june.xlsx');
        INSERT INTO staff_roster VALUES
            (1, 1, '2026-05-01'), (2, 2, '2026-06-01');
        INSERT INTO staff_assignment VALUES
            (1, 1, 'person-a', 'Barista'),
            (2, 1, 'person-a', 'Barista'),
            (2, 1, 'person-a', 'Barista'),
            (2, 1, 'person-b', 'Barista'),
            (2, 1, 'person-c', 'Supervisor'),
            (2, 2, 'person-d', 'Barista');
    """)
    repository = MySQLRepository()

    def query(sql, parameters=()):
        return [
            dict(row) for row in connection.execute(
                sql.replace("%s", "?"),
                tuple(value.isoformat() if isinstance(value, date) else value for value in parameters),
            )
        ]

    monkeypatch.setattr(repository, "_query", query)
    monkeypatch.setattr(repository, "_canonical_outlets", lambda: ["JUR-01", "JEM-01"])
    monkeypatch.setattr(repository, "_resolve_outlet", lambda code, *_: (code, {}))
    try:
        yield repository
    finally:
        connection.close()


def test_headcount_tool_defaults_to_latest_roster_and_counts_distinct_staff(
    headcount_repository, monkeypatch,
):
    monkeypatch.setattr(kaya, "_repository", lambda: headcount_repository)
    result = kaya.analyze_outlets_sql.invoke({
        "analysis": "headcount", "outlet_codes": ["JUR-01"],
    })
    assert result["ok"]
    assert result["period"] == {"start": "2026-06-01", "end": "2026-06-30"}
    assert result["status"] == "COMPLETE"
    outlet = result["results"][0]
    assert outlet["outlet_code"] == "JUR-01"
    assert outlet["counts_by_month_and_role"] == [
        {"roster_month": "2026-06-01", "role": "Barista", "headcount": 2},
        {"roster_month": "2026-06-01", "role": "Supervisor", "headcount": 1},
    ]
    assert outlet["citation"]["source_uris"] == ["june.xlsx"]
    assert "person-a" not in str(result)
    assert "hourly_rate" not in str(result)


def test_headcounts_remain_separate_by_month_and_outlet(headcount_repository):
    result = headcount_repository.analyze_outlets(
        "headcount", [], date(2026, 5, 15), date(2026, 6, 15),
    )
    assert result["status"] == "PARTIAL"
    outlets = {row["outlet_code"]: row for row in result["results"]}
    jur, jem = outlets["JUR-01"], outlets["JEM-01"]
    assert jur["counts_by_month_and_role"] == [
        {"roster_month": "2026-05-01", "role": "Barista", "headcount": 1},
        {"roster_month": "2026-06-01", "role": "Barista", "headcount": 2},
        {"roster_month": "2026-06-01", "role": "Supervisor", "headcount": 1},
    ]
    assert jur["missing_roster_months"] == []
    assert jem["counts_by_month_and_role"] == [
        {"roster_month": "2026-06-01", "role": "Barista", "headcount": 1},
    ]
    assert jem["missing_roster_months"] == ["2026-05"]


def test_headcount_outlet_type_does_not_widen_empty_selection(headcount_repository):
    result = headcount_repository.analyze_outlets(
        "headcount", ["JUR-01"], None, None, outlet_type="mall",
    )
    assert result["results"] == []
    assert result["status"] == "NO_OBSERVATIONS"


def test_missing_roster_is_not_zero_staff(headcount_repository):
    result = headcount_repository.analyze_outlets(
        "headcount", ["JUR-01"], date(2026, 7, 1), date(2026, 7, 31),
    )
    assert result["status"] == "NO_OBSERVATIONS"
    assert result["results"][0]["roster_available"] is False
    assert result["results"][0]["counts_by_month_and_role"] == []
    assert result["results"][0]["missing_roster_months"] == ["2026-07"]


def test_no_roster_and_incomplete_dates(headcount_repository):
    with pytest.raises(ValueError, match="both dates"):
        headcount_repository.analyze_outlets("headcount", [], date(2026, 6, 1), None)
    headcount_repository._query("DELETE FROM staff_assignment")
    headcount_repository._query("DELETE FROM staff_roster")
    result = headcount_repository.analyze_outlets("headcount", [], None, None)
    assert result == {"analysis": "headcount", "status": "NO_OBSERVATIONS", "results": []}
