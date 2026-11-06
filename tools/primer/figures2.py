"""Findings figures, from the real per-signal data."""
exec(open("figures.py").read().split("# --- 1.")[0])

# --- 3. MFE and MAE on one trade -------------------------------------------
rng2 = np.random.default_rng(3)
m = 60
path = 150 + np.cumsum(rng2.normal(0.012, 0.11, m))
entry = path[0]
fig, ax = plt.subplots(figsize=(7.2, 3.4))
ax.plot(path, color=INK, linewidth=1.5)
ax.axhline(entry, color=AXIS, linewidth=1, linestyle=(0, (4, 3)))
hi_i, lo_i = int(np.argmax(path)), int(np.argmin(path))
ax.plot(hi_i, path[hi_i], "o", color=UP, markersize=7, zorder=5)
ax.plot(lo_i, path[lo_i], "o", color=DOWN, markersize=7, zorder=5)
ax.annotate("", xy=(hi_i, path[hi_i]), xytext=(hi_i, entry),
            arrowprops=dict(arrowstyle="<->", color=UP, linewidth=1.4))
ax.annotate("", xy=(lo_i, path[lo_i]), xytext=(lo_i, entry),
            arrowprops=dict(arrowstyle="<->", color=DOWN, linewidth=1.4))
box = dict(facecolor=SURFACE, edgecolor="none", pad=2.0, alpha=0.92)
ax.text(hi_i - 2.0, (path[hi_i] + entry) / 2,
        "MFE\nthe best it ever got\n(what a target\ncould have caught)",
        color=UP, size=8.5, va="center", ha="right", bbox=box, zorder=6)
ax.text(lo_i + 2.0, (path[lo_i] + entry) / 2 - 0.04,
        "MAE\nthe worst it ever got\n(how deep a stop\nwould have to sit)",
        color=DOWN, size=8.5, va="center", ha="left", bbox=box, zorder=6)
ax.text(0.5, entry + 0.05, "you bought here", size=8.5, color=INK_2,
        bbox=box, zorder=6)
ax.set_ylim(path.min() - 0.12, path.max() + 0.12)
ax.set_xlabel("minutes after entry")
ax.set_ylabel("Price")
ax.spines[["top", "right"]].set_visible(False)
ax.set_title("One trade, measured both ways", loc="left", size=10.5, color=INK)
save(fig, "fig_mfe_mae.png")

# --- 4. the symmetry -------------------------------------------------------
horizons = [15, 30, 60]
mfe = [df[f"mfe_{h}"].median() for h in horizons]
mae = [abs(df[f"mae_{h}"].median()) for h in horizons]
x = np.arange(len(horizons))
fig, ax = plt.subplots(figsize=(7.2, 3.2))
ax.bar(x - 0.19, mfe, 0.38, color=UP, label="median best case (MFE)")
ax.bar(x + 0.19, mae, 0.38, color=DOWN, label="median worst case (MAE)")
for i, (a, b) in enumerate(zip(mfe, mae)):
    ax.text(i - 0.19, a + 0.015, f"{a:.2f}%", ha="center", size=8.5, color=INK_2)
    ax.text(i + 0.19, b + 0.015, f"{b:.2f}%", ha="center", size=8.5, color=INK_2)
ax.set_xticks(x)
ax.set_xticklabels([f"{h} minutes after the signal" for h in horizons])
ax.set_ylabel("% from entry")
ax.legend(frameon=False, fontsize=8.5, loc="upper left")
ax.set_ylim(0, max(mfe + mae) * 1.30)
ax.spines[["top", "right"]].set_visible(False)
ax.set_title("1,040 signals: it went as far against you as for you",
             loc="left", size=10.5, color=INK)
save(fig, "fig_symmetry.png")

# --- 5. the clock ----------------------------------------------------------
slots = ["09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
         "13:00", "13:30", "14:00", "14:30", "15:00", "15:30"]
swings = [1279, 760, 480, 342, 312, 290, 237, 206, 223, 205, 185, 284, 471]
signals = [65, 81, 80, 81, 83, 80, 73, 70, 94, 84, 87, 81, 85]
share_sw = [100 * v / sum(swings) for v in swings]
share_si = [100 * v / sum(signals) for v in signals]
x = np.arange(len(slots))
fig, ax = plt.subplots(figsize=(7.4, 3.4))
ax.bar(x - 0.19, share_sw, 0.38, color=ACCENT, label="share of the day's movement")
ax.bar(x + 0.19, share_si, 0.38, color=SECOND, label="share of the day's alerts")
ax.axvspan(-0.5, 2.5, color=UP, alpha=0.06)
ax.axvspan(6.5, 10.5, color=DOWN, alpha=0.06)
ax.text(1.0, 22, "most movement\nfewest alerts", ha="center", size=8.5, color=INK_2)
ax.text(8.5, 14.5, "fewest moves\nmost alerts", ha="center", size=8.5, color=INK_2)
ax.set_xticks(x)
ax.set_xticklabels(slots, rotation=45, size=8)
ax.set_ylabel("% of the day")
ax.legend(frameon=False, fontsize=8.5, loc="upper right")
ax.set_ylim(0, 27)
ax.spines[["top", "right"]].set_visible(False)
ax.set_title("The signal has no idea what time it is", loc="left", size=10.5, color=INK)
save(fig, "fig_clock.png")

# --- 6. three days ---------------------------------------------------------
morn = df[MORNING].groupby("date")["ret_60"].sum().sort_values(ascending=False)
cum = morn.cumsum()
fig, ax = plt.subplots(figsize=(7.2, 3.2))
ax.plot(range(1, len(cum) + 1), cum.values, color=ACCENT, linewidth=1.8)
ax.axhline(morn.sum(), color=AXIS, linewidth=1, linestyle=(0, (4, 3)))
ax.plot(3, cum.values[2], "o", color=DOWN, markersize=8, zorder=5)
ax.annotate(f"the best 3 days alone: {cum.values[2]:.0f}%\n"
            f"= {100 * cum.values[2] / morn.sum():.0f}% of the 69-day total",
            xy=(3, cum.values[2]), xytext=(13, cum.values[2] + 6), size=9,
            color=DOWN, arrowprops=dict(arrowstyle="->", color=DOWN, linewidth=1.2))
ax.text(len(cum) - 1, morn.sum() + 2.5, f"all 69 days: {morn.sum():.0f}%",
        ha="right", size=9, color=INK_2)
ax.set_xlabel("days, ranked best to worst")
ax.set_ylabel("running total (%)")
ax.spines[["top", "right"]].set_visible(False)
ax.set_title("Three days carried the whole morning result — and then some",
             loc="left", size=10.5, color=INK)
save(fig, "fig_concentration.png")

# --- 7. the stop curve -----------------------------------------------------
winners = df[df["ret_60"] > 0.5]
stops = np.arange(0.25, 2.01, 0.05)
kept = [(winners["mae_60"] > -st).mean() * 100 for st in stops]
fig, ax = plt.subplots(figsize=(7.2, 3.3))
ax.plot(stops, kept, color=ACCENT, linewidth=2)
for st in (0.5, 0.75, 1.0):
    v = (winners["mae_60"] > -st).mean() * 100
    ax.plot(st, v, "o", color=SECOND if st == 0.75 else INK_2,
            markersize=9 if st == 0.75 else 6, zorder=5)
    ax.annotate(f"{st:.2f}% stop\nkeeps {v:.0f}%", xy=(st, v),
                xytext=(st + 0.06, v - 11), size=8.5,
                color=SECOND if st == 0.75 else INK_2)
ax.set_xlabel("how far below your entry the stop sits (%)")
ax.set_ylabel("% of winning trades kept")
ax.set_ylim(55, 102)
ax.spines[["top", "right"]].set_visible(False)
ax.set_title(f"Of the {len(winners)} trades that worked, how many a stop would "
             "have survived", loc="left", size=10.5, color=INK)
save(fig, "fig_stop.png")
print("figures 3-7 done")
