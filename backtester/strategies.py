"""Strategies turn prices into a target position for each day.

A strategy returns a Series of 0 (out of the market) or 1 (fully invested),
decided using information available at that day's CLOSE. The engine then
trades on the NEXT day, so a strategy can never peek into the future.

To add your own: write a function (df, **params) -> pd.Series and register
it in STRATEGIES at the bottom.
"""
from __future__ import annotations

import pandas as pd


# ---------- indicators ----------

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI (0-100)."""
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


# ---------- strategies ----------

def buy_and_hold(df: pd.DataFrame) -> pd.Series:
    """Always invested. The benchmark every strategy has to beat."""
    return pd.Series(1, index=df.index, dtype=float)


def sma_crossover(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.Series:
    """Trend following: hold while the fast average is above the slow one."""
    if fast >= slow:
        raise ValueError("fast must be smaller than slow")
    f, s = sma(df["Close"], fast), sma(df["Close"], slow)
    return (f > s).astype(float).where(s.notna(), 0.0)


def rsi_reversion(df: pd.DataFrame, period: int = 14, buy_below: float = 30, sell_above: float = 70) -> pd.Series:
    """Mean reversion: buy when oversold (RSI < buy_below), sell when overbought (RSI > sell_above)."""
    r = rsi(df["Close"], period)
    pos, holding = [], 0.0
    for v in r:
        if pd.notna(v):
            if holding == 0 and v < buy_below:
                holding = 1.0
            elif holding == 1 and v > sell_above:
                holding = 0.0
        pos.append(holding)
    return pd.Series(pos, index=df.index)


def breakout(df: pd.DataFrame, lookback: int = 55, exit_lookback: int = 20) -> pd.Series:
    """Donchian breakout (Turtle-style): buy a new N-day high, sell a new M-day low."""
    hi = df["High"].rolling(lookback).max().shift(1)
    lo = df["Low"].rolling(exit_lookback).min().shift(1)
    pos, holding = [], 0.0
    for c, h, l in zip(df["Close"], hi, lo):
        if holding == 0 and pd.notna(h) and c > h:
            holding = 1.0
        elif holding == 1 and pd.notna(l) and c < l:
            holding = 0.0
        pos.append(holding)
    return pd.Series(pos, index=df.index)


STRATEGIES = {
    "buyhold": buy_and_hold,
    "sma": sma_crossover,
    "rsi": rsi_reversion,
    "breakout": breakout,
}
