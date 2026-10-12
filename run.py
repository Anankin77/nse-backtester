"""Command-line entry point.

Examples
  python run.py test RELIANCE --strategy sma --param fast=50 --param slow=200
  python run.py compare TCS --start 2018-01-01
  python run.py optimize INFY --strategy sma --split 2022-01-01
  python run.py compare DEMO            # offline, synthetic prices
  python run.py test mydata.csv --strategy rsi
  python run.py scan                    # every strategy on every stock in watchlist.txt
  python run.py today                   # today's signals, for paper trading
  python run.py update                  # refresh the saved prices in prices/
"""
from __future__ import annotations

import argparse
import itertools
from datetime import date
from pathlib import Path

import pandas as pd

from backtester import DELIVERY, ZERO, STRATEGIES, download, load_csv, run, synthetic, table
from backtester.analysis import drop_incomplete_bar, signal_row, stock_row, summary
from backtester.plotting import report

OUT = Path(__file__).resolve().parent / "results"

GRIDS = {  # parameter ranges tried by `optimize`
    "sma": {"fast": [10, 20, 50, 100], "slow": [50, 100, 150, 200]},
    "rsi": {"period": [7, 14, 21], "buy_below": [20, 25, 30, 35], "sell_above": [60, 65, 70, 75]},
    "breakout": {"lookback": [20, 40, 55, 100], "exit_lookback": [10, 20, 40]},
}


def load(source: str, start: str, end: str | None) -> pd.DataFrame:
    if source.upper() == "DEMO":
        return synthetic()
    if source.lower().endswith(".csv"):
        df = load_csv(source)
        return df.loc[start:end] if end else df.loc[start:]
    return download(source, start, end)


def parse_params(pairs: list[str]) -> dict:
    params = {}
    for p in pairs or []:
        k, v = p.split("=", 1)
        params[k] = float(v) if "." in v else int(v)
    return params


def risk_kwargs(args) -> dict:
    """CLI percentages -> engine fractions. Buy & hold never gets these: it stays a pure benchmark."""
    pct = lambda v: None if v is None else v / 100  # noqa: E731
    return {
        "stop": pct(getattr(args, "stop", None)),
        "trailing": getattr(args, "trailing", False),
        "size": pct(getattr(args, "size", 100)),
        "vol_target": pct(getattr(args, "vol_target", None)),
        "risk": pct(getattr(args, "risk", None)),
    }


def risk_label(args) -> str:
    k = risk_kwargs(args)
    parts = []
    if k["stop"]:
        parts.append(f"{'trail ' if k['trailing'] else ''}stop {args.stop:g}%")
    if k["size"] < 1:
        parts.append(f"size {args.size:g}%")
    if k["vol_target"]:
        parts.append(f"vol {args.vol_target:g}%")
    if k["risk"]:
        parts.append(f"risk {args.risk:g}%")
    return ", ".join(parts)


def backtest(df, strategy, params, args, label=None):
    sig = STRATEGIES[strategy](df, **params)
    name = label or (strategy + (" " + ",".join(f"{k}={v}" for k, v in params.items()) if params else ""))
    extra = {} if strategy == "buyhold" else risk_kwargs(args)
    if extra and risk_label(args) and not label:
        name += f" [{risk_label(args)}]"
    return run(df, sig, capital=args.capital, costs=ZERO if args.no_costs else DELIVERY, name=name, **extra)


def cmd_test(args):
    df = load(args.source, args.start, args.end)
    params = parse_params(args.param)
    res = backtest(df, args.strategy, params, args)
    bh = backtest(df, "buyhold", {}, args, label="Buy & hold")
    show(df, [res, bh], args, f"{args.source.upper()} - {res.name}")
    if len(res.trades):
        print("\nLast 10 trades:")
        print(res.trades.tail(10).to_string(index=False))
        path = OUT / f"{_slug(args.source)}_{args.strategy}_trades.csv"
        res.trades.to_csv(path, index=False)
        print(f"\nAll trades saved to {path}")


def cmd_compare(args):
    df = load(args.source, args.start, args.end)
    results = [backtest(df, s, {}, args) for s in STRATEGIES if s != "buyhold"]
    results.append(backtest(df, "buyhold", {}, args, label="Buy & hold"))
    extra = f" [{risk_label(args)}]" if risk_label(args) else ""
    show(df, results, args, f"{args.source.upper()} - all strategies (default settings){extra}")


def cmd_optimize(args):
    """Grid search on the TRAIN period, then check the best settings on unseen TEST data."""
    df = load(args.source, args.start, args.end)
    split = pd.Timestamp(args.split)
    train, test = df.loc[:split - pd.Timedelta(days=1)], df.loc[split:]
    if len(train) < 250 or len(test) < 60:
        raise SystemExit("Need at least ~1 year of train data and ~3 months of test data around --split.")

    grid = GRIDS[args.strategy]
    keys = list(grid)
    rows = []
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        try:
            r = backtest(train, args.strategy, params, args)
        except ValueError:
            continue  # invalid combo, e.g. fast >= slow
        rows.append({**params, **{k: r.stats[k] for k in ("CAGR %", "Sharpe", "Max drawdown %", "Trades")}})

    ranked = pd.DataFrame(rows).sort_values("Sharpe", ascending=False)
    print(f"\nTRAIN {train.index[0].date()} -> {train.index[-1].date()}: top 10 by Sharpe")
    print(ranked.head(10).round(2).to_string(index=False))

    best = {k: ranked.iloc[0][k] for k in keys}
    best = {k: int(v) if float(v).is_integer() else float(v) for k, v in best.items()}
    res = backtest(test, args.strategy, best, args)
    bh = backtest(test, "buyhold", {}, args, label="Buy & hold")
    print(f"\nTEST {test.index[0].date()} -> {test.index[-1].date()} (data the optimizer never saw):")
    show(test, [res, bh], args, f"{args.source.upper()} - best train settings on test period")
    print("\nIf the test result is much worse than train, the 'best' settings were luck (overfitting).")


def watchlist(tickers: list[str], list_name: str = "watchlist") -> list[str]:
    if tickers:
        return tickers
    path = Path(__file__).resolve().parent / f"{list_name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found (try --list watchlist or --list nifty50)")
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def cmd_scan(args):
    """Every strategy on every stock: which ideas work where?"""
    tickers = watchlist(args.tickers, args.list)
    rows, skipped = [], []
    print(f"Scanning {len(tickers)} stocks" + (f" with {risk_label(args)}" if risk_label(args) else "") + " ...")
    for n, tk in enumerate(tickers, 1):
        try:
            df = load(tk, args.start, args.end)
            if len(df) < 250:
                raise ValueError("less than a year of data")
            rows.append(stock_row(tk, df, args.capital, ZERO if args.no_costs else DELIVERY, **risk_kwargs(args)))
            print(f"  [{n}/{len(tickers)}] {tk.upper()}")
        except Exception as e:  # one bad ticker shouldn't kill a 50-stock scan
            skipped.append(tk)
            print(f"  [{n}/{len(tickers)}] skip {tk}: {e}")

    if not rows:
        raise ValueError("no stocks could be scanned")
    out = pd.DataFrame(rows).set_index("Stock").round(1)
    print(f"\nCAGR % and max drawdown % per strategy, {args.start} -> {args.end or 'today'}\n")
    print(out.to_string())
    print("\nSummary across all stocks\n")
    print(summary(out).to_string())
    if skipped:
        print(f"\nSkipped: {', '.join(skipped)}")

    OUT.mkdir(exist_ok=True)
    path = OUT / f"scan_{args.list if not args.tickers else 'custom'}.csv"
    out.to_csv(path)
    print(f"\nSaved to {path}")


def cmd_today(args):
    """Current signal for each stock, for paper trading. Run after market close (3:30 pm)."""
    start = (pd.Timestamp.today() - pd.Timedelta(days=500)).strftime("%Y-%m-%d")  # enough for SMA 200
    rows, partial = [], False
    for tk in watchlist(args.tickers, args.list):
        try:
            df = download(tk, start, None, use_cache=False)
        except ValueError as e:
            print(f"  skip {tk}: {e}")
            continue
        df, dropped = drop_incomplete_bar(df)
        partial = partial or dropped
        rows.append(signal_row(tk, df))

    if partial:
        print("\nNote: the market is still open, so today's unfinished bar was ignored.")
        print("Signals below are as of the previous close. Run again after 3:45 pm for today's.")
    print("\nSignals at the last close. 'BUY tmrw' / 'SELL tmrw' = act at the next open;")
    print("'hold' = stay in; '-' = stay out. Paper trade these; don't treat them as advice.\n")
    print(pd.DataFrame(rows).to_string(index=False))


def cmd_update(args):
    """Refresh prices/ from Yahoo. GitHub runs this every weekday evening."""
    from backtester import update
    code = update.main(args.tickers)
    if code:
        raise SystemExit(code)


def show(df, results, args, title):
    OUT.mkdir(exist_ok=True)
    print(f"\n{title}")
    print(f"{df.index[0].date()} -> {df.index[-1].date()}  |  {len(df)} trading days  |  "
          f"costs: {'OFF' if args.no_costs else 'Zerodha delivery approx.'}\n")
    print(table(results).to_string())
    if not args.no_plot:
        path = report(df, results, title, OUT / f"{_slug(title)}.png")
        print(f"\nChart saved to {path}")


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in s).strip("_").lower()[:80]


def main():
    p = argparse.ArgumentParser(description="Backtest trading strategies on NSE stocks.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def risk_opts(sp):
        g = sp.add_argument_group("stop-loss and position sizing (percent values)")
        g.add_argument("--stop", type=float, help="stop-loss, %% below entry, e.g. 10")
        g.add_argument("--trailing", action="store_true", help="stop trails the highest close since entry")
        g.add_argument("--size", type=float, default=100, help="%% of equity per trade (default 100)")
        g.add_argument("--vol-target", type=float, help="size so the position's volatility is ~this %%/yr, e.g. 15")
        g.add_argument("--risk", type=float, help="%% of equity lost if the stop hits, e.g. 1 (needs --stop)")

    def common(sp, source=True):
        if source:
            sp.add_argument("source", help="NSE ticker (RELIANCE, TCS, ^NSEI), a .csv file, or DEMO")
        sp.add_argument("--start", default="2015-01-01")
        sp.add_argument("--end", default=None, help=f"default: today ({date.today()})")
        sp.add_argument("--capital", type=float, default=100_000)
        sp.add_argument("--no-costs", action="store_true", help="ignore taxes, charges and slippage")
        sp.add_argument("--no-plot", action="store_true")
        risk_opts(sp)

    t = sub.add_parser("test", help="one strategy vs buy & hold")
    common(t)
    t.add_argument("--strategy", choices=STRATEGIES, default="sma")
    t.add_argument("--param", action="append", help="strategy setting, e.g. fast=20 (repeatable)")
    t.set_defaults(func=cmd_test)

    c = sub.add_parser("compare", help="all strategies side by side")
    common(c)
    c.set_defaults(func=cmd_compare)

    o = sub.add_parser("optimize", help="find best settings on old data, verify on newer data")
    common(o)
    o.add_argument("--strategy", choices=list(GRIDS), default="sma")
    o.add_argument("--split", default="2022-01-01", help="train before this date, test after")
    o.set_defaults(func=cmd_optimize)

    sc = sub.add_parser("scan", help="all strategies across many stocks (watchlist.txt by default)")
    sc.add_argument("tickers", nargs="*", help="e.g. TCS ITC SBIN; empty = the --list file")
    sc.add_argument("--list", default="watchlist", help="watchlist (default) or nifty50, i.e. <name>.txt")
    common(sc, source=False)
    sc.set_defaults(func=cmd_scan)

    td = sub.add_parser("today", help="current buy/sell signals for paper trading")
    td.add_argument("tickers", nargs="*", help="empty = the --list file")
    td.add_argument("--list", default="watchlist")
    td.set_defaults(func=cmd_today)

    up = sub.add_parser("update", help="refresh the saved prices in prices/ (GitHub does this daily)")
    up.add_argument("tickers", nargs="*", help="empty = everything in watchlist.txt and nifty50.txt")
    up.set_defaults(func=cmd_update)

    args = p.parse_args()
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
