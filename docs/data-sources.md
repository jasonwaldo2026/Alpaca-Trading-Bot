# What each data source can give us

A reference for deciding what the alert system should be built from.
Everything here was checked against the providers' own documentation on
2026-09-04. Plan limits change; re-check before relying on a number.

The system uses three services. They do not overlap much:

| Service | What it is for us | Cost we are on |
|---|---|---|
| **Alpaca** | The price tape: bars, trades, quotes, snapshots, "what is moving right now", the list of every tradable stock, and the paper-trading account | Free (Basic) — IEX feed only, 200 calls/min |
| **Financial Modeling Prep (FMP)** | Facts *about* a company that the tape does not carry: share float, market cap, sector, earnings dates, news, a stock screener, and long daily history | Depends on plan — see below; the free plan is 250 calls **per day** |
| **Pushover** | The phone. One-way: the scanner sends, the phone buzzes | Free — 10,000 messages/month |

Robinhood is not a data source. It is only the `robinhood://instrument/…`
link in the alert that opens the app on the symbol.

---

## Alpaca

Two APIs behind one key pair: **Market Data** (prices) and **Trading**
(account). Both work with the paper account.

### Market Data — stocks

| Endpoint | What it returns | Notes for alerts |
|---|---|---|
| **Bars** (`/v2/stocks/bars`) | Open, high, low, close, volume, **trade count**, **VWAP** per bar. Timeframes: any 1–59 min, 1–23 hour, day, week, month. Up to 10,000 bars per request (total, not per symbol); many symbols per request. Adjusted for splits/dividends on request. History back to 2016. | This is what the scanner runs on. Bars are built from trades, so a 5-minute window with no trades yields **no bar** — common pre-market on IEX. |
| **Snapshots** (`/v2/stocks/snapshots`) | For each symbol in one call: latest trade, latest quote (bid/ask), current minute bar, **today's daily bar so far**, previous day's daily bar. | The cheap way to ask "what has every stock done today" — today's volume and % change for thousands of symbols without downloading history. The natural first stage of a two-stage scan. |
| **Latest bar / trade / quote** | The single most recent bar, trade, or quote per symbol. | Real-time on IEX; SIP requires the paid plan. |
| **Trades** / **Quotes** (historical) | Every individual trade (price, size, exchange, conditions) or quote (bid/ask/sizes) with timestamps. | Far more data than bars; only needed for tape-reading features. |
| **Most actives** (`/v1beta1/screener/stocks/most-actives`) | Top 1–100 stocks by today's cumulative **volume** or **trade count**, built from **SIP** data (all exchanges) even on the free plan. | One call, whole market. A ready-made "high volume today" list. |
| **Top movers** (`/v1beta1/screener/stocks/movers`) | Top 1–50 **gainers and losers** by % change vs previous close, with price and change. Resets at the open. | One call, whole market. A ready-made "up a lot today" list. |
| **News** (`/v1beta1/news`) | Headline, summary, full content (optional), author, source (Benzinga), URL, related symbols, images, timestamps. Filter by symbols and dates. | Catalyst check: "is there news on this name today?" |
| **Corporate actions** | Splits, reverse splits, dividends, mergers, spin-offs, name changes, with ex/record/pay dates. | Reverse splits matter for low-float names; a name change explains a symbol that vanished. |
| **Crypto bars / trades / quotes / snapshots** | Same shapes for crypto pairs (`BTC/USD`), 24/7. | Not part of the stock alert plan. |

**Feeds.** `iex` (free) is one exchange, roughly 2–3% of volume — thin before
9:30. `sip` is the consolidated tape from every exchange. On the free plan
you can *read historical* SIP data as long as the request ends at least 15
minutes ago; anything more recent needs Algo Trader Plus. `boats` and
`overnight` cover overnight trading; `otc` covers over-the-counter names.

**Plans.**

| | Basic (free) | Algo Trader Plus ($99/mo) |
|---|---|---|
| Real-time feed | IEX only | SIP — all US exchanges |
| API calls | 200 / minute | 10,000 / minute |
| Websocket symbols | 30 | unlimited |
| Historical | 2016 onward, both | |

Note the price: Alpaca's paid data plan is $99/month, not the $9 I quoted
earlier from memory. That changes the calculation on pre-market data.

### Trading API

| Endpoint | What it returns |
|---|---|
| **Assets** | Every symbol Alpaca knows: class, exchange, name, active/inactive, **tradable**, **shortable**, **fractionable**, marginable, borrow status, and attributes such as **`ipo`** (recent listing), `has_options`, `overnight_tradable`. ~13,400 active US equities. |
| **Account** | Cash, equity, buying power, portfolio value, margin figures, trading-blocked flags. |
| **Positions** | Open positions with quantity, average entry, current price, unrealised P&L. (Crypto reported as `BTCUSD`.) |
| **Orders** | Place/cancel/list orders: market, limit, stop, stop-limit, trailing stop, bracket/OCO; time in force day/GTC/IOC/FOK/opg/cls. Extended hours needs a **limit** order, **day**, `extended_hours=true`, whole shares. |
| **Clock** | Is the market open now; next open and close. |
| **Calendar** | Trading days with open/close times, including early closes. |
| **Watchlists** | Named symbol lists stored on the account. |
| **Portfolio history** | Equity and P&L over time. |
| **Account activities** | Fills, dividends, fees, transfers. |

---

## Financial Modeling Prep

About 230 endpoints. Grouped by what they are good for here.

### Directly useful for stock alerts

| Endpoint | What it returns | Plan |
|---|---|---|
| **All shares float** (`shares-float-all`, paged 1,000 at a time) | Float shares, free float %, outstanding shares for **every** company. ~14 calls covers the market. | The scanner uses this, cached once a day. Endpoint availability by plan should be confirmed on your key — the scanner now says plainly if the plan refuses it. |
| **Shares float** (`shares-float?symbol=`) | The same, one symbol. | |
| **Company screener** (`company-screener`) | Filter the whole market by market cap, price, volume, beta, sector, industry, exchange, country, dividend, ETF/fund flags, actively-trading flag. Returns matching symbols with those figures. | One call to shrink 13,000 names to the few hundred that fit "under $20, over 500k average volume, small cap". The cheapest possible pre-filter. |
| **Biggest gainers / losers / most actives** | Today's top movers and most traded, with price and % change. | One call each. |
| **Quote** / **Batch quote** | Price, change, % change, day high/low, year high/low, market cap, volume, **average volume**, open, previous close, EPS, PE, earnings date, shares outstanding. Batch takes many symbols. | Average volume in one call is what a proper relative-volume needs. |
| **Quote short** / **Batch quote short** | Just price, change, volume. | Cheaper. |
| **Stock price change** | % change over 1 day, 5 days, 1 month, 3 months, 6 months, YTD, 1 year, 3 years, 5 years, 10 years, max. | "Has this stock spiked in the last 12 months" comes from here, per symbol, or from daily history. |
| **Aftermarket trade / quote** (+ batch) | Post-market trades and bid/ask. | After-hours activity without a paid Alpaca feed. |
| **Historical price (end of day)** — light / full / unadjusted / dividend-adjusted | Daily open/high/low/close/volume, VWAP, change, going back years. | Free plan: yes. The 12-month spike check, and any daily-chart context. |
| **Intraday charts** — 1 min, 5 min, 15 min, 30 min, 1 hour, 4 hour | Intraday bars. | **Premium plan** and up for intraday; **Ultimate** for 1-minute. On Basic/Starter this is not available, which is why Alpaca is the intraday source. |
| **Company profile** | Sector, industry, market cap, price, beta, average volume, exchange, IPO date, description, CEO, employees, is-ETF, is-actively-trading. | Sector filters; "how new is this company". |
| **Market cap** (single, batch, historical) | Market capitalisation. | |
| **Earnings calendar** / **Earnings** | Upcoming and past report dates, EPS/revenue estimate vs actual. | "Earnings today" is a catalyst flag — or a reason to stay out. |
| **Stock news** / **Press releases** (latest and search) | Headlines, text, source, URL by symbol. | Catalyst check; press releases are where pumps start. |
| **Splits** / **Splits calendar** | Historical and upcoming splits with ratio. | Reverse splits on low-float names. |
| **Symbol changes** / **Delisted companies** / **Actively trading list** | Housekeeping for a whole-market universe. | |
| **Technical indicators** — SMA, EMA, WMA, DEMA, TEMA, RSI, standard deviation, Williams %R, ADX | Pre-computed on daily or intraday timeframes. | We compute our own from bars so every app agrees; these are a cross-check at most. |
| **Exchange market hours** / **Holidays by exchange** | Session times and holiday calendar. | Could feed the holiday calendar the sessions code leaves empty. |

### Available but not needed for alerts

Financial statements (income, balance sheet, cash flow, TTM, growth,
as-reported), ratios and key metrics, DCF valuations, analyst estimates,
ratings, price targets and grades, insider trading, Senate/House trades,
institutional 13F holdings, ETF/mutual fund holdings and weightings, SEC
filings (8-K and full search), earnings call transcripts, IPO calendar and
prospectuses, dividends calendar, M&A, executives and compensation, sector
and industry performance and P/E, economic indicators and calendar,
treasury rates, indexes and constituents, commodities, forex, crypto, ESG,
commitment of traders, crowdfunding and fundraising, and bulk CSV
downloads of most of the above.

One from that list worth a thought later: **insider trading** and **8-K
filings** are the two feeds that most often precede the moves your two
scenarios look for.

### Plans

| Plan | Calls | History | Notable |
|---|---|---|---|
| **Basic (free)** | **250 per day** | End-of-day only | Profile and reference data; 150+ endpoints |
| Starter ($22/mo annual) | 300 / min | 5 years | US coverage, daily prices, news |
| Premium ($59/mo annual) | 750 / min | 30 years | **Intraday charts**, technical indicators, calendars |
| Ultimate ($149/mo annual) | 3,000 / min | full | 1-minute intraday, bulk delivery |

All plans have a monthly bandwidth cap (free: 500 MB). **250 calls a day
is the number that shapes the design:** one bulk float load (~14 calls)
and one screener call per scan is fine; one call per symbol is not.

---

## Pushover

One API, one job: put a message on the phone.

| Parameter | What it does | Limit |
|---|---|---|
| `message` | The alert text | 1,024 characters |
| `title` | Bold line above it | 250 characters; defaults to the app name |
| `url` + `url_title` | A tappable link under the message — this is the Robinhood link | 512 / 100 characters |
| `priority` | −2 silent badge only · −1 no sound · 0 normal · **1 bypasses quiet hours** · 2 emergency (repeats until acknowledged; needs `retry` ≥ 30 s and `expire` ≤ 3 h) | |
| `sound` | One of ~26 built-in sounds, or a custom one — a different sound per scenario is possible | |
| `html` | Bold/italic/colour/links inside the message | Stripped in the lock-screen preview |
| `monospace` | Fixed-width text, e.g. a small table | Mutually exclusive with html |
| `attachment` | An **image** with the message — a chart screenshot could ride along | 5 MB, image only |
| `ttl` | Seconds until the message deletes itself from the phone | Ignored for emergency |
| `device` | Send to one named device rather than all | |
| `timestamp` | Override the shown time | |

Receipts and acknowledgement exist only for emergency priority. There is
no way for the phone to send anything *back* through Pushover; it is
strictly outbound. Groups, subscriptions and "glances" (a tiny live
readout on a watch face or widget) are also available.

**Quota:** 10,000 messages a month across all apps on the account,
counted per successful send per user, resetting on the 1st. A busy
scanner day of 50 alerts is 1,500 a month — comfortable, but a runaway
loop would burn it, which is why alerts are deduplicated and capped per
scan.

---

## What this suggests

- **Alpaca is the intraday engine.** Bars with VWAP and trade count, plus
  snapshots for a cheap whole-market "what moved today" pass. Its two
  screener endpoints already answer "most volume" and "biggest gainers"
  in one call each, from all-exchange data, on the free plan.
- **FMP is the fact sheet.** Float, market cap, sector, average volume,
  earnings date, news, and a screener that shrinks the universe before
  Alpaca is asked for a single bar. Used sparingly, because of the daily
  call budget.
- **Pushover has more headroom than we use.** A distinct sound per
  scenario, high priority for the strongest setups, and a chart image in
  the alert are all one field each.
