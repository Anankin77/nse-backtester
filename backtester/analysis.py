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
