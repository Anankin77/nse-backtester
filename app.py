"""Web interface for nse-backtester.

Run:  streamlit run app.py        (or ./start.sh)
Same engine as the command line, so the numbers are identical.
"""
from __future__ import annotations

import os
import zlib
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from backtester import DELIVERY, STRATEGIES, ZERO, download, run, synthetic
from backtester.analysis import SCAN_STRATEGIES, drop_incomplete_bar, signal_row, stock_row, summary
from backtester.metrics import table

ROOT = Path(__file__).resolve().parent
OFFLINE = bool(os.environ.get("BACKTESTER_OFFLINE"))  # fake prices, for testing without internet

BLUE, GREY, GREEN, RED, ORANGE = "#1f4e79", "#8a8a8a", "#4f8a3c", "#c0504d", "#d08c2a"

LABELS = {
    "sma": "SMA crossover: trend following",
    "rsi": "RSI: mean reversion",
    "breakout": "Breakout: momentum",
}
BLURB = {
    "sma": "Hold while the fast moving average is above the slow one.",
    "rsi": "Buy when oversold (RSI low), sell when overbought (RSI high).",
    "breakout": "Buy a new N-day high, sell a new M-day low.",
}
# name: (min, max, default)
SPEC = {
    "sma": {"fast": (5, 100, 50), "slow": (20, 300, 200)},
    "rsi": {"period": (5, 30, 14), "buy_below": (10, 40, 30), "sell_above": (60, 90, 70)},
    "breakout": {"lookback": (10, 200, 55), "exit_lookback": (5, 100, 20)},
}

st.set_page_config(page_title="NSE Backtester", layout="wide")
st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1400px;}
      [data-testid="stMetricValue"] {font-size: 1.5rem;}
      h1 {font-size: 1.7rem !important; margin-bottom: 0;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- data

def read_list(name: str) -> list[str]:
    path = ROOT / f"{name}.txt"
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


@st.cache_data(show_spinner=False, ttl=6 * 3600)
def prices(ticker: str, start: str, end: str | None) -> pd.DataFrame:
    if ticker == "DEMO" or OFFLINE:
        df = synthetic(1500, seed=zlib.crc32(ticker.encode()) % 1000)
        return df.loc[start:end] if end else df.loc[start:]
    return download(ticker, start, end)


def fresh_prices(ticker: str) -> pd.DataFrame:
    """Latest data, never from the day cache (used for today's signals)."""
    start = (pd.Timestamp.today() - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    if OFFLINE:
        return synthetic(500, seed=zlib.crc32(ticker.encode()) % 1000)
    return download(ticker, start, None, use_cache=False)


# ---------------------------------------------------------------- sidebar

st.sidebar.markdown("### Settings")
nifty = read_list("nifty50")
pick = st.sidebar.selectbox("Stock", ["RELIANCE", "^NSEI", *[s for s in nifty if s != "RELIANCE"], "DEMO"],
                            help="^NSEI is the Nifty 50 index. DEMO is fake prices that work offline.")
typed = st.sidebar.text_input("...or type any NSE symbol", placeholder="e.g. IRCTC").strip().upper()
ticker = typed or pick

c1, c2 = st.sidebar.columns(2)
start = c1.date_input("From", date(2015, 1, 1), min_value=date(2000, 1, 1), max_value=date.today())
end = c2.date_input("To", date.today(), min_value=date(2000, 1, 1), max_value=date.today())
capital = st.sidebar.number_input("Starting capital (Rs)", 10_000, 100_000_000, 100_000, step=10_000)
use_costs = st.sidebar.checkbox("Include Zerodha charges and slippage", value=True)

st.sidebar.markdown("### Strategy")
strategy = st.sidebar.selectbox("Rule", list(LABELS), format_func=LABELS.get)
st.sidebar.caption(BLURB[strategy])
params = {k: st.sidebar.slider(k.replace("_", " "), lo, hi, dflt) for k, (lo, hi, dflt) in SPEC[strategy].items()}

with st.sidebar.expander("Stop-loss and position size"):
    stop_pct = st.slider("Stop-loss %", 0, 40, 0, help="0 = no stop. Sell if price falls this far below entry.")
    trailing = st.checkbox("Trailing", help="The stop follows the highest close since entry.", disabled=stop_pct == 0)
    size_pct = st.slider("Share of money per trade %", 10, 100, 100)
    vol_pct = st.slider("Volatility target %/yr", 0, 40, 0, help="0 = off. Calm stock gets a bigger position.")
    risk_pct = st.slider("Max loss per trade % of equity", 0.0, 5.0, 0.0, step=0.25,
                         help="0 = off. Needs a stop. Position = risk / stop.", disabled=stop_pct == 0)
st.sidebar.caption("Tip: a 10% stop is usually inside normal noise. Try 20%.")

risk = {
    "stop": stop_pct / 100 or None,
    "trailing": trailing and stop_pct > 0,
    "size": size_pct / 100,
    "vol_target": vol_pct / 100 or None,
    "risk": risk_pct / 100 or None,
}
costs = DELIVERY if use_costs else ZERO
end_arg = None if end >= date.today() else end.isoformat()

# ---------------------------------------------------------------- compute


def simulate(df, name, p=None, with_risk=True, label=None):
    sig = STRATEGIES[name](df, **(p or {}))
    kw = risk if (with_risk and name != "buyhold") else {}
    return run(df, sig, capital=capital, costs=costs, name=label or name, **kw)


def risk_text() -> str:
    parts = []
    if risk["stop"]:
        parts.append(f"{'trailing ' if risk['trailing'] else ''}stop {stop_pct}%")
    if size_pct < 100:
        parts.append(f"size {size_pct}%")
    if risk["vol_target"]:
        parts.append(f"vol target {vol_pct}%")
    if risk["risk"]:
        parts.append(f"risk {risk_pct:g}%")
    return ", ".join(parts)


st.title("NSE Backtester")
st.caption("Replay the past with real Indian trading costs before risking money. Not investment advice.")

if start >= end:
    st.error("The start date must be before the end date.")
    st.stop()
try:
    with st.spinner(f"Loading {ticker}..."):
        df = prices(ticker, start.isoformat(), end_arg)
    if len(df) < 250:
        st.error(f"Only {len(df)} trading days of data for {ticker}. Need at least a year; widen the dates.")
        st.stop()
    res = simulate(df, strategy, params, label=LABELS[strategy].split(":")[0])
    bh = simulate(df, "buyhold", with_risk=False, label="Buy & hold")
except ValueError as e:
    st.error(str(e))
    st.stop()

tab_bt, tab_cmp, tab_scan, tab_today, tab_log, tab_learn = st.tabs(
    ["Backtest", "Compare", "Scan", "Today", "Paper log", "Learn"])

# ---------------------------------------------------------------- tab: backtest

with tab_bt:
    s, b = res.stats, bh.stats
    head = f"**{ticker}**, {df.index[0].date()} to {df.index[-1].date()}, {LABELS[strategy].split(':')[0]}"
    if risk_text():
        head += f" with {risk_text()}"
    st.markdown(head)

    cols = st.columns(6)
    cols[0].metric("Yearly return (CAGR)", f"{s['CAGR %']:.1f}%", f"{s['CAGR %'] - b['CAGR %']:+.1f} vs hold")
    cols[1].metric("Worst fall", f"{s['Max drawdown %']:.1f}%", f"{s['Max drawdown %'] - b['Max drawdown %']:+.1f} vs hold")
    cols[2].metric("Sharpe", f"{s['Sharpe']:.2f}", f"{s['Sharpe'] - b['Sharpe']:+.2f} vs hold")
    cols[3].metric("Trades", f"{s['Trades']}")
    cols[4].metric("Win rate", f"{s['Win rate %']:.0f}%")
    cols[5].metric("Final value", f"Rs {s['Final value']:,.0f}", f"{s['Final value'] - b['Final value']:+,.0f} vs hold")

    d_ret, d_dd = s["CAGR %"] - b["CAGR %"], s["Max drawdown %"] - b["Max drawdown %"]
    verdict = (
        f"Versus simply holding: returns {abs(d_ret):.1f} points a year **{'higher' if d_ret > 0 else 'lower'}**, "
        f"worst fall {abs(d_dd):.1f} points **{'smaller' if d_dd > 0 else 'larger'}**. "
        f"You were invested {s['Time in market %']:.0f}% of the time."
    )
    (st.success if d_ret > 0 else st.info)(verdict)

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=[0.42, 0.33, 0.25],
                        subplot_titles=("Price and trades", "Portfolio value", "Drawdown from peak, %"))
    fig.add_trace(go.Scatter(x=df.index, y=df["Close"], name="Close", line=dict(color=GREY, width=1)), 1, 1)
    t = res.trades
    if len(t):
        fig.add_trace(go.Scatter(x=t["entry_date"], y=t["entry_price"], mode="markers", name="Buy",
                                 marker=dict(symbol="triangle-up", color=GREEN, size=10),
                                 customdata=t["return_pct"], hovertemplate="Buy %{y:.2f}<extra></extra>"), 1, 1)
        done = t[~t["still_open"]]
        for reason, color, sym, nm in [("signal", RED, "triangle-down", "Sell (signal)"),
                                       ("stop", ORANGE, "x", "Sell (stop)")]:
            sub = done[done["exit_reason"].str.startswith(reason)]
            if len(sub):
                fig.add_trace(go.Scatter(x=sub["exit_date"], y=sub["exit_price"], mode="markers", name=nm,
                                         marker=dict(symbol=sym, color=color, size=10),
                                         customdata=sub["return_pct"],
                                         hovertemplate="Sell %{y:.2f}<br>Trade result %{customdata:+.1f}%<extra></extra>"), 1, 1)
    for r, color, w in [(bh, GREY, 1.2), (res, BLUE, 1.8)]:
        fig.add_trace(go.Scatter(x=r.equity.index, y=r.equity, name=r.name, line=dict(color=color, width=w),
                                 legendgroup=r.name, showlegend=True), 2, 1)
        dd = 100 * (r.equity / r.equity.cummax() - 1)
        fig.add_trace(go.Scatter(x=dd.index, y=dd, name=r.name, line=dict(color=color, width=w),
                                 legendgroup=r.name, showlegend=False), 3, 1)
    fig.update_layout(height=800, template="plotly_white", hovermode="x unified", margin=dict(l=10, r=10, t=90, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.07, x=0))
    st.plotly_chart(fig)

    st.markdown("**Trade log**")
    if len(t):
        shown = t.drop(columns="still_open").rename(columns={
            "entry_date": "Bought", "exit_date": "Sold", "entry_price": "Buy price", "exit_price": "Sell price",
            "days": "Days held", "return_pct": "Result %", "exit_reason": "Exit reason"})
        st.dataframe(shown, hide_index=True)
        st.download_button("Download trades (CSV)", shown.to_csv(index=False), f"{ticker}_{strategy}_trades.csv")
    else:
        st.write("No trades in this period.")

    with st.expander("What do these numbers mean?"):
        st.markdown(
            "- **CAGR**: the steady yearly growth that turns your start amount into the end amount.\n"
            "- **Worst fall (max drawdown)**: the biggest drop from a peak. Could you sit through it?\n"
            "- **Sharpe**: return per unit of bumpiness, above a 6.5% safe rate. Above 0.5 is okay, above 1 is good.\n"
            "- **Win rate**: share of trades that made money. Misleading alone: small frequent wins can still lose to holding.\n"
            "- **Costs**: STT, exchange and SEBI fees, stamp duty, GST and 0.05% slippage per side. Tax and the flat DP charge are not included."
        )

# ---------------------------------------------------------------- tab: compare

with tab_cmp:
    st.markdown(f"**All strategies on {ticker}**, default settings" + (f", with {risk_text()}" if risk_text() else ""))
    results = [simulate(df, k, label=k) for k in SCAN_STRATEGIES] + [bh]
    st.dataframe(table(results), width="stretch")
    fig2 = go.Figure()
    for r, color in zip(results, [BLUE, RED, GREEN, GREY]):
        fig2.add_trace(go.Scatter(x=r.equity.index, y=r.equity, name=r.name, line=dict(color=color, width=1.6)))
    fig2.update_layout(height=420, template="plotly_white", hovermode="x unified", yaxis_title="Portfolio value (Rs)",
                       margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.08, x=0))
    st.plotly_chart(fig2)

# ---------------------------------------------------------------- tab: scan

with tab_scan:
    st.markdown("**Does an idea work broadly, or only on a lucky few?** Runs every strategy on every stock in a list.")
    which = st.radio("Stocks", ["Nifty 50", "My watchlist"], horizontal=True)
    names = read_list("nifty50" if which == "Nifty 50" else "watchlist")
    st.caption(f"{len(names)} stocks, settings from the sidebar (dates, costs"
               f"{', ' + risk_text() if risk_text() else ''}). First run downloads data and takes 2-3 minutes; "
               "later runs the same day are instant.")
    if st.button("Run scan", type="primary"):
        rows, skipped = [], []
        bar = st.progress(0.0, text="Starting...")
        for i, tk in enumerate(names, 1):
            bar.progress(i / len(names), text=f"{tk} ({i}/{len(names)})")
            try:
                d = prices(tk, start.isoformat(), end_arg)
                if len(d) < 250:
                    raise ValueError("less than a year of data")
                rows.append(stock_row(tk, d, capital, costs, **risk))
            except Exception as e:  # one bad ticker shouldn't stop the scan
                skipped.append(f"{tk} ({e})")
        bar.empty()
        if rows:
            st.session_state["scan"] = (pd.DataFrame(rows).set_index("Stock").round(1), skipped, risk_text(), which)
        else:
            st.error("No stocks could be scanned. Check your internet connection.")
    if "scan" in st.session_state:
        out, skipped, rtxt, w = st.session_state["scan"]
        st.markdown(f"**Summary across {len(out)} stocks** ({w}{', ' + rtxt if rtxt else ''})")
        st.dataframe(summary(out).astype(str), width="stretch")
        st.caption("Read it like this: if 'Beat B&H' is a small fraction, the idea rarely beats holding. "
                   "Survivorship bias flatters these results: the list is today's members.")
        st.markdown("**Every stock**")
        st.dataframe(out, width="stretch", height=420,
                     column_config={"Beat B&H?": st.column_config.TextColumn(width="medium"),
                                    "Years": st.column_config.NumberColumn(format="%.1f")})
        st.download_button("Download scan (CSV)", out.to_csv(), "scan.csv")
        if skipped:
            st.caption("Skipped: " + ", ".join(skipped))

# ---------------------------------------------------------------- tab: today

with tab_today:
    st.markdown("**Today's signals for paper trading.** The best time to look is after 3:45 pm, once the day's bar is final.")
    which_t = st.radio("Stocks", ["My watchlist", "Nifty 50"], horizontal=True, key="today_list")
    t_names = read_list("watchlist" if which_t == "My watchlist" else "nifty50")
    if st.button("Get signals", type="primary"):
        rows, partial, skipped = [], False, []
        bar = st.progress(0.0, text="Starting...")
        for i, tk in enumerate(t_names, 1):
            bar.progress(i / len(t_names), text=f"{tk} ({i}/{len(t_names)})")
            try:
                d, dropped = drop_incomplete_bar(fresh_prices(tk))
                partial = partial or dropped
                rows.append(signal_row(tk, d))
            except Exception as e:
                skipped.append(f"{tk} ({e})")
        bar.empty()
        st.session_state["today"] = (pd.DataFrame(rows), partial, skipped)
    if "today" in st.session_state:
        sig, partial, skipped = st.session_state["today"]
        if partial:
            st.warning("The market is still open, so today's unfinished bar was ignored. "
                       "These signals are as of the previous close. Check again after 3:45 pm.")

        def colour(v):
            return {"BUY tmrw": "background-color:#dcefd5;font-weight:600",
                    "SELL tmrw": "background-color:#f6d9d6;font-weight:600",
                    "hold": "color:#1f4e79"}.get(v, "color:#9a9a9a")

        st.dataframe(sig.style.map(colour, subset=SCAN_STRATEGIES), hide_index=True,
                     column_config={"Price": st.column_config.NumberColumn("Price (Rs)", format="%.2f"),
                                    "Last close": st.column_config.TextColumn("Last close")})
        st.caption("BUY tmrw / SELL tmrw: act at the next market open. hold: stay in. -: stay out. "
                   "Write fake trades in the Paper log tab and follow ONE strategy for 6-8 weeks.")
        if skipped:
            st.caption("Skipped: " + ", ".join(skipped))

# ---------------------------------------------------------------- tab: paper log

with tab_log:
    st.markdown("**Paper trade log.** Fake trades only. Add a row each time a signal says BUY or SELL.")
    LOG = ROOT / "paper_trades.csv"
    COLS = ["date", "stock", "strategy", "side", "price", "qty", "note"]
    if LOG.exists():
        log = pd.read_csv(LOG, dtype={"date": str, "note": str}).reindex(columns=COLS)
    else:
        log = pd.DataFrame({c: pd.Series(dtype="object") for c in COLS})
    edited = st.data_editor(
        log, num_rows="dynamic", hide_index=True, key="log_editor",
        column_config={
            "date": st.column_config.TextColumn("Date", help="YYYY-MM-DD"),
            "stock": st.column_config.TextColumn("Stock"),
            "strategy": st.column_config.SelectboxColumn("Strategy", options=SCAN_STRATEGIES),
            "side": st.column_config.SelectboxColumn("Side", options=["BUY", "SELL"]),
            "price": st.column_config.NumberColumn("Price (Rs)", min_value=0.0, format="%.2f"),
            "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            "note": st.column_config.TextColumn("Note"),
        },
    )
    if st.button("Save log"):
        edited.to_csv(LOG, index=False)
        st.success(f"Saved {len(edited)} rows to paper_trades.csv on this computer.")

    if len(edited.dropna(subset=["stock", "side", "price", "qty"])):
        e = edited.dropna(subset=["stock", "side", "price", "qty"]).copy()
        e["qty"], e["price"] = e["qty"].astype(float), e["price"].astype(float)
        e["bought"] = e["qty"].where(e["side"] == "BUY", 0.0)
        e["sold"] = e["qty"].where(e["side"] == "SELL", 0.0)
        e["net_cash"] = (e["sold"] - e["bought"]) * e["price"]
        pnl = e.groupby("stock")[["bought", "sold", "net_cash"]].sum()
        pnl["status"] = ["closed" if b == s else "open" for b, s in zip(pnl["bought"], pnl["sold"])]
        st.markdown("**Per stock.** Net cash equals your profit only once the position is closed.")
        st.dataframe(pnl.round(2))

# ---------------------------------------------------------------- tab: learn

with tab_learn:
    guide = ROOT / "docs" / "LEARNING.md"
    if guide.exists():
        text = guide.read_text()
        intro, *sections = text.split("\n## ")
        st.markdown(intro.split("\n---")[0])
        st.info("The commands in this guide are the terminal versions. You don't need the terminal: "
                "`compare` is the Compare tab, `test` is the Backtest tab, `scan` is the Scan tab, "
                "`today` is the Today tab, and the stop and size options are in the sidebar.")
        for sec in sections:
            title, _, body = sec.partition("\n")
            body = body.strip().removesuffix("---").strip()
            with st.expander(title.strip()):
                st.markdown(body)
    else:
        st.write("docs/LEARNING.md not found.")
