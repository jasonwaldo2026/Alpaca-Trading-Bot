# The read checklist

What you were looking at in the moment before you acted — recorded as a
fixed list of readings rather than a sentence.

**Why fixed categories:** "sellers exhausted" records the conclusion and
throws away the evidence. If every read says that, we can never find out
*which* input told you so. A fixed list means each entry is a row of data
and the factors can be ranked against each other.

**This file is yours to change.** Rename a tag, fix a `+` I got backwards,
add one I missed, delete one you don't actually use. Send it back and I'll
update the code to match. Editing this file alone changes nothing — the
tags live in `daily_report.py:FACTORS` and have to be changed there too.

---

## The tags

| Tag | What you're reading | `+` means | `−` means |
|---|---|---|---|
| `of` | Order flow / tape | buyers lifting offers | sellers hitting bids |
| `poc` | Volume Profile POC | rising / above VWAP | falling / below |
| `vw` | Price vs VWAP | above | below |
| `form` | The candle forming now | building toward its high | bleeding to its low |
| `wick` | The run of recent lower wicks | lows lining up — a floor | lows stepping down |
| `macd` | MACD | rising, diverging up | falling |
| `big` | The longer-timeframe chart | agrees with the entry | disagrees |

## How to write one

```
11:03 IN c3: of+ poc+ vw- form+ wick+ macd+ big-
12:12 OUT c2: of- form-
13:00 alarm fired late again
```

- **`IN`** before an entry, **`OUT`** before an exit. Neither = a plain
  observation about the day.
- **`c1` / `c2` / `c3`** — optional. How strongly you felt it. Whether
  conviction itself predicts anything is one of the better questions here.
- **`+` / `−` / `0`** — `0` means *you looked and could not tell*.
- **Leaving a tag out means you did not look at it.** That is different
  from `0`, and it is stored differently. Do not put `0` for things you
  skipped — it would turn "I don't know" into "I didn't look" and make the
  sample say something untrue.
- You can add words: `IN c2: of+ poc+ tape thinning out`. The tags get
  counted, the words get drawn.

Write it anywhere — phone notes, a text to yourself. Never edit the JSON
during the day.

---

## What this can and cannot answer

**Can — rank the factors one at a time.** "Does `poc+` win more often than
`poc−`?" needs roughly 30–40 entries per state, so about 60–80 trades.
One to two weeks at your volume.

**Cannot — find the winning combination.** Seven factors at three states
is 2,187 cells. You will never fill that, and anyone claiming to have
found the magic confluence on a few hundred trades is fitting noise.

**The multiple-comparisons trap.** Testing seven factors means roughly a
1-in-3 chance that at least one looks good by luck alone. So: whatever
wins in the first batch gets written down and tested on the *next* batch.
The first batch nominates. The second decides. Same rule as everything
else in this repo.

---

## The one rule that makes the data worth having

**A read written after the close is worse than no read.** By the evening
you know how it turned out and you cannot un-know it — the entry will
feel like evidence and it is not. If you miss one in the moment, leave it
blank. Blanks cost nothing. Reconstructed entries poison the sample.
