"""The paper broker: daily fills, the ledger, the kill switch and the daily reconcile check."""
import json

import numpy as np
import pandas as pd
import pytest

from backtester import ZERO, synthetic
from backtester.costs import DELIVERY, CostModel
from backtester.paper import (DirStore, GitHubStore, check_prices, load_config, read_account,
                              scorecard, step)
from backtester.portfolio import Book, run_portfolio
from test_github_file import FakeGitHub

TOML = """
[account]
name = "test"
capital = 100000
start = "{start}"
benchmark = "^IDX"
[strategy]
name = "sma"
stocks = ["A", "B", "C", "D"]
fast = 5
slow = 20
[risk]
per_stock = 30
total = 90
{risk}
"""


def world(days=260):
    data = {t: synthetic(days, seed=i + 1) for i, t in enumerate("ABCD")}
    return data, synthetic(days, seed=99)


def cut(data, index, k):
    return {t: df.iloc[:k] for t, df in data.items()}, index.iloc[:k]


def config(index, first=100, risk=""):
    return load_config(TOML.format(start=index.index[first].date(), risk=risk))


def live(cfg, data, index, store, first=100, last=190, costs=DELIVERY):
    """Run the broker once per evening, each time seeing one more day of prices."""
    out = []
    for k in range(first + 1, last + 1):
        d, i = cut(data, index, k)
        out.append(step(cfg, d, i, store, costs=costs, log=lambda *_: None))
    return out


def scaled(df, factor, upto):
    """What Yahoo does on an ex-dividend day: every earlier price multiplied by one factor."""
    df = df.copy()
    df.loc[df.index <= upto, ["Open", "High", "Low", "Close"]] *= factor
    return df


# ---------- fills, ledger, equity ----------

def test_first_run_starts_the_account_and_writes_every_file(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    out = step(cfg, *cut(data, index, 140), store, log=lambda *_: None)
    acct = read_account(store)
    assert out["new_days"] == 40 and acct["state"]["last_day"] == str(index.index[139].date())
    assert len(acct["equity"]) == 40 and len(acct["ledger"]) > 0
    assert (acct["ledger"]["shares"] % 1 == 0).all()                       # whole shares
    assert acct["ledger"]["date"].min() >= str(cfg.start.date())
    assert set(acct["positions"]["ticker"]) == set(acct["state"]["book"]["shares"])
    # the books balance: cash + shares at the close = the account value
    held = (acct["positions"]["shares"] * acct["positions"]["last_close"]).sum()
    assert acct["state"]["book"]["cash"] + held == pytest.approx(acct["equity"]["equity"].iloc[-1], abs=2)


def test_account_waits_until_its_start_date(tmp_path):
    data, index = world()
    cfg, store = config(index, first=200), DirStore(tmp_path)
    assert step(cfg, *cut(data, index, 150), store, log=lambda *_: None)["status"] == "waiting"
    assert store.read("state.json") is None


def test_fills_are_at_the_open_with_full_costs(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=160)
    led = read_account(store)["ledger"]
    for r in led.itertuples():
        assert r.price == pytest.approx(data[r.ticker]["Open"].loc[r.date], abs=1e-3)
        want = r.value * DELIVERY.buy if r.side == "buy" else r.value * DELIVERY.sell + DELIVERY.dp_charge
        assert r.charges == pytest.approx(want, abs=0.01)
    assert (led["side"] == "sell").any()                                   # the DP charge was exercised


def test_day_by_day_equals_one_long_backtest(tmp_path):
    data, index = world()
    cfg = config(index)
    a, b = DirStore(tmp_path / "daily"), DirStore(tmp_path / "once")
    live(cfg, data, index, a)
    step(cfg, *cut(data, index, 190), b, log=lambda *_: None)
    assert a.read("ledger.csv") == b.read("ledger.csv")
    assert a.read("equity.csv") == b.read("equity.csv")
    assert len(read_account(a)["warnings"]) == 0


def test_ledger_only_grows_and_a_second_run_changes_nothing(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=150)
    before = {f: store.read(f) for f in ("ledger.csv", "equity.csv", "state.json")}
    out = step(cfg, *cut(data, index, 150), store, log=lambda *_: None)    # the backup run, same evening
    assert out["new_days"] == 0 and before == {f: store.read(f) for f in before}
    live(cfg, data, index, store, first=150, last=170)
    assert store.read("ledger.csv").startswith(before["ledger.csv"])
    assert store.read("equity.csv").startswith(before["equity.csv"])


def test_a_missed_evening_is_caught_up(tmp_path):
    data, index = world()
    cfg = config(index)
    a, b = DirStore(tmp_path / "a"), DirStore(tmp_path / "b")
    live(cfg, data, index, a, last=180)
    live(cfg, data, index, b, last=150)
    step(cfg, *cut(data, index, 180), b, log=lambda *_: None)              # job was down for 30 days
    assert a.read("ledger.csv") == b.read("ledger.csv")


def test_limits_hold_every_day(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store)
    eq = read_account(store)["equity"]
    assert (eq["invested"] / eq["equity"]).max() <= 0.90 + 0.05            # bought within 90%, may drift a little
    assert (eq["cash"] >= -1e-6).all()
    assert eq["benchmark"].iloc[0] == pytest.approx(100_000, rel=0.05)     # the index held instead


def test_orders_for_the_next_open_are_what_gets_filled(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    checked = 0
    for k in range(101, 170):
        pending = step(cfg, *cut(data, index, k), store, log=lambda *_: None)["pending"]
        if pending:
            step(cfg, *cut(data, index, k + 1), store, log=lambda *_: None)
            led = read_account(store)["ledger"]
            today = led[led["date"] == str(index.index[k].date())]
            assert {(p["ticker"], p["side"]) for p in pending} >= set(zip(today["ticker"], today["side"]))
            checked += len(today)
    assert checked > 3


# ---------- kill switch ----------

def falling(days=260, drop_from=150):
    data, index = world(days)
    for df in data.values():
        f = np.ones(days)
        f[drop_from:] = np.linspace(1, 0.5, days - drop_from)
        df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].mul(f, axis=0)
    return data, index


def test_kill_switch_sells_everything_and_stays_out(tmp_path):
    data, index = falling()
    always = TOML.replace('name = "sma"', 'name = "breakout"').replace("fast = 5\nslow = 20", "lookback = 2\nexit_lookback = 250")
    cfg = load_config(always.format(start=index.index[100].date(), risk="max_drawdown = 10"))
    store = DirStore(tmp_path)
    live(cfg, data, index, store, last=215)
    acct = read_account(store)
    eq, led = acct["equity"], acct["ledger"]
    tripped = eq[eq["killed"] == 1]["date"].iloc[0]
    assert eq[eq["date"] == tripped]["drawdown_pct"].iloc[0] <= -10
    after = led[led["date"] > tripped]
    assert len(after) and (after["side"] == "sell").all() and (after["reason"] == "kill switch").all()
    assert after["date"].nunique() == 1                                    # all out at the very next open
    assert acct["state"]["book"]["shares"] == {} and acct["state"]["book"]["killed"]
    assert eq["equity"].iloc[-1] == pytest.approx(eq[eq["date"] == after["date"].iloc[0]]["equity"].iloc[0])
    assert eq["equity"].iloc[-1] > 0.85 * eq["peak"].iloc[-1]              # the fall stopped near the limit


def test_engine_kill_switch_and_resume():
    idx = pd.bdate_range("2024-01-01", periods=8, name="Date")
    px = [100, 100, 100, 70, 70, 70, 70, 70]
    df = pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px, "Volume": 1}, index=idx, dtype=float)
    t = pd.DataFrame({"A": [1.0]}, index=[idx[0]])
    kw = dict(capital=1000, costs=ZERO, max_weight=1.0, max_drawdown=0.2)
    res = run_portfolio({"A": df}, t, **kw)
    assert list(res.trades["side"]) == ["buy", "sell"] and res.trades.iloc[1]["date"] == idx[4]
    assert res.book.killed and res.book.killed_on == str(idx[3].date()) and res.holdings == {}
    back = run_portfolio({"A": df}, t, resume=idx[6], **kw)
    assert list(back.trades["side"]) == ["buy", "sell", "buy"] and back.trades.iloc[2]["date"] == idx[6]
    assert not back.book.killed and back.book.peak == pytest.approx(700)   # a fresh peak from the resume day


def test_engine_run_can_be_continued_from_its_book():
    data, index = world()
    cal = index.index
    t = pd.DataFrame({k: (v["Close"] > v["Close"].rolling(10).mean()).astype(float) * 0.3 for k, v in data.items()})
    kw = dict(capital=50_000, max_weight=0.3, max_exposure=0.8, calendar=cal, max_drawdown=0.05)
    whole = run_portfolio(data, t, start=cal[50], **kw)
    first = run_portfolio(data, t, start=cal[50], end=cal[150], **kw)
    rest = run_portfolio(data, t, start=cal[151], initial=first.book, **kw)
    joined = pd.concat([first.trades, rest.trades], ignore_index=True)
    joined["date"] = pd.to_datetime(joined["date"])
    pd.testing.assert_frame_equal(joined, whole.trades, check_dtype=False)
    assert rest.equity.iloc[-1] == pytest.approx(whole.equity.iloc[-1])
    assert whole.book.killed                                               # the kill switch state carried over too


def test_min_order_skips_small_orders():
    data, index = world()
    t = pd.DataFrame({k: (v["Close"] > v["Close"].rolling(10).mean()).astype(float) * 0.25 for k, v in data.items()})
    res = run_portfolio(data, t, capital=20_000, max_weight=0.25, band=0.02, min_order=3000, calendar=index.index)
    partial = res.trades[(res.trades["value"] < 3000) & (res.trades["side"] == "buy")]
    assert len(res.trades) > 5 and partial.empty


# ---------- price check and reconcile ----------

def test_revised_past_price_is_flagged_once(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=180)
    fill = read_account(store)["ledger"].iloc[-1]                          # a recent fill
    bad = dict(data)
    bad[fill.ticker] = data[fill.ticker].copy()
    bad[fill.ticker].loc[fill.date, ["Open", "Close"]] *= 1.03             # Yahoo quietly revises one old bar
    out = step(cfg, *cut(bad, index, 181), store, log=lambda *_: None)
    kinds = {w["kind"] for w in out["warnings"]}
    assert "prices revised" in kinds and "fill price differs" in kinds
    assert all(w["found_on"] == str(index.index[180].date()) for w in out["warnings"])
    again = step(cfg, *cut(bad, index, 182), store, log=lambda *_: None)
    assert again["warnings"] == []                                         # reported once, not every day
    assert len(read_account(store)["warnings"]) == len(out["warnings"])


def test_bad_tick_on_a_stock_not_held_is_still_flagged(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=180)
    bad = dict(data)
    bad["D"] = data["D"].copy()
    bad["D"].loc[index.index[175], "Close"] *= 0.5
    out = step(cfg, *cut(bad, index, 181), store, log=lambda *_: None)
    assert any(w["kind"] == "prices revised" and w["ticker"] == "D" for w in out["warnings"])


def test_dividend_is_paid_in_cash_and_is_not_a_warning(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=180)
    book = read_account(store)["state"]["book"]
    tk, qty = next(iter(book["shares"].items()))
    ex = index.index[180]
    paid = dict(data)
    paid[tk] = scaled(data[tk], 0.98, index.index[179])                    # 2% dividend, ex-date = day 180
    outs = live(cfg, paid, index, store, first=180, last=190)
    assert all(o["warnings"] == [] for o in outs)
    acct = read_account(store)
    div = acct["ledger"][acct["ledger"]["side"] == "dividend"]
    assert len(div) == 1 and div.iloc[0]["ticker"] == tk and div.iloc[0]["date"] == str(ex.date())
    assert div.iloc[0]["value"] == pytest.approx(qty * data[tk]["Close"].iloc[179] * 0.02, rel=1e-3)
    assert acct["state"]["checked"]["differences"] == 0 and acct["state"]["checked"]["days"] == 90
    # earlier fills keep the prices of the day; nothing in the ledger was rewritten
    old = acct["ledger"][(acct["ledger"]["ticker"] == tk) & (acct["ledger"]["date"] < str(ex.date()))].iloc[0]
    assert old["price"] == pytest.approx(data[tk]["Open"].loc[old["date"]], abs=1e-3)


def test_split_doubles_the_shares(tmp_path):
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)
    live(cfg, data, index, store, last=180)
    acct = read_account(store)
    tk, qty = next(iter(acct["state"]["book"]["shares"].items()))
    value = acct["equity"]["equity"].iloc[-1]
    split = dict(data)
    split[tk] = scaled(data[tk], 0.5, index.index[179])
    split[tk].loc[split[tk].index > index.index[179], ["Open", "High", "Low", "Close"]] *= 0.5   # trades at half from now on
    out = step(cfg, *cut(split, index, 181), store, log=lambda *_: None)
    acct = read_account(store)
    assert out["warnings"] == [] and (acct["ledger"]["side"] == "split").sum() == 1
    row = acct["ledger"][acct["ledger"]["side"] == "split"].iloc[0]
    assert row["shares"] == 2 * qty
    assert acct["equity"]["equity"].iloc[-1] == pytest.approx(value, rel=0.08)   # no fake 50% loss


def test_check_prices_tells_adjustment_from_revision():
    idx = pd.bdate_range("2024-01-01", periods=10)
    before = pd.DataFrame({"A": np.linspace(100, 110, 10), "B": 50.0, "C": 20.0}, index=idx)
    now = before.copy()
    now["A"] *= 0.97                      # dividend: everything moved together
    now.loc[idx[4], "B"] = 55.0           # one bad tick
    adj, warn = check_prices(before, now)
    assert [(a["ticker"], a["kind"], a["factor"]) for a in adj] == [("A", "dividend", 0.97)]
    assert [(w["ticker"], w["kind"], w["date"]) for w in warn] == [("B", "prices revised", str(idx[4].date()))]
    assert check_prices(before, before) == ([], [])


def test_changing_the_rules_restarts_the_check_without_false_alarms(tmp_path):
    data, index = world()
    store = DirStore(tmp_path)
    live(config(index), data, index, store, last=160)
    tighter = config(index, risk="max_drawdown = 15")
    outs = live(tighter, data, index, store, first=160, last=180)
    assert all(o["warnings"] == [] for o in outs)
    state = read_account(store)["state"]
    assert state["checkpoint"]["day"] == str(index.index[159].date())
    assert state["checked"]["days"] == 20


# ---------- config, scorecard, private repo ----------

def test_config_rejects_unknown_strategy_and_reads_list_files():
    with pytest.raises(ValueError):
        load_config('[strategy]\nname = "magic"\nstocks = ["A"]')
    cfg = load_config('[strategy]\nname = "momentum"\nstocks = "nifty50"\ntop_n = 3')
    assert len(cfg.stocks) == 50 and cfg.params["top_n"] == 3 and cfg.per_stock == 0.25
    same = load_config('[strategy]\nname = "momentum"\nstocks = "nifty50"\ntop_n = 3')
    other = load_config('[strategy]\nname = "momentum"\nstocks = "nifty50"\ntop_n = 5')
    assert cfg.key == same.key != other.key


def test_repo_paper_toml_is_valid():
    cfg = load_config()
    assert cfg.strategy == "momentum" and cfg.max_drawdown == 0.20 and cfg.capital > 0


def test_scorecard_needs_time_and_a_real_lead_over_the_index():
    days = pd.bdate_range("2026-01-01", periods=200).strftime("%Y-%m-%d")
    eq = pd.DataFrame({"date": days, "equity": np.linspace(100, 120, 200), "benchmark": np.linspace(100, 110, 200),
                       "killed": 0})
    none = pd.DataFrame(columns=["found_on", "date", "ticker", "kind", "detail"])
    assert all(c["ok"] for c in scorecard(eq, none, capital=100))
    assert not scorecard(eq.head(50), none, capital=100)[0]["ok"]          # too short
    behind = eq.assign(equity=np.linspace(100, 105, 200))
    assert not scorecard(behind, none, capital=100)[1]["ok"]
    barely = eq.assign(equity=np.linspace(100, 110.5, 200))                # ahead, but by less than the dividends
    assert not scorecard(barely, none, capital=100)[1]["ok"]
    warned = pd.DataFrame([{"found_on": days[-3], "date": days[-9], "ticker": "A", "kind": "prices revised", "detail": ""}])
    assert not scorecard(eq, warned, capital=100)[-1]["ok"]


def test_scorecard_ignores_days_filled_in_from_history(tmp_path):
    days = pd.bdate_range("2026-01-01", periods=200).strftime("%Y-%m-%d")
    eq = pd.DataFrame({"date": days, "equity": np.linspace(100, 120, 200), "benchmark": np.linspace(100, 110, 200),
                       "killed": 0})
    none = pd.DataFrame(columns=["found_on", "date", "ticker", "kind", "detail"])
    assert not scorecard(eq, none, live_since=days[150], capital=100)[0]["ok"]   # only 50 days were live
    data, index = world()
    cfg, store = config(index), DirStore(tmp_path)                        # the account is back-dated to day 100
    live(cfg, data, index, store, first=149, last=160)                    # but the bot first ran on day 150
    assert read_account(store)["state"]["live_since"] == str(index.index[149].date())


def test_bot_writes_to_the_private_repo(tmp_path):
    data, index = world()
    gh = FakeGitHub(repo="me/nse-paper-log")
    store = GitHubStore("me/nse-paper-log", "good", session=gh, api="https://api.github.com")
    cfg = config(index)
    live(cfg, data, index, store, last=130)
    assert {"bot/ledger.csv", "bot/equity.csv", "bot/positions.csv", "bot/state.json", "bot/snapshot.csv"} <= set(gh.files)
    local = DirStore(tmp_path)
    live(cfg, data, index, local, last=130)
    fresh = GitHubStore("me/nse-paper-log", "good", session=gh)            # e.g. the web app reading it back
    assert fresh.read("ledger.csv") == local.read("ledger.csv")
    assert json.loads(fresh.read("state.json"))["book"] == json.loads(local.read("state.json"))["book"]
    assert Book.from_dict(json.loads(fresh.read("state.json"))["book"]).cash >= 0
    assert isinstance(CostModel(), CostModel)
