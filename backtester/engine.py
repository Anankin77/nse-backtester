"""The backtest engine: a day-by-day simulation of one stock, with cash and shares.

Timing rule (avoids look-ahead bias):
    signal decided at day t's CLOSE  ->  order filled at day t+1's OPEN.

Each day t:
  1. Signal says OUT and we hold shares   -> sell everything at Open[t].
  2. Signal says IN and we hold nothing   -> buy at Open[t]; how much is set by sizing.
  3. Stop-loss check (if a stop is set):
       Open[t] at or below stop  -> sold at Open[t]   (gap down: you get the worse price)
       Low[t]  at or below stop  -> sold at the stop price
     After a stop, no re-entry until the signal first turns OUT, then IN again.
  4. Portfolio value is marked at Close[t].

Sizing (fraction of current equity put into a new trade; the smallest one applies):
  size       : fixed fraction, e.g. 0.5 = half your money per trade        (default 1.0)
  vol_target : aim for this annual volatility, e.g. 0.15. Calm stock = bigger position,
               wild stock = smaller. Uses the last 20 days' volatility before entry.
  risk       : max loss per trade if the stop hits, e.g. 0.01 = 1% of equity.
               Position = risk / stop, so a 1% risk with a 10% stop = 10% of equity.
Uninvested money sits in cash and earns 0%.
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
    position: pd.Series        # fraction of equity in the stock at each close (0 to 1)
    trades: pd.DataFrame       # one row per round trip
    stats: dict = field(default_factory=dict)


def run(df: pd.DataFrame, signal: pd.Series, capital: float = 100_000,
        costs: CostModel = DELIVERY, name: str = "strategy",
        stop: float | None = None, trailing: bool = False,
        size: float = 1.0, vol_target: float | None = None, risk: float | None = None) -> Result:
    if risk is not None and not stop:
        raise ValueError("risk sizing needs a stop-loss (risk / stop decides the position size)")
    if not 0 < size <= 1:
        raise ValueError("size must be between 0 and 1")

    sig = signal.reindex(df.index).fillna(0).clip(0, 1).round().to_numpy()
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    vol20 = (df["Close"].pct_change().rolling(20).std() * np.sqrt(252)).to_numpy()
    n = len(df)

    cash, shares = float(capital), 0.0
    entry_i = entry_px = peak = None
    blocked = False
    equity = np.empty(n)
    exposure = np.empty(n)
    trades = []

    def sell(i, px, reason):
        nonlocal cash, shares, entry_i, entry_px, peak
        cash += shares * px * (1 - costs.sell)
        trades.append(_trade(df.index, entry_i, i, entry_px, px, costs, reason))
        shares, entry_i, entry_px, peak = 0.0, None, None, None

    for i in range(n):
        want = sig[i - 1] if i > 0 else 0.0
        if blocked and want == 0:
            blocked = False

        if shares > 0 and want == 0:
            sell(i, o[i], "signal")

        elif shares == 0 and want == 1 and not blocked:
            frac = size
            if vol_target is not None:
                v = vol20[i - 1] if i > 0 else np.nan
                frac = min(frac, vol_target / v) if np.isfinite(v) and v > 0 else frac
            if risk is not None:
                frac = min(frac, risk / stop)
            value = frac * cash
            shares = value / (o[i] * (1 + costs.buy))
            cash -= shares * o[i] * (1 + costs.buy)
            entry_i, entry_px, peak = i, o[i], o[i]

        if shares > 0 and stop:
            level = (peak if trailing else entry_px) * (1 - stop)
            if o[i] <= level and entry_i != i:
                sell(i, o[i], "stop (gap)")
                blocked = True
            elif l[i] <= level:
                sell(i, level, "stop")
                blocked = True
            else:
                peak = max(peak, c[i])  # trail on closes: we don't know if the high came before the low

        equity[i] = cash + shares * c[i]
        exposure[i] = shares * c[i] / equity[i] if equity[i] > 0 else 0.0

    if shares > 0:  # still holding at the end: mark to last close, don't charge a sale
        trades.append(_trade(df.index, entry_i, n - 1, entry_px, c[-1], costs, "still open"))

    eq = pd.Series(equity, index=df.index)
    rets = eq.pct_change()
    rets.iloc[0] = eq.iloc[0] / capital - 1
    pos = pd.Series(exposure, index=df.index)
    cols = ["entry_date", "exit_date", "entry_price", "exit_price", "days", "return_pct", "exit_reason", "still_open"]
    tdf = pd.DataFrame(trades, columns=cols)

    res = Result(name, eq, rets, pos, tdf)
    res.stats = summarize(eq, rets, pos, tdf)
    return res


def _trade(index, i_in, i_out, px_in, px_out, costs, reason):
    net = px_out * (1 - costs.sell) / (px_in * (1 + costs.buy)) - 1
    d_in, d_out = index[i_in].date(), index[i_out].date()
    return {
        "entry_date": d_in,
        "exit_date": d_out,
        "entry_price": round(float(px_in), 2),
        "exit_price": round(float(px_out), 2),
        "days": int(np.busday_count(d_in, d_out)),
        "return_pct": round(100 * float(net), 2),
        "exit_reason": reason,
        "still_open": reason == "still open",
    }
