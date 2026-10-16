"""The SPCX primer: what the words mean, and what we found."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether,
                                NextPageTemplate, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

INK = colors.HexColor("#0b0b0b")
INK_2 = colors.HexColor("#52514e")
MUTED = colors.HexColor("#898781")
RULE = colors.HexColor("#c3c2b7")
PLANE = colors.HexColor("#f5f5f1")
ACCENT = colors.HexColor("#2a78d6")
SECOND = colors.HexColor("#eb6834")
UP = colors.HexColor("#0ca30c")
DOWN = colors.HexColor("#d03b3b")

PAGE_W, PAGE_H = A4
MARGIN = 21 * mm
COL_W = PAGE_W - 2 * MARGIN

ss = getSampleStyleSheet()


def style(name, **kw):
    base = dict(fontName="Times-Roman", fontSize=10.3, leading=15.2,
                textColor=INK, spaceAfter=7)
    base.update(kw)
    return ParagraphStyle(name, **base)


BODY = style("body", alignment=TA_JUSTIFY)
LEAD = style("lead", fontSize=11.6, leading=17, textColor=INK_2, spaceAfter=11)
H1 = style("h1", fontName="Helvetica-Bold", fontSize=17, leading=21,
           spaceBefore=4, spaceAfter=3,
           borderPadding=(0, 0, 7, 0), borderWidth=0)
H2 = style("h2", fontName="Helvetica-Bold", fontSize=11.6, leading=15,
           spaceBefore=13, spaceAfter=4, textColor=INK)
KICKER = style("kicker", fontName="Helvetica-Bold", fontSize=8.2, leading=11,
               textColor=ACCENT, spaceAfter=2)
CAPTION = style("caption", fontSize=8.6, leading=12, textColor=MUTED,
                spaceBefore=3, spaceAfter=13)
TERM = style("term", fontSize=10.0, leading=14, spaceAfter=5)
PULL = style("pull", fontName="Times-Italic", fontSize=11.4, leading=16.5,
             textColor=INK, spaceBefore=6, spaceAfter=10,
             leftIndent=10, rightIndent=10)
COVER_T = style("ct", fontName="Helvetica-Bold", fontSize=30, leading=35,
                spaceAfter=8)
COVER_S = style("cs", fontSize=13.5, leading=19, textColor=INK_2, spaceAfter=20)


def figure(path, caption, width=COL_W):
    from PIL import Image as PILImage
    w, h = PILImage.open(path).size
    img = Image(path, width=width, height=width * h / w)
    return KeepTogether([img, Paragraph(caption, CAPTION)])


def callout(title, body, tone=ACCENT):
    inner = [[Paragraph(f'<font color="{tone}"><b>{title}</b></font>',
                        style("ci", fontName="Helvetica-Bold", fontSize=9.4,
                              leading=12, spaceAfter=4)),],
             [Paragraph(body, style("cb", fontSize=9.6, leading=13.8))]]
    t = Table(inner, colWidths=[COL_W - 16])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PLANE),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (0, 0), 9),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, tone),
    ]))
    return KeepTogether([Spacer(1, 4), t, Spacer(1, 11)])


def datatable(rows, widths, align_right=()):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.2),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK_2),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for c in align_right:
        cmds.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return KeepTogether([Spacer(1, 3), t, Spacer(1, 11)])


def glossary(pairs):
    out = []
    for term, meaning in pairs:
        out.append(Paragraph(f"<b>{term}</b> &nbsp;&nbsp;{meaning}", TERM))
    return out


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------

def chrome(canvas, doc):
    canvas.saveState()
    if doc.page > 1:
        canvas.setFont("Helvetica", 7.6)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 13 * mm, "SPCX — a working primer")
        canvas.drawRightString(PAGE_W - MARGIN, 13 * mm, str(doc.page - 1))
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, 16.5 * mm, PAGE_W - MARGIN, 16.5 * mm)
    canvas.restoreState()


doc = BaseDocTemplate("SPCX_primer.pdf", pagesize=A4,
                      leftMargin=MARGIN, rightMargin=MARGIN,
                      topMargin=MARGIN, bottomMargin=23 * mm,
                      title="SPCX — a working primer",
                      author="Feed-Check", subject="Read-only market research")
frame = Frame(MARGIN, 23 * mm, COL_W, PAGE_H - MARGIN - 23 * mm, id="main")
doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=chrome)])

S = []
P = lambda text, st=BODY: S.append(Paragraph(text, st))


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------
S.append(Spacer(1, 42 * mm))
P("SPCX", COVER_T)
P("A working primer: what the words mean, "
  "what we measured, and what it actually showed.", COVER_S)
S.append(Spacer(1, 4))
cover = Table([[Paragraph(
    "Written for someone learning to trade. Every term is explained the first "
    "time it appears, and again in the glossary at the back. Nothing here is "
    "advice about what to buy — it is a record of what ninety days of SPCX "
    "price data said when we asked it careful questions.", LEAD)]],
    colWidths=[COL_W])
cover.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 2, ACCENT),
                           ("TOPPADDING", (0, 0), (-1, -1), 14),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
S.append(cover)
S.append(Spacer(1, 26 * mm))
P("24 September 2026 &nbsp;·&nbsp; 70 trading sessions, 12 June to 22 September "
  "&nbsp;·&nbsp; 1,040 signals &nbsp;·&nbsp; 27,164 one-minute bars",
  style("meta", fontSize=9, textColor=MUTED))
S.append(Spacer(1, 16))


# ---------------------------------------------------------------------------
# What this is
# ---------------------------------------------------------------------------
P("Start here", KICKER)
P("What this document is", H1)
S.append(Spacer(1, 6))
P("You asked me to explain the findings in terms you could follow, and to "
  "teach the vocabulary so you can think and talk like a trader. This is that. "
  "It is in two halves.")
P("<b>The first half teaches the language.</b> A price chart, the two "
  "indicators we use, and the handful of measurements that decide whether a "
  "trading idea is any good. If you understand the second half of this "
  "document, you will understand most of what traders say to each other.")
P("<b>The second half is what the data said.</b> Four findings, three of them "
  "negative. I have put them in plainly, because a tool that tells you what "
  "you hoped to hear is worth less than nothing.")
P("One honest note before we start. The main idea we tested — a signal built "
  "from something called the MACD — did not work. It has now failed three "
  "separate tests. That is not a failure of the tools; it is the tools doing "
  "their job. Most trading ideas do not work, and the ones that look best on "
  "a chart are usually the ones that fool you hardest. Finding that out from "
  "ninety days of data is enormously cheaper than finding it out from ninety "
  "days of trading.")
S.append(callout(
    "The one thing worth taking away",
    "Of the trades that eventually worked, the typical one dipped only "
    "<b>0.27%</b> against you first. A stop placed <b>0.75%</b> below your "
    "entry would have kept you in <b>88%</b> of them. That is a real, usable "
    "number, and it came out of the losing study. Section 9 explains it.", UP))
S.append(PageBreak())

# ---------------------------------------------------------------------------
# 1. The chart
# ---------------------------------------------------------------------------
P("Part one — the language", KICKER)
P("1. How to read a price chart", H1)
S.append(Spacer(1, 6))
P("A <b>bar</b> — or <b>candle</b>, the same thing drawn prettier — is a "
  "summary of one slice of time. We use one-minute candles, so each one "
  "answers four questions about a single minute: where it started, where it "
  "ended, the highest anyone paid, and the lowest.")
P("Those four numbers have names, and traders use them constantly: the "
  "<b>open</b>, the <b>close</b>, the <b>high</b> and the <b>low</b>. You will "
  "see them written as OHLC.")
S.append(figure("fig_candle.png",
                "Green when the close is above the open, red when it is below. "
                "That colour convention is universal — every platform you will "
                "ever use draws it this way."))
P("The <b>body</b> is the thick part, from open to close. The <b>wicks</b> "
  "(also called shadows or tails) are the thin lines showing how far price "
  "got before coming back. A long upper wick means buyers pushed the price up "
  "and then lost it again within that minute — often a sign that sellers were "
  "waiting up there.")
P("You saw this on 18 September: the 09:30 candle ran up more than two dollars "
  "and closed 98 cents off its high. That 98 cents was a wick, and the six red "
  "candles that followed suggested it had been telling the truth.")

P("Volume", H2)
P("<b>Volume</b> is how many shares changed hands. It is the second dimension "
  "of every chart and the one beginners ignore. Price tells you where; volume "
  "tells you how much conviction was behind it. A one-percent move on tiny "
  "volume and a one-percent move on enormous volume are different events, "
  "even though the chart line looks identical.")
P("Raw volume on its own is nearly useless, because volume is wildly uneven "
  "through the day — the first minutes after the open routinely carry fifty "
  "times what a quiet minute at lunchtime does. So we always compare a minute "
  "against <b>the same clock minute on previous days</b>. That is what your "
  "alerts mean when they say <i>1.8× usual for 09:35</i>: nearly twice the "
  "shares that 09:35 normally carries.")
S.append(callout(
    "Why the comparison has to be time-of-day",
    "An earlier version of this idea compared each minute against a rolling "
    "average of the morning so far. It looked reasonable and was badly wrong: "
    "it turned the question <i>is this minute unusually busy?</i> into "
    "<i>is it near the open yet?</i>, because the open is always busier than "
    "what came before it. Comparing 09:35 against previous 09:35s is the only "
    "honest version."))

P("The session", H2)
P("The <b>regular session</b> runs 09:30 to 16:00 Eastern. Before and after "
  "that is <b>pre-market</b> and <b>after-hours</b> (together, <b>extended "
  "hours</b>) — trading still happens, but thinly, and the rules are "
  "different. Your tools start reading at 09:25 deliberately, to show you the "
  "last five minutes of positioning before the bell.")
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 2. VWAP
# ---------------------------------------------------------------------------
P("Part one — the language", KICKER)
P("2. VWAP: the day's average price", H1)
S.append(Spacer(1, 6))
P("<b>VWAP</b> stands for volume-weighted average price. It answers: across "
  "everyone who has traded this stock today, what is the average price they "
  "paid?")
P("Weighted by volume means a trade of ten thousand shares counts ten times as "
  "much as a trade of a thousand. So VWAP is not the average of the price "
  "line; it is the average of the <i>money</i>.")
P("Traders watch it for a simple reason: it is a scoreboard. If you bought "
  "this morning and price is above VWAP, you are doing better than the average "
  "buyer today. Large institutions are often measured against VWAP explicitly "
  "— which means they have a real incentive to buy under it and sell over it, "
  "and that makes it a line where things happen.")
S.append(callout(
    "One detail that matters, and that we got right",
    "VWAP <b>resets every morning</b>. It is the average for <i>this day</i>, "
    "not a rolling average of the last few hundred minutes. Get that wrong and "
    "your line drifts further from the real one every day you run — and it "
    "stops being the line anyone else is looking at, which was the entire "
    "point of drawing it."))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 3. MACD
# ---------------------------------------------------------------------------
P("Part one — the language", KICKER)
P("3. The MACD", H1)
S.append(Spacer(1, 6))
P("The <b>MACD</b> — moving average convergence divergence, a name nobody "
  "enjoys — is built in three steps, and each one is simple.")
P("<b>Step one: two averages.</b> Take the average price over the last 9 "
  "minutes, and the average over the last 17. Both follow the price, but the "
  "9-minute one turns faster because it has less history weighing it down.")
P("<b>Step two: subtract them.</b> Fast minus slow. That difference is the "
  "MACD line. When price accelerates upward the fast average pulls away from "
  "the slow one and the line rises. When price stalls, they converge and it "
  "falls back toward zero. Above zero means the short term is running ahead of "
  "the longer term; below zero, behind.")
P("<b>Step three: smooth it.</b> Take a 6-minute average <i>of the MACD line "
  "itself</i>. That is the <b>signal line</b>. When the MACD line crosses up "
  "through it, the recent trend has turned up faster than its own recent "
  "average — that is a <b>crossover</b>, and it is what your alert fires on.")
S.append(figure("fig_macd.png",
                "Top: price and its two averages. Bottom: the gap between "
                "them (the MACD line), a smoothed copy of that gap (the signal "
                "line), and the difference between those two drawn as bars — "
                "the histogram. The dots mark crossovers."))
P("The numbers 9, 17 and 6 are your settings. The conventional ones are 12, 26 "
  "and 9. There is no magic in either set, and this matters more than it "
  "sounds: <b>the periods are counted in bars, not minutes.</b> Your 9/17/6 on "
  "one-minute bars means 9, 17 and 6 minutes. The exact same settings on "
  "five-minute bars would mean 45, 85 and 30 minutes — a completely different "
  "indicator wearing the same name.")
S.append(callout(
    "Why the chart in your PDF uses one-minute bars",
    "Your report draws five-minute candles, but the MACD panel underneath is "
    "computed on one-minute bars. That is deliberate. If it were recomputed on "
    "the five-minute candles it would be a plausible-looking line with no "
    "relationship to your alerts, and its crossovers would not sit under your "
    "signal markers. Same function, same numbers, same line the alert read."))
P("Divergence", H2)
P("You used the word <b>divergence</b> once, and it is worth knowing you meant "
  "something we have not built. Divergence is when price makes a new low but "
  "the MACD makes a <i>higher</i> low — the selling is continuing but with "
  "less force behind it each time. It is a genuine and more advanced idea. "
  "What your alert actually fires on is the crossover described above.")
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 4. Judging a signal
# ---------------------------------------------------------------------------
P("Part one — the language", KICKER)
P("4. How you judge whether a signal is any good", H1)
S.append(Spacer(1, 6))
P("This is the section that matters most, and it is the vocabulary you were "
  "missing. Everything in the second half of this document is built from these "
  "five ideas.")

P("MFE and MAE", H2)
P("Suppose you buy at 150.23. Over the next hour price wanders: down to "
  "149.62, then up to 150.80, then back to 150.63 where you are looking at it.")
P("<b>MFE</b> — maximum favourable excursion — is the best it ever got. Here, "
  "150.80, or +0.38%. It is the ceiling on what any target could have caught. "
  "Set a target above the typical MFE and it simply never fills.")
P("<b>MAE</b> — maximum adverse excursion — is the worst it ever got. Here, "
  "149.62, or −0.41%. It is how deep a stop would have needed to sit to "
  "survive this trade. Set a stop inside the typical MAE and you get thrown "
  "out of trades that were about to work.")
S.append(figure("fig_mfe_mae.png",
                "The same trade, measured both ways. MFE and MAE are simply "
                "the best and worst points reached — they say nothing about "
                "where it finished."))
P("These two numbers, measured across a thousand trades, are how you size a "
  "stop and a target from evidence rather than from a round number that feels "
  "about right.")

P("Stop, target, bracket", H2)
P("A <b>stop</b> (stop-loss) is an order that sells automatically if price "
  "falls to a level you set. A <b>target</b> (or limit sell) sells "
  "automatically if price rises to a level you set. Together they form a "
  "<b>bracket</b>: your trade now has a floor and a ceiling, and neither "
  "requires you to be watching.")
S.append(callout(
    "This is the one that cost you money",
    "A stop you intended to place is not a stop. The whole value of a resting "
    "stop order is that it works while your attention is elsewhere — at your "
    "job, asleep, or on a phone with no signal. Most brokers let you attach "
    "the stop and target to the entry so all three go in as one action. If "
    "forgetting is possible, it will eventually happen; make it impossible "
    "instead of resolving to remember.", DOWN))

P("Edge, spread, and slippage", H2)
P("An <b>edge</b> is the amount by which a method beats doing nothing "
  "thoughtful. It is always a comparison, never an absolute — and picking "
  "what you compare against is where most self-deception happens.")
P("The <b>spread</b> is the gap between the highest price anyone is currently "
  "willing to pay and the lowest anyone will sell for. You buy at the higher "
  "one and sell at the lower one, so the spread is a cost you pay on every "
  "round trip whether or not your broker charges commission. "
  "<b>Slippage</b> is the extra you lose when your order fills at a slightly "
  "worse price than you saw — normal when things are moving fast.")
P("Together these set a floor. On a $150 stock, a penny or two of spread is "
  "roughly 0.01%. So an edge of 0.004% per trade is not a small edge; it is "
  "<b>nothing at all</b>, buried inside a cost you cannot avoid. This is why "
  "the studies keep saying an edge under 0.05% is noise.")

P("The control", H2)
P("A trading idea can only be judged against an alternative. Ours is "
  "deliberately stupid: <b>buy every fifth bar, regardless of anything.</b> "
  "No signal, no thought, no conditions.")
P("This is the single most important piece of machinery in the whole project. "
  "A stock that drifts upward over ninety days will make almost any buying "
  "rule look profitable — including one that ignores the chart entirely. "
  "Without the dumb version to compare against, you have no way to tell "
  "whether your clever idea contributed anything, or whether you simply "
  "measured the drift and called it a strategy.")
S.append(PULL and Paragraph(
    "If your method cannot beat buying at random, then whatever it is "
    "detecting, it is not something that makes money.", PULL))
S.append(PageBreak())

# ---------------------------------------------------------------------------
# 5. Finding one
# ---------------------------------------------------------------------------
P("Part two — what the data said", KICKER)
P("5. The signal is a coin flip", H1)
S.append(Spacer(1, 6))
P("Across 70 sessions the MACD crossover fired 1,040 times — about 15 a day. "
  "For each one we measured how far price went in each direction over the next "
  "15, 30 and 60 minutes.")
S.append(figure("fig_symmetry.png",
                "Median best case against median worst case. If the signal "
                "found anything, the green bars would stand taller than the "
                "red ones."))
P("They are the same height. At every horizon, the typical signal went almost "
  "exactly as far against you as it went for you. An hour later, price was "
  "higher <b>51%</b> of the time — which is a coin flip with a rounding error.")
P("This pattern has a name: it is what a <b>random walk</b> looks like. A "
  "random walk is a price that moves by an unpredictable amount each minute, "
  "with no memory of what it just did. You cannot forecast one, and every "
  "measurement you take of one comes back symmetric — exactly as these did.")

P("It also loses to the dumb version", H2)
P("We then tried every combination of stop and target — six stops against six "
  "targets, thirty-six brackets — on the signals, and the same thirty-six on "
  "the buy-every-fifth-bar control.")
S.append(datatable([
    ["", "Best bracket found", "Average per trade"],
    ["On the MACD signals", "0.75% stop / 3.00% target", "+0.107%"],
    ["Buying at random", "1.00% stop / 3.00% target", "+0.111%"],
    ["The signal's edge", "", "−0.004%"],
], [COL_W * 0.34, COL_W * 0.38, COL_W * 0.28], align_right=(2,)))
P("The signal lost to entering at random. And the +0.107% flatters it, because "
  "it is the winner of thirty-six attempts on the same data — the best of "
  "thirty-six coin-flip experiments always looks better than it is.")
P("This is now the third time we have asked, on 29 days, then 69, then 90. It "
  "has failed every time. The first test produced a 54% figure that looked "
  "promising; it did not survive more data. That is the normal life cycle of a "
  "trading idea, and recognising it early is the skill.")
S.append(callout(
    "What this does not mean",
    "It does not mean the alert is useless to <i>you</i>. These studies "
    "measure the signal as an autopilot — buy every time it fires, with a "
    "fixed bracket, no judgement. You are not doing that. You use it to decide "
    "where to look, and then you decide. What the studies say is narrower and "
    "still important: <b>do not take a trade because the MACD fired.</b> Take "
    "it because of what you see when you get there."))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 6. Finding two
# ---------------------------------------------------------------------------
P("Part two — what the data said", KICKER)
P("6. The signal has no idea what time it is", H1)
S.append(Spacer(1, 6))
P("This is the finding that changed how your tools behave, and the only "
  "structural thing we found.")
P("SPCX does not move evenly through the day. The average one-minute candle "
  "spans 0.585% of price in the half hour after the open, and 0.181% around "
  "14:30 — the stock is three times livelier in the morning. Every trader "
  "knows this in the abstract; here it is measured on your stock.")
P("The MACD does not know it. It fires at a flat rate all day.")
S.append(figure("fig_clock.png",
                "Blue: where the day's movement actually is. Orange: where "
                "your alerts arrive. They are almost mirror images."))
S.append(datatable([
    ["Stretch", "Share of movement", "Share of alerts"],
    ["09:30–11:00", "48%", "22%"],
    ["13:00–14:30", "16%", "32%"],
], [COL_W * 0.34, COL_W * 0.33, COL_W * 0.33], align_right=(1, 2)))
P("The 13:30 half hour produces more alerts than any other slot in the day — "
  "94 of them — during the stretch where the stock moves least. Those are the "
  "ones pulling you away from your job to look at nothing.")
P("Worse, the afternoon signals were not merely fewer-moving but worse: "
  "13:00 through 14:30 is four consecutive half hours where the typical signal "
  "went <i>further against you than for you</i>. One bad slot would be noise. "
  "Four adjacent ones agreeing is a pattern.")

P("What we changed", H2)
P("Your alerts now run <b>09:40 to 11:00</b> and go quiet after. That takes "
  "you from about 15 alerts a session to about 3, while keeping well over a "
  "third of the day's movement in view.")
P("Everything is still measured and written to the database after 11:00 — the "
  "gate decides what buzzes your phone, never what gets recorded. If the data "
  "later says the window is wrong, we will have the data to say so.")
S.append(callout(
    "Be clear about what this buys",
    "A quieter phone, not a better entry. We tested the gate with the control "
    "restricted to the same hours — because narrowing to the busiest part of "
    "the day raises returns by itself, and without that control we would have "
    "measured the time of day and called it a signal. With the control in "
    "place, no window beat random. Three alerts you read beat fifteen you "
    "learn to ignore; that is the entire argument, and it does not need an "
    "edge to be true."))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 7. Finding three
# ---------------------------------------------------------------------------
P("Part two — what the data said", KICKER)
P("7. Three days out of sixty-nine", H1)
S.append(Spacer(1, 6))
P("When we restricted to the morning window, one measurement came back "
  "faintly positive. Before getting attached to it, we asked a question you "
  "should always ask of a good-looking result: <b>where did it come from?</b>")
S.append(figure("fig_concentration.png",
                "Every morning-window day, ranked best to worst, added up one "
                "at a time. A healthy result climbs steadily. This one is "
                "carried by its first three days and gives most of it back."))
P("Adding up every morning signal's one-hour return across 69 days gives "
  "+27.8%. The best three days alone give +36.2% — <b>130% of the total.</b> "
  "Remove 28 July, 8 September and 30 June and the whole thing is negative.")
P("The median day contributed +0.23%. Fifty-two per cent of days were positive. "
  "That is a coin flip with three lucky Tuesdays on top.")
P("This is <b>concentration</b>, and checking for it is one of the most useful "
  "habits you can build. An average is a single number that hides its own "
  "story. Two strategies can report the same average return where one made a "
  "little on most days and the other made nothing on 66 days and a fortune on "
  "three. The first is a strategy. The second is a lottery ticket you have "
  "already scratched.")
S.append(callout(
    "The question to ask of any good result",
    "<i>If I remove the best three days, is it still there?</i> If the answer "
    "is no, you have not found a method. You have found three days."))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 8. Finding four — the useful one
# ---------------------------------------------------------------------------
P("Part two — what the data said", KICKER)
P("8. Where the stop goes", H1)
S.append(Spacer(1, 6))
P("This is the one finding you can act on tomorrow, and it corrects advice I "
  "gave you earlier in the week.")
P("I had told you a 0.5% stop would take you out of half your trades, based on "
  "the median MAE across all 1,040 signals — about −0.48%. That number is "
  "true and it is the wrong number, because it is dominated by the losers. "
  "Losing trades dig deep by definition; including them tells you where "
  "<i>failures</i> go, not where winners go.")
P("The right question is narrower: <b>of the trades that ended up working, how "
  "far did they dip first?</b> The answer is much more encouraging.")
S.append(figure("fig_stop.png",
                "318 of the 1,040 signals finished at least 0.5% up an hour "
                "later. This is how many of those a stop at each distance "
                "would have survived."))
S.append(datatable([
    ["Stop distance", "Winners kept", "In the 09:40–11:00 window"],
    ["0.50%", "74%", "69%"],
    ["0.75%", "88%", "87%"],
    ["1.00%", "93%", "90%"],
    ["1.50%", "96%", "94%"],
], [COL_W * 0.28, COL_W * 0.28, COL_W * 0.44], align_right=(1, 2)))
P("The median winner dipped only <b>0.27%</b> before going. Winners mostly "
  "just go — they do not typically spend twenty minutes underwater first. "
  "That is why the curve bends sharply and then flattens: past about 0.75% you "
  "are paying a lot more risk for very few extra winners.")
S.append(callout(
    "The recommendation, and its limits",
    "<b>0.75% below your entry</b> keeps you in 88% of the trades that work "
    "while cutting losers far sooner than 1% does. Then size the position so "
    "that a 0.75% loss is a dollar amount you are genuinely fine losing — "
    "because roughly half of all entries will hit it.<br/><br/>"
    "What this does <b>not</b> do is make the entries good. A well-placed stop "
    "on a coin-flip entry is still a coin flip; it just fails cheaply and "
    "predictably instead of expensively and at random. That is worth a great "
    "deal, and it is not an edge.", UP))
S.append(PageBreak())

# ---------------------------------------------------------------------------
# 9. How not to fool yourself
# ---------------------------------------------------------------------------
P("Part three — the method", KICKER)
P("9. How not to fool yourself", H1)
S.append(Spacer(1, 6))
P("Everything above rests on a handful of disciplines. They are the real skill "
  "in this work, they are learnable, and they are what separates a trader with "
  "a method from a trader with a story.")

P("In-sample and out-of-sample", H2)
P("Data you have already looked at is <b>in-sample</b>. Data you have not is "
  "<b>out-of-sample</b>. Any rule invented by staring at in-sample data will "
  "fit it well, because you built it to. The only honest test is on data the "
  "rule has never seen.")
P("This is the difference between <i>explaining</i> the past and "
  "<i>predicting</i> the future, and almost every failed trading system in "
  "existence confused the two.")

P("Multiple comparisons", H2)
P("Test one idea at the 5% threshold and you have a one-in-twenty chance of a "
  "false positive. Test twenty ideas and you should <i>expect</i> one to look "
  "good by luck alone. Report only that one and you have produced a discovery "
  "out of nothing.")
P("We searched thirty-six brackets in section 5. We looked at thirteen "
  "half-hour slots in section 6. If I told you the best of those was "
  "meaningful without mentioning how many I tried, I would be lying to you "
  "with true numbers.")

P("Pre-registration", H2)
P("The fix is to write down the test <b>before</b> you run it: the rule, the "
  "bracket, what would count as success. Then run it once. If it fails, it "
  "failed — you do not get to adjust the rule until it passes.")
P("We did this with the first study. The pre-registered claim was that 54% of "
  "signals would still be higher an hour later. On more data it came back 51%. "
  "It failed, and saying so was the entire value of having written it down.")

P("A live example: the VWAP idea", H2)
P("Here is one working through the process right now, so you can watch the "
  "discipline being applied rather than just described.")
P("In the per-signal data, morning signals that fired <i>above</i> VWAP looked "
  "better than those below: best-case to worst-case ratio of 1.67 against "
  "0.79, and higher an hour later 58% of the time against 48%.")
P("That is the most interesting thing in the file. It is also exactly the "
  "shape of the 54% that already fooled us once: about a hundred observations, "
  "one of four cells examined, and roughly 1.7 standard errors from a coin "
  "flip — a gap that appears by chance more often than people expect.")
P("It does have a mechanism behind it, which is a point in its favour. VWAP is "
  "a line real institutions are measured against, so it is a place where "
  "behaviour genuinely changes. A pattern with a reason is more likely to be "
  "real than one without. But a plausible story is how you talk yourself into "
  "things, not how you test them.")
S.append(callout(
    "So it gets tested properly, or not at all",
    "The rule is fixed now: <b>only signals in 09:40–11:00 with price above "
    "VWAP, at a 0.75% stop and a 2.00% target</b>, against the same control "
    "restricted the same way. It will be run on trading days <b>before</b> "
    "12 June 2026 — days neither of us has looked at.<br/><br/>"
    "If it survives that, it is worth something. If it does not, it joins the "
    "54% and we say so and move on. What we will not do is search the existing "
    "ninety days for the version that looks best; that is not a test, it is a "
    "portrait."))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 10. Glossary
# ---------------------------------------------------------------------------
P("Reference", KICKER)
P("10. Glossary", H1)
S.append(Spacer(1, 8))
S.extend(glossary([
    ("After-hours", "Trading between 16:00 and 20:00 Eastern. Thin, and prices can move a long way on very little."),
    ("Bar / candle", "One slice of time summarised as open, high, low, close and volume. We use one minute."),
    ("Baseline / control", "The deliberately unsophisticated alternative your idea must beat. Ours buys every fifth bar regardless."),
    ("Body", "The thick part of a candle: open to close."),
    ("Bracket", "An entry with a stop and a target attached, so the trade is bounded without you watching."),
    ("Concentration", "When a result comes from a handful of days rather than steadily. A warning sign, not a detail."),
    ("Crossover", "The MACD line crossing up through its signal line. What your alert fires on."),
    ("Divergence", "Price making a new low while the indicator makes a higher low — weakening pressure. Not what we built."),
    ("Edge", "How much a method beats the control by. Always a comparison. Under 0.05% a trade, it is inside the spread and therefore nothing."),
    ("EMA", "Exponential moving average: an average that weights recent prices more heavily. The MACD is built from two of them."),
    ("Extended hours", "Pre-market and after-hours together — outside 09:30–16:00 Eastern."),
    ("Fill", "The actual execution of your order, at the actual price you got."),
    ("Higher after", "The share of signals where price was higher N minutes later. 50% is a coin flip."),
    ("Histogram", "The bars on a MACD panel: the gap between the MACD line and its signal line."),
    ("IEX / SIP", "Two market data feeds. IEX is one exchange, free. SIP is the full consolidated tape from all exchanges. Never compare a number from one against a baseline from the other."),
    ("In-sample", "Data you have already examined. Any rule fits it, because you built the rule on it."),
    ("Limit order", "An order to trade at a set price or better. Will not fill at a worse price; may not fill at all."),
    ("MACD", "Fast average minus slow average, plus a smoothed copy of that difference. A momentum indicator."),
    ("MAE", "Maximum adverse excursion: the worst price reached before the trade closed. Sizes your stop."),
    ("MFE", "Maximum favourable excursion: the best price reached. Caps what a target can catch."),
    ("Market order", "An order to trade immediately at whatever price is available. Fast, and you pay the spread."),
    ("Out-of-sample", "Data the rule has never seen. The only honest test."),
    ("Pre-market", "Trading between 04:00 and 09:30 Eastern."),
    ("Pre-registration", "Writing down the test before running it, so a failure cannot be quietly renamed a success."),
    ("Random walk", "A price with no memory of its last move. Unpredictable by construction, and it measures symmetric — which is what ours did."),
    ("Relative volume", "This minute's volume against what that same clock minute usually carries. Never against a rolling average."),
    ("Session", "The regular trading day, 09:30 to 16:00 Eastern."),
    ("Signal line", "A smoothed average of the MACD line. The line it crosses."),
    ("Slippage", "The difference between the price you expected and the price you got."),
    ("Spread", "The gap between the best bid and the best offer. A cost you pay on every round trip, commission or not."),
    ("Stop / stop-loss", "A resting order that sells if price falls to your level. Works whether or not you are watching. That is the point."),
    ("Target", "A resting order that sells if price rises to your level."),
    ("Volume", "Shares traded. Price says where; volume says with how much conviction."),
    ("VWAP", "Volume-weighted average price for the day. Resets each morning. A scoreboard institutions are measured against."),
    ("Wick / shadow", "The thin lines above and below a candle body: how far price got and came back from."),
]))
S.append(Spacer(1, 16))

# ---------------------------------------------------------------------------
# 11. Where we are
# ---------------------------------------------------------------------------
P("Reference", KICKER)
P("11. Where things stand", H1)
S.append(Spacer(1, 8))
S.append(datatable([
    ["", "Status"],
    ["MACD crossover as an entry signal", "Failed three tests. Do not trade it on its own."],
    ["Time-of-day gate, 09:40–11:00", "Shipped. Buys attention, not edge."],
    ["Stop at 0.75%", "Supported by the data. Use it."],
    ["Volume spike alarm", "Running. Threshold 1.5× is a starting guess, not a finding."],
    ["Morning candle stream", "Running. Descriptive, and never advisory."],
    ["VWAP filter", "Hypothesis. Pre-registered, not yet tested out of sample."],
    ["Your trades on the chart", "Waiting on an export from your broker."],
], [COL_W * 0.42, COL_W * 0.58]))
P("What the tools do", H2)
P("Three programs, all read-only. None of them can place an order; none of "
  "them knows your broker or your account. That was deliberate from the first "
  "day and has not changed.")
S.append(datatable([
    ["Program", "What it does"],
    ["open_candles.py", "Reads the tape to your phone. Full detail 09:25–10:00, then volume spikes only, to 16:00."],
    ["spcx_alert.py", "Watches the MACD. Alerts 09:40–11:00; records all day."],
    ["daily_report.py", "Builds the one-page PDF. Rebuilds every 15 minutes while the market is open."],
], [COL_W * 0.28, COL_W * 0.72]))
P("Closing", H2)
P("Three of the four findings in this document are negative, and I want to be "
  "plain about why that is not a disappointing result.")
P("You now know something about SPCX that most people trading it do not: that "
  "this particular signal, on this particular stock, is indistinguishable from "
  "chance, and that the busiest-looking part of the afternoon is where your "
  "attention is worth least. You learned it from ninety days of data rather "
  "than ninety days of losses. And you got one genuinely useful number out of "
  "it, which is where to put a stop.")
P("The machinery that produced these answers is the durable part. The next "
  "idea you have can be put through the same mill — control, concentration "
  "check, pre-registration, out-of-sample test — in an afternoon. That is the "
  "thing worth having. Not this signal; the ability to find out.")

doc.build(S)
print("SPCX_primer.pdf written")
