"""Portfolio mode: one pool of cash shared across many stocks, whole shares only.

Timing rule (same as the single-stock engine, avoids look-ahead bias):
    target weights decided at day t's CLOSE  ->  orders filled at day t+1's OPEN.

On a rebalance day:
  1. Each target weight is capped at `max_weight` (per-stock limit); if the weights
     then add up to more than `max_exposure` (total limit) they are scaled down.
  2. Wanted shares = floor(weight x equity / cost of one share). Never a fraction.
     If that is 0 but ONE share still fits inside the per-stock limit, one share is
     wanted: with Rs 4,000 that is the only way to own most stocks.
  3. A stock already held is left alone when the change would be smaller than
     `band` x its target value. Each trim costs a flat DP charge, so tiny trims
     only feed the broker.
  4. Sells go first (they free the cash), then buys, highest `priority` first
     (column order of `targets` if no priority is given), each limited by the
     cash left and by the total exposure limit.
  5. A stock with no price bar that day cannot be traded and is left as it is.

Costs: percentage charges on every fill, plus the flat DP charge once per scrip
per day it is sold (costs.py). Uninvested money sits in cash and earns 0%.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import DELIVERY, CostModel
from .metrics import RISK_FREE, TRADING_DAYS, max_drawdown

TRADE_COLUMNS = ["date", "ticker", "side", "shares", "price", "value", "charges"]


@dataclass
class PortfolioResult:
    name: str
    equity: pd.Series          # portfolio value at each close
    returns: pd.Series         # daily returns (day 1 is measured against the starting capital)
    exposure: pd.Series        # fraction of equity in stocks at each close
    trades: pd.DataFrame       # one row per fill: date, ticker, side, shares, price, value, charges
    holdings: dict             # ticker -> whole shares held at the end
    cash: float
    capital: float
    stats: dict = field(default_factory=dict)


def panel(data: dict[str, pd.DataFrame], column: str, calendar: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """One column (e.g. 'Close') of every stock side by side, on a shared calendar. Gaps stay NaN."""
    out = pd.DataFrame({t: df[column] for t, df in data.items()})
    return out.reindex(calendar) if calendar is not None else out.sort_index()


def stats(equity: pd.Series, returns: pd.Series, capital: float, risk_free: float = RISK_FREE) -> dict:
    years = max(len(equity) / TRADING_DAYS, 1e-9)
    total = equity.iloc[-1] / capital - 1
    sd = returns.std()
    return {
        "Total return %": 100 * total,
        "CAGR %": 100 * ((1 + total) ** (1 / years) - 1) if total > -1 else -100.0,
        "Max drawdown %": 100 * max_drawdown(pd.concat([pd.Series([capital]), equity], ignore_index=True)),
        "Sharpe": float((returns - risk_free / TRADING_DAYS).mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0,
        "Final value": float(equity.iloc[-1]),
    }


def run_portfolio(data: dict[str, pd.DataFrame], targets: pd.DataFrame, capital: float = 100_000,
                  costs: CostModel = DELIVERY, max_weight: float = 0.40, max_exposure: float = 1.0,
                  band: float = 0.20, start=None, end=None, priority: pd.DataFrame | None = None,
                  calendar: pd.DatetimeIndex | None = None, name: str = "portfolio") -> PortfolioResult:
    """Simulate `targets` (rows = decision dates at the close, columns = tickers,
    values = wanted fraction of equity, 0 = don't hold) from `start` to `end`.

    A row is acted on at the first trading day after its date, if that day lies
    inside start..end. So a decision made on the last close before `start` is
    filled on the first day of the run, as it would be for a live trader.
    """
    if not 0 < max_weight <= 1 or not 0 < max_exposure <= 1:
        raise ValueError("max_weight and max_exposure must be between 0 and 1")
    tickers = [t for t in targets.columns if t in data]
    if calendar is None:
        calendar = pd.DatetimeIndex(sorted(set().union(*(data[t].index for t in tickers))))
    days = calendar[(calendar >= pd.Timestamp(start or calendar[0])) & (calendar <= pd.Timestamp(end or calendar[-1]))]
    if len(days) == 0:
        raise ValueError("no trading days between start and end")

    opens = panel({t: data[t] for t in tickers}, "Open", calendar)
    closes = panel({t: data[t] for t in tickers}, "Close", calendar)
    o = opens.loc[days].to_numpy(dtype=float)
    c = closes.ffill().loc[days].to_numpy(dtype=float)          # value untraded days at the last close
    prev_c = closes.ffill().shift(1).loc[days].to_numpy(dtype=float)

    # decision date -> the trading day it is filled on
    orders, order_of = {}, {}
    for d, row in targets[tickers].fillna(0.0).iterrows():
        k = calendar.searchsorted(d, side="right")
        if k < len(calendar) and days[0] <= calendar[k] <= days[-1]:
            orders[calendar[k]] = row.to_numpy(dtype=float)
            if priority is not None and d in priority.index:
                score = priority.loc[d].reindex(tickers).fillna(-np.inf).to_numpy(dtype=float)
                order_of[calendar[k]] = np.argsort(-score, kind="stable")

    n, m = len(days), len(tickers)
    shares = np.zeros(m)
    cash = float(capital)
    equity, exposure, log = np.empty(n), np.empty(n), []

    for i, day in enumerate(days):
        w = orders.get(day)
        if w is not None:
            w = np.clip(w, 0.0, max_weight)
            if w.sum() > max_exposure:
                w = w * (max_exposure / w.sum())
            # what we own is worth today's open (or the last close if a stock has no bar today)
            mark = np.where(np.isfinite(o[i]), o[i], prev_c[i])
            eq = cash + float(np.nansum(shares * mark))
            want = shares.copy()
            for j in range(m):
                px = o[i, j]
                if not np.isfinite(px) or px <= 0:
                    continue                                   # no bar: can't trade it today
                unit = px * (1 + costs.buy)
                q = math.floor(w[j] * eq / unit + 1e-9) if w[j] > 0 else 0
                if q == 0 and w[j] > 0 and unit <= max_weight * eq:
                    q = 1
                if shares[j] > 0 and q > 0 and abs(q - shares[j]) * px < band * w[j] * eq:
                    q = shares[j]
                want[j] = q

            for j in np.flatnonzero(want < shares):            # sells first
                q, px = shares[j] - want[j], o[i, j]
                got = costs.sell_proceeds(q * px)
                cash += got
                shares[j] = want[j]
                log.append((day, tickers[j], "sell", int(q), px, q * px, q * px - got))
            for j in order_of.get(day, range(m)):              # then buys, best-ranked first
                if want[j] <= shares[j]:
                    continue
                px = o[i, j]
                unit = px * (1 + costs.buy)
                invested = float(np.nansum(shares * mark))
                room = max_exposure * eq - invested
                q = min(want[j] - shares[j], math.floor(cash / unit + 1e-9), math.floor(room / px + 1e-9))
                if q > 0:
                    cash -= q * unit
                    shares[j] += q
                    log.append((day, tickers[j], "buy", int(q), px, q * px, q * (unit - px)))

        held = float(np.nansum(shares * c[i]))
        equity[i] = cash + held
        exposure[i] = held / equity[i] if equity[i] > 0 else 0.0

    eq_s = pd.Series(equity, index=days)
    rets = eq_s.pct_change()
    rets.iloc[0] = eq_s.iloc[0] / capital - 1
    res = PortfolioResult(name, eq_s, rets, pd.Series(exposure, index=days),
                          pd.DataFrame(log, columns=TRADE_COLUMNS),
                          {t: int(s) for t, s in zip(tickers, shares) if s > 0}, cash, float(capital))
    res.stats = stats(eq_s, rets, capital)
    sells = res.trades[res.trades["side"] == "sell"]
    res.stats.update({
        "Fills": int(len(res.trades)),
        "Sells": int(len(sells)),
        "Charges": float(res.trades["charges"].sum()),
        "DP charges": float(len(sells) * costs.dp_charge),
        "Avg exposure %": 100 * float(res.exposure.mean()),
    })
    return res
