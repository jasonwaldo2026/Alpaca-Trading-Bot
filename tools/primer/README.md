# SPCX — a working primer

`SPCX_primer.pdf` explains, for someone learning to trade, what the terms
mean and what ninety days of SPCX data actually showed. Three of its four
findings are negative; the fourth is where to put a stop.

## Rebuilding it

    pip install reportlab pillow matplotlib pandas
    python figures.py          # the two teaching diagrams
    python figures2.py         # the five findings charts, from the signal CSV
    python fig_candle_fix.py   # the candle anatomy, laid out to avoid collisions
    python build_pdf.py        # assembles SPCX_primer.pdf

`figures2.py` reads the per-signal CSV that `swing_study.py --csv` writes.
Point `CSV` at your own copy; everything else follows from it, so the
numbers in the document cannot drift from the numbers in the study.

The palette is the one `daily_report.py` uses, so the primer and the daily
report look like one system.
