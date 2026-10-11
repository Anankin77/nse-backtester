# Learning path: from zero to paper trading with this repo

A self-paced plan. Each step pairs a concept with a command you run here, so you learn by seeing real NSE numbers, not just reading.

**System, not motivation:** one step per sitting, about 30–45 minutes. Keep a plain text file `journal.md` in this folder: date, what you ran, one line on what you learned. The journal is the thing that makes it stick.

---

## The outline

| Phase | Goal | Time |
|---|---|---|
| 1. Market mechanics | How an order turns into shares, and what it costs | 2 sittings |
| 2. Reading a backtest | What every number in the results table means | 3 sittings |
| 3. Strategy families | Trend vs mean reversion vs breakout, and when each works | 3 sittings |
| 4. Backtest honesty | The 5 ways backtests lie, and how to catch them | 3 sittings |
| 5. Your own strategy | Edit Python to test an idea of your own | 3–4 sittings |
| 6. Paper trading | Follow signals with fake money for 6–8 weeks | daily, 5 min |
| 7. Real money (maybe) | Small, rule-based, with a stop | only after 6 |

Phase 6 can start in parallel with phase 2. Paper trading only needs `python run.py today` once a day.

---

## Phase 1: Market mechanics

**Learn**
- Delivery (CNC) vs intraday (MIS). This repo models delivery: you buy, hold for days or months, then sell.
- Market vs limit orders, and why this repo adds **slippage**: you rarely get the exact price you saw.
- Settlement is T+1: shares reach your demat account one trading day after you buy.
- **Costs.** Read `backtester/costs.py`. Each round trip costs roughly 0.25–0.3%, and STT is most of it.
- **Taxes on listed shares** (unchanged by Budget 2026):
  - Sold within 12 months: 20% short-term capital gains (STCG).
  - Held over 12 months: 12.5% long-term capital gains (LTCG), with the first ₹1.25 lakh per year exempt.
  - The backtester ignores tax. A strategy that trades every few weeks pays STCG on every win, so its real edge is smaller than it looks.

**Do**
```bash
python run.py test RELIANCE --strategy sma --param fast=10 --param slow=30
python run.py test RELIANCE --strategy sma --param fast=10 --param slow=30 --no-costs
```
Compare the CAGR of the two runs. The gap is what costs ate. Fast strategies trade more, so they lose more to costs.

**Read:** Zerodha Varsity, module *Introduction to Stock Markets*.

---

## Phase 2: Reading a backtest

Intuition first, then the formula.

| Metric | Intuition | Formula |
|---|---|---|
| **CAGR** | The steady yearly rate that would have turned start into end | (End/Start)^(1/years) − 1 |
| **Volatility** | How bumpy the ride is | std(daily returns) × √252 |
| **Max drawdown** | The worst "I'm down this much from my best" moment, the number that makes people quit | min(equity / running peak − 1) |
| **Sharpe** | Extra return per unit of bumpiness, above a safe rate (6.5% assumed here) | (mean daily return − rf/252) / std × √252 |
| **Sortino** | Sharpe, but only downside bumps count as risk | (annual return − rf) / downside std |
| **Calmar** | Yearly return per unit of worst pain | CAGR / abs(max drawdown) |
| **Win rate** | Share of trades that made money. **Misleading on its own** | wins / closed trades |
| **Time in market** | How often your money is actually invested | days invested / total days |

**Why √252?** There are about 252 trading days a year. If daily returns are independent, variance adds up over days, so standard deviation grows with √days.

**Worked example (your RELIANCE run, 2015 to Oct 2026)**

*Given (your output):* RSI had a 91.7% win rate and 9.4% CAGR. Buy & hold had 17.1% CAGR.

*Deduced:* RSI was invested only 36% of the time, so it missed most of an 11-year uptrend. This is **opportunity cost**: being out of a rising market is a loss you never see in the trade log. RSI's small, frequent wins and the big runs it skipped are a classic **win-rate illusion**.

**Do**
```bash
python run.py compare ^NSEI
python run.py compare ITC
python run.py compare TATASTEEL
```
For each, write in your journal which strategy had the best Calmar, not the best CAGR, and why you think so.

---

## Phase 3: Strategy families

| Family | Bet | Wins when | Loses when | Here |
|---|---|---|---|---|
| **Trend following** | What's rising keeps rising | Long, smooth trends | Choppy sideways markets (gets whipsawed) | `sma` |
| **Mean reversion** | Extremes snap back to average | Range-bound stocks | Strong trends (buys a falling knife, sells a rocket early) | `rsi` |
| **Breakout / momentum** | A new high signals a new move | Explosive moves after quiet periods | False breakouts | `breakout` |

The key idea is **regime dependence**. No strategy works everywhere, because markets switch between trending and ranging. A strategy is a bet on which regime you'll be in.

**Do**
```bash
python run.py scan
```
This runs every strategy on all 10 stocks in `watchlist.txt`. Then ask yourself:
- On how many stocks did any strategy beat buy & hold on CAGR?
- On how many did a strategy have a **smaller max drawdown** than buy & hold?

Usually the second answer is much bigger than the first. That's the honest pitch for most strategies: they offer less pain, not more profit.

Then scale it up: `python run.py scan --list nifty50`. Ten stocks can be luck; fifty is a pattern. Read the **Summary** table at the bottom first.

**Read:** Varsity, module *Technical Analysis*, the chapters on moving averages, RSI and support/resistance.

---

## Phase 4: Backtest honesty

The five ways backtests lie, and where this repo stands on each:

1. **Look-ahead bias.** The strategy uses information it couldn't have had yet. *Handled:* signals at today's close fill at tomorrow's open (`engine.py`).
2. **Overfitting.** You tune settings until the past looks perfect, and the result is fitted to noise. *Handled by* `optimize`, which tunes on old data and tests on new.
3. **Survivorship bias.** Your watchlist is today's winners. Nobody backtests the stocks that fell out of the Nifty. *Not handled:* this inflates every result here, buy & hold included. See it yourself: `python run.py compare WIPRO`, `INDUSINDBK` and `HEROMOTOCO` all left the Nifty 50 in the last year and aren't in `nifty50.txt`.
4. **Ignoring costs and taxes.** *Costs handled; taxes not.*
5. **Data snooping.** You try 50 ideas and keep the best one. Even with a test set, one of them will look great by luck. *Defence:* decide what you'll test before you look, and write it in your journal.

**Do**
```bash
python run.py optimize RELIANCE --strategy sma --split 2021-01-01
python run.py optimize ITC --strategy rsi --split 2021-01-01
```
Compare the best train Sharpe with the test Sharpe. A big drop is overfitting you just watched happen.

**Read:** *Evidence-Based Technical Analysis* by David Aronson, chapters 1–6. It's the best book on why most trading rules fail statistical tests.

---

## Phase 5: Your own strategy

**Python you need** (about 3 sittings, free): the Kaggle *Pandas* micro-course (kaggle.com/learn/pandas). You only need DataFrames, `rolling()`, `shift()` and boolean filters.

**Do: add a strategy**
Open `backtester/strategies.py` and add this below `breakout`:

```python
def sma_with_filter(df, fast=20, slow=50, trend=200):
    """SMA crossover, but only while price is above the 200-day average (long-term uptrend filter)."""
    c = df["Close"]
    cross = sma(c, fast) > sma(c, slow)
    uptrend = c > sma(c, trend)
    return (cross & uptrend).astype(float)
```
Register it in `STRATEGIES` as `"smaf": sma_with_filter`, then run:
```bash
python run.py test ^NSEI --strategy smaf
pytest
```
`pytest` checks that the new strategy runs cleanly and stays within 0 or 1.

**Ideas to test next:** RSI only during uptrends; exit after N days no matter what; a 10% stop-loss. Write the hypothesis in your journal before running it.

**Read:** *Quantitative Trading* by Ernest Chan. It's short and practical, written for individuals building exactly this kind of system.

---

## Phase 6: Paper trading

The backtest tells you the past. Paper trading tells you whether **you** can follow the rules: you'll see the real delays, gaps and temptation to override them.

**Daily routine (after 3:30 pm close)**
```bash
python run.py today
```
- `BUY tmrw`: note a fake buy at tomorrow's open price.
- `SELL tmrw`: note a fake sell.
- `hold` or `-`: do nothing.

Log trades in a sheet (date, stock, strategy, buy/sell, price). That sheet is your paper account. Pick **one** strategy and stick to it for 6–8 weeks. Edit `watchlist.txt` to change the stocks.

---

## Phase 7: Real money (only after paper trading)

**First, see what risk control does.** Intuition: a stop-loss caps how much one bad trade can hurt; position sizing caps how much of your money that trade can touch. Together they set the loss per trade: **loss ≈ position size × stop distance**.

```bash
python run.py compare ^NSEI
python run.py compare ^NSEI --stop 10 --trailing
python run.py compare ^NSEI --stop 8 --risk 1
python run.py scan --list nifty50 --stop 10 --trailing
```
For each run, compare **Max drawdown %** and **CAGR %**. You're buying a smaller worst case with lower returns. That trade-off is the whole game, and only you can decide what's worth it. Also check **Stops hit**: a stop that triggers on every wiggle is too tight, so you get stopped out by noise and pay costs each time.

Ground rules worth adopting before day one:
- Start with an amount you could lose entirely without it hurting.
- Risk at most 1–2% of capital per trade.
- Stay in equity delivery. Keep F&O off, as you have it.
- **Bots:** under the SEBI/NSE retail algo framework (in effect since Oct 2025), a personal script on your own account under **10 orders/sec** doesn't need exchange registration. It does need broker-approved API access with a static IP for order placement. Until you've paper traded profitably, though, let the script print signals and place orders yourself.

**Read:** Varsity, module *Risk Management & Trading Psychology*.

---

## Why F&O stays off: the data

SEBI's own studies of individual equity-derivatives traders:
- **FY25:** 90.9% lost money.
- **FY26:** 87.7% lost money, with aggregate losses of about ₹91,700 crore and an average loss of about ₹1.17 lakh per trader.
- Before costs, 82.1% were losing. Costs alone pushed the share to 87.7%.

---

## Resource list

**Free**
- **Zerodha Varsity** (zerodha.com/varsity): free, no signup, no ads. Read in order: *Introduction to Stock Markets*, *Technical Analysis*, *Fundamental Analysis*, *Risk Management & Trading Psychology*, *Trading Systems*.
- **Zerodha charges page** (zerodha.com/charges) and its brokerage calculator.
- **Kaggle Pandas** course (kaggle.com/learn/pandas).
- *Python for Data Analysis*, 3rd ed., by Wes McKinney, the creator of pandas. Free to read online at wesmckinney.com/book.

**Books (in order)**
1. *A Random Walk Down Wall Street* by Malkiel: the case for buy & hold, to understand what every strategy must beat.
2. *Quantitative Trading* by Ernest Chan: practical systematic trading for individuals.
3. *Evidence-Based Technical Analysis* by Aronson: how to tell real edges from luck.
4. *Fooled by Randomness* by Taleb: why good results are so often luck. Lighter reading.
5. Later, if you go deep: *Advances in Financial Machine Learning* by López de Prado. Advanced; skip until phase 5 feels easy.

---

## Facts in this guide, and where they came from (checked Oct 2026)

- **Costs:** STT 0.1% each side, NSE transaction charge 0.00307%, stamp duty 0.015% on buys, SEBI ₹10/crore, DP ₹15.34 per scrip on sells. Source: zerodha.com/charges.
- **Taxes:** STCG 20%, LTCG 12.5% over 12 months, ₹1.25 lakh exemption, all unchanged in Budget 2026. Source: sahi.com, "LTCG tax on mutual funds and stocks, Budget 2026".
- **Retail algo framework:** effective Oct 1, 2025 (moved from Aug 1). Source: PTI via The Week, Jul 2025. 10 orders/sec threshold and static IP for order placement. Source: Zerodha Z-Connect overview of the NSE circular.
- **F&O losses:** FY25 and FY26 figures from SEBI's studies, as reported by Finnovate (Sep 2026) and Business Standard (Jul 2025).
- **Nifty 50 list:** BSE in, Wipro out, effective Sep 30, 2026 (NSE Indices press release, Aug 2026). No Nifty 50 changes in the March 2026 review (Bajaj Broking, Feb 2026). IndiGo and Max Healthcare replaced Hero MotoCorp and IndusInd Bank in Sep 2025 (Business Standard). Tata Motors' passenger business trades as `TMPV` after the demerger.
- **Everything else** (metric definitions, strategy families, the five biases) is standard material, not looked up.
