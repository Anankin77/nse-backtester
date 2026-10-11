"""Performance statistics. All percentages are returned as plain percent numbers."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252
RISK_FREE = 0.065  # ~Indian 91-day T-bill yield; an assumption, change if needed


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough fall, as a negative fraction."""
    return float((equity / equity.cummax() - 1).min())


def summarize(equity: pd.Series, returns: pd.Series, position: pd.Series, trades: pd.DataFrame,
              risk_free: float = RISK_FREE) -> dict:
    years = max(len(equity) / TRADING_DAYS, 1e-9)
    start_value = equity.iloc[0] / (1 + returns.iloc[0])  # capital before day 1
    total = equity.iloc[-1] / start_value - 1
    cagr = (1 + total) ** (1 / years) - 1
    vol = returns.std() * np.sqrt(TRADING_DAYS)
    excess = returns - risk_free / TRADING_DAYS
    sharpe = excess.mean() / returns.std() * np.sqrt(TRADING_DAYS) if returns.std() > 0 else 0.0
    downside = returns[returns < 0].std() * np.sqrt(TRADING_DAYS)
    sortino = (returns.mean() * TRADING_DAYS - risk_free) / downside if downside > 0 else 0.0
    mdd = max_drawdown(equity)
    closed = trades[~trades["still_open"]] if len(trades) else trades
    wins = (closed["return_pct"] > 0).sum() if len(closed) else 0

    return {
        "Total return %": 100 * total,
        "CAGR %": 100 * cagr,
        "Volatility %": 100 * vol,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Max drawdown %": 100 * mdd,
        "Calmar": cagr / abs(mdd) if mdd < 0 else 0.0,
        "Trades": int(len(trades)),
        "Win rate %": 100 * wins / len(closed) if len(closed) else 0.0,
        "Avg trade %": float(closed["return_pct"].mean()) if len(closed) else 0.0,
        "Time in market %": 100 * float(position.mean()),
        "Final value": float(equity.iloc[-1]),
    }


def table(results) -> pd.DataFrame:
    """Side-by-side stats for several Results."""
    return pd.DataFrame({r.name: r.stats for r in results}).round(2)
