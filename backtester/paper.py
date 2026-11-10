"""The paper broker: a fake account that trades itself every evening.

Run by the daily price job right after prices are saved (or: python run.py paper).
It reads paper.toml (stocks, strategy, capital, risk rules) and then:

  1. PRICE CHECK   compares today's saved prices with the copy it kept yesterday.
                   A stock whose whole history moved by one factor had a dividend
                   or split: the account gets the dividend in cash, or the new
                   share count. Anything else is Yahoo revising the past: a warning.
  2. FILLS         every trading day since the last run: the decision made at the
                   previous close is filled at that day's open, whole shares, full
                   costs, one cash pool, per-stock and total limits, kill switch.
  3. RECONCILE     re-runs the backtester over the paper period with today's prices
                   and compares it with the ledger, fill by fill and day by day.
                   Any difference is a warning (revised prices, late data, or a bug).
  4. ORDERS        works out what to buy and sell at the next open.

What it writes (to the bot/ folder of the private nse-paper-log repo):
  ledger.csv     every fill, dividend and split. Rows are only ever added.
  equity.csv     one row per day: account value, cash, the index held instead.
  positions.csv  what is held now.
  warnings.csv   every price-check and reconcile warning. Rows are only ever added.
  state.json     where the run got to, and the orders for the next open.
  snapshot.csv   yesterday's copy of recent prices, for the price check.

Fills use the prices as they were on the day (not adjusted backwards later), so
the ledger never needs rewriting when a dividend is paid.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .costs import DELIVERY, CostModel
from .data import ROOT, download
from .github_file import GitHubFile
from .momentum import LOOKBACKS, momentum_scores, momentum_targets
from .portfolio import Book, panel, run_portfolio
from .strategies import STRATEGIES

CONFIG_FILE = ROOT / "paper.toml"
LEDGER_COLS = ["date", "ticker", "side", "shares", "price", "value", "charges", "cash_after", "reason"]
EQUITY_COLS = ["date", "equity", "cash", "invested", "benchmark", "peak", "drawdown_pct", "killed"]
WARNING_COLS = ["found_on", "date", "ticker", "kind", "detail"]
POSITION_COLS = ["ticker", "shares", "avg_cost", "last_close", "value", "pnl", "pnl_pct", "weight_pct"]
SNAPSHOT_DAYS = 60       # how much recent history the price check watches
PRICE_TOL = 2e-4         # saved prices have 4 decimals; smaller differences are rounding
SIGNAL_STRATEGIES = [s for s in STRATEGIES if s != "buyhold"]


# ------------------------------------------------------------------ config

@dataclass(frozen=True)
class Config:
    name: str
    capital: float
    start: pd.Timestamp
    benchmark: str
    strategy: str
    stocks: tuple
    params: dict = field(default_factory=dict, hash=False)
    per_stock: float = 0.25          # most of the account in one stock (fraction)
    total: float = 1.0               # most of the account in stocks
    max_drawdown: float | None = None
    min_order: float = 0.0
    band: float = 0.20
    resume: pd.Timestamp | None = None

    @property
    def key(self) -> str:
        """Changes whenever a rule changes. The daily check restarts from that day."""
        d = {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in self.__dict__.items()}
        d["stocks"], d["params"] = list(self.stocks), dict(sorted(self.params.items()))
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def read_list(name: str) -> list[str]:
    path = ROOT / f"{name}.txt"
    if not path.exists():
        raise ValueError(f"{path.name} not found (paper.toml: stocks)")
    return [ln.strip().upper() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def load_config(source: str | Path = CONFIG_FILE) -> Config:
    """Read paper.toml (a path, or the text itself)."""
    import tomllib  # Python 3.11+

    text = source.read_text() if isinstance(source, Path) else source
    raw = tomllib.loads(text)
    acct, strat, risk = raw.get("account", {}), dict(raw.get("strategy", {})), raw.get("risk", {})
    name = strat.pop("name", "momentum")
    if name != "momentum" and name not in SIGNAL_STRATEGIES:
        raise ValueError(f"paper.toml: unknown strategy '{name}'. Use momentum, {', '.join(SIGNAL_STRATEGIES)}.")
    stocks = strat.pop("stocks", "nifty50")
    stocks = read_list(stocks) if isinstance(stocks, str) else [str(s).upper() for s in stocks]
    stocks = [s for s in dict.fromkeys(stocks) if not s.startswith("^")]
    if not stocks:
        raise ValueError("paper.toml: no stocks to trade")
    if name == "momentum" and strat.get("lookback", "6m+12m") not in LOOKBACKS:
        raise ValueError(f"paper.toml: lookback must be one of {', '.join(LOOKBACKS)}")
    pct = lambda v: None if v in (None, 0) else float(v) / 100  # noqa: E731
    cfg = Config(
        name=str(acct.get("name", "paper")), capital=float(acct.get("capital", 100_000)),
        start=pd.Timestamp(str(acct.get("start", pd.Timestamp.today().date()))),
        benchmark=str(acct.get("benchmark", "^NSEI")), strategy=name, stocks=tuple(stocks), params=strat,
        per_stock=pct(risk.get("per_stock", 25)) or 1.0, total=pct(risk.get("total", 100)) or 1.0,
        max_drawdown=pct(risk.get("max_drawdown")), min_order=float(risk.get("min_order", 0)),
        band=float(risk.get("band", 20)) / 100,
        resume=pd.Timestamp(str(risk["resume"])) if risk.get("resume") else None,
    )
    if cfg.capital <= 0 or not 0 < cfg.per_stock <= 1 or not 0 < cfg.total <= 1:
        raise ValueError("paper.toml: capital must be positive, per_stock and total between 1 and 100")
    return cfg


def build_targets(cfg: Config, data: dict[str, pd.DataFrame], index_df: pd.DataFrame):
    """(target weights per decision day, buy priority or None) for the configured strategy."""
    cal = index_df.index
    if cfg.strategy == "momentum":
        closes = panel(data, "Close", cal)
        p = cfg.params
        lookback, top_n = p.get("lookback", "6m+12m"), int(p.get("top_n", 5))
        targets = momentum_targets(closes, index_df["Close"], lookback, top_n, sma_days=int(p.get("sma_days", 200)))
        return targets, momentum_scores(closes, lookback)
    # one signal per stock: every stock gets the same slot, so nothing is resized when signals change
    slot = min(cfg.per_stock, cfg.total / len(data))
    signals = {t: STRATEGIES[cfg.strategy](df, **cfg.params).reindex(cal).ffill().fillna(0.0) for t, df in data.items()}
    return pd.DataFrame(signals) * slot, None


# ------------------------------------------------------------------ where the files live

class DirStore:
    """Files in a local folder (tests, and trying the bot on your laptop)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.where = f"the folder {self.root}"

    def read(self, name: str) -> str | None:
        f = self.root / name
        return f.read_text() if f.exists() else None

    def write(self, name: str, text: str, message: str = ""):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(text)


class GitHubStore:
    """Files in the bot/ folder of the private paper-log repo."""

    def __init__(self, repo: str, token: str, prefix: str = "bot", session=None, api: str = "https://api.github.com"):
        self.repo, self.token, self.prefix, self.session, self.api = repo, token, prefix.strip("/"), session, api
        self.where = f"your private repo {repo}"
        self._sha: dict = {}

    def _file(self, name: str) -> GitHubFile:
        return GitHubFile(self.repo, f"{self.prefix}/{name}", self.token, session=self.session, api=self.api)

    def read(self, name: str) -> str | None:
        text, self._sha[name] = self._file(name).read()
        return text

    def write(self, name: str, text: str, message: str = ""):
        if name not in self._sha:
            self.read(name)
        self._sha[name] = self._file(name).write(text, self._sha[name], message or f"Paper bot: {name}")


def open_store(env=os.environ):
    """PAPER_LOG_DIR (a folder) or PAPER_LOG_REPO + PAPER_LOG_TOKEN (the private repo). None if neither."""
    if env.get("PAPER_LOG_DIR"):
        return DirStore(env["PAPER_LOG_DIR"])
    if env.get("PAPER_LOG_REPO") and env.get("PAPER_LOG_TOKEN"):
        return GitHubStore(env["PAPER_LOG_REPO"], env["PAPER_LOG_TOKEN"])
    return None


def _table(text: str | None, cols: list[str]) -> pd.DataFrame:
    if not text or not text.strip():
        return pd.DataFrame(columns=cols)
    return pd.read_csv(io.StringIO(text), dtype={"date": str, "found_on": str, "ticker": str})


def _append(old: str | None, rows: pd.DataFrame, cols: list[str], fmt: str) -> str:
    """Old text plus new lines. Existing lines are never touched."""
    head = old if old and old.strip() else ",".join(cols) + "\n"
    if not head.endswith("\n"):
        head += "\n"
    if rows.empty:
        return head
    return head + rows[cols].to_csv(index=False, header=False, float_format=fmt, lineterminator="\n")


def read_account(store) -> dict:
    """Everything the bot has written, as tables. Used by the web page."""
    state = store.read("state.json")
    return {
        "state": json.loads(state) if state else None,
        "ledger": _table(store.read("ledger.csv"), LEDGER_COLS),
        "equity": _table(store.read("equity.csv"), EQUITY_COLS),
        "positions": _table(store.read("positions.csv"), POSITION_COLS),
        "warnings": _table(store.read("warnings.csv"), WARNING_COLS),
    }


# ------------------------------------------------------------------ price check

def snapshot(closes: pd.DataFrame) -> str:
    return closes.tail(SNAPSHOT_DAYS).to_csv(float_format="%.4f", lineterminator="\n")


def check_prices(before: pd.DataFrame, now: pd.DataFrame, tol: float = PRICE_TOL):
    """Compare yesterday's copy of recent closes with today's, on the days both cover.

    Returns (adjustments, warnings). An adjustment is a whole history moved by one
    factor (dividend, split, bonus). Everything else is a warning.
    """
    adjustments, warnings = [], []
    days = before.index.intersection(now.index)
    gone = before.index.difference(now.index)
    gone = gone[gone >= now.index[0]] if len(now) else gone
    cols = [t for t in before.columns if t in now.columns]
    B, N, G = before.loc[days, cols].to_numpy(float), now.loc[days, cols].to_numpy(float), before.loc[gone, cols].notna()
    for k, t in enumerate(cols):
        old, new = B[:, k], N[:, k]
        had, has = np.isfinite(old), np.isfinite(new)
        lost = list(days[had & ~has]) + list(gone[G[t].to_numpy()])
        if lost:
            warnings.append(_warn(min(lost), t, "prices removed",
                                  f"{len(lost)} day(s) that were saved before are gone, first {min(lost).date()}"))
        first = np.argmax(had) if had.any() else len(days)
        added = days[~had & has & (np.arange(len(days)) > first)]
        if len(added):
            warnings.append(_warn(added[0], t, "prices added late",
                                  f"{len(added)} past day(s) appeared that were missing before, first {added[0].date()}"))
        both = had & has
        if not both.any():
            continue
        ratio = pd.Series(new[both] / old[both], index=days[both])
        if (ratio - 1).abs().max() <= tol:
            continue
        f = float(ratio.median())
        uniform = len(ratio) >= 3 and (ratio / f - 1).abs().max() <= 5 * tol
        if uniform and f < 1 - tol:
            adjustments.append({"ticker": t, "after": str(ratio.index[-1].date()), "factor": round(f, 6),
                                "kind": "dividend" if f >= 0.8 else "split"})
        elif uniform and f > 1.02:
            adjustments.append({"ticker": t, "after": str(ratio.index[-1].date()), "factor": round(f, 6), "kind": "split"})
        else:
            # leave out the part explained by a uniform shift, report the days that moved on their own
            base = f if abs(f - 1) > tol and ((ratio / f - 1).abs() <= 5 * tol).mean() > 0.5 else 1.0
            off = (ratio / base - 1)
            bad = off[off.abs() > tol]
            worst = bad.abs().idxmax()
            warnings.append(_warn(bad.index[0], t, "prices revised",
                                  f"{len(bad)} past close(s) changed after the bot saw them; "
                                  f"largest {100 * off[worst]:+.2f}% on {worst.date()}"))
    return adjustments, warnings


def _warn(day, ticker, kind, detail) -> dict:
    return {"date": str(pd.Timestamp(day).date()) if day is not None else "", "ticker": ticker,
            "kind": kind, "detail": detail}


def deadjust(data: dict[str, pd.DataFrame], adjustments: list[dict]) -> dict[str, pd.DataFrame]:
    """Prices as they were on the day: undo each later dividend/split adjustment."""
    out = dict(data)
    for a in adjustments:
        t = a["ticker"]
        if t not in out:
            continue
        df = out[t].copy() if out[t] is data.get(t) else out[t]
        mask = df.index <= pd.Timestamp(a["after"])
        df.loc[mask, ["Open", "High", "Low", "Close"]] = df.loc[mask, ["Open", "High", "Low", "Close"]] / a["factor"]
        out[t] = df
    return out


def _actions(adjustments: list[dict], cal: pd.DatetimeIndex) -> list:
    out = []
    for a in adjustments:
        k = cal.searchsorted(pd.Timestamp(a["after"]), side="right")
        if k < len(cal):
            out.append((cal[k], a["ticker"], a["kind"], a["factor"]))
    return out


# ------------------------------------------------------------------ reconcile

def compare(ledger: pd.DataFrame, equity: pd.DataFrame, rerun) -> list[dict]:
    """Differences between what the ledger says happened and what the backtester does now."""
    out = []
    key = ["date", "ticker", "side"]
    a = ledger[ledger["side"].isin(["buy", "sell"])].copy()
    b = rerun.trades[rerun.trades["side"].isin(["buy", "sell"])].copy()
    b["date"] = pd.to_datetime(b["date"]).dt.strftime("%Y-%m-%d")
    m = a.merge(b, on=key, how="outer", suffixes=("_ledger", "_now"), indicator=True).sort_values(key)
    for _, r in m.iterrows():
        if r["_merge"] == "left_only":
            out.append(_warn(r["date"], r["ticker"], "fill not repeated",
                             f"ledger has {r['side']} {int(r['shares_ledger'])} @ {r['price_ledger']:.2f}; "
                             "the backtester no longer makes this trade"))
        elif r["_merge"] == "right_only":
            out.append(_warn(r["date"], r["ticker"], "fill missing from ledger",
                             f"the backtester now makes {r['side']} {int(r['shares_now'])} @ {r['price_now']:.2f}; "
                             "the ledger has no such trade"))
        elif int(r["shares_ledger"]) != int(r["shares_now"]):
            out.append(_warn(r["date"], r["ticker"], "share count differs",
                             f"{r['side']}: ledger {int(r['shares_ledger'])}, backtester {int(r['shares_now'])}"))
        elif abs(r["price_ledger"] - r["price_now"]) > 0.001 + PRICE_TOL * abs(r["price_ledger"]):
            out.append(_warn(r["date"], r["ticker"], "fill price differs",
                             f"{r['side']}: ledger {r['price_ledger']:.2f}, backtester {r['price_now']:.2f} "
                             f"({100 * (r['price_now'] / r['price_ledger'] - 1):+.2f}%)"))
    if len(equity):
        was = equity.set_index("date")["equity"].astype(float)
        now = rerun.equity.copy()
        now.index = now.index.strftime("%Y-%m-%d")
        both = was.index.intersection(now.index)
        diff = (now[both] / was[both] - 1)
        bad = diff[diff.abs() > PRICE_TOL]
        if len(bad):
            worst = bad.abs().idxmax()
            out.append(_warn(bad.index[0], "", "account value differs",
                             f"{len(bad)} day(s) from {bad.index[0]}; largest {100 * diff[worst]:+.2f}% on {worst} "
                             f"(ledger {was[worst]:,.2f}, backtester {now[worst]:,.2f})"))
    return out


# ------------------------------------------------------------------ the daily step

def positions_table(ledger: pd.DataFrame, book: Book, closes: pd.Series, equity: float) -> pd.DataFrame:
    cost, qty = {}, {}
    for r in ledger.itertuples():
        t = r.ticker
        if r.side == "buy":
            cost[t] = cost.get(t, 0.0) + r.value + r.charges
            qty[t] = qty.get(t, 0) + r.shares
        elif r.side == "sell" and qty.get(t):
            keep = max(qty[t] - r.shares, 0) / qty[t]
            cost[t], qty[t] = cost[t] * keep, qty[t] - r.shares
        elif r.side == "split":
            qty[t] = r.shares
    rows = []
    for t, q in sorted(book.shares.items()):
        px = float(closes.get(t, np.nan))
        value, paid = q * px, cost.get(t, np.nan)
        rows.append({"ticker": t, "shares": q, "avg_cost": paid / q, "last_close": px, "value": value,
                     "pnl": value - paid, "pnl_pct": 100 * (value / paid - 1) if paid else np.nan,
                     "weight_pct": 100 * value / equity if equity else 0.0})
    return pd.DataFrame(rows, columns=POSITION_COLS)


def step(cfg: Config, data: dict[str, pd.DataFrame], index_df: pd.DataFrame, store,
         costs: CostModel = DELIVERY, log=print) -> dict:
    """One run of the paper broker. Safe to run twice: days already done are not repeated."""
    cal = index_df.index
    data = {t: data[t] for t in cfg.stocks if t in data}
    missing = [t for t in cfg.stocks if t not in data]
    state = json.loads(store.read("state.json") or "null")
    first_days = cal[cal >= cfg.start]
    if state is None and not len(first_days):
        log(f"Paper account starts {cfg.start.date()}; prices only reach {cal[-1].date()}. Nothing to do yet.")
        return {"status": "waiting", "new_days": 0, "warnings": []}

    found_on = str(cal[-1].date())
    warnings: list[dict] = []
    adjustments = list(state["adjustments"]) if state else []

    # 1. price check against yesterday's copy
    closes_now = panel({**data, cfg.benchmark: index_df}, "Close")
    snap = store.read("snapshot.csv")
    if snap:
        before = pd.read_csv(io.StringIO(snap), index_col=0, parse_dates=True)
        new_adj, found = check_prices(before, closes_now)
        adjustments += new_adj
        warnings += found
        for a in new_adj:
            log(f"  {a['ticker']}: {a['kind']} detected (prices before {a['after']} moved x{a['factor']})")
    if missing:
        warnings.append(_warn(found_on, ",".join(missing), "no prices", "listed in paper.toml but no saved prices"))

    targets, priority = build_targets(cfg, data, index_df)
    targets = targets.loc[cfg.start - pd.Timedelta(days=45):]
    seen = deadjust(data, adjustments)                      # prices as they were on each day
    kw = dict(capital=cfg.capital, costs=costs, max_weight=cfg.per_stock, max_exposure=cfg.total, band=cfg.band,
              priority=priority, calendar=cal, max_drawdown=cfg.max_drawdown, resume=cfg.resume,
              min_order=cfg.min_order, actions=_actions(adjustments, cal))

    ledger_text, equity_text = store.read("ledger.csv"), store.read("equity.csv")
    if state is None:
        book, new_days, checkpoint = None, first_days, {"day": None, "book": None}
        units = cfg.capital / (float(index_df["Open"].loc[first_days[0]]) * (1 + costs.buy))
        pending = []
    else:
        book, new_days = Book.from_dict(state["book"]), cal[cal > pd.Timestamp(state["last_day"])]
        checkpoint, units, pending = state["checkpoint"], state["benchmark_units"], state.get("pending", [])
        if state["config"] != cfg.key:
            checkpoint = {"day": state["last_day"], "book": state["book"]}
            log("  paper.toml changed: the daily check restarts from today.")

    # 2. fills for every new day
    fills = pd.DataFrame(columns=LEDGER_COLS)
    if len(new_days):
        res = run_portfolio(seen, targets, start=new_days[0], end=new_days[-1], initial=book, **kw)
        book, pending = res.book, res.pending
        fills = res.trades.copy()
        fills["date"] = pd.to_datetime(fills["date"]).dt.strftime("%Y-%m-%d")
        eq = res.daily.reset_index(names="date")
        eq["benchmark"] = units * index_df["Close"].reindex(res.daily.index).ffill().to_numpy()
        eq["date"] = eq["date"].dt.strftime("%Y-%m-%d")
        eq["killed"] = eq["killed"].astype(int)
        done = _table(equity_text, EQUITY_COLS)["date"].max() if equity_text else None
        if isinstance(done, str):                            # an earlier run died half-way: don't write days twice
            fills, eq = fills[fills["date"] > done], eq[eq["date"] > done]
        ledger_text = _append(ledger_text, fills, LEDGER_COLS, "%.4f")
        equity_text = _append(equity_text, eq, EQUITY_COLS, "%.2f")
    last_day = str((new_days[-1] if len(new_days) else pd.Timestamp(state["last_day"])).date())
    ledger, equity = _table(ledger_text, LEDGER_COLS), _table(equity_text, EQUITY_COLS)

    # 3. reconcile: the backtester over the paper period, with today's prices, against the ledger
    since = pd.Timestamp(checkpoint["day"]) if checkpoint["day"] else None
    span = cal[(cal > since) if since is not None else (cal >= cfg.start)]
    span = span[span <= pd.Timestamp(last_day)]
    checked = {"on": found_on, "from": str(span[0].date()) if len(span) else last_day, "days": len(span), "differences": 0}
    if state is not None and len(span):
        again = run_portfolio(seen, targets, start=span[0], end=span[-1],
                              initial=Book.from_dict(checkpoint["book"]) if checkpoint["book"] else None, **kw)
        lo = str(span[0].date())
        diffs = compare(ledger[ledger["date"] >= lo], equity[equity["date"] >= lo], again)
        checked["differences"] = len(diffs)
        if diffs:
            warnings += diffs
            checkpoint = {"day": last_day, "book": book.to_dict()}   # reported once; check on from here

    # 4. save
    for w in warnings:
        w["found_on"] = found_on
    if warnings:
        log(f"  {len(warnings)} WARNING(S):")
        for w in warnings:
            log(f"    {w['date']} {w['ticker']:<12} {w['kind']}: {w['detail']}")
    last_close = closes_now.ffill().loc[:last_day].iloc[-1]
    seen_close = panel(seen, "Close").ffill().loc[:last_day].iloc[-1]
    value = float(equity["equity"].iloc[-1]) if len(equity) else cfg.capital
    positions = positions_table(ledger, book, seen_close, value)
    new_state = {
        "version": 1, "account": cfg.name, "last_day": last_day, "updated": found_on,
        "book": book.to_dict(), "config": cfg.key, "checkpoint": checkpoint, "adjustments": adjustments,
        "benchmark_units": units, "pending": pending, "checked": checked,
        "live_since": state["live_since"] if state else max(found_on, str(cfg.start.date())),
        "rules": {"strategy": cfg.strategy, "params": cfg.params, "stocks": len(cfg.stocks), "capital": cfg.capital,
                  "benchmark": cfg.benchmark, "per_stock_pct": 100 * cfg.per_stock, "total_pct": 100 * cfg.total,
                  "max_drawdown_pct": 100 * cfg.max_drawdown if cfg.max_drawdown else None,
                  "min_order": cfg.min_order, "start": str(cfg.start.date())},
    }
    note = f"Paper bot {last_day}: {len(fills)} fill(s), {len(warnings)} warning(s)"
    if len(new_days):
        store.write("ledger.csv", ledger_text, note)
        store.write("equity.csv", equity_text, note)
    store.write("positions.csv", positions.to_csv(index=False, float_format="%.2f", lineterminator="\n"), note)
    if warnings or store.read("warnings.csv") is None:
        store.write("warnings.csv", _append(store.read("warnings.csv"), pd.DataFrame(warnings, columns=WARNING_COLS),
                                            WARNING_COLS, "%.4f"), note)
    store.write("snapshot.csv", snapshot(closes_now), note)
    store.write("state.json", json.dumps(new_state, indent=1) + "\n", note)   # last: marks the run as complete

    bench = float(equity["benchmark"].iloc[-1]) if len(equity) else cfg.capital
    log(f"{cfg.name}: {len(new_days)} new day(s) up to {last_day}, {len(fills)} fill(s). "
        f"Account Rs {value:,.0f} ({100 * (value / cfg.capital - 1):+.1f}%), "
        f"{cfg.benchmark} held instead Rs {bench:,.0f} ({100 * (bench / cfg.capital - 1):+.1f}%).")
    if book.killed:
        log(f"  KILL SWITCH is on since {book.killed_on}: everything is sold, nothing is bought.")
    for p in pending:
        log(f"  next open: {p['side'].upper():<4} {p['shares']:>5} {p['ticker']:<12} ~Rs {p['est_price']:,.2f}")
    return {"status": "ok", "new_days": len(new_days), "fills": len(fills), "warnings": warnings,
            "pending": pending, "equity": value, "benchmark": bench, "killed": book.killed, "last_day": last_day}


# ------------------------------------------------------------------ is it good enough for real money?

DIVIDEND_YIELD = 0.012   # the index number leaves out dividends (~1.2%/yr); the bot gets its dividends


def scorecard(equity: pd.DataFrame, warnings: pd.DataFrame, max_drawdown_pct: float | None = None,
              live_since: str | None = None, capital: float | None = None, min_days: int = 180) -> list[dict]:
    """Plain pass/fail checks to clear before any real money follows this bot.

    Only days from `live_since` count: days the bot traded as they happened. Anything
    earlier was filled in from history when the account was set up, which is a
    backtest, not paper trading.
    """
    if not len(equity):
        return []
    eq = equity.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    since = pd.Timestamp(live_since) if live_since else eq["date"].iloc[0]
    earlier, eq = eq[eq["date"] < since], eq[eq["date"] >= since]
    if not len(eq):
        return []
    base = earlier.iloc[-1] if len(earlier) else None
    mine0 = float(base["equity"]) if base is not None else float(capital or eq["equity"].iloc[0])
    idx0 = float(base["benchmark"]) if base is not None else float(capital or eq["benchmark"].iloc[0])
    span = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days
    mine = pd.concat([pd.Series([mine0]), eq["equity"].astype(float)], ignore_index=True)
    idx = pd.concat([pd.Series([idx0]), eq["benchmark"].astype(float)], ignore_index=True)
    my_ret, idx_ret = 100 * (mine.iloc[-1] / mine0 - 1), 100 * (idx.iloc[-1] / idx0 - 1)
    hurdle = idx_ret + 100 * DIVIDEND_YIELD * span / 365
    my_dd = 100 * float((mine / mine.cummax() - 1).min())
    idx_dd = 100 * float((idx / idx.cummax() - 1).min())
    recent = warnings[pd.to_datetime(warnings["found_on"]) > eq["date"].iloc[-1] - pd.Timedelta(days=30)] if len(warnings) else warnings
    return [
        {"check": f"At least {min_days // 30} months of live paper trading", "ok": span >= min_days,
         "detail": f"{span} days so far"},
        {"check": "Ahead of holding the index, after costs and the index's dividends", "ok": bool(my_ret > hurdle),
         "detail": f"bot {my_ret:+.1f}%, index {idx_ret:+.1f}% (+{hurdle - idx_ret:.1f}% dividends)"},
        {"check": "Worst fall no deeper than the index's", "ok": bool(my_dd >= idx_dd),
         "detail": f"bot {my_dd:.1f}%, index {idx_dd:.1f}%"},
        {"check": "Kill switch never tripped", "ok": not bool(eq["killed"].astype(int).any()),
         "detail": "" if max_drawdown_pct is None else f"limit {max_drawdown_pct:.0f}%"},
        {"check": "No price or reconcile warnings in the last 30 days", "ok": len(recent) == 0,
         "detail": f"{len(recent)} warning(s)"},
    ]


# ------------------------------------------------------------------ command line

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Run the paper broker once (the daily price job does this).")
    ap.add_argument("--config", default=str(CONFIG_FILE))
    ap.add_argument("--dir", help="write to this folder instead of the private repo (for trying it out)")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.dir:
        env["PAPER_LOG_DIR"] = args.dir
    store = open_store(env)
    if store is None:
        msg = ("Paper bot skipped: no place to write. Set the PAPER_LOG_TOKEN secret (see README), "
               "or run with --dir paper_log to try it locally.")
        print(f"::warning::{msg}" if env.get("GITHUB_ACTIONS") else msg)
        return 0
    if not Path(args.config).exists():
        print(f"Paper bot skipped: {args.config} not found.")
        return 0
    cfg = load_config(Path(args.config))
    index_df = download(cfg.benchmark, "2000-01-01")
    data = {}
    for t in cfg.stocks:
        try:
            data[t] = download(t, "2000-01-01")
        except Exception as e:
            print(f"  no prices for {t}: {str(e).splitlines()[0][:100]}")
    out = step(cfg, data, index_df, store)
    print(f"Written to {store.where}.")
    if env.get("GITHUB_ACTIONS"):
        for w in out["warnings"]:
            print(f"::warning::{w['date']} {w['ticker']} {w['kind']}: {w['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
