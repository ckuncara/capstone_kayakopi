"""Purchase matching rules shared by staged and uploaded invoices."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def select_purchase(
    purchases: list[dict[str, Any]], invoice_date: date | None, subtotal: Decimal
) -> tuple[dict[str, Any], str]:
    def rank(purchase: dict[str, Any]) -> tuple[bool, Decimal, int]:
        try:
            purchase_date = datetime.strptime(purchase["purchase_date"], "%d/%m/%Y").date()
            amount = Decimal(purchase["total_amount"])
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(
                f"Purchase {purchase['purchase_id']} has an invalid date or subtotal."
            ) from exc
        return purchase_date != invoice_date, abs(amount - subtotal), purchase["purchase_id"]

    selected = min(purchases, key=rank)
    purchase_date = datetime.strptime(selected["purchase_date"], "%d/%m/%Y").date()
    method = (
        "SUPPLIER_INVOICE_NUMBER_DATE"
        if purchase_date == invoice_date
        else "SUPPLIER_INVOICE_NUMBER_CLOSEST_AMOUNT"
    )
    return selected, method
