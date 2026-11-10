"""Walk-forward test of monthly momentum against buying and holding the index.

For each test year:
  1. TRAIN on the 5 years before it: run every setting in GRID, keep the one with
     the best CAGR after costs.
  2. TEST that one setting on the year itself, which the choice never saw.
Then roll forward one year and repeat. Only the test years count ("out-of-sample").

Each test year starts fresh with the stated capital, in cash, so "Rs 4,000" means
Rs 4,000 in every year (whole-share problems don't fade as the account grows).
The combined result chains the test years' daily returns together.

Benchmark: buy and hold the index over the same days, bought once, fractional
units allowed (think of an index fund; you can't buy Rs 4,000 of Nifty directly).

Survivorship control: the same stock list, simply held in equal amounts (re-split
each January, no costs, fractions allowed). The list is today's index members, so
it is hand-picked with hindsight. If plain holding of that list also beats the
index, the "edge" is the list, not the strategy. Momentum has to beat this too.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import DELIVERY, CostModel
from .engine import run
from .metrics import RISK_FREE, TRADING_DAYS, max_drawdown
from .momentum import LOOKBACKS, momentum_scores, momentum_targets
from .portfolio import panel, run_portfolio
from .strategies import buy_and_hold

GRID = [(lb, n) for lb in LOOKBACKS for n in (3, 5, 10)]

# ^NSEI is a PRICE index: it leaves out dividends (~1.2%/yr on the Nifty 50), while the
# stock prices here are dividend-adjusted. So the strategy must win by more than this
# before the win means anything.
DIVIDEND_YIELD = 0.012


@dataclass
class WalkForward:
    capital: float
    windows: pd.DataFrame      # one row per test year
    combined: dict             # out-of-sample totals: strategy, index, same list held
    equity: pd.DataFrame       # chained out-of-sample growth of Rs 1: strategy, index, list


def _growth_stats(returns: pd.Series) -> dict:
    growth = (1 + returns).cumprod()
    years = len(returns) / TRADING_DAYS
    sd = returns.std()
    return {
        "CAGR %": 100 * (growth.iloc[-1] ** (1 / years) - 1),
        "Total return %": 100 * (growth.iloc[-1] - 1),
        "Max drawdown %": 100 * max_drawdown(pd.concat([pd.Series([1.0]), growth], ignore_index=True)),
        "Sharpe": float((returns - RISK_FREE / TRADING_DAYS).mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0,
    }


def hold_the_list(closes: pd.DataFrame, start, end) -> pd.Series:
    """Daily returns of holding every stock that has a price at `start`, equal rupees, untouched till `end`."""
    c = closes.ffill().loc[start:end]
    c = c.loc[:, c.iloc[0].notna()]
    value = (c / c.iloc[0]).mean(axis=1)
    rets = value.pct_change()
    rets.iloc[0] = 0.0
    return rets


def walk_forward(data: dict[str, pd.DataFrame], index_df: pd.DataFrame, capital: float,
                 first_year: int = 2012, train_years: int = 5, costs: CostModel = DELIVERY,
                 max_weight: float = 0.40, max_exposure: float = 1.0, band: float = 0.20,
                 sma_days: int = 200, grid=None) -> WalkForward:
    grid = grid or GRID
    cal = index_df.index
    closes = panel(data, "Close", cal)
    idx_close = index_df["Close"]
    plans = {}
    for lb, n in grid:
        plans[(lb, n)] = (momentum_targets(closes, idx_close, lb, n, sma_days=sma_days), momentum_scores(closes, lb))

    def sim(cfg, start, end):
        targets, scores = plans[cfg]
        return run_portfolio(data, targets, capital, costs, max_weight, max_exposure, band,
                             start, end, priority=scores, calendar=cal)

    last_year = cal[-1].year
    test_years = range(first_year + train_years, last_year + 1)
    if not len(test_years):
        raise ValueError("not enough data for one train + test window")

    oos_start = f"{test_years[0]}-01-01"
    bench = run(index_df.loc[oos_start:], buy_and_hold(index_df.loc[oos_start:]), capital, costs, "Buy & hold index")

    rows, strat_rets, list_rets = [], [], []
    for year in test_years:
        tr0, tr1 = f"{year - train_years}-01-01", f"{year - 1}-12-31"
        te0, te1 = f"{year}-01-01", f"{year}-12-31"
        trained = {cfg: sim(cfg, tr0, tr1).stats["CAGR %"] for cfg in grid}
        best = max(grid, key=lambda cfg: trained[cfg])            # ties: first in GRID
        test = sim(best, te0, te1)
        b = bench.returns.loc[te0:te1]
        b_ret = 100 * ((1 + b).prod() - 1)
        strat_rets.append(test.returns)
        held = hold_the_list(closes, te0, te1)
        list_rets.append(held)
        rows.append({
            "Test": f"{year}" + ("" if test.equity.index[-1].month == 12 else f" (to {test.equity.index[-1]:%b %d})"),
            "Train": f"{year - train_years}-{year - 1}",
            "Chosen": f"{best[0]} top {best[1]}",
            "Train CAGR %": trained[best],
            "Strategy %": test.stats["Total return %"],
            "Index %": b_ret,
            "Edge %": test.stats["Total return %"] - b_ret,
            "List held %": 100 * ((1 + held).prod() - 1),
            "Strat MaxDD %": test.stats["Max drawdown %"],
            "Exposure %": test.stats["Avg exposure %"],
            "Sells": test.stats["Sells"],
            "Costs % of capital": 100 * test.stats["Charges"] / capital,
        })

    s = pd.concat(strat_rets)
    b = bench.returns.loc[s.index[0]:s.index[-1]]
    windows = pd.DataFrame(rows)
    combined = {
        "strategy": _growth_stats(s),
        "index": _growth_stats(b),
        "list": _growth_stats(pd.concat(list_rets)),
        "years": len(s) / TRADING_DAYS,
        "windows_won": int((windows["Edge %"] > 0).sum()),
        "windows": len(windows),
        "costs_per_year_pct": float(windows["Costs % of capital"].mean()),
    }
    combined["edge"] = combined["strategy"]["CAGR %"] - combined["index"]["CAGR %"]
    combined["edge_after_dividends"] = combined["edge"] - 100 * DIVIDEND_YIELD
    combined["edge_over_list"] = combined["strategy"]["CAGR %"] - combined["list"]["CAGR %"]
    equity = pd.DataFrame({"strategy": (1 + s).cumprod(), "index": (1 + b).cumprod(),
                           "list": (1 + pd.concat(list_rets)).cumprod()})
    return WalkForward(float(capital), windows, combined, equity)


def verdict(wf: WalkForward) -> str:
    c = wf.combined
    if c["edge"] <= 0:
        return "DOES NOT BEAT the index out-of-sample"
    if c["edge_after_dividends"] <= 0:
        return "DOES NOT BEAT the index out-of-sample once the index's dividends are counted"
    if c["edge_over_list"] <= 0:
        return ("NOT PROVEN: ahead of ^NSEI on paper, but behind the same stock list "
                "simply held, so the edge is survivorship bias")
    return "BEATS the index out-of-sample, and beats simply holding the same stock list"


def report(wf: WalkForward) -> str:
    c, w = wf.combined, wf.windows
    lines = [f"Capital Rs {wf.capital:,.0f}", ""]
    lines.append(w.to_string(index=False, float_format=lambda v: f"{v:.1f}"))
    lines.append("")
    summary = pd.DataFrame({"Momentum (walk-forward)": c["strategy"], "Buy & hold ^NSEI": c["index"],
                            "Same list, just held (control)": c["list"]}).T
    lines.append(f"Combined out-of-sample, {wf.equity.index[0].date()} -> {wf.equity.index[-1].date()} "
                 f"({c['years']:.1f} years):")
    lines.append(summary.to_string(float_format=lambda v: f"{v:.2f}"))
    lines.append("")
    lines.append(f"Test years won: {c['windows_won']} of {c['windows']}   |   "
                 f"Trading costs: {c['costs_per_year_pct']:.1f}% of capital per year")
    lines.append(f"Edge over ^NSEI: {c['edge']:+.1f} CAGR points   |   "
                 f"after ~{100 * DIVIDEND_YIELD:.1f}%/yr index dividends: {c['edge_after_dividends']:+.1f}   |   "
                 f"over the same list just held: {c['edge_over_list']:+.1f}")
    lines.append(f"VERDICT at Rs {wf.capital:,.0f}: {verdict(wf)}")
    return "\n".join(lines)


NOTES = """Read before believing any of this:
- SURVIVORSHIP BIAS: nifty50.txt is TODAY's index. The test picks among stocks we
  already know grew big enough to be in the Nifty in 2026 (it could buy BSE or Trent
  years before they joined) and never meets the ones that fell out (Yes Bank, Zee,
  Vedanta, ...). That flatters the strategy by an unknown but likely large amount.
  The index got no such help. The "same list, just held" row shows the size of it:
  that row uses no strategy at all, only hindsight about which stocks to list.
- Dividends: stock prices include dividends, ^NSEI does not (~1.2%/yr). The
  "after dividends" line corrects for this roughly.
- Whole shares use adjusted prices, which are LOWER than what a share really cost
  before a split or bonus. Real small accounts could afford fewer stocks than shown.
- The index is treated as buyable in any amount at no running cost. A real index
  fund or ETF costs ~0.05-0.2%/yr.
- Capital-gains tax is not counted. Monthly selling means paying 20% on short-term
  gains every year; buy and hold defers tax until sold.
- Each test year starts fresh in cash with the stated capital; cash earns 0%."""
