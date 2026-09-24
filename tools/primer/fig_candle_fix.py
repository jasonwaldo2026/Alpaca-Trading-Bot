exec(open("figures.py").read().split("# --- 1.")[0])
from matplotlib.patches import Rectangle

fig, ax = plt.subplots(figsize=(7.2, 3.5))
# Annotations run to the LEFT of the first candle, so nothing crosses the second.
for x, o, c, hi, lo, label in ((3.0, 10.2, 11.0, 11.3, 10.0, "rising"),
                               (4.3, 11.0, 10.3, 11.4, 10.1, "falling")):
    colour = UP if c >= o else DOWN
    ax.vlines(x, lo, hi, color=colour, linewidth=1.8)
    ax.add_patch(Rectangle((x - 0.24, min(o, c)), 0.48, abs(c - o),
                           facecolor=colour, edgecolor=colour))
    ax.text(x, 9.80, label, ha="center", size=9, color=INK_2)

for y, text in ((11.3, "high — the most it traded for"),
                (11.0, "close — where the minute ended"),
                (10.2, "open — where the minute began"),
                (10.0, "low — the least it traded for")):
    ax.annotate(text, xy=(2.90, y), xytext=(2.60, y), va="center", ha="right",
                size=8.8, color=INK_2,
                arrowprops=dict(arrowstyle="-", color=AXIS, linewidth=0.8))

ax.annotate("", xy=(4.55, 11.0), xytext=(4.55, 10.3),
            arrowprops=dict(arrowstyle="<->", color=INK_2, linewidth=1.0))
ax.text(4.72, 10.65, "BODY\nopen to close", size=8.8, color=INK_2, va="center")
ax.annotate("", xy=(4.55, 11.4), xytext=(4.55, 11.02),
            arrowprops=dict(arrowstyle="<->", color=INK_2, linewidth=1.0))
ax.text(4.72, 11.21, "WICK\nhow far it got,\nand came back from",
        size=8.8, color=INK_2, va="center")

ax.set_xlim(0.15, 6.5)
ax.set_ylim(9.68, 11.62)
ax.set_ylabel("Price")
ax.set_xticks([])
ax.spines[["top", "right", "bottom"]].set_visible(False)
ax.set_title("One candle = one slice of time", loc="left", size=10.5, color=INK)
save(fig, "fig_candle.png")
