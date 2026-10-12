"""Web interface for nse-backtester.

Run:  ./start.sh      (or: streamlit run app.py)
Same engine as the command line, so the numbers are identical.
"""
from __future__ import annotations

import io
import os
import zlib
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from backtester import DELIVERY, STRATEGIES, ZERO, download, run, synthetic
from backtester.analysis import (PRESETS, RANK_BY, SCAN_STRATEGIES, drop_incomplete_bar, ranked,
                                 signal_row, strategy_table, tournament)
from backtester.data import stored_status
from backtester.github_file import GitHubError, GitHubFile

ROOT = Path(__file__).resolve().parent
OFFLINE = bool(os.environ.get("BACKTESTER_OFFLINE"))  # fake prices, for testing without internet

BLUE, GREY, GREEN, RED, AMBER = "#58a6ff", "#8b949e", "#3fb950", "#f85149", "#d29922"
# No floating toolbar: on phones it covers the legend. Zoom by dragging, double-tap to reset.
CHART_CONFIG = {"displayModeBar": False}

# slider ranges per strategy family: (min, max, default)
SPEC = {
    "sma": {"fast": (5, 100, 50), "slow": (20, 300, 200)},
    "rsi": {"period": (5, 30, 14), "buy_below": (10, 40, 30), "sell_above": (60, 90, 70)},
    "breakout": {"lookback": (10, 200, 55), "exit_lookback": (5, 100, 20)},
}
BLURB = {
    "sma": "Trend following: hold while the fast moving average is above the slow one.",
    "rsi": "Mean reversion: buy when the stock is oversold, sell when it is overbought.",
    "breakout": "Momentum: buy a new N-day high, sell a new M-day low.",
}

st.set_page_config(page_title="NSE Backtester", layout="wide")
st.markdown(
    """
    <style>
      .block-container {padding-top: 3rem; max-width: 1180px;}
      h1 {font-weight: 600 !important; letter-spacing: -0.01em;}
      [data-testid="stMetricValue"] {font-size: 1.45rem;}
      .lead {color: #8b949e; font-size: 1.05rem; margin-top: -0.6rem;}
      /* Phones: two numbers per row instead of one tall stack */
      @media (max-width: 640px) {
        .block-container {padding-top: 2.2rem; padding-left: 1rem; padding-right: 1rem;}
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {flex-flow: row wrap; gap: 0.6rem 1rem;}
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="stColumn"] {
          flex: 1 1 calc(50% - 0.5rem) !important; min-width: calc(50% - 0.5rem) !important;
          width: calc(50% - 0.5rem) !important;}
        [data-testid="stMetricValue"] {font-size: 1.2rem;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ================================================================ data

def read_list(name: str) -> list[str]:
    path = ROOT / f"{name}.txt"
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def prices(ticker: str, start: str, end: str | None) -> pd.DataFrame:
    # The version changes when the daily job saves new prices, so the cache never serves yesterday's.
    return _prices(ticker, start, end, version=f"{stored_status().get('updated')}|{date.today()}")


@st.cache_data(show_spinner=False, ttl=6 * 3600, max_entries=300)
def _prices(ticker: str, start: str, end: str | None, version: str) -> pd.DataFrame:
    if ticker == "DEMO" or OFFLINE:
        df = synthetic(1500, seed=zlib.crc32(ticker.encode()) % 1000)
        return df.loc[start:end] if end else df.loc[start:]
    try:
        return download(ticker, start, end)
    except Exception as e:  # Yahoo can refuse hosted servers; say what to do instead of crashing
        msg = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
        why = "Yahoo sent no data" if msg.startswith("No data") else f"Yahoo said: {msg}"
        raise ValueError(f"Couldn't load {ticker}: {why}. If the symbol is right, add it to watchlist.txt "
                         "on GitHub and the price job saves it within a few minutes.") from e


def prices_note() -> tuple[str, bool]:
    """Date of the saved prices, and whether that looks stale (the daily job may be failing)."""
    last = stored_status().get("last_bar")
    if OFFLINE or not last:
        return "", False
    last = pd.Timestamp(last)
    stale = (pd.Timestamp.today().normalize() - last).days > 4   # more than a weekend plus a holiday
    return f"{last:%a %d %b %Y}", stale


def fresh_prices(ticker: str) -> pd.DataFrame:
    """Saved prices, or for unsaved tickers a fresh Yahoo download (used for today's signals)."""
    if OFFLINE:
        return synthetic(500, seed=zlib.crc32(ticker.encode()) % 1000)
    start = (pd.Timestamp.today() - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    return download(ticker, start, None, use_cache=False)


def lead(text: str):
    st.markdown(f'<p class="lead">{text}</p>', unsafe_allow_html=True)


# ================================================================ sidebar settings (shared by every page)

def sidebar_settings() -> dict:
    sb = st.sidebar
    sb.markdown("#### Settings")
    c1, c2 = sb.columns(2)
    start = c1.date_input("From", date(2015, 1, 1), min_value=date(2000, 1, 1), max_value=date.today(), key="start")
    end = c2.date_input("To", date.today(), min_value=date(2000, 1, 1), max_value=date.today(), key="end")
    capital = sb.number_input("Capital (Rs)", 10_000, 100_000_000, 100_000, step=10_000, key="capital")
    use_costs = sb.toggle("Zerodha charges and slippage", True, key="use_costs")
    with sb.expander("Stop-loss and position size"):
        stop = st.slider("Stop-loss %", 0, 40, 0, key="stop", help="0 = off. Sell if price falls this far below entry.")
        trailing = st.checkbox("Trailing stop", key="trailing", disabled=stop == 0,
                               help="The stop follows the highest close since entry.")
        size = st.slider("Money per trade %", 10, 100, 100, key="size")
        vol = st.slider("Volatility target %/yr", 0, 40, 0, key="vol", help="0 = off. Calm stocks get bigger positions.")
        risk_pct = st.slider("Max loss per trade %", 0.0, 5.0, 0.0, step=0.25, key="risk", disabled=stop == 0,
                             help="0 = off. Needs a stop. Position = risk / stop.")
        st.caption("A 10% stop usually sits inside normal noise. Try 20%.")
    sb.caption("These apply to every page. Not investment advice.")
    day, stale = prices_note()
    if stale:
        sb.warning(f"Prices stop at {day}. The daily update may be failing: "
                   "check GitHub > Actions > Update prices.", icon=":material/schedule:")
    elif day:
        sb.caption(f":material/schedule: Prices up to {day}. Updated weekdays around 4:40 pm.")

    risk = {"stop": stop / 100 or None, "trailing": bool(trailing and stop), "size": size / 100,
            "vol_target": vol / 100 or None, "risk": (risk_pct / 100 or None) if stop else None}
    parts = []
    if stop:
        parts.append(f"{'trailing ' if risk['trailing'] else ''}stop {stop}%")
    if size < 100:
        parts.append(f"{size}% per trade")
    if vol:
        parts.append(f"vol target {vol}%")
    if risk["risk"]:
        parts.append(f"max loss {risk_pct:g}%/trade")
    return {
        "start": start, "end": end, "start_s": start.isoformat(),
        "end_s": None if end >= date.today() else end.isoformat(),
        "capital": capital, "costs": DELIVERY if use_costs else ZERO, "use_costs": use_costs,
        "risk": risk, "risk_text": ", ".join(parts),
    }


def fingerprint(*extra) -> tuple:
    """Everything that changes a result, to tell when shown results are out of date."""
    return (cfg["start"], cfg["end"], cfg["capital"], cfg["use_costs"], tuple(cfg["risk"].items()), *extra)


def stale_note(saved_fp, now_fp):
    if saved_fp != now_fp:
        st.caption(":material/info: Settings changed since this run. Press the button again to update.")


# ================================================================ charts

def backtest_chart(df, res, bh) -> go.Figure:
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.42, 0.33, 0.25],
                        subplot_titles=("Price and trades", "Portfolio value (Rs)", "Fall from peak, %"))
    fig.add_trace(go.Scatter(x=df.index, y=df["Close"], name="Price", line=dict(color=GREY, width=1)), 1, 1)
    t = res.trades
    if len(t):
        fig.add_trace(go.Scatter(x=t["entry_date"], y=t["entry_price"], mode="markers", name="Buy",
                                 marker=dict(symbol="triangle-up", color=GREEN, size=10),
                                 hovertemplate="Buy %{y:.2f}<extra></extra>"), 1, 1)
        done = t[~t["still_open"]]
        for reason, color, sym, nm in [("signal", RED, "triangle-down", "Sell"), ("stop", AMBER, "x", "Stop-loss")]:
            sub = done[done["exit_reason"].str.startswith(reason)]
            if len(sub):
                fig.add_trace(go.Scatter(x=sub["exit_date"], y=sub["exit_price"], mode="markers", name=nm,
                                         marker=dict(symbol=sym, color=color, size=10), customdata=sub["return_pct"],
                                         hovertemplate=nm + " %{y:.2f}<br>Trade %{customdata:+.1f}%<extra></extra>"), 1, 1)
    for r, color, w in [(bh, GREY, 1.2), (res, BLUE, 1.8)]:
        fig.add_trace(go.Scatter(x=r.equity.index, y=r.equity, name=r.name, legendgroup=r.name,
                                 line=dict(color=color, width=w)), 2, 1)
        dd = 100 * (r.equity / r.equity.cummax() - 1)
        fig.add_trace(go.Scatter(x=dd.index, y=dd, name=r.name, legendgroup=r.name, showlegend=False,
                                 line=dict(color=color, width=w)), 3, 1)
    fig.update_layout(height=760, hovermode="x unified", margin=dict(l=10, r=10, t=80, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.07, x=0))
    return fig


# ================================================================ pages

def home():
    st.title("NSE Backtester")
    lead("Test trading ideas on Indian stocks, with real costs, before you risk money.")
    st.write("")
    cards = [
        (P_BACKTEST, "Backtest", "One company, one strategy. See every trade it would have made."),
        (P_COMPETE, "Compete", "Strategies and companies compete. Find what actually works, and what was luck."),
        (P_TODAY, "Today's signals", "What the strategies say for tomorrow. For paper trading."),
    ]
    for col, (page, title, text) in zip(st.columns(3, gap="medium"), cards):
        with col.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(text)
            st.page_link(page, label="Open", icon=":material/arrow_forward:")
    st.write("")
    st.caption("New here? Start with **Learn** in the sidebar. Dates, costs and stop-loss live in the sidebar too.")


def backtest():
    st.title("Backtest")
    lead("One company, one strategy, every trade.")
    nifty = read_list("nifty50")
    options = ["RELIANCE", "^NSEI", *[s for s in nifty if s != "RELIANCE"], "DEMO"]
    c1, c2 = st.columns(2)
    company = c1.selectbox("Company", options, accept_new_options=True, key="bt_company",
                           help="Type any NSE symbol. ^NSEI is the Nifty 50 index; DEMO is fake prices that work offline.")
    company = str(company).strip().upper()
    choices = [p for p in PRESETS if p != "Buy & hold"]
    preset = c2.selectbox("Strategy", choices, key="bt_preset")
    family, base = PRESETS[preset]
    st.caption(BLURB[family])
    with st.expander("Fine-tune the strategy's settings"):
        params = {k: st.slider(k.replace("_", " "), lo, hi, base.get(k, d), key=f"bt_{preset}_{k}")
                  for k, (lo, hi, d) in SPEC[family].items()}

    fp = fingerprint(company, preset, tuple(params.items()))
    if st.button("Run backtest", type="primary", icon=":material/play_arrow:"):
        try:
            with st.spinner(f"Loading {company}..."):
                df = prices(company, cfg["start_s"], cfg["end_s"])
            if len(df) < 250:
                raise ValueError(f"Only {len(df)} trading days of data for {company}. Widen the dates in the sidebar.")
            res = run(df, STRATEGIES[family](df, **params), cfg["capital"], cfg["costs"], preset.split(" (")[0], **cfg["risk"])
            bh = run(df, STRATEGIES["buyhold"](df), cfg["capital"], cfg["costs"], "Buy & hold")
            st.session_state["bt"] = dict(df=df, res=res, bh=bh, company=company, preset=preset, fp=fp,
                                          risk_text=cfg["risk_text"])
        except ValueError as e:
            st.error(str(e))
            st.session_state.pop("bt", None)

    r = st.session_state.get("bt")
    if not r:
        return
    st.divider()
    stale_note(r["fp"], fp)
    df, res, bh = r["df"], r["res"], r["bh"]
    s, b = res.stats, bh.stats
    st.markdown(f"**{r['company']}** · {r['preset']} · {df.index[0].date()} to {df.index[-1].date()}"
                + (f" · {r['risk_text']}" if r["risk_text"] else ""))
    cols = st.columns(5)
    cols[0].metric("Yearly return", f"{s['CAGR %']:.1f}%", f"{s['CAGR %'] - b['CAGR %']:+.1f} vs hold")
    cols[1].metric("Worst fall", f"{s['Max drawdown %']:.1f}%", f"{s['Max drawdown %'] - b['Max drawdown %']:+.1f} vs hold")
    cols[2].metric("Sharpe", f"{s['Sharpe']:.2f}", f"{s['Sharpe'] - b['Sharpe']:+.2f} vs hold")
    cols[3].metric("Trades", f"{s['Trades']}", f"{s['Win rate %']:.0f}% won", delta_color="off", delta_arrow="off")
    cols[4].metric("Final value", f"Rs {s['Final value']:,.0f}", f"{s['Final value'] - b['Final value']:+,.0f} vs hold")

    d_ret, d_dd = s["CAGR %"] - b["CAGR %"], s["Max drawdown %"] - b["Max drawdown %"]
    msg = (f"Compared with simply holding: {abs(d_ret):.1f} points a year **{'more' if d_ret > 0 else 'less'}** return, "
           f"worst fall {abs(d_dd):.1f} points **{'smaller' if d_dd > 0 else 'bigger'}**. "
           f"Invested {s['Time in market %']:.0f}% of the time.")
    (st.success if d_ret > 0 else st.info)(msg)

    st.plotly_chart(backtest_chart(df, res, bh), config=CHART_CONFIG)

    with st.expander(f"Trade log ({len(res.trades)} trades)"):
        t = res.trades
        if len(t):
            shown = t.drop(columns="still_open").rename(columns={
                "entry_date": "Bought", "exit_date": "Sold", "entry_price": "Buy price", "exit_price": "Sell price",
                "days": "Days held", "return_pct": "Result %", "exit_reason": "Exit"})
            st.dataframe(shown, hide_index=True)
            st.download_button("Download CSV", shown.to_csv(index=False), f"{r['company']}_trades.csv")
        else:
            st.write("No trades in this period.")
    with st.expander("What do these numbers mean?"):
        st.markdown(
            "- **Yearly return (CAGR)**: the steady yearly growth that turns the start amount into the end amount.\n"
            "- **Worst fall (max drawdown)**: the biggest drop from a peak. Could you sit through it with real money?\n"
            "- **Sharpe**: return per unit of bumpiness, above a 6.5% safe rate. Above 0.5 is okay, above 1 is good.\n"
            "- **Won %**: share of trades that made money. Misleading alone: many small wins can still lose to holding.\n"
            "- **Costs**: STT, exchange and SEBI fees, stamp duty, GST and 0.05% slippage per side. "
            "Tax and the flat DP charge are not included."
        )


def compete():
    st.title("Compete")
    lead("Pick companies and strategies, or let them all compete. Buy & hold competes too: it's the bar to beat.")
    nifty = read_list("nifty50")

    group = st.segmented_control("Companies", ["Nifty 50", "My watchlist", "Choose"], default="Nifty 50",
                                 required=True, key="cmp_group")
    if group == "Choose":
        picked = st.multiselect("Choose companies", ["^NSEI", *nifty], default=["RELIANCE", "TCS", "HDFCBANK", "ITC"],
                                accept_new_options=True, key="cmp_pick", placeholder="Type or pick NSE symbols")
        companies = list(dict.fromkeys(str(p).strip().upper() for p in picked))
    else:
        companies = read_list("nifty50" if group == "Nifty 50" else "watchlist")
        st.caption(f"{len(companies)} companies" + (" (edit watchlist.txt to change)" if group == "My watchlist" else ""))
    strategies = st.pills(
        "Strategies", list(PRESETS), selection_mode="multi", default=list(PRESETS), key="cmp_strats",
        format_func=lambda s: s.split(" (")[0],
        help="Click to include or leave out. "
             + " · ".join(f"{s.split(' (')[0]}: {s.split(' (')[1].rstrip(')')}" for s in PRESETS if "(" in s))
    strategies = [s for s in PRESETS if s in (strategies or [])]

    c3, c4 = st.columns(2)
    rank_label = c3.selectbox("Rank by", list(RANK_BY), key="cmp_rank")
    metric = RANK_BY[rank_label]
    fair = c4.toggle("Fair test (recommended)", True, key="cmp_fair",
                     help="Pick winners using older data, then check them on newer data they never saw. "
                          "A winner that collapses on new data was luck.")
    split = c4.date_input("Newer data starts", date(2022, 1, 1), key="cmp_split", disabled=not fair,
                          min_value=date(2001, 1, 1), max_value=date.today())

    fp = fingerprint(tuple(companies), tuple(strategies), metric, fair, split)
    if st.button("Start competition", type="primary", icon=":material/emoji_events:"):
        if not companies or not strategies:
            st.warning("Pick at least one company and one strategy.")
            return
        split_ts = pd.Timestamp(split) if fair else None
        if fair and not (cfg["start"] < split < cfg["end"]):
            st.warning("'Newer data starts' must fall between the From and To dates in the sidebar.")
            return
        entries = {s: PRESETS[s] for s in strategies}
        parts, data, skipped = [], {}, []
        bar = st.progress(0.0, text="Starting...")
        for i, tk in enumerate(companies, 1):
            bar.progress(i / len(companies), text=f"{tk} ({i}/{len(companies)})")
            try:
                df = prices(tk, cfg["start_s"], cfg["end_s"])
                if fair:
                    old, new = df.loc[:split_ts - pd.Timedelta(days=1)], df.loc[split_ts:]
                    if len(old) < 250 or len(new) < 120:
                        raise ValueError("not enough data on one side of the split")
                elif len(df) < 250:
                    raise ValueError("less than a year of data")
                parts.append(tournament({tk: df}, entries, cfg["capital"], cfg["costs"], split_ts, cfg["risk"]))
                data[tk.upper()] = df
            except Exception as e:  # one bad ticker shouldn't stop the competition
                skipped.append(f"{tk} ({e})")
        bar.empty()
        if not parts:
            st.error("No company could be tested. Check your internet connection or the dates.")
            st.session_state.pop("cmp", None)
            return
        st.session_state["cmp"] = dict(res=pd.concat(parts, ignore_index=True), data=data, skipped=skipped,
                                       metric=metric, rank_label=rank_label, split=split_ts, fp=fp,
                                       risk_text=cfg["risk_text"])

    r = st.session_state.get("cmp")
    if r:
        st.divider()
        stale_note(r["fp"], fp)
        show_competition(r)


def pretty(col: str) -> str:
    col = col.replace("Max drawdown %", "Worst fall %").replace("Edge", "Edge vs hold")
    return col.replace(" [all]", "").replace(" [old]", " (old)").replace(" [new]", " (new)")


def show_competition(r):
    res, metric, split = r["res"], r["metric"], r["split"]
    fair = split is not None
    board = ranked(res, metric)
    strat = strategy_table(res, metric)
    n_pairs, n_cos, n_str = len(board), res["Company"].nunique(), res["Strategy"].nunique()
    best_pair, best_strat = board.iloc[0], strat.index[0]
    p = "old" if fair else "all"

    st.markdown(f"**{n_cos} companies × {n_str} strategies = {n_pairs} pairs**, ranked by {r['rank_label']}"
                + (f", winners picked on data before {split.date()}" if fair else "")
                + (f" · {r['risk_text']}" if r["risk_text"] else ""))

    c1, c2, c3 = st.columns(3)
    with c1.container(border=True):
        st.caption("Best strategy overall")
        st.markdown(f"**{best_strat}**")
        st.caption(f"median {pretty(metric)}: {strat.iloc[0, 0]:.2f}" + (" on old data" if fair else ""))
    with c2.container(border=True):
        st.caption("Best single pair")
        st.markdown(f"**{best_pair['Strategy']}** on **{best_pair['Company']}**")
        if fair:
            st.caption(f"{pretty(metric)} {best_pair[f'{metric} [old]']:.2f} on old data, "
                       f"{best_pair[f'{metric} [new]']:.2f} on new (rank #{best_pair['Rank on new data']} of {n_pairs})")
        else:
            st.caption(f"{pretty(metric)} {best_pair[f'{metric} [all]']:.2f}")
    with c3.container(border=True):
        st.caption("Buy & hold")
        if "Buy & hold" in strat.index:
            pos = list(strat.index).index("Buy & hold") + 1
            st.markdown(f"**#{pos}** of {n_str} strategies")
            st.caption("ranked by median score across companies")
        else:
            st.markdown("**Not competing**")
            st.caption("add it to see if any rule beats holding")

    # plain-language verdict
    if "Buy & hold" in strat.index and best_strat == "Buy & hold":
        st.info("Buy & hold won. On these companies, no timing rule beat simply holding.")
    if fair and n_pairs >= 4:
        new_rank = int(best_pair["Rank on new data"])
        share = new_rank / n_pairs  # 0.1 = top 10% on data the ranking never saw
        if share <= 0.10:
            st.success(f"The top pair held up: #{new_rank} of {n_pairs} on data it never saw.")
        elif share <= 0.33:
            st.info(f"The top pair slipped to #{new_rank} of {n_pairs} on new data: some of its edge was luck.")
        else:
            st.warning(f"The top pair fell to #{new_rank} of {n_pairs} on new data, mid-table or worse. "
                       "Its win on old data was mostly luck.")

    tab_s, tab_p = st.tabs(["By strategy", "By pair"])
    with tab_s:
        shown = strat.rename(columns=lambda c: c.replace(metric, pretty(metric)).replace("Edge vs hold", "edge vs hold"))
        st.dataframe(shown)
        st.caption("Best on: for how many companies this strategy came first. "
                   "Beat hold on: for how many it returned more than buying and holding."
                   + (f" Old = before {split.date()}, used to rank. New = after it, unseen." if fair else ""))
    with tab_p:
        cols = ["Rank", "Company", "Strategy", f"{metric} [{p}]"]
        if fair:
            cols += [f"{metric} [new]", "Rank on new data"]
        q = "new" if fair else "all"
        cols += [f"CAGR % [{q}]", f"Max drawdown % [{q}]", f"Edge [{q}]"]
        cols = list(dict.fromkeys(c for c in cols if c in board))  # metric may repeat a column
        show_all = st.toggle(f"Show all {n_pairs} pairs", key="cmp_all") if n_pairs > 25 else True
        view = board[cols] if show_all else board[cols].head(25)
        st.dataframe(view.rename(columns=pretty).round(2), hide_index=True)
        st.download_button("Download CSV", board.rename(columns=pretty).to_csv(index=False), "competition.csv")

    with st.expander("Look at one pair"):
        labels = [f"#{row.Rank}  {row.Strategy} on {row.Company}" for row in board.head(50).itertuples()]
        choice = st.selectbox("Pair", labels, key="cmp_inspect")
        row = board.iloc[labels.index(choice)]
        df = r["data"][row["Company"]]
        fam, params = PRESETS[row["Strategy"]]
        kw = {} if fam == "buyhold" else cfg["risk"]
        res1 = run(df, STRATEGIES[fam](df, **params), cfg["capital"], cfg["costs"], row["Strategy"], **kw)
        bh = run(df, STRATEGIES["buyhold"](df), cfg["capital"], cfg["costs"], "Buy & hold")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=bh.equity.index, y=bh.equity, name="Buy & hold", line=dict(color=GREY, width=1.2)))
        if fam != "buyhold":
            fig.add_trace(go.Scatter(x=res1.equity.index, y=res1.equity, name=row["Strategy"],
                                     line=dict(color=BLUE, width=1.8)))
        if fair:
            fig.add_vline(x=split, line=dict(color=AMBER, dash="dot", width=1))
            fig.add_annotation(x=split, y=1, yref="paper", text="new data →", showarrow=False,
                               xanchor="left", font=dict(color=AMBER, size=11))
        fig.update_layout(height=380, hovermode="x unified", yaxis_title="Portfolio value (Rs)",
                          margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.12, x=0))
        st.plotly_chart(fig, config=CHART_CONFIG)

    if r["skipped"]:
        st.caption("Skipped: " + ", ".join(r["skipped"]))
    st.caption("Survivorship bias: the Nifty 50 list is today's members, so past losers that left the index are missing.")


def today():
    st.title("Today's signals")
    lead("What each strategy says for the next market open. For paper trading only.")
    which = st.segmented_control("Companies", ["My watchlist", "Nifty 50"], default="My watchlist",
                                 required=True, key="today_list")
    names = read_list("watchlist" if which == "My watchlist" else "nifty50")
    st.caption("Prices refresh every weekday around 4:40 pm. Before that, signals are from the previous "
               "trading day, which is fine: every signal acts at the next open anyway.")
    if st.button("Get signals", type="primary", icon=":material/refresh:"):
        rows, partial, skipped = [], False, []
        bar = st.progress(0.0, text="Starting...")
        for i, tk in enumerate(names, 1):
            bar.progress(i / len(names), text=f"{tk} ({i}/{len(names)})")
            try:
                d, dropped = drop_incomplete_bar(fresh_prices(tk))
                partial = partial or dropped
                rows.append(signal_row(tk, d))
            except Exception as e:
                skipped.append(f"{tk} ({e})")
        bar.empty()
        st.session_state["today"] = (pd.DataFrame(rows), partial, skipped)

    if "today" not in st.session_state:
        return
    sig, partial, skipped = st.session_state["today"]
    st.divider()
    if partial:
        st.warning("The market is still open, so today's unfinished prices were ignored. "
                   "These signals are as of the previous close.")
    if sig.empty:
        st.error("No signals. Check your internet connection.")
        return
    acts = sig[SCAN_STRATEGIES].isin(["BUY tmrw", "SELL tmrw"]).sum().sum()
    st.markdown(f"**{acts} action{'s' if acts != 1 else ''}** for the next open across {len(sig)} companies.")

    def colour(v):
        return {"BUY tmrw": f"background-color: rgba(63,185,80,0.18); color: {GREEN}; font-weight: 600",
                "SELL tmrw": f"background-color: rgba(248,81,73,0.18); color: {RED}; font-weight: 600",
                "hold": f"color: {BLUE}"}.get(v, f"color: {GREY}")

    st.dataframe(sig.style.map(colour, subset=SCAN_STRATEGIES), hide_index=True,
                 column_config={"Price": st.column_config.NumberColumn("Price (Rs)", format="%.2f"),
                                "Last close": st.column_config.TextColumn("Prices as of"),
                                "sma": "SMA 50/200", "rsi": "RSI 14", "breakout": "Breakout 55/20"})
    st.caption("BUY / SELL tmrw: act at the next open. hold: stay in. -: stay out. "
               "Log fake trades in Paper log and follow one strategy for 6-8 weeks.")
    if skipped:
        st.caption("Skipped: " + ", ".join(skipped))
    failed = {k: v for k, v in stored_status().get("failed", {}).items() if k in set(sig["Stock"])}
    if failed:
        st.caption("Not refreshed in the last daily update (older prices shown): " + ", ".join(failed))


LOG_COLS = ["date", "stock", "strategy", "side", "price", "qty", "note"]
LOG_FILE = ROOT / "paper_trades.csv"


def log_repo() -> GitHubFile | None:
    """The private log repo from the app's secrets ([paper_log] repo, token), if set up."""
    try:
        conf = st.secrets.get("paper_log") or {}
    except Exception:  # no secrets file at all
        conf = {}
    if conf.get("repo") and conf.get("token"):
        return GitHubFile(conf["repo"], conf.get("path", "paper_trades.csv"), conf["token"],
                          api=conf.get("api", "https://api.github.com"))
    return None


def load_log() -> dict:
    """Read the log once per visit: {df, sha, where}. Raises GitHubError if the repo can't be read."""
    repo = log_repo()
    if repo:
        text, sha = repo.read()
        where = f"your private repo {repo.repo}"
    else:
        text, sha = (LOG_FILE.read_text() if LOG_FILE.exists() else None), None
        where = "paper_trades.csv next to the app"
    if text and text.strip():
        # Text columns stay text even when blank, or the editor refuses to open them.
        df = pd.read_csv(io.StringIO(text), dtype={c: str for c in ["date", "stock", "strategy", "side", "note"]})
        df = df.reindex(columns=LOG_COLS).dropna(how="all").reset_index(drop=True)
    else:
        df = pd.DataFrame({c: pd.Series(dtype="object") for c in LOG_COLS})
    return {"df": df, "sha": sha, "where": where, "github": repo is not None, "v": 0}


def save_log(log: dict, edited: pd.DataFrame):
    edited = edited.dropna(how="all").reset_index(drop=True)   # blank rows from a stray "+"
    edited["qty"] = pd.to_numeric(edited["qty"], errors="coerce").round().astype("Int64")
    text = edited.to_csv(index=False)
    repo = log_repo()
    if repo:
        log["sha"] = repo.write(text, log["sha"], f"Paper log: {len(edited)} rows")
    else:
        LOG_FILE.write_text(text)
    # New base data, fresh editor (otherwise the editor re-applies the same edits on top).
    log["df"], log["v"] = edited.reset_index(drop=True), log["v"] + 1


def paper_log():
    st.title("Paper log")
    lead("Fake trades only. Add a row each time a signal says BUY or SELL, then press Save.")
    if "paper" not in st.session_state:
        try:
            st.session_state["paper"] = load_log()
        except GitHubError as e:
            st.error(f"Couldn't open the paper log. {e}")
            return
    log = st.session_state["paper"]
    edited = st.data_editor(
        log["df"], num_rows="dynamic", hide_index=True, key=f"log_editor_{log['v']}",
        column_config={
            "date": st.column_config.TextColumn("Date", help="YYYY-MM-DD"),
            "stock": st.column_config.TextColumn("Stock"),
            "strategy": st.column_config.SelectboxColumn("Strategy", options=[p for p in PRESETS if p != "Buy & hold"]),
            "side": st.column_config.SelectboxColumn("Side", options=["BUY", "SELL"]),
            "price": st.column_config.NumberColumn("Price (Rs)", min_value=0.0, format="%.2f"),
            "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1),
            "note": st.column_config.TextColumn("Note"),
        },
    )
    c1, c2 = st.columns([1, 1])
    if c1.button("Save", type="primary", icon=":material/save:", width="stretch"):
        try:
            save_log(log, edited)
            st.session_state["paper_saved"] = len(edited)
            st.rerun()
        except GitHubError as e:
            st.error(str(e))
    if c2.button("Reload", icon=":material/refresh:", width="stretch",
                 help="Fetch the latest copy, e.g. after saving on another device. Unsaved edits are lost."):
        st.session_state.pop("paper", None)
        st.rerun()
    if n := st.session_state.pop("paper_saved", None):
        st.success(f"Saved {n} row{'s' if n != 1 else ''} to {log['where']}.")
    elif log["github"]:
        st.caption(f":material/lock: Saved in {log['where']}.")
    else:
        st.caption(f"Saved in {log['where']}. On the hosted app this file is wiped whenever the app restarts: "
                   "set up the private log repo (see README) to keep it.")

    e = edited.dropna(subset=["stock", "side", "price", "qty"]).copy()
    if len(e):
        e["qty"], e["price"] = e["qty"].astype(float), e["price"].astype(float)
        e["bought"] = e["qty"].where(e["side"] == "BUY", 0.0)
        e["sold"] = e["qty"].where(e["side"] == "SELL", 0.0)
        e["net_cash"] = (e["sold"] - e["bought"]) * e["price"]
        pnl = e.groupby("stock")[["bought", "sold", "net_cash"]].sum()
        pnl["status"] = ["closed" if b == s else "open" for b, s in zip(pnl["bought"], pnl["sold"])]
        st.divider()
        st.markdown("**Per stock.** Net cash equals your profit once a position is closed.")
        st.dataframe(pnl.round(2))


def learn():
    st.title("Learn")
    guide = ROOT / "docs" / "LEARNING.md"
    if not guide.exists():
        st.write("docs/LEARNING.md not found.")
        return
    intro, *sections = guide.read_text().split("\n## ")
    body = intro.split("\n---")[0].split("\n", 1)[-1].strip()  # drop the file's own title
    st.markdown(body)
    st.info("The guide shows terminal commands, but you don't need the terminal: `test` is the Backtest page, "
            "`compare`, `scan` and `optimize` are covered by Compete (turn on Fair test), and `today` is "
            "Today's signals. Stop-loss and position size are in the sidebar.")
    for sec in sections:
        title, _, text = sec.partition("\n")
        with st.expander(title.strip()):
            st.markdown(text.strip().removesuffix("---").strip())


# ================================================================ navigation

P_HOME = st.Page(home, title="Home", icon=":material/home:", default=True)
P_BACKTEST = st.Page(backtest, title="Backtest", icon=":material/show_chart:")
P_COMPETE = st.Page(compete, title="Compete", icon=":material/emoji_events:")
P_TODAY = st.Page(today, title="Today's signals", icon=":material/notifications:")
P_LOG = st.Page(paper_log, title="Paper log", icon=":material/edit_note:")
P_LEARN = st.Page(learn, title="Learn", icon=":material/school:")

page = st.navigation([P_HOME, P_BACKTEST, P_COMPETE, P_TODAY, P_LOG, P_LEARN])
cfg = sidebar_settings()
page.run()
