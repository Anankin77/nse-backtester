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
    assert ((res.position >= 0) & (res.position <= 1 + 1e-9)).all()


# ---------- stop-losses ----------

def ohlc(rows):
    """rows of (open, high, low, close)"""
    idx = pd.bdate_range("2024-01-01", periods=len(rows), name="Date")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1}, index=idx)


def always_in(df):
    return pd.Series(1.0, index=df.index)


def test_fixed_stop_exits_at_stop_price():
    df = ohlc([(100, 100, 100, 100), (100, 102, 99, 101), (101, 101, 89, 92), (92, 95, 91, 94)])
    res = run(df, always_in(df), capital=1000, costs=ZERO, stop=0.10)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(90)
    assert res.equity.iloc[-1] == pytest.approx(900)        # lost exactly 10%, then stayed out


def test_gap_down_fills_at_open_not_stop():
    df = ohlc([(100, 100, 100, 100), (100, 101, 99, 100), (80, 82, 78, 81)])
    res = run(df, always_in(df), capital=1000, costs=ZERO, stop=0.10)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop (gap)" and t["exit_price"] == pytest.approx(80)


def test_no_reentry_until_signal_resets():
    rows = [(100, 100, 100, 100), (100, 100, 85, 88), (88, 90, 87, 89), (89, 90, 88, 90), (90, 91, 89, 90), (90, 92, 89, 91)]
    df = ohlc(rows)
    sig = pd.Series([1, 1, 1, 0, 1, 1], index=df.index, dtype=float)
    res = run(df, sig, capital=1000, costs=ZERO, stop=0.10)
    assert len(res.trades) == 2                           # stopped, then re-entered only after the 0
    assert res.trades.iloc[1]["entry_date"] == df.index[5].date()


def test_trailing_stop_follows_closes():
    rows = [(100, 100, 100, 100), (100, 110, 99, 110), (110, 121, 109, 120), (120, 120, 107, 109)]
    df = ohlc(rows)
    res = run(df, always_in(df), capital=1000, costs=ZERO, stop=0.10, trailing=True)
    assert res.trades.iloc[0]["exit_price"] == pytest.approx(108)   # 120 peak close * 0.9


# ---------- position sizing ----------

def test_fixed_fraction_size():
    df = prices([100, 100, 110, 110])
    res = run(df, always_in(df), capital=1000, costs=ZERO, size=0.5)
    assert res.equity.iloc[-1] == pytest.approx(1050)     # half invested, +10% on that half


def test_risk_sizing_uses_stop_distance():
    df = prices([100, 100, 100, 100])
    res = run(df, always_in(df), capital=1000, costs=ZERO, stop=0.10, risk=0.01)
    assert res.position.iloc[2] == pytest.approx(0.10)    # 1% risk / 10% stop = 10% of equity


def test_risk_without_stop_is_rejected():
    df = prices([100, 100])
    with pytest.raises(ValueError):
        run(df, always_in(df), risk=0.01)


def test_vol_target_shrinks_position_on_wild_stock():
    calm, wild = synthetic(300, seed=1), synthetic(300, seed=1)
    rng = np.random.default_rng(0)
    shock = np.exp(np.cumsum(rng.normal(0, 0.05, len(wild))))  # same path plus 5%/day extra noise
    for col in ("Open", "High", "Low", "Close"):
        wild[col] = wild[col] * shock
    sig = lambda d: pd.Series([0.0] * 30 + [1.0] * (len(d) - 30), index=d.index)  # noqa: E731
    p_calm = run(calm, sig(calm), vol_target=0.15).position.iloc[40]
    p_wild = run(wild, sig(wild), vol_target=0.15).position.iloc[40]
    assert p_wild < p_calm
