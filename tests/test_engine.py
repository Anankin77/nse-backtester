import numpy as np
import pandas as pd
import pytest

from backtester import DELIVERY, ZERO, STRATEGIES, run, synthetic
from backtester.strategies import rsi


def prices(closes, opens=None):
    idx = pd.bdate_range("2024-01-01", periods=len(closes), name="Date")
    c = np.array(closes, dtype=float)
    o = np.array(opens if opens is not None else closes, dtype=float)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c, "Volume": 1}, index=idx)


def test_buy_and_hold_matches_price_without_costs():
    df = prices([100, 110, 121, 133.1])
    res = run(df, STRATEGIES["buyhold"](df), capital=1000, costs=ZERO)
    # signal at close of day 0 -> buy at open of day 1 (110) -> end 133.1
    assert res.equity.iloc[-1] == pytest.approx(1000 * 133.1 / 110)


def test_no_lookahead_signal_trades_next_open():
    df = prices([100, 100, 200, 200], opens=[100, 100, 150, 200])
    sig = pd.Series([0, 1, 1, 1], index=df.index, dtype=float)  # decided at close of day 1
    res = run(df, sig, capital=1000, costs=ZERO)
    assert res.returns.iloc[1] == 0              # not invested yet on the signal day
    assert res.returns.iloc[2] == pytest.approx(200 / 150 - 1)  # bought at day-2 open


def test_costs_reduce_returns():
    df = synthetic(600)
    sig = STRATEGIES["sma"](df, fast=10, slow=30)
    free = run(df, sig, costs=ZERO)
    paid = run(df, sig, costs=DELIVERY)
    assert paid.equity.iloc[-1] < free.equity.iloc[-1]


def test_trade_log_consistency():
    df = synthetic(800)
    res = run(df, STRATEGIES["sma"](df, fast=10, slow=40), costs=ZERO)
    t = res.trades
    assert len(t) > 0
    assert (pd.to_datetime(t["exit_date"]) >= pd.to_datetime(t["entry_date"])).all()
    # compounding every trade's return must reproduce the final equity
    growth = np.prod(1 + t["return_pct"] / 100)
    assert res.equity.iloc[-1] / 100_000 == pytest.approx(growth, rel=1e-2)


def test_rsi_bounds():
    r = rsi(synthetic(300)["Close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_every_strategy_runs(name):
    df = synthetic(500)
    res = run(df, STRATEGIES[name](df))
    assert np.isfinite(res.equity).all()
    assert set(res.position.unique()) <= {0.0, 1.0}
