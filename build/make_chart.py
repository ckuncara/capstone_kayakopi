#!/usr/bin/env python3
"""Demo-deck chart for the 10-cent kopi cost trend.
Reads build/out/kopi_trend.csv, writes build/out/kopi_trend_chart.png.
Panel 1: S$/kg over time. Panel 2: S$ per 5kg bag over time (the raw invoice number).
All points are 5kg bags, so bag-price and per-kg move in lockstep. Dark deck theme.
"""
import csv, datetime, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_IN = os.path.join(HERE, "out", "kopi_trend.csv")
PNG_OUT = os.path.join(HERE, "out", "kopi_trend_chart.png")

KOPHI, TEA = "#FFB84C", "#6FD1A9"
BG, PANEL, GRID, TXT, MUT = "#0f1115", "#161a20", "#2a313c", "#e7ecf3", "#93a0b4"


def d(s):
    return datetime.datetime.strptime(s, "%d/%m/%Y").date()


rows = list(csv.DictReader(open(CSV_IN)))

def ser(product, field):
    out = [(d(r["date"]), float(r[field])) for r in rows
           if r["product"].startswith(product) and r[field]]
    out.sort()
    return out

kopi_kg, tea_kg = ser("Kopi", "per_kg"), ser("Tea", "per_kg")
kopi_bag, tea_bag = ser("Kopi", "unit_price"), ser("Tea", "unit_price")  # 5kg bags (raw invoice)


def s2(p):
    return "S$%.2f" % p


def style(ax, ymin, ymax):
    ax.set_facecolor(PANEL)
    ax.grid(True, color=GRID, lw=0.8, alpha=0.7)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=MUT)
    ax.set_ylim(ymin, ymax)
    ax.set_ylabel("S$/kg", color=MUT, fontsize=9)
    ax.set_xlabel("date", color=MUT, fontsize=9)


def plot(ax, a, b):
    if a:
        ax.plot([p[0] for p in a], [p[1] for p in a], "-o", color=KOPHI, lw=2.2,
                ms=7, mfc=KOPHI, mec=BG, zorder=4)
    if b:
        ax.plot([p[0] for p in b], [p[1] for p in b], "-o", color=TEA, lw=2.2,
                ms=7, mfc=TEA, mec=BG, zorder=4)
    if a:
        ax.annotate("kopi", (a[0][0], a[0][1]), xytext=(-4, -18),
                    textcoords="offset points", color=KOPHI, fontsize=10, weight="bold")
        for x, y in a:
            ax.annotate(s2(y), (x, y), xytext=(0, 11), textcoords="offset points",
                        color=KOPHI, fontsize=8.5, ha="center")
    if b:
        ax.annotate("tea", (b[-1][0], b[-1][1]), xytext=(8, 8),
                    textcoords="offset points", color=TEA, fontsize=10, weight="bold")
        for x, y in b:
            ax.annotate(s2(y), (x, y), xytext=(0, -17), textcoords="offset points",
                        color=TEA, fontsize=8.5, ha="center")


fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 7.6), sharex=True,
                               facecolor=BG, constrained_layout=True)
fig.suptitle("10-cent Kopi — input cost trend (Heng Huat / SUP-01 paper invoices)",
             color=TXT, fontsize=15, weight="bold")
fig.text(0.5, 0.932, "Dec 2025 → Jun 2026  ·  all points are 5kg bags  ·  thin sample (7 legible scans) — directional, not an index",
         color=MUT, ha="center", fontsize=9.5)

style(ax1, 9, 15)
plot(ax1, kopi_kg, tea_kg)
ax1.set_title("S$ per kg of roasted kopi / tea input", color=TXT, fontsize=11, loc="left", pad=9)

style(ax2, 42, 74)
ax2.set_ylabel("S$ per 5kg bag", color=MUT, fontsize=9)
plot(ax2, kopi_bag, tea_bag)
ax2.set_title("Raw invoice bag price (S$ per 5kg bag) — kopi +6.1%, tea +12.9% over the window",
              color=TXT, fontsize=11, loc="left", pad=9)

ticks = sorted({p[0] for p in (kopi_kg + tea_kg)})
ax2.set_xticks(ticks)
ax2.set_xticklabels([t.strftime("%d %b %y") for t in ticks], rotation=28, ha="right", fontsize=8.5)

fig.text(0.985, 0.012,
         "source: SUP-01 paper invoices (source of truth, Q7) · 7/12 Heng Huat scans legible · not an estimate",
         color=MUT, ha="right", fontsize=8)

fig.savefig(PNG_OUT, dpi=160, facecolor=BG)
print("wrote", PNG_OUT, os.path.getsize(PNG_OUT), "bytes")
