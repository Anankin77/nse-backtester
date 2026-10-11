"""Charts: equity curves, drawdowns, and trade markers on price."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # save to file; works without a display
import matplotlib.pyplot as plt  # noqa: E402

GREY = "#8a8a8a"
COLORS = ["#1f4e79", "#c0504d", "#4f8a3c", "#8064a2", "#d08c2a"]


def report(df, results, title: str, out: str | Path) -> Path:
    """Three stacked panels: price + trades, equity curves, drawdowns."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(11, 9), sharex=True,
                                        gridspec_kw={"height_ratios": [2, 2, 1]})

    ax1.plot(df.index, df["Close"], color=GREY, lw=1, label="Close")
    main = next((r for r in results if r.name != "Buy & hold"), results[0])
    if len(main.trades):
        t = main.trades
        ax1.scatter(t["entry_date"], t["entry_price"], marker="^", color="#4f8a3c", s=40, label="Buy", zorder=3)
        closed = t[~t["still_open"]]
        ax1.scatter(closed["exit_date"], closed["exit_price"], marker="v", color="#c0504d", s=40, label="Sell", zorder=3)
    ax1.set_title(title, loc="left", fontsize=12)
    ax1.set_ylabel("Price (Rs)")
    ax1.legend(loc="upper left", frameon=False, fontsize=8)

    for i, r in enumerate(results):
        color = GREY if r.name == "Buy & hold" else COLORS[i % len(COLORS)]
        ax2.plot(r.equity.index, r.equity, lw=1.4, color=color, label=r.name)
        dd = 100 * (r.equity / r.equity.cummax() - 1)
        ax3.plot(dd.index, dd, lw=1, color=color)
    ax2.set_ylabel("Portfolio (Rs)")
    ax2.legend(loc="upper left", frameon=False, fontsize=8)
    ax3.set_ylabel("Drawdown %")

    for ax in (ax1, ax2, ax3):
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out
