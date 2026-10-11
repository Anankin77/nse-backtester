"""The backtest engine.

Timing rule (avoids look-ahead bias):
    signal decided at day t's CLOSE  ->  order filled at day t+1's OPEN.

Daily return of the portfolio on day t:
    holding yesterday and today : Close[t] / Close[t-1] - 1
    entered today at the open   : Close[t] / Open[t]   - 1 - buy_cost
    exited today at the open    : Open[t]  / Close[t-1] - 1 - sell_cost
    flat                        : 0   (cash earns nothing here)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import DELIVERY, CostModel
from .metrics import summarize


@dataclass
class Result:
    name: str
    equity: pd.Series          # portfolio value per day
    returns: pd.Series         # daily portfolio returns
    position: pd.Series        # 1 = invested that day, 0 = in cash
    trades: pd.DataFrame       # one row per round trip
    stats: dict = field(default_factory=dict)


def run(df: pd.DataFrame, signal: pd.Series, capital: float = 100_000,
        costs: CostModel = DELIVERY, name: str = "strategy") -> Result:
    signal = signal.reindex(df.index).fillna(0).clip(0, 1).round()

    held = signal.shift(1).fillna(0)          # position during day t (filled at t's open)
    prev = held.shift(1).fillna(0)

    o, c = df["Open"], df["Close"]
    c_prev = c.shift(1)

    r = pd.Series(0.0, index=df.index)
    stay = (held == 1) & (prev == 1)
    enter = (held == 1) & (prev == 0)
    exit_ = (held == 0) & (prev == 1)

    r[stay] = (c / c_prev - 1)[stay]
    r[enter] = (c / o - 1 - costs.buy)[enter]
    r[exit_] = (o / c_prev - 1 - costs.sell)[exit_]
    r = r.fillna(0.0)

    equity = capital * (1 + r).cumprod()
    trades = _trades(df, held, costs)
    res = Result(name, equity, r, held, trades)
    res.stats = summarize(equity, r, held, trades)
    return res


def _trades(df: pd.DataFrame, held: pd.Series, costs: CostModel) -> pd.DataFrame:
    rows, entry_date = [], None
    change = held.diff().fillna(held)
    for date, ch in change.items():
        if ch == 1:
            entry_date = date
        elif ch == -1 and entry_date is not None:
            rows.append(_trade_row(df, entry_date, date, df.at[date, "Open"], costs, open_=False))
            entry_date = None
    if entry_date is not None:  # still holding at the end: mark to last close
        last = df.index[-1]
        rows.append(_trade_row(df, entry_date, last, df.at[last, "Close"], costs, open_=True))
    cols = ["entry_date", "exit_date", "entry_price", "exit_price", "days", "return_pct", "still_open"]
    return pd.DataFrame(rows, columns=cols)


def _trade_row(df, entry_date, exit_date, exit_price, costs, open_):
    entry_price = df.at[entry_date, "Open"]
    net = exit_price * (1 - costs.sell) / (entry_price * (1 + costs.buy)) - 1
    return {
        "entry_date": entry_date.date(),
        "exit_date": exit_date.date(),
        "entry_price": round(float(entry_price), 2),
        "exit_price": round(float(exit_price), 2),
        "days": int(np.busday_count(entry_date.date(), exit_date.date())),
        "return_pct": round(100 * float(net), 2),
        "still_open": open_,
    }
