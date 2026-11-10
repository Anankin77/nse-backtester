import numpy as np
import pandas as pd
import pytest

from backtester import DELIVERY, ZERO, CostModel, run
from backtester.momentum import momentum_scores, momentum_targets
from backtester.portfolio import run_portfolio
from backtester.walkforward import walk_forward

DP_ONLY = CostModel(0, 0, 0, 0, 0, 0, dp_charge=15.34)


def stock(opens, closes=None, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(opens), name="Date")
    o = np.array(opens, dtype=float)
    c = np.array(closes if closes is not None else opens, dtype=float)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c, "Volume": 1}, index=idx)


def targets(data, rows):
    """rows: {day number: {ticker: weight}} decided at that day's close."""
    idx = next(iter(data.values())).index
    return pd.DataFrame({idx[d]: w for d, w in rows.items()}).T.reindex(columns=list(data)).fillna(0.0)


# ---------- whole-share fills ----------

def test_only_whole_shares_are_bought():
    data = {"A": stock([300] * 4)}
    res = run_portfolio(data, targets(data, {0: {"A": 1.0}}), capital=1000, costs=ZERO, max_weight=1.0)
    assert res.holdings == {"A": 3}                       # 3.33 shares wanted, 3 bought
    assert res.cash == pytest.approx(100)                 # the rest stays in cash
    assert res.equity.iloc[-1] == pytest.approx(1000)
    assert (res.trades["shares"] % 1 == 0).all()


def test_fill_is_at_next_open_not_decision_close():
    data = {"A": stock([100, 200, 200], closes=[100, 200, 400])}
    res = run_portfolio(data, targets(data, {0: {"A": 1.0}}), capital=1000, costs=ZERO, max_weight=1.0)
    assert res.trades.iloc[0]["price"] == 200 and res.holdings == {"A": 5}
    assert res.equity.iloc[0] == 1000                     # nothing owned on the decision day


def test_costs_count_when_sizing_whole_shares():
    data = {"A": stock([100] * 3)}
    res = run_portfolio(data, targets(data, {0: {"A": 1.0}}), capital=1000, costs=DELIVERY, max_weight=1.0)
    assert res.holdings == {"A": 9}                       # 10 shares + charges would cost more than Rs 1,000
    assert res.cash >= 0


def test_one_share_is_bought_if_it_fits_the_per_stock_limit():
    data = {"A": stock([350] * 3), "B": stock([450] * 3)}
    t = targets(data, {0: {"A": 0.25, "B": 0.25}})        # Rs 250 each: less than one share of either
    res = run_portfolio(data, t, capital=1000, costs=ZERO, max_weight=0.40)
    assert res.holdings == {"A": 1}                       # 350 fits the 40% limit, 450 does not


def test_per_stock_limit_caps_a_position():
    data = {"A": stock([10] * 3)}
    res = run_portfolio(data, targets(data, {0: {"A": 1.0}}), capital=1000, costs=ZERO, max_weight=0.25)
    assert res.holdings == {"A": 25}


def test_total_exposure_limit():
    data = {t: stock([10] * 3) for t in "ABCD"}
    t = targets(data, {0: dict.fromkeys("ABCD", 0.25)})
    res = run_portfolio(data, t, capital=1000, costs=ZERO, max_weight=0.25, max_exposure=0.60)
    assert sum(res.holdings.values()) == 60               # 15 shares each, Rs 600 of Rs 1,000
    assert res.exposure.max() <= 0.60 + 1e-9


def test_one_cash_pool_is_shared_and_best_ranked_is_bought_first():
    data = {"A": stock([100] * 3), "B": stock([100] * 3)}
    t = targets(data, {0: {"A": 0.6, "B": 0.6}})
    kw = dict(capital=1000, costs=ZERO, max_weight=0.6, max_exposure=0.8)
    assert run_portfolio(data, t, **kw).holdings == {"A": 4, "B": 4}          # scaled to 80% in total
    prio = pd.DataFrame({"A": [1.0], "B": [2.0]}, index=t.index)              # B ranks higher
    data2 = {"A": stock([100] * 3), "B": stock([150] * 3)}
    res = run_portfolio(data2, t, priority=prio, **kw)
    assert res.holdings == {"A": 4, "B": 2} and res.trades.iloc[0]["ticker"] == "B"
    assert res.cash == pytest.approx(300)


# ---------- DP charges ----------

def test_dp_charge_is_in_the_cost_model():
    assert DELIVERY.dp_charge == 15.34 and ZERO.dp_charge == 0
    assert DELIVERY.sell_proceeds(10_000) == pytest.approx(10_000 * (1 - DELIVERY.sell) - 15.34)


def test_dp_charged_once_per_scrip_per_sell_day():
    data = {"A": stock([100] * 5), "B": stock([100] * 5)}
    t = targets(data, {0: {"A": 0.5, "B": 0.5}, 2: {}})   # buy both, later sell both on one day
    res = run_portfolio(data, t, capital=1000, costs=DP_ONLY, max_weight=0.5)
    assert res.stats["Sells"] == 2
    assert res.cash == pytest.approx(1000 - 2 * 15.34)    # one flat charge per scrip, whatever the share count
    assert res.stats["DP charges"] == pytest.approx(2 * 15.34)


def test_buying_and_holding_pays_no_dp_charge():
    data = {"A": stock([100] * 5)}
    res = run_portfolio(data, targets(data, {0: {"A": 1.0}}), capital=1000, costs=DP_ONLY, max_weight=1.0)
    assert res.equity.iloc[-1] == pytest.approx(1000) and res.stats["DP charges"] == 0


def test_dp_charge_hurts_small_accounts_more():
    data = {"A": stock([100] * 5)}
    t = targets(data, {0: {"A": 1.0}, 2: {}})
    small = run_portfolio(data, t, capital=4_000, costs=DP_ONLY, max_weight=1.0)
    big = run_portfolio(data, t, capital=100_000, costs=DP_ONLY, max_weight=1.0)
    assert small.stats["Total return %"] == pytest.approx(-100 * 15.34 / 4_000)
    assert big.stats["Total return %"] == pytest.approx(-100 * 15.34 / 100_000)


def test_single_stock_engine_also_pays_dp_on_each_sale():
    df = stock([100] * 6)
    sig = pd.Series([1, 1, 0, 0, 0, 0], index=df.index, dtype=float)
    res = run(df, sig, capital=1000, costs=DP_ONLY)
    assert res.equity.iloc[-1] == pytest.approx(1000 - 15.34)
    assert res.trades.iloc[0]["return_pct"] == pytest.approx(-1.53, abs=0.01)


# ---------- rebalancing ----------

def test_rebalance_sells_dropped_stock_and_buys_new_one():
    data = {"A": stock([100] * 6), "B": stock([50] * 6)}
    t = targets(data, {0: {"A": 1.0}, 2: {"B": 1.0}})
    res = run_portfolio(data, t, capital=1000, costs=ZERO, max_weight=1.0)
    assert res.holdings == {"B": 20}
    assert list(res.trades["side"]) == ["buy", "sell", "buy"]     # the sale pays for the purchase
    assert res.trades.iloc[1]["date"] == data["A"].index[3]       # decided day 2, traded day 3


def test_zero_targets_move_everything_to_cash():
    data = {"A": stock([100, 100, 100, 50, 50], closes=[100, 100, 100, 50, 50])}
    t = targets(data, {0: {"A": 1.0}, 1: {}})
    res = run_portfolio(data, t, capital=1000, costs=ZERO, max_weight=1.0)
    assert res.holdings == {} and res.equity.iloc[-1] == pytest.approx(1000)   # out before the fall


def test_small_drift_is_left_alone_but_big_drift_is_trimmed():
    # A doubles: its weight drifts from 50% to 67%
    data = {"A": stock([100, 100, 100, 200, 200, 200]), "B": stock([100] * 6)}
    t = targets(data, {0: {"A": 0.5, "B": 0.5}, 2: {"A": 0.5, "B": 0.5}})
    kw = dict(capital=1000, costs=ZERO, max_weight=0.5)
    wide = run_portfolio(data, t, band=0.60, **kw)
    assert wide.holdings == {"A": 5, "B": 5} and len(wide.trades) == 2        # within the band: no trade
    tight = run_portfolio(data, t, band=0.10, **kw)
    assert tight.holdings == {"A": 3, "B": 7}                                 # back to ~Rs 750 each
    assert tight.equity.iloc[-1] == pytest.approx(1500)                       # rebalancing itself is free at zero cost


def test_stock_without_a_bar_is_not_traded():
    a = stock([100] * 5)
    b = stock([100] * 5).drop(stock([100] * 5).index[1])  # B has no bar on the trade day
    data = {"A": a, "B": b}
    res = run_portfolio(data, targets(data, {0: {"A": 0.5, "B": 0.5}}), capital=1000, costs=ZERO, max_weight=0.5)
    assert res.holdings == {"A": 5}


def test_window_start_uses_the_decision_made_just_before_it():
    data = {"A": stock([100] * 6)}
    t = targets(data, {1: {"A": 1.0}})
    idx = data["A"].index
    res = run_portfolio(data, t, capital=1000, costs=ZERO, max_weight=1.0, start=idx[2])
    assert res.trades.iloc[0]["date"] == idx[2]           # in the market from the first day of the window


# ---------- momentum ----------

def monthly_world(months=20):
    """A rises 5%/month, B 1%/month, C falls 2%/month; the index rises throughout."""
    idx = pd.bdate_range("2020-01-01", periods=21 * months, name="Date")
    t = np.arange(len(idx)) / 21
    mk = lambda g: pd.DataFrame({k: 100 * (1 + g) ** t for k in ("Open", "High", "Low", "Close")}, index=idx).assign(Volume=1)  # noqa: E731
    return {"A": mk(0.05), "B": mk(0.01), "C": mk(-0.02)}, mk(0.01)


def test_momentum_holds_the_strongest_and_skips_latest_month():
    data, index = monthly_world()
    closes = pd.DataFrame({k: v["Close"] for k, v in data.items()})
    w = momentum_targets(closes, index["Close"], "6m", top_n=1, sma_days=0)
    assert w.iloc[:7].sum().sum() == 0                    # needs 6 months + the skipped one
    assert (w.iloc[8:]["A"] == 1).all() and w.iloc[8:][["B", "C"]].sum().sum() == 0
    # a spike in the latest month must not change this month's score
    spiked = closes.copy()
    ends = spiked.index.to_series().groupby(spiked.index.to_period("M")).max()
    spiked.loc[spiked.index > ends.iloc[-2], "C"] *= 10   # only the days after the previous month-end
    assert momentum_scores(spiked, "6m").iloc[-1].equals(momentum_scores(closes, "6m").iloc[-1])


def test_momentum_goes_to_cash_when_index_is_below_its_average():
    data, index = monthly_world()
    closes = pd.DataFrame({k: v["Close"] for k, v in data.items()})
    falling = index["Close"].iloc[::-1].set_axis(index.index)          # index now falls all the way
    w = momentum_targets(closes, falling, "6m", top_n=2, sma_days=200)
    assert w.sum().sum() == 0
    w_up = momentum_targets(closes, index["Close"], "6m", top_n=2, sma_days=200)
    assert w_up.iloc[-1].sum() == pytest.approx(1.0)


def test_walk_forward_windows_do_not_overlap_and_chain():
    data, index = monthly_world(months=50)                # 2020 -> early 2024
    wf = walk_forward(data, index, capital=100_000, first_year=2021, train_years=2, costs=ZERO,
                      max_weight=1.0, grid=[("6m", 1), ("6m", 2)])
    assert list(wf.windows["Train"]) == ["2021-2022", "2022-2023"]
    assert wf.windows["Test"].str[:4].tolist() == ["2023", "2024"]
    assert wf.equity.index.is_unique and wf.equity.index[0].year == 2023
    chained = np.prod(1 + wf.windows["Strategy %"] / 100)
    assert wf.equity["strategy"].iloc[-1] == pytest.approx(chained)
    assert (wf.windows["Chosen"] == "6m top 1").all()     # training picks the setting that owns only A
