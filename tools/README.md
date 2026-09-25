# SPCX day-trading tools

A set of standalone, read-only tools for day-trading one symbol on a
one-minute chart while holding down a job. They watch the tape, message a
phone when something is worth a look, and answer — with measurements
rather than opinions — whether any of it is worth acting on.

**Nothing here can place a trade.** Every file uses Alpaca's market-data
client only. There is no trading client and no order object anywhere in
the folder. Eight of the eleven modules go further and assert it: their
`--self-test` reads their own source and fails if the words
`TradingClient`, `submit_order` or `MarketOrderRequest` appear in it.
The three that do not carry that assertion are `feed_check.py`,
`swing_study.py` and `daily_report.py` — the last has no self-test at
all. Trades are placed by hand, in DAS.

---

## The tools

| File | What it does | Run during the session? |
|---|---|---|
| `feed_check.py` | IEX against the full SIP tape; the shared indicator and condition code everything else imports | no |
| `open_candles.py` | **The watcher.** Reads the tape to your phone, rebuilds the PDF, decides what makes a sound | **yes, all day** |
| `daily_report.py` | The session as a one-page PDF | rebuilt by the watcher |
| `spcx_alert.py` | The MACD alert, 09:40–11:00, and the Pushover sender everything uses | optional |
| `swing_study.py` | Does the signal mark anything worth acting on, and with what bracket | research |
| `benchmark_report.py` | The stock against the market, with share unlocks marked | research |
| `screener.py` | Which other symbols are worth the same attention | research |
| `session_clock.py` | The trading day as a clock face — where movement is, where signals fire | research |
| `bracket.py` | The stop, target and share count to type, from an entry and a risk budget | **before entering** |
| `lockups.py` + `lockups.json` | The share-unlock calendar | read by the others |
| `launches.py` + `launches.json` | The notable-launch calendar | read by the others |
| `primer/` | A 16-page PDF explaining the terms and the findings for a beginner | — |

They import each other. **Keep them in one folder** and keep them at the
same commit — see *Failure modes* below, because mixing versions is the
single most common thing that has gone wrong.

---

## Setup from nothing

### 1. Python and packages

Python 3.11 or later. Then:

```
pip install alpaca-py pandas matplotlib python-dotenv
```

`matplotlib` is only needed by the tools that draw. The watcher imports it
lazily, so a missing plotting stack costs a chart, not a morning.

### 2. Alpaca keys

A free account at alpaca.markets gives IEX data. A paid plan gives SIP,
the consolidated tape. Put the keys in a file called `.env` **in the same
folder as the tools**:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_DATA_FEED=iex
```

Set `ALPACA_DATA_FEED=sip` if you have the paid plan. The difference
matters more than it sounds — see *Feeds* below.

### 3. Pushover

Pushover is the phone notification service. Two different values, and
confusing them is the usual first failure:

- **User key** — identifies *you*. On the main page after logging in at
  pushover.net.
- **Application token** — identifies *this app*. Create an application
  (call it Feed-Check), and the token is on its page.

Add both to the same `.env`:

```
PUSHOVER_USER_KEY=...
PUSHOVER_APP_TOKEN=...
```

Then install the Pushover app on the phone and **sign it into the same
account**. Pushover accepts messages for an account with no attached
device and reports success, so a send that "worked" proves nothing on its
own — which is why `--test-push` asks who is listening before it sends
anything.

### 4. The alert sounds

Direction rides the sound, so you know which way to look before you have
looked at anything. Two custom sounds are uploaded to the Pushover
account:

| Name | Plays on |
|---|---|
| `Buy_Stock` | the buy side |
| `Sell_Positions` | the sell side |

To recreate them: record or find two short sounds, **convert to MP3**
(Pushover requires MP3, max 500 KB, max 30 seconds — and iOS will not
play anything longer), upload at pushover.net → Sounds, and give each a
name. The names in the code must match the account **character for
character**: a name that does not match is *not* an error — Pushover
delivers the message with your default sound and reports success.

Check what the account actually has:

```
python open_candles.py --list-sounds
```

Then send one of each kind to the phone:

```
python open_candles.py --test-push
```

Three arrive six seconds apart — a silent reading, a buy ring, a sell
ring — every title prefixed `TEST —` so none can be mistaken for a live
signal.

### 5. Verify the install

Every tool has an offline self-test. No network, no credentials:

```
python bracket.py --self-test
python feed_check.py --self-test
python open_candles.py --self-test
python spcx_alert.py --self-test
python swing_study.py --self-test
python benchmark_report.py --self-test
python screener.py --self-test
python launches.py --self-test
python session_clock.py --self-test
python lockups.py
```

All ten should pass before you rely on anything.

---

## The daily routine

One PowerShell window, from the tools folder:

```
python open_candles.py
```

That is the whole morning. It:

- reads one-minute bars from **09:15** to 16:00
- prints the unlock and launch calendars before the tape
- sends every reading to the phone **silently** (priority −1)
- rebuilds the session PDF every 15 minutes, and attaches it to a volume spike
- **makes a sound only** when the tape leans past a pressing band with real
  volume behind it, or price crosses VWAP on the same

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | print, send nothing |
| `--replay 2026-09-23` | read a past session instead of today |
| `--from 09:30 --until 11:00` | a different window |
| `--buy-sound NAME` / `--sell-sound NAME` | try a different sound without editing anything |
| `--detail-until 10:30` | how long the per-minute stream keeps reaching the phone |
| `--pdf-every 0` | stop rebuilding the PDF |
| `--volume-alert 2.0` | require more volume before a bar counts as a spike |

### The research tools, run when you want them

```
python bracket.py --entry 152.40 --risk 50
python swing_study.py --symbol SPCX --days 90
python benchmark_report.py
python session_clock.py
python session_clock.py --today
python screener.py --symbols TSLA,NVDA,COIN --pdf
python launches.py
python lockups.py
```

---

## What was measured, and what came back

These are the answers, so nobody has to re-derive them. Three of the four
are negative, which is the point of having measured at all.

**The MACD crossover is a coin flip.** Tested on 29 days, then 69, then
90. Median favourable and adverse excursions are near-symmetric at every
horizon (+0.35% / −0.34% at 15 minutes, +0.69% / −0.67% at 60). Price is
higher an hour later 51% of the time. The best of 36 brackets returned
+0.107% against +0.111% for entering every fifth bar regardless. A first
study's promising 54% did not survive more data.

**The signal has no idea what time it is.** The 09:30 half hour carries
24% of the day's swings and 6% of the signals; 13:00–14:30 carries 16% of
the movement and 32% of the signals, across four consecutive half hours
where the typical signal went further *against* than for. Alerts are
gated to 09:40–11:00 as a result — about 3 a session instead of 15. That
buys attention, not edge: with random entry restricted to the same
window as a control, no window beat chance.

**An apparent morning edge was three days out of 69.** The best three
days are 130% of the whole morning total. Remove them and it is negative.

**The one usable number — a 0.75% stop.** Of the 318 signals that
finished at least 0.5% up an hour later, the median dipped only 0.27%
first, so a 0.75% stop keeps 88% of them. This corrected earlier advice
of 1%, which came from the all-signal median and is dominated by losers.

**No out-of-sample past exists.** SPCX listed on 12 June 2026, so the
90-day sample is its entire history. The VWAP hypothesis in
`swing_study.py` is pre-registered in the source and can only be tested
forward, or on another symbol.

---

## Decisions that should not be quietly undone

Each of these was a bug once, or would be.

**A control in every comparison.** Entering every fifth bar regardless. A
drifting stock makes almost any rule look profitable without one. When a
test narrows — to a time window, say — **the control narrows identically**,
or the section measures the time of day and reports it as a signal.

**Under 0.05% is not an edge.** That is inside the spread. The verdict
says noise rather than naming a winner.

**Fewer than 20 trades prints "too few"**, not three decimal places.

**No look-ahead.** Entry fills at the *next* bar's open. A bar touching
both bracket levels resolves as the stop, because a one-minute bar does
not record which came first.

**Volume is compared against the same clock slot on recent sessions**,
never a rolling average of today. 09:35 and 14:35 are different animals;
comparing them turns "is this bar busy" into "is it the morning".

**VWAP is anchored to the session** and lives in exactly one function,
`feed_check.add_vwap()`. Two copies would eventually disagree, and the
disagreement would be invisible until it mattered.

**The alerts describe the day, not the bar.** A message used to read
identically whether the stock was up three percent or down three, because
every line described one candle in isolation.

**Sound is reserved for direction with participation behind it.**
Everything else arrives silent. A phone that shouts at every busy minute
is a phone whose shouting stops meaning anything.

**The pressing bands (≤30 / ≥70) were chosen by checking, not taste.**
Against 23 September 2026 — a session that fell 3.53% — the stricter
extremes (≤18 / ≥82) fired on *none* of that day's five volume alarms:
the phone would have stayed silent through the entire decline. The bands
fire once, at 13:50. That threshold is fitted to a single day, so the
self-test pins the 23 September outcome; moving the numbers silently
shows up as a failure.

**Charts are positioned by time, not by bar number.** A five-minute slot
that never traded leaves a gap where it happened rather than closing up
and shifting every later bar leftward.

**The axis spans the whole window from the first rebuild.** Candles keep
their true width all day and two rebuilds of a session are comparable.

**No dual axes.** Two measures of different scale get two panels. Sliding
one scale against another is the most reliable way to make a chart appear
to prove something.

**Sequential colour is one hue light to dark; diverging is two hues with
a neutral middle.** A hue at a diverging midpoint colours "balanced" as
though it meant something.

**Both calendars degrade to silence.** A missing or malformed
`lockups.json` or `launches.json` costs a line of output, never the
session. The whole suite still runs with both modules deleted.

**Launch dates are reported, never predicted**, with the source's own
confidence (`Go` / `TBC` / `TBD`) carried verbatim. Placeholder dates —
the schedule prints 31 December to mean "sometime in 2026" — are stored
undated and never drawn.

---

## Failure modes we actually hit

Every one of these cost real time. They are here so they cost less next
time.

**Mixed versions — five times.** The tools import each other and were
downloaded one file at a time. The worst case: a current `open_candles.py`
calling an older `send_pushover()` that took no `sound`. Python only
notices when the call runs, and live that happens *only when an alarm
fires* — so the watcher would have started cleanly, logged all morning,
and died at the first ring. There is now a startup guard that refuses to
run and names the file and the commit. **Always download the whole set at
one commit.**

**`.env` was only read on the fetch path.** `--test-push` reported the
keys MISSING while they were sitting in the file. Every entry point now
calls `load_env()` first.

**"Sent" but nothing arrived.** The Pushover key was valid with no device
attached. The API reports success. `registered_devices()` now asks who is
listening before anything is sent.

**A sound played that we did not choose.** Twice, for two different
reasons. First, a name that did not match the account — Pushover
substitutes the default and reports success. Second, and harder:
**Pushover's own per-device notification settings were enabled and
overriding the per-message sound.** `--list-sounds` proves the server has
the sound; only the handset can prove it will play it.

**Baseline feed mismatch.** A SIP baseline compared against IEX readings
produced `0.0× usual`, which meant the spike alarm could never fire. The
baseline now takes the same feed as the readings.

**A saturated column.** The screener's morning share was the morning's
high-to-low over the day's — which returns exactly 1.0 whenever both
extremes land before 11:00. Six of seven symbols clustered between 78%
and 100%. It measures distance travelled now.

**Shares per trade compared prices, not traders.** A $25 stock shows ten
times the share count of a $250 one for the same money. It is dollars per
trade now.

**A self-test that tested nothing.** Run as a script the module is
`__main__`, so importing it by its own name loaded a second copy and the
patch never reached the running code. The test passed without executing
its check. It patches `sys.modules[__name__]` now.

**PowerShell is not a Python prompt.** Sample alert text and code
snippets pasted into the terminal produce alarming red errors and mean
nothing. If a block starts with `python`, `cd` or `curl.exe`, it is to be
typed; anything else is being shown.

**`curl` in PowerShell is not curl.** It aliases `Invoke-WebRequest` and
`-o` is ambiguous. Use `curl.exe`.

---

## Feeds

The data feed changes what you see more than any setting here.

**IEX** (free, the default) is one venue. Pre-market is very thin —
whole minutes with no prints — so the 09:15–09:30 stretch will often look
empty. That is the feed, not the tool.

**SIP** (paid) is the consolidated tape, every venue. `feed_check.py`
exists to measure the gap between them on your own symbol.

Because Alpaca builds bars from trades, **a window with no trades yields
no bar**. Sparse pre-market stretches every rolling window beyond its
nominal span; `feed_check.py` reports the coverage.

---

## Rebuilding from scratch

```
git clone https://github.com/jasonwaldo2026/Alpaca-Trading-Bot
cd Alpaca-Trading-Bot/tools
pip install alpaca-py pandas matplotlib python-dotenv
```

Create `.env` as above, then run the nine self-tests. The tools are
standalone — they do not import the rest of the repository, and can be
copied into any folder together.

To fetch a single file at a known commit:

```
curl.exe -L -o open_candles.py https://raw.githubusercontent.com/jasonwaldo2026/Alpaca-Trading-Bot/<commit>/tools/open_candles.py
```

Check the byte count against the repository after every download. Size is
the cheapest way to catch a download that did not land, though note that
two versions can share a size — the self-test output is the better check.

---

## What this does not do

It does not predict. Nothing measured in this project beats a coin flip,
and no sound, colour or line here means "buy". The alerts say *something
is happening now, with participation behind it, in a direction you care
about* — and the judgement stays with whoever is reading them.
