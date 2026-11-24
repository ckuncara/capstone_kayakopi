"""Local, bounded invoice analytics using stored OCR data."""

import calendar
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from kaya_kopi.repositories.sqlite_ops import SQLiteOpsRepository

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})


def _period(question: str) -> tuple[str | None, str | None, str] | str:
    if re.search(r"\b(between|before|after|since|until|through|last year|this year|"
                 r"last month|this month|past|previous|quarter|q[1-4])\b", question):
        return "Please specify one calendar year, month and year, or exact ISO date (YYYY-MM-DD)."
    dates = re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", question)
    if dates:
        if len(dates) != 1 or len(re.findall(r"\b(?:19|20)\d{2}\b", question)) != 1:
            return "Please specify only one date, month, or calendar year."
        try:
            day = date.fromisoformat(dates[0])
        except ValueError:
            return "Please provide a valid date in YYYY-MM-DD format."
        return day.isoformat(), day.isoformat(), f"on {day.isoformat()}"
    year_months = re.findall(r"\b((?:19|20)\d{2})-(\d{2})\b", question)
    if year_months:
        if len(year_months) != 1 or len(re.findall(r"\b(?:19|20)\d{2}\b", question)) != 1:
            return "Please specify only one date, month, or calendar year."
        year, month = map(int, year_months[0])
        if not 1 <= month <= 12:
            return "Please provide a valid month in YYYY-MM format."
        return (
            date(year, month, 1).isoformat(),
            date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
            f"for {calendar.month_name[month]} {year}",
        )
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", question))
    months = {MONTHS[word] for word in re.findall(r"\b[a-z]+\b", question) if word in MONTHS}
    if len(years) > 1 or len(months) > 1:
        return "Please specify only one date, month, or calendar year."
    if months and not years:
        return "Please specify the year as well as the month."
    if not years:
        return None, None, "for all available dates"
    year = int(years.pop())
    if months:
        month = months.pop()
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return start.isoformat(), end.isoformat(), f"for {calendar.month_name[month]} {year}"
    return f"{year:04d}-01-01", f"{year:04d}-12-31", f"for {year}"


def _supplier(question: str, rows: list[dict]) -> str | None:
    names = {row["vendor_name"] for row in rows if row["vendor_name"]}
    matches = [
        name for name in names
        if re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", question)
    ]
    if matches:
        matches.sort(key=len, reverse=True)
        # If both a full name and its substring match, retain the longer name.
        matches = [name for name in matches if not any(
            name.casefold() != other.casefold() and name.casefold() in other.casefold()
            for other in matches
        )]
        if len(matches) > 1:
            return ""
        return matches[0]
    return None


def sqlite_analytics_answer(
    question: str, branch_code: str, repo: SQLiteOpsRepository
) -> str | None:
    text = question.casefold()
    price = bool(
        re.search(r"\b(prices?|unit costs?)\b", text)
        and re.search(r"\b(rais\w*|ris\w*|ros\w*|increas\w*|decreas\w*|"
                      r"fall\w*|fell|dropp\w*|chang\w*|trend\w*|compar\w*|up|down)\b", text)
    )
    spending = bool(re.search(
        r"\b(spend\w*|spent|expenditure|purchas\w*|invoice totals?|"
        r"total (?:cost|amount)|amount spent)\b", text
    )) or bool(re.search(r"\bsuppliers?\b", text) and re.search(r"\bcosts?\b", text))
    invoice = bool(re.search(r"\binvoices?\b", text))
    count = invoice and bool(re.search(r"\b(how many|count|number of)\b", text))
    failed = bool(re.search(r"\b(fail\w*|invalid|discrepanc\w*|audit issues?)\b", text))
    passed_only = bool(re.search(r"\b(pass\w*|valid)\b", text)) and not failed
    listing = invoice and bool(re.search(r"\b(list|show|find|which)\b", text))
    if not (price or spending or count or (invoice and (failed or listing))):
        return None
    requested_branches = set(re.findall(r"\b(?:amk|bdk|jur|tam|tpy|wdl)-\d{2}\b", text))
    if requested_branches and requested_branches != {branch_code.casefold()}:
        return "Select the requested branch in the sidebar before running this analysis."
    if not price and re.search(r"\b(average|mean|median|subtotal|subtotals)\b|"
                               r"\b(before|excluding|without)\s+gst\b", text):
        return "This SQLite analysis supports OCR grand totals, not averages or pre-GST subtotals."
    if not price and re.search(r"\b(products?|items?)\b", text):
        return "Product-specific spending is not supported; invoice totals may include multiple items."

    period = _period(text)
    if isinstance(period, str):
        return period
    start, end, period_label = period
    all_rows = repo.get_invoice_summaries(branch_code)
    supplier = _supplier(text, all_rows)
    if supplier == "":
        return "Please specify one supplier at a time."
    if supplier is None and re.search(r"\b(?:from|for)\s+(?:supplier\s+)?[a-z]", text):
        suffix = re.search(r"\b(?:from|for)\s+(?:supplier\s+)?([a-z]+)", text).group(1)
        if suffix not in MONTHS and suffix not in {"all", "each", "the", "every", "product", "item"}:
            return "Supplier not recognised. Use the full supplier name shown in your invoices."

    rows = []
    undated = 0
    for row in all_rows:
        if supplier and row["vendor_name"] != supplier:
            continue
        if start is not None:
            try:
                day = date.fromisoformat(row["invoice_date"]).isoformat()
            except (TypeError, ValueError):
                undated += 1
                continue
            if not start <= day <= end:
                continue
        rows.append(row)

    branch_label = "HQ (all branches)" if branch_code.upper() == "HQ" else branch_code.upper()
    scope = f"{period_label}; branch: {branch_label}"
    if supplier:
        scope += f"; supplier: {supplier}"
    warning = (
        f"Scope: {scope}. Includes failed or unreviewed OCR audits; verify flagged invoices."
        " Only the displayed scope and grouping are applied."
    )
    if undated:
        warning += f" {undated} invoice(s) without a valid date excluded from the period."

    if price:
        if failed or passed_only:
            return "Price comparisons include all audit statuses; filtering by audit status is not supported."
        if start is None or start[5:] != "01-01" or end[5:] != "12-31":
            return "Please specify one calendar year for price comparisons, such as 2025."
        observations = repo.get_price_observations(int(start[:4]), branch_code)
        if supplier:
            observations = [row for row in observations if row["vendor_name"] == supplier]
        descriptions = {row["description"] for row in observations if row["description"]}
        matched = [
            item for item in descriptions
            if re.search(r"(?<!\w)" + re.escape(item.casefold()) + r"(?!\w)", text)
        ]
        explicit_item = re.search(
            r"\b(?:product|item)\s+(.+?)(?:\s+(?:in|during|for)\s+(?:19|20)\d{2}\b|[?.!,]|$)",
            text,
        )
        if explicit_item and not matched:
            phrase = explicit_item.group(1).strip(" '\"")
            matched = [item for item in descriptions if phrase in item.casefold()]
            if not matched:
                return "Product not recognised. Use the item description from an invoice."
        if len(matched) > 1:
            return "Multiple product descriptions match; specify one exact item description: " + ", ".join(sorted(matched)[:10])
        if matched:
            observations = [row for row in observations if row["description"] == matched[0]]
            scope += f"; product: {matched[0]}"
        ranking = bool(re.search(r"\b(most|highest|largest|biggest|greatest|maximum)\b", text))
        if re.search(r"\b(decreas\w*|fall\w*|fell|dropp\w*|down)\b", text):
            direction = "decrease"
        elif re.search(r"\b(rais\w*|ris\w*|ros\w*|increas\w*|up)\b", text):
            direction = "increase"
        else:
            direction = "change"
        return _price_answer(observations, int(start[:4]), ranking, scope, direction)

    if not rows:
        return f"No SQLite invoices found {scope}."
    if count or (failed and not listing and not spending):
        passed = sum(row["audit_status"] == "PASS" for row in rows)
        issues = len(rows) - passed
        if failed:
            return (
                f"**Failed or unreviewed invoices:** {issues} of {len(rows)} total; "
                f"passed audits: {passed}.\n\n{warning}"
            )
        if passed_only:
            return f"**Passed invoices:** {passed} of {len(rows)} total.\n\n{warning}"
        return (
            f"**Invoice count:** {len(rows)}; passed audits: {passed}; "
            f"failed or unreviewed: {issues}.\n\n{warning}"
        )
    if passed_only:
        rows = [row for row in rows if row["audit_status"] == "PASS"]
        warning += " Filtered to passing audits as requested."
    elif failed:
        rows = [row for row in rows if row["audit_status"] != "PASS"]
        warning += " Filtered to failed or unreviewed audits as requested."
    if not rows:
        return f"No matching invoices found {scope}."
    if listing and not spending:
        lines = [f"**Invoices** ({len(rows)} found; showing up to 30):"]
        for row in rows[:30]:
            lines.append(
                f"- {row['invoice_number']} | {row['invoice_date'] or 'date unknown'} | "
                f"{row['vendor_name'] or 'supplier unknown'} | {row['currency'] or 'currency unknown'} "
                f"{row['ocr_grand_total'] if row['ocr_grand_total'] is not None else 'amount missing'} | "
                f"audit: {row['audit_status'] or 'unreviewed'}"
            )
        return "\n".join(lines) + f"\n\n{warning}"

    monthly = bool(re.search(r"\b(monthly|month|months|per month|trend)\b", text))
    by_supplier = bool(re.search(r"\b(by|per|each)\s+(?:the\s+)?suppliers?\b", text))
    grouped = defaultdict(list)
    for row in rows:
        try:
            month_key = date.fromisoformat(row["invoice_date"]).strftime("%Y-%m")
        except (TypeError, ValueError):
            month_key = "date unknown"
        key = (month_key, row["currency"] or "currency unknown")
        if not monthly:
            key = (row["vendor_name"] or "supplier unknown", key[1])
        elif by_supplier:
            key = (f"{month_key} | {row['vendor_name'] or 'supplier unknown'}", key[1])
        grouped[key].append(row)
    summaries = []
    for (group, currency), items in grouped.items():
        amounts = [Decimal(str(item["ocr_grand_total"])) for item in items
                   if item["ocr_grand_total"] is not None]
        total = sum(amounts, Decimal(0))
        summaries.append((group, currency, total, len(items), len(items) - len(amounts),
                          sum(item["audit_status"] != "PASS" for item in items), items))
    summaries.sort(key=lambda entry: (entry[1], entry[0]) if monthly else (entry[1], -entry[2]))
    lines = ["**Monthly invoice spending:**" if monthly else "**Supplier invoice spending:**"]
    for group, currency, total, count_value, missing, issues, items in summaries[:30]:
        sources = ", ".join(str(item["invoice_number"]) for item in items[:8])
        if len(items) > 8:
            sources += f", ... ({len(items)} invoices total)"
        amount = f"{currency} {total:.2f}" if missing != count_value else "amount unavailable"
        lines.append(
            f"- {group}: {amount} across {count_value} invoice(s); "
            f"{issues} failed/unreviewed, {missing} missing amount(s). Invoices: {sources}."
        )
    if len(summaries) > 30:
        lines.append(f"Showing 30 of {len(summaries)} groups.")
    totals = defaultdict(list)
    for row in rows:
        totals[row["currency"] or "currency unknown"].append(row["ocr_grand_total"])
    for currency, amounts in sorted(totals.items()):
        available = [Decimal(str(amount)) for amount in amounts if amount is not None]
        if available:
            lines.append(f"- Total {currency}: {sum(available, Decimal(0)):.2f} "
                         f"({len(amounts) - len(available)} missing amount(s)).")
        else:
            lines.append(f"- Total {currency}: unavailable ({len(amounts)} missing amount(s)).")
    return "\n".join(lines) + f"\n\nTotals are OCR grand totals, grouped by currency.\n\n{warning}"


def _price_answer(rows: list[dict], year: int, ranking: bool, scope: str, direction: str) -> str:
    groups = defaultdict(list)
    excluded = 0
    for row in rows:
        try:
            parsed_date = date.fromisoformat(row["invoice_date"])
        except (TypeError, ValueError):
            excluded += 1
            continue
        if (
            not row["vendor_name"]
            or not row["description"]
            or not row["currency"]
            or row["is_gst_inclusive"] not in (0, 1)
            or row["ocr_unit_price"] is None
            or row["ocr_unit_price"] <= 0
        ):
            excluded += 1
            continue
        key = (
            " ".join(row["vendor_name"].casefold().split()),
            " ".join(row["description"].casefold().split()),
            row["currency"].upper(),
            row["is_gst_inclusive"],
        )
        groups[key].append((parsed_date, row))

    comparisons = []
    ambiguous = 0
    for observations in groups.values():
        observations.sort(key=lambda pair: pair[0])
        first_date, last_date = observations[0][0], observations[-1][0]
        if first_date == last_date:
            continue
        first_rows = [row for day, row in observations if day == first_date]
        last_rows = [row for day, row in observations if day == last_date]
        if len({row["ocr_unit_price"] for row in first_rows}) > 1 or len(
            {row["ocr_unit_price"] for row in last_rows}
        ) > 1:
            ambiguous += 1
            continue
        first, last = first_rows[0], last_rows[0]
        first_price = Decimal(str(first["ocr_unit_price"]))
        last_price = Decimal(str(last["ocr_unit_price"]))
        comparisons.append(((last_price - first_price) / first_price * 100, first, last))

    limitation = (
        f"Scope: {scope}. Compared matching supplier, exact item description, currency "
        "and GST basis. Failed or unreviewed audits and invalid lines are included and flagged. "
        f"Excluded {excluded} incomplete line(s) and {ambiguous} ambiguous price group(s). "
        "Units are not stored separately, so exact descriptions do not guarantee comparable "
        "units; missing or non-ISO dates are not included. Only the displayed scope is "
        "applied. OCR results and coverage should be verified."
    )
    if direction == "increase":
        comparisons = [entry for entry in comparisons if entry[0] > 0]
    elif direction == "decrease":
        comparisons = [entry for entry in comparisons if entry[0] < 0]
    if ranking:
        if comparisons:
            maximum = max(abs(entry[0]) for entry in comparisons)
            comparisons = [entry for entry in comparisons if abs(entry[0]) == maximum]
    if not comparisons:
        return f"No comparable price {direction} was found in SQLite for {year}.\n\n{limitation}"
    comparisons.sort(key=lambda entry: entry[0], reverse=True)
    result = [
        f"**Largest observed supplier/product price {direction}:**" if ranking
        else f"**Supplier/product price changes** ({len(comparisons)} found; showing up to 30):"
    ]
    for change, first, last in comparisons[:30]:
        result.append(
            f"- {first['vendor_name']} — {first['description']}: {change:.2f}% "
            f"({first['currency']} {first['ocr_unit_price']} on {first['invoice_date']}, "
            f"invoice {first['invoice_number']} line {first['line_no']} "
            f"[audit: {first['audit_status'] or 'unreviewed'}; line valid: {first['is_valid']}]; "
            f"{last['currency']} {last['ocr_unit_price']} on {last['invoice_date']}, "
            f"invoice {last['invoice_number']} line {last['line_no']} "
            f"[audit: {last['audit_status'] or 'unreviewed'}; line valid: {last['is_valid']}])."
        )
    return "\n".join(result) + f"\n\n{limitation}"
