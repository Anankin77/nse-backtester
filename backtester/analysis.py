"""Higher-level helpers shared by the command line (run.py) and the web app (app.py)."""
from __future__ import annotations

import pandas as pd

from .costs import DELIVERY, CostModel
from .engine import run
from .strategies import STRATEGIES

SCAN_STRATEGIES = [s for s in STRATEGIES if s != "buyhold"]
IST = "Asia/Kolkata"
MARKET_CLOSE = (15, 45)  # NSE closes 15:30; wait a little for the final bar to settle


def stock_row(ticker: str, df: pd.DataFrame, capital: float = 100_000,
              costs: CostModel = DELIVERY, **risk) -> dict:
    """One scan row: every strategy's CAGR and max drawdown next to buy & hold."""
    row = {"Stock": ticker.upper(), "Years": round(len(df) / 252, 1)}
    bh = run(df, STRATEGIES["buyhold"](df), capital, costs, "Buy & hold").stats
    row["B&H CAGR"], row["B&H MaxDD"] = bh["CAGR %"], bh["Max drawdown %"]
    for s in SCAN_STRATEGIES:
        st = run(df, STRATEGIES[s](df), capital, costs, s, **risk).stats
        row[f"{s} CAGR"], row[f"{s} MaxDD"] = st["CAGR %"], st["Max drawdown %"]
    best = max(SCAN_STRATEGIES, key=lambda s: row[f"{s} CAGR"])
    row["Beat B&H?"] = f"yes ({best})" if row[f"{best} CAGR"] > row["B&H CAGR"] else "no"
    return row


def summary(out: pd.DataFrame) -> pd.DataFrame:
    """Across all scanned stocks: does an idea work broadly, or only on a lucky few?"""
    n = len(out)
    table = {}
    for s in SCAN_STRATEGIES:
        table[s] = {
            "Beat B&H on CAGR": f"{(out[f'{s} CAGR'] > out['B&H CAGR']).sum()} / {n}",
            "Smaller drawdown than B&H": f"{(out[f'{s} MaxDD'] > out['B&H MaxDD']).sum()} / {n}",
            "Median CAGR %": round(out[f"{s} CAGR"].median(), 1),
            "Median MaxDD %": round(out[f"{s} MaxDD"].median(), 1),
        }
    table["Buy & hold"] = {"Beat B&H on CAGR": "-", "Smaller drawdown than B&H": "-",
                           "Median CAGR %": round(out["B&H CAGR"].median(), 1),
                           "Median MaxDD %": round(out["B&H MaxDD"].median(), 1)}
    return pd.DataFrame(table)


# ---------------------------------------------------------------- competition

# Named strategy presets that can compete. Buy & hold competes too: it is the bar to beat.
PRESETS: dict[str, tuple[str, dict]] = {
    "Buy & hold": ("buyhold", {}),
    "SMA 50/200 (slow trend)": ("sma", {"fast": 50, "slow": 200}),
    "SMA 20/50 (fast trend)": ("sma", {"fast": 20, "slow": 50}),
    "RSI 14, 30/70 (dip buying)": ("rsi", {"period": 14, "buy_below": 30, "sell_above": 70}),
    "RSI 7, 20/80 (deep dips)": ("rsi", {"period": 7, "buy_below": 20, "sell_above": 80}),
    "Breakout 55/20 (Turtle)": ("breakout", {"lookback": 55, "exit_lookback": 20}),
    "Breakout 20/10 (fast)": ("breakout", {"lookback": 20, "exit_lookback": 10}),
}

# What a competition can be ranked by. Every one is "higher is better"
# (a max drawdown of -20% beats -50%, and -20 > -50).
RANK_BY = {
    "Sharpe (return per unit of risk)": "Sharpe",
    "Return (CAGR)": "CAGR %",
    "Return per worst fall (Calmar)": "Calmar",
    "Smallest worst fall": "Max drawdown %",
    "Edge over buy & hold (CAGR points)": "Edge",
}
_KEYS = ["CAGR %", "Max drawdown %", "Sharpe", "Calmar", "Trades"]


def tournament(data: dict[str, pd.DataFrame], entries: dict[str, tuple[str, dict]],
               capital: float = 100_000, costs: CostModel = DELIVERY,
               split: pd.Timestamp | None = None, risk: dict | None = None) -> pd.DataFrame:
    """Every strategy on every company. One row per (company, strategy).

    Without `split`, columns look like 'Sharpe [all]'. With `split`, each pair is
    scored twice: 'Sharpe [old]' on data before the split (used to pick winners) and
    'Sharpe [new]' after it (data the ranking never saw).

    Signals are computed on the full history, then each period is simulated on its own
    slice. Strategies only look backwards, so this is not peeking; it just gives the
    newer period a proper indicator warm-up, exactly as a live trader would have.
    """
    rows = []
    for tk, df in data.items():
        if split is None:
            periods = {"all": df}
        else:
            periods = {"old": df.loc[: split - pd.Timedelta(days=1)], "new": df.loc[split:]}
        hold = {p: run(d, STRATEGIES["buyhold"](d), capital, costs).stats["CAGR %"] for p, d in periods.items()}
        for name, (strat, params) in entries.items():
            sig = STRATEGIES[strat](df, **params)
            kw = {} if strat == "buyhold" else (risk or {})
            row = {"Company": tk.upper(), "Strategy": name}
            for p, d in periods.items():
                stats = run(d, sig.loc[d.index], capital, costs, name, **kw).stats
                for k in _KEYS:
                    row[f"{k} [{p}]"] = stats[k]
                row[f"Edge [{p}]"] = stats["CAGR %"] - hold[p]
            rows.append(row)
    return pd.DataFrame(rows)


def ranked(results: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Sort pairs by `metric` on the ranking period; add rank columns (and the new-data rank)."""
    pick = "old" if f"{metric} [old]" in results else "all"
    out = results.sort_values(f"{metric} [{pick}]", ascending=False).reset_index(drop=True)
    out.insert(0, "Rank", range(1, len(out) + 1))
    if pick == "old":
        out["Rank on new data"] = out[f"{metric} [new]"].rank(ascending=False, method="min").astype(int)
    return out


def strategy_table(results: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Per strategy, across all companies: typical score, how often it was the best, how often it beat holding."""
    periods = ["old", "new"] if f"{metric} [old]" in results else ["all"]
    n = results["Company"].nunique()
    out = pd.DataFrame(index=pd.Index(results["Strategy"].unique(), name="Strategy"))
    for p in periods:
        tag = {"all": "", "old": " (old)", "new": " (new)"}[p]
        col = f"{metric} [{p}]"
        best = results.loc[results.groupby("Company")[col].idxmax(), "Strategy"].value_counts()
        out[f"Median {metric}{tag}"] = results.groupby("Strategy")[col].median().round(2)
        out[f"Best on{tag}"] = [f"{best.get(s, 0)} / {n}" for s in out.index]
        beat = results.assign(b=results[f"Edge [{p}]"] > 0).groupby("Strategy")["b"].sum()
        out[f"Beat hold on{tag}"] = ["-" if s == "Buy & hold" else f"{int(beat[s])} / {n}" for s in out.index]
    first = out.columns[0]
    return out.sort_values(first, ascending=False)


def drop_incomplete_bar(df: pd.DataFrame, now: pd.Timestamp | None = None) -> tuple[pd.DataFrame, bool]:
    """While the market is open, Yahoo's last daily bar is a half-finished day. Signals computed
    from it would be wrong (and flip back and forth), so drop it. Returns (data, dropped?)."""
    now = now if now is not None else pd.Timestamp.now(tz=IST)
    open_now = now.weekday() < 5 and (now.hour, now.minute) < MARKET_CLOSE
    if open_now and len(df) and df.index[-1].date() == now.date():
        return df.iloc[:-1], True
    return df, False


def signal_row(ticker: str, df: pd.DataFrame) -> dict:
    """Latest signal per strategy. Orders fill at the NEXT open, hence 'tmrw'."""
    row = {"Stock": ticker.upper(), "Last close": df.index[-1].date(), "Price": round(float(df["Close"].iloc[-1]), 2)}
    for s in SCAN_STRATEGIES:
        sig = STRATEGIES[s](df)
        now, before = sig.iloc[-1], sig.iloc[-2]
        if now != before:
            row[s] = "BUY tmrw" if now else "SELL tmrw"
        else:
            row[s] = "hold" if now else "-"
    return row
