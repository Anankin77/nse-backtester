"""Monthly cross-sectional momentum: own the stocks that rose most, drop the rest.

Decided at the last close of each month (traded at the next open by portfolio.py):
  1. Momentum = a stock's return over the past 6 and/or 12 months, SKIPPING the
     latest month (last month's winners tend to bounce back, so it is left out).
  2. Rank every stock that has enough history. Hold the top N, equal weight.
  3. Market filter: if the index closed below its 200-day average, hold nothing.

Everything uses closes up to the decision day only.
"""
from __future__ import annotations

import pandas as pd

from .strategies import sma

LOOKBACKS = {"6m": (6,), "12m": (12,), "6m+12m": (6, 12)}


def month_ends(calendar: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each month in the calendar."""
    s = calendar.to_series()
    return pd.DatetimeIndex(s.groupby(calendar.to_period("M")).max().to_numpy())


def momentum_scores(closes: pd.DataFrame, lookback: str = "6m+12m", skip: int = 1) -> pd.DataFrame:
    """Score per month-end and stock (higher = stronger). NaN = not enough history.

    One lookback: the return itself. Two: the average of the two percentile ranks.
    """
    ends = month_ends(closes.index)
    monthly = closes.ffill().loc[ends]          # still NaN before a stock's first price
    parts = []
    for months in LOOKBACKS[lookback]:
        ret = monthly.shift(skip) / monthly.shift(skip + months) - 1
        parts.append(ret)
    if len(parts) == 1:
        return parts[0]
    ok = parts[0].notna() & parts[1].notna()
    ranks = [p.where(ok).rank(axis=1, pct=True) for p in parts]
    return (ranks[0] + ranks[1]) / 2


def momentum_targets(closes: pd.DataFrame, index_close: pd.Series, lookback: str = "6m+12m",
                     top_n: int = 5, skip: int = 1, sma_days: int = 200) -> pd.DataFrame:
    """Target weights at each month-end: 1/top_n for the top N, 0 for the rest.

    All zeros when the index is below its `sma_days` average (sma_days=0 turns the filter off).
    Pass `momentum_scores(...)` to the portfolio engine as `priority` so that, when
    cash is short, the strongest stock is bought first.
    """
    scores = momentum_scores(closes, lookback, skip)
    rank = scores.rank(axis=1, ascending=False, method="first")
    weights = (rank <= top_n).astype(float) / top_n
    if sma_days:
        idx = index_close.reindex(closes.index).ffill()
        avg = sma(idx, sma_days)
        risk_on = (idx >= avg) & avg.notna()
        weights = weights.mul(risk_on.loc[weights.index].astype(float), axis=0)
    return weights
