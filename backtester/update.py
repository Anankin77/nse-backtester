"""Refresh the saved prices in prices/ from Yahoo Finance.

Run by GitHub Actions every weekday after the market closes (.github/workflows/prices.yml),
or by hand:  python run.py update
"""
from __future__ import annotations

import json
import time

import pandas as pd

from .analysis import IST, drop_incomplete_bar
from .data import PRICES_DIR, download, load_stored, stored_file, stored_status

START = "2000-01-01"   # the web app's earliest date
KEEP_SHARE = 0.95      # refuse a download that is much shorter than what is already saved


def to_text(df: pd.DataFrame) -> str:
    df = df.copy()
    df["Volume"] = df["Volume"].fillna(0).round().astype("int64")
    return df.to_csv(float_format="%.4f", lineterminator="\n")


def fetch(ticker: str, tries: int = 3) -> pd.DataFrame:
    """Full history, retrying when Yahoo throttles."""
    for attempt in range(tries):
        try:
            return download(ticker, START, use_cache=False, use_store=False)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def update_prices(tickers: list[str], now: pd.Timestamp | None = None, fetcher=fetch,
                  pause: float = 0.5, log=print) -> dict:
    """Download every ticker, save the ones that changed, write prices/_status.json.

    A bad download never replaces good saved data: if Yahoo returns less history than
    is saved, or older prices, the saved file is kept and the ticker is reported as failed.
    """
    now = now if now is not None else pd.Timestamp.now(tz=IST)
    PRICES_DIR.mkdir(exist_ok=True)
    changed, failed = [], {}
    for i, tk in enumerate(tickers):
        if i and pause:
            time.sleep(pause)
        try:
            new, _ = drop_incomplete_bar(fetcher(tk), now)
            if new.empty:
                raise ValueError("no data")
            old = load_stored(tk)
            if old is not None and (len(new) < KEEP_SHARE * len(old) or new.index[-1] < old.index[-1]):
                raise ValueError(f"Yahoo sent {len(new)} days up to {new.index[-1].date()}, "
                                 f"saved copy has {len(old)} up to {old.index[-1].date()}; kept the saved copy")
            f, text = stored_file(tk), to_text(new)
            if not f.exists() or f.read_text() != text:
                f.write_text(text)
                changed.append(tk)
            log(f"ok    {tk:<12} {len(new):>5} days, last {new.index[-1].date()}")
        except Exception as e:
            failed[tk] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            log(f"FAIL  {tk:<12} {failed[tk]}")

    saved = [load_stored(tk) for tk in tickers if stored_file(tk).exists()]
    last = max((d.index[-1] for d in saved if len(d)), default=None)
    old_status = stored_status()
    status = {
        "last_bar": str(last.date()) if last is not None else None,
        "updated": now.strftime("%Y-%m-%d %H:%M IST") if changed else old_status.get("updated"),
        "tickers": len(tickers),
        "failed": failed,
    }
    if status != old_status:
        (PRICES_DIR / "_status.json").write_text(json.dumps(status, indent=1) + "\n")
    log(f"\n{len(tickers) - len(failed)}/{len(tickers)} ok, {len(changed)} changed, last trading day {status['last_bar']}")
    return {"changed": changed, "failed": failed, "status": status}


def saved_tickers() -> list[str]:
    """Every ticker in watchlist.txt and nifty50.txt, without repeats."""
    names = []
    for list_name in ("watchlist", "nifty50"):
        path = PRICES_DIR.parent / f"{list_name}.txt"
        if path.exists():
            names += [ln.strip().upper() for ln in path.read_text().splitlines()
                      if ln.strip() and not ln.startswith("#")]
    return list(dict.fromkeys(names))


def main(tickers: list[str] | None = None) -> int:
    tickers = tickers or saved_tickers()
    out = update_prices(tickers)
    # Mostly failing usually means Yahoo blocked the server: fail loudly so GitHub emails you.
    return 1 if len(out["failed"]) > len(tickers) / 2 else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
