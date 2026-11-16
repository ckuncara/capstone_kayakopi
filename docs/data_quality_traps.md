# Data Quality Report — trap list (deliverable)

> Fill in after the 16 Nov data zip lands. One row per trap found:
> where it is, what it does to a naive query, and how the app handles it.

## Known trap categories to check (from CLIENT_BRIEF.md)

| # | Category | Where to look | What to test | Our handling |
|---|---|---|---|---|
| 1 | Duplicate / soft-deleted rows | MySQL sales DB | `COUNT(*)` vs distinct order ids | — |
| 2 | Date/timezone mismatch | CSVs vs DB vs invoices | same sale appears on different days | — |
| 3 | Currency / units | invoices vs sales | cents vs dollars, net vs gross | — |
| 4 | Outlet naming drift | 8 CSVs vs DB | "Mall A" vs "MALL_A" vs "mall-a" | — |
| 5 | Missing/null measures | all sources | margin = NULL vs 0 | — |
| 6 | Scan quality | 70 invoices | upside-down, partial, multi-page | — |
| 7 | Stale supplier names | invoices | renamed suppliers across months | — |
| 8 | Staffing definition | DB | FTE vs hours vs shifts | — |
| 9 | Mall vs non-mall flag | outlets table | is the flag reliable? | — |
| 10 | Quarter boundary | sales DB | fiscal vs calendar quarter | — |

## Findings (fill during Phase B)

1. **Trap:** _(e.g. `outlet_code` has trailing spaces in 3 CSVs)_
   **Impact:** _(e.g. joins silently drop rows → understated wastage)_
   **Fix:** _(e.g. `TRIM()` in every SQL tool; CSV reader strips whitespace)_
   **Proof:** _(eval case id / query before-after)_
