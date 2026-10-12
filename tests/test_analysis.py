import pandas as pd
import pytest

from backtester import synthetic
from backtester.analysis import drop_incomplete_bar, signal_row, stock_row, summary


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
