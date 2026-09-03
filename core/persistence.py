"""
"Has this been true for a while?" — run-length helpers.

A single bar above VWAP is noise; twenty consecutive bars above it is a
trend. Strategies that care about persistence rather than a single crossing
need run lengths, and getting them right (resetting on every False, counting
the current bar) is fiddly enough to be worth one tested implementation.
"""

import pandas as pd


def consecutive_true(flags: pd.Series) -> pd.Series:
    """
    Length of the current run of True values, ending at each row.

    ``[F, T, T, F, T] -> [0, 1, 2, 0, 1]``

    NaN is treated as False and breaks the run: an indicator that is still
    warming up must not be counted as satisfying a condition.
    """
    truth = flags.fillna(False).astype(bool)
    # Each False starts a new group; cumulative count within the group gives
    # the run length, and the Falses themselves are zeroed out afterwards.
    groups = (~truth).cumsum()
    return truth.groupby(groups).cumsum().astype(int)


def held_for(flags: pd.Series, bars: int) -> pd.Series:
    """True where `flags` has been continuously true for at least `bars`."""
    if bars < 1:
        raise ValueError(f"bars must be >= 1; got {bars}.")
    return consecutive_true(flags) >= bars


def bars_since(flags: pd.Series) -> pd.Series:
    """
    Bars since `flags` was last True, or NaN before the first True.

    Useful for "did this happen recently" conditions without pinning them to
    the exact bar it happened on.
    """
    truth = flags.fillna(False).astype(bool)
    positions = pd.Series(range(len(truth)), index=truth.index)
    last_true = positions.where(truth).ffill()
    return (positions - last_true).where(last_true.notna())
