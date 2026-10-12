import pandas as pd
import pytest

from backtester import synthetic
from backtester import run, STRATEGIES, ZERO
from backtester.analysis import (PRESETS, drop_incomplete_bar, ranked, signal_row, stock_row,
                                 strategy_table, summary, tournament)


def daily(end="2026-10-12", n=60):
    df = synthetic(n)
    df.index = pd.bdate_range(end=end, periods=n, name="Date")
    return df


@pytest.mark.parametrize("now, dropped", [
    ("2026-10-12 12:38", True),    # Monday, market open: today's bar is unfinished
    ("2026-10-12 16:00", False),   # after close: bar is final
    ("2026-10-13 10:00", False),   # next morning: last bar is yesterday's, already final
    ("2026-10-11 12:00", False),   # Sunday
])
def test_incomplete_bar_dropped_only_while_market_open(now, dropped):
    df = daily()
    out, was_dropped = drop_incomplete_bar(df, pd.Timestamp(now, tz="Asia/Kolkata"))
    assert was_dropped == dropped
    assert len(out) == len(df) - (1 if dropped else 0)


def test_signal_row_labels():
    row = signal_row("abc", synthetic(400))
    assert row["Stock"] == "ABC"
    for s in ("sma", "rsi", "breakout"):
        assert row[s] in {"BUY tmrw", "SELL tmrw", "hold", "-"}


def test_scan_row_and_summary_shapes():
    rows = [stock_row(t, synthetic(1200, seed=i)) for i, t in enumerate(["AAA", "BBB", "CCC"])]
    out = pd.DataFrame(rows).set_index("Stock")
    s = summary(out)
    assert list(s.columns) == ["sma", "rsi", "breakout", "Buy & hold"]
    assert s.loc["Beat B&H on CAGR", "sma"].endswith("/ 3")


# ---------- competition ----------

def companies(n=3, days=1500):
    return {f"C{i}": synthetic(days, seed=i) for i in range(n)}


def test_tournament_has_every_pair_and_hold_has_zero_edge():
    res = tournament(companies(), PRESETS, costs=ZERO)
    assert len(res) == 3 * len(PRESETS)
    hold = res[res["Strategy"] == "Buy & hold"]
    assert (hold["Edge [all]"].abs() < 1e-9).all()


def test_tournament_matches_a_direct_backtest():
    data = companies(1)
    res = tournament(data, {"SMA 20/50 (fast trend)": PRESETS["SMA 20/50 (fast trend)"]}, costs=ZERO)
    df = data["C0"]
    direct = run(df, STRATEGIES["sma"](df, fast=20, slow=50), costs=ZERO).stats
    assert res.iloc[0]["Sharpe [all]"] == pytest.approx(direct["Sharpe"])


def test_fair_test_splits_into_old_and_new():
    split = pd.Timestamp("2022-01-03")
    res = tournament(companies(), PRESETS, split=split)
    assert {"Sharpe [old]", "Sharpe [new]", "Edge [old]", "Edge [new]"} <= set(res.columns)
    r = ranked(res, "Sharpe")
    assert r["Rank"].tolist() == list(range(1, len(r) + 1))
    assert r["Sharpe [old]"].is_monotonic_decreasing      # ranked on old data only
    assert "Rank on new data" in r


def test_strategy_table_counts_best_per_company():
    res = tournament(companies(4), PRESETS)
    t = strategy_table(res, "CAGR %")
    total_best = sum(int(v.split(" / ")[0]) for v in t["Best on"])
    assert total_best == 4                                  # exactly one winner per company
    assert t.loc["Buy & hold", "Beat hold on"] == "-"
