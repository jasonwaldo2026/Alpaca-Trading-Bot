"""Figures for the SPCX primer. Same palette as the daily report."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle, FancyArrowPatch

INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE, PLANE = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f5f5f1"
UP, DOWN = "#0ca30c", "#d03b3b"
ACCENT, SECOND, VWAP_HUE = "#2a78d6", "#eb6834", "#4a3aa7"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE, "text.color": INK,
    "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True,
})

CSV = ("/root/.claude/uploads/e995d9eb-4307-5581-981f-ea691629bfb3/"
       "2ec603ca-SPCX_signals_20260520_20260922.csv")
df = pd.read_csv(CSV, parse_dates=["time"])
df["hhmm"] = df["time"].dt.hour * 60 + df["time"].dt.minute
MORNING = (df["hhmm"] >= 580) & (df["hhmm"] < 660)


def save(fig, name, h=None):
    fig.savefig(name, dpi=170, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print(f"  {name}")


# --- 1. anatomy of a candle -------------------------------------------------
fig, ax = plt.subplots(figsize=(7.2, 3.6))
for x, o, c, hi, lo, label in ((1, 10.2, 11.0, 11.3, 10.0, "rising"),
                               (3, 11.0, 10.3, 11.4, 10.1, "falling")):
    colour = UP if c >= o else DOWN
    ax.vlines(x, lo, hi, color=colour, linewidth=1.6)
    ax.add_patch(Rectangle((x - 0.28, min(o, c)), 0.56, abs(c - o),
                           facecolor=colour, edgecolor=colour))
    ax.text(x, lo - 0.16, label, ha="center", size=9, color=INK_2)

ann = [(1.36, 11.3, "high — the most it traded for"),
       (1.36, 10.0, "low — the least it traded for"),
       (1.36, 11.0, "close — where the minute ended"),
       (1.36, 10.2, "open — where the minute began")]
for x, y, text in ann:
    ax.annotate(text, xy=(1.05, y), xytext=(x, y), va="center", size=8.5,
                color=INK_2,
                arrowprops=dict(arrowstyle="-", color=AXIS, linewidth=0.8))
ax.text(3.45, 10.75, "The thick part is the BODY:\nopen to close.\n\n"
                     "The thin lines are WICKS:\nhow far it got and\ncame back from.",
        va="center", size=8.5, color=INK_2)
ax.set_xlim(0.2, 5.6)
ax.set_ylim(9.7, 11.7)
ax.set_ylabel("Price")
ax.set_xticks([])
ax.spines[["top", "right", "bottom"]].set_visible(False)
ax.set_title("One candle = one slice of time", loc="left", size=10.5, color=INK)
save(fig, "fig_candle.png")


# --- 2. how the MACD is built ----------------------------------------------
rng = np.random.default_rng(7)
n = 220
price = 150 + np.cumsum(rng.normal(0, 0.09, n)) + np.sin(np.arange(n) / 22) * 1.6
s = pd.Series(price)
fast, slow = s.ewm(span=9, adjust=False).mean(), s.ewm(span=17, adjust=False).mean()
macd = fast - slow
signal = macd.ewm(span=6, adjust=False).mean()

fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.2, 4.6), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1.3], "hspace": 0.18})
a1.plot(s, color=MUTED, linewidth=1.0, label="price")
a1.plot(fast, color=ACCENT, linewidth=1.6, label="fast average (9 min)")
a1.plot(slow, color=SECOND, linewidth=1.6, label="slow average (17 min)")
a1.legend(frameon=False, fontsize=8, loc="upper left", ncol=3)
a1.set_ylabel("Price")
a1.spines[["top", "right"]].set_visible(False)
a1.set_title("Two averages of the same price, one quicker than the other",
             loc="left", size=10.5, color=INK)

a2.axhline(0, color=AXIS, linewidth=1)
gap = macd - signal
a2.bar(range(n), gap, color=[UP if g >= 0 else DOWN for g in gap], alpha=0.35,
       linewidth=0, width=1.0)
a2.plot(macd, color=ACCENT, linewidth=1.6, label="MACD = fast − slow")
a2.plot(signal, color=SECOND, linewidth=1.4, label="signal = smoothed MACD")
crosses = [i for i in range(1, n)
           if macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]]
for i in crosses:
    a2.plot(i, macd[i], "o", color=INK, markersize=4.5, zorder=5)
a2.legend(frameon=False, fontsize=8, loc="upper left", ncol=2)
a2.set_ylabel("MACD")
a2.set_xlabel("minutes")
a2.spines[["top", "right"]].set_visible(False)
a2.set_title("The gap between them, and a smoothed copy of that gap. "
             "Dots are crossings.", loc="left", size=10.5, color=INK)
save(fig, "fig_macd.png")
print("figures 1-2 done")
