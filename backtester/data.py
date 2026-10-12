"""Price data: saved prices, Yahoo Finance downloads, CSV files, or fake prices.

All loaders return a DataFrame indexed by date with columns:
Open, High, Low, Close, Volume  (Close is split/dividend adjusted).

Where prices come from, in order:
1. prices/<TICKER>.csv, saved in the repo by a daily GitHub job (see backtester/update.py).
   The hosted app never has to reach Yahoo itself, and every page sees the same prices.
2. Yahoo Finance, for any ticker that isn't saved, cached in data/ for the day.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data"
PRICES_DIR = ROOT / "prices"
COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def nse_symbol(ticker: str) -> str:
    """'RELIANCE' -> 'RELIANCE.NS'. Index symbols like '^NSEI' are left alone."""
    t = ticker.strip().upper()
    if t.startswith("^") or t.endswith(".NS") or t.endswith(".BO"):
        return t
    return f"{t}.NS"


def stored_file(ticker: str) -> Path:
    """'RELIANCE' -> prices/RELIANCE.csv, '^NSEI' -> prices/IDX_NSEI.csv."""
    t = ticker.strip().upper().removesuffix(".NS")
    return PRICES_DIR / (t.replace("^", "IDX_").replace("&", "_AND_") + ".csv")


def load_stored(ticker: str) -> pd.DataFrame | None:
    f = stored_file(ticker)
    return load_csv(f) if f.exists() else None


def stored_status() -> dict:
    """What the last daily update wrote: last trading day, when, and which tickers failed."""
    f = PRICES_DIR / "_status.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def download(ticker: str, start: str, end: str | None = None, use_cache: bool = True,
             use_store: bool = True) -> pd.DataFrame:
    """Daily OHLCV for an NSE ticker from `start` up to (not including) `end`.

    Uses the saved copy in prices/ when there is one, otherwise Yahoo (cached in data/)."""
    if use_store:
        saved = load_stored(ticker)
        if saved is not None:
            last = pd.Timestamp(end) - pd.Timedelta(days=1) if end else None
            return saved.loc[start:last]
    symbol = nse_symbol(ticker)
    CACHE_DIR.mkdir(exist_ok=True)
    # Open-ended requests are cached per calendar day, so rerunning a 50-stock scan is instant.
    tag = end or f"to{pd.Timestamp.today().date()}"
    safe = symbol.replace("^", "IDX_").replace("&", "and")
    cache = CACHE_DIR / f"{safe}_{start}_{tag}.csv"

    if use_cache and cache.exists():
        return load_csv(cache)

    import yfinance as yf  # imported lazily so tests/demo work offline

    raw = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise ValueError(
            f"No data for {symbol}. Check the ticker (NSE symbol, e.g. RELIANCE, TCS, ^NSEI) "
            "and your internet connection."
        )
    if isinstance(raw.columns, pd.MultiIndex):  # yfinance returns (field, ticker) columns
        raw.columns = raw.columns.get_level_values(0)
    df = _clean(raw)
    df.to_csv(cache)
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load a CSV with a Date column plus Open/High/Low/Close/Volume (case-insensitive)."""
    df = pd.read_csv(path)
    df.columns = [c.strip().title() for c in df.columns]
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col)
    df.index.name = "Date"
    return _clean(df)


def synthetic(days: int = 1500, seed: int = 7, start_price: float = 1000.0) -> pd.DataFrame:
    """Fake but realistic-looking prices (trending regimes + noise). For demos and tests."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=days, name="Date")
    regime = np.repeat(rng.normal(0.0004, 0.0012, days // 120 + 1), 120)[:days]
    rets = regime + rng.normal(0, 0.015, days)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.004, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, days)))
    vol = rng.integers(1e5, 5e6, days)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Price data is missing columns: {missing}")
    df = df[COLUMNS].apply(pd.to_numeric, errors="coerce")
    df = df[~df.index.duplicated()].sort_index()
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "Date"
    return df
