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

  6. A buy or a trim worth less than `min_order` rupees is skipped (the flat DP
     charge makes tiny orders pointless). Selling a stock completely is never skipped.

Kill switch (`max_drawdown`): if the account closes this far below its highest
close, everything is sold at the next open and nothing is bought again until the
`resume` date. On that day the peak restarts from the current value and the latest
targets are bought again.

Corporate actions (`actions`): a dividend is paid into cash, a split or bonus
changes the share count, both before the day's trading. Used by the paper broker,
whose prices are not adjusted backwards the way backtest prices are.

The run can be continued: pass the `book` a previous run ended with as `initial`.
Running day by day gives exactly the same fills as running the whole period at
once; the paper broker's daily check relies on that.

Costs: percentage charges on every fill, plus the flat DP charge once per scrip
per day it is sold (costs.py). Uninvested money sits in cash and earns 0%.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .costs import DELIVERY, CostModel
from .metrics import RISK_FREE, TRADING_DAYS, max_drawdown

TRADE_COLUMNS = ["date", "ticker", "side", "shares", "price", "value", "charges", "cash_after", "reason"]
DAILY_COLUMNS = ["equity", "cash", "invested", "peak", "drawdown_pct", "killed"]


@dataclass
class Book:
    """Everything needed to carry an account from one day to the next."""
    cash: float
    shares: dict = field(default_factory=dict)   # ticker -> whole shares
    peak: float = 0.0                            # highest closing value so far (for the kill switch)
    killed: bool = False
    killed_on: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        return cls(float(d["cash"]), {k: int(v) for k, v in d.get("shares", {}).items()},
                   float(d.get("peak", 0.0)), bool(d.get("killed", False)), d.get("killed_on"))


@dataclass
class PortfolioResult:
    name: str
    equity: pd.Series          # portfolio value at each close
    returns: pd.Series         # daily returns (day 1 is measured against the starting value)
    exposure: pd.Series        # fraction of equity in stocks at each close
    trades: pd.DataFrame       # one row per fill (and per dividend / split), see TRADE_COLUMNS
    holdings: dict             # ticker -> whole shares held at the end
    cash: float
    capital: float
    stats: dict = field(default_factory=dict)
    book: Book | None = None                     # pass as `initial` to continue the run
    daily: pd.DataFrame | None = None            # per day: equity, cash, invested, peak, drawdown_pct, killed
    pending: list = field(default_factory=list)  # orders for the next open, sized at the last close


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
                  calendar: pd.DatetimeIndex | None = None, name: str = "portfolio",
                  max_drawdown: float | None = None, resume=None, min_order: float = 0.0,
                  initial: Book | None = None, actions: list | None = None) -> PortfolioResult:
    """Simulate `targets` (rows = decision dates at the close, columns = tickers,
    values = wanted fraction of equity, 0 = don't hold) from `start` to `end`.

    A row is acted on at the first trading day after its date. A fresh account
    (no `initial`) also acts on the latest row before `start` on its first day,
    so it is invested from day one instead of waiting for the next decision.

    actions: [(day, ticker, "dividend" | "split", factor)], where factor is
    new price / old price (0.98 = a 2% dividend, 0.5 = a 1:1 bonus or 2-for-1 split).
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
    col = {t: j for j, t in enumerate(tickers)}
    n, m = len(days), len(tickers)

    # decision date -> the trading day it is filled on
    tgt = targets[tickers].fillna(0.0)
    orders, order_of, rank = {}, {}, {}
    for d, row in tgt.iterrows():
        if priority is not None and d in priority.index:
            score = priority.loc[d].reindex(tickers).fillna(-np.inf).to_numpy(dtype=float)
            rank[d] = np.argsort(-score, kind="stable")
        k = calendar.searchsorted(d, side="right")
        if k < len(calendar) and days[0] <= calendar[k] <= days[-1]:
            orders[calendar[k]] = row.to_numpy(dtype=float)
            if d in rank:
                order_of[calendar[k]] = rank[d]

    def latest_before(day):
        """The newest decision made before `day`: (weights, buy order), or (None, None)."""
        k = tgt.index.searchsorted(day, side="left") - 1
        if k < 0:
            return None, None
        return tgt.iloc[k].to_numpy(dtype=float), rank.get(tgt.index[k])

    by_day: dict = {}
    for day, tk, kind, factor in actions or []:
        if tk in col:
            by_day.setdefault(pd.Timestamp(day), []).append((col[tk], kind, float(factor)))

    if initial is None:
        book = Book(float(capital), {}, float(capital))
        start_value = float(capital)
    else:
        book = Book.from_dict(initial.to_dict())
        unknown = [t for t, q in book.shares.items() if q and t not in col]
        if unknown:
            raise ValueError(f"the account holds stocks that are not in the targets: {unknown}")
        held0 = sum(q * prev_c[0, col[t]] for t, q in book.shares.items() if q)
        start_value = book.cash + (held0 if np.isfinite(held0) else 0.0)
    shares = np.zeros(m)
    for t, q in book.shares.items():
        shares[col[t]] = q
    cash, peak, killed, killed_on = book.cash, book.peak or start_value, book.killed, book.killed_on
    resume = pd.Timestamp(resume) if resume else None
    daily, log = np.empty((n, 6)), []

    def plan(w, px, mark):
        """Whole shares wanted per stock for target weights `w` at prices `px`."""
        w = np.clip(w, 0.0, max_weight)
        if w.sum() > max_exposure:
            w = w * (max_exposure / w.sum())
        eq = cash + float(np.nansum(shares * mark))
        want = shares.copy()
        for j in range(m):
            p = px[j]
            if not np.isfinite(p) or p <= 0:
                continue                                       # no bar: can't trade it today
            unit = p * (1 + costs.buy)
            q = math.floor(w[j] * eq / unit + 1e-9) if w[j] > 0 else 0
            if q == 0 and w[j] > 0 and unit <= max_weight * eq:
                q = 1
            if shares[j] > 0 and q > 0 and abs(q - shares[j]) * p < band * w[j] * eq:
                q = shares[j]
            if q > 0 and abs(q - shares[j]) * p < min_order:
                q = shares[j]                                  # too small to be worth the charges
            want[j] = q
        return want, eq

    def buys(want, px, mark, eq, order):
        """(stock, quantity) to buy, best-ranked first, within cash and the total limit."""
        out, left, invested = [], cash, float(np.nansum(np.minimum(shares, want) * mark))
        for j in (order if order is not None else range(m)):
            if want[j] <= shares[j]:
                continue
            unit = px[j] * (1 + costs.buy)
            room = max_exposure * eq - invested
            q = min(want[j] - shares[j], math.floor(left / unit + 1e-9), math.floor(room / px[j] + 1e-9))
            if q > 0 and q * px[j] >= min_order:
                out.append((j, q))
                left -= q * unit
                invested += q * px[j]
        return out

    for i, day in enumerate(days):
        for j, kind, f in by_day.get(day, []):                  # corporate actions, before the open
            if shares[j] <= 0 or not np.isfinite(prev_c[i, j]):
                continue
            if kind == "dividend":
                per_share = prev_c[i, j] * (1 - f)
                cash += shares[j] * per_share
                log.append((day, tickers[j], "dividend", int(shares[j]), per_share, shares[j] * per_share, 0.0, cash, "dividend"))
            else:
                shares[j] = round(shares[j] / f)
                log.append((day, tickers[j], "split", int(shares[j]), prev_c[i, j] * f, 0.0, 0.0, cash, "split"))

        w, order, reason = orders.get(day), order_of.get(day), "rebalance"
        if i == 0 and initial is None and w is None:
            w, order = latest_before(day)
        if killed and resume is not None and pd.Timestamp(killed_on) < resume <= day:
            killed, killed_on = False, None                    # back in: start a new peak from here
            peak = cash + float(np.nansum(shares * prev_c[i]))
            if w is None:
                w, order = latest_before(day)
        if killed:
            w, order, reason = (np.zeros(m) if shares.any() else None), None, "kill switch"

        if w is not None:
            # what we own is worth today's open (or the last close if a stock has no bar today)
            mark = np.where(np.isfinite(o[i]), o[i], prev_c[i])
            want, eq = plan(w, o[i], mark)
            for j in np.flatnonzero(want < shares):            # sells first
                q, px = shares[j] - want[j], o[i, j]
                got = costs.sell_proceeds(q * px)
                cash += got
                shares[j] = want[j]
                log.append((day, tickers[j], "sell", int(q), px, q * px, q * px - got, cash, reason))
            for j, q in buys(want, o[i], mark, eq, order):     # then buys, best-ranked first
                unit = o[i, j] * (1 + costs.buy)
                cash -= q * unit
                shares[j] += q
                log.append((day, tickers[j], "buy", int(q), o[i, j], q * o[i, j], q * (unit - o[i, j]), cash, reason))

        held = float(np.nansum(shares * c[i]))
        value = cash + held
        peak = max(peak, value)
        if max_drawdown and not killed and value <= peak * (1 - max_drawdown):
            killed, killed_on = True, str(day.date())
        daily[i] = (value, cash, held, peak, 100 * (value / peak - 1) if peak > 0 else 0.0, killed)

    # orders for the next open, sized at the last close (the real open will differ a little)
    pending = []
    w, order = (tgt.loc[days[-1]].to_numpy(dtype=float), rank.get(days[-1])) if days[-1] in tgt.index else (None, None)
    why = "rebalance"
    if killed:
        w, order, why = (np.zeros(m) if shares.any() else None), None, "kill switch"
    if w is not None:
        want, eq = plan(w, c[-1], c[-1])
        for j in np.flatnonzero(want < shares):
            pending.append({"ticker": tickers[j], "side": "sell", "shares": int(shares[j] - want[j]),
                            "est_price": round(float(c[-1, j]), 2), "reason": why})
        freed = sum(costs.sell_proceeds(p["shares"] * p["est_price"]) for p in pending)
        cash += freed                                          # buys may spend what the sells bring in
        for j, q in buys(want, c[-1], c[-1], eq, order):
            pending.append({"ticker": tickers[j], "side": "buy", "shares": int(q),
                            "est_price": round(float(c[-1, j]), 2), "reason": why})
        cash -= freed

    frame = pd.DataFrame(daily, index=days, columns=DAILY_COLUMNS)
    frame["killed"] = frame["killed"].astype(bool)
    eq_s = frame["equity"]
    rets = eq_s.pct_change()
    rets.iloc[0] = eq_s.iloc[0] / start_value - 1
    holdings = {t: int(s) for t, s in zip(tickers, shares) if s > 0}
    res = PortfolioResult(name, eq_s, rets, (frame["invested"] / eq_s).where(eq_s > 0, 0.0),
                          pd.DataFrame(log, columns=TRADE_COLUMNS), holdings, cash, float(start_value),
                          book=Book(cash, holdings, peak, killed, killed_on), daily=frame, pending=pending)
    res.stats = stats(eq_s, rets, start_value)
    sells = res.trades[res.trades["side"] == "sell"]
    res.stats.update({
        "Fills": int(res.trades["side"].isin(["buy", "sell"]).sum()),
        "Sells": int(len(sells)),
        "Charges": float(res.trades["charges"].sum()),
        "DP charges": float(len(sells) * costs.dp_charge),
        "Avg exposure %": 100 * float(res.exposure.mean()),
    })
    return res
