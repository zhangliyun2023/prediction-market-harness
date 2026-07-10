# Prediction Market Harness

![Python](https://img.shields.io/badge/python-3.12-blue)
![Status](https://img.shields.io/badge/status-research-orange)
![LLM in number path](https://img.shields.io/badge/LLM_in_number_path-never-red)

> Autonomous prediction-market research & trading harness — built with an
> agent-engineering discipline: **the LLM never touches the truth path.**

A quantitative research/trading agent for Polymarket. Design philosophy:
probabilities, prices and risk limits live in deterministic code — the LLM
orchestrates, it never prices. Every strategy must survive a
falsification-first backtest before it sees a dollar, and every real order
passes through layered risk gates. **This is a research framework, not a
money printer — its proudest output so far is killing its own first
strategy with its own backtest sieve.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Data Layer                            │
│  markets.py (Gamma API)   binance.py (spot + EWMA vol)       │
│  intel/ (CN social signals: P0/P1/P2 tiering, triangulation) │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                      Signal Layer                            │
│  signal.py   model prob (lognormal d2) vs market prob → edge │
│  scout.py    hunt lazy-priced markets + capacity estimation  │
│  arbitrage.py  spread-vs-exact-score consistency check       │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   Validation Layer  ← nothing skips this     │
│  backtest.py   look-ahead-bias-proof backtest + param sweep  │
│  paper.py      paper ledger, Wilson CI, n<20 = noise warning │
│  (local launchd + GitHub Actions cloud loop, isolated books) │
└──────────────────────┬──────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   Execution Layer                            │
│  orders.py   FOK market / limit / batch, hard notional cap   │
│  risk.py     MAX_SPEND ceiling — changing it requires a      │
│              code edit (the cooling-off period by design)    │
│  place_order.py  3-question gate before any real order:      │
│    ① where is the edge?  ② who's the counterparty and why    │
│    are they wrong?  ③ % of bankroll? — blank answer = abort  │
└─────────────────────────────────────────────────────────────┘
```

## Key engineering decisions

**1. LLM stays out of the number path.**
Probabilities come from a closed-form model (no-drift lognormal, `N(d2)`),
volatility from EWMA over 5-minute klines (RiskMetrics-style, half-life
matched to market settlement horizon). Free, millisecond-fast, reproducible,
backtestable. An LLM computing probabilities would be slow, expensive, and
non-deterministic — it orchestrates, it never prices.

**2. Verification culture: the backtest that killed its own strategy.**
The first signal strategy (Binance spot vs Polymarket BTC threshold markets)
showed **+9.7% ROI** on a single backtest run. A 12-cell parameter sweep then
showed the "edge" was entirely controlled by decision-lead-time, not model
skill — profits at 1h lead (market nearly settled, trivially predictable),
losses at 6h lead, no cell with a Wilson 95% lower bound above 50%.
**Strategy rejected before a single real dollar.** The framework's job is to
kill bad ideas cheaply; it works.

**3. Look-ahead bias treated as the enemy.**
The backtest reconstructs "what was knowable at decision time T": market
price = last tick ≤ T, spot = last kline close ≤ T, volatility = returns
strictly before T. Settlement outcomes are used for scoring only, never
visible to the decision.

**4. Risk gates are code, not discipline.**
Order notional hard cap lives in source (`MAX_ORDER_NOTIONAL_USD`) — raising
it requires editing code, which *is* the cooling-off period. A pre-trade
3-question gate forces the trader to articulate the edge and the counterparty
before the confirm prompt. Paper ledger prints a loud "n<20 = noise, not a
conclusion" banner.

**5. Deterministic-first tiering.**
Everything that can be code is code (pricing, dedup, capacity math, order
construction). Browser automation only where APIs are gated (CN social
collectors, reused from a maintained toolkit via a thin bridge — never
forked, because scraper endpoints rot). LLM only at the top, as planner.

**6. Capacity-aware signals: edge × capacity is the only number that pays.**
The scout ranks under-priced markets but also walks the real order book to
compute deployable dollars within 2% slippage. A 20% edge on a $30-deep
book is $6 of expectancy — the scanner says so out loud instead of
flattering the signal.

**7. Intelligence tiering for information asymmetry.**
CN-language social signals are classified P0 (already priced — a headline is
a deadline, not a signal), P1 (half-priced, minutes of window), P2 (unpriced;
requires ≥2 independent sources or first-hand verification before it may
even reach the capacity check). Single-source P2 is hard-rejected in code.

## Ops

- **Dual paper loops**: local launchd (15-min cadence, survives reboots) +
  GitHub Actions cloud loop (read-only, isolated ledger via env override,
  commits its own book back to the repo). No secrets in CI — the cloud loop
  is structurally incapable of trading.
- **Journal**: every real fill synced from the public data API with
  mandatory `thesis` / `review` fields — decisions are graded on the odds
  at entry, not the outcome.
- **Secrets hygiene**: keys in `.env` (0600, gitignored), API creds cached
  locally, secret-pattern scan before the initial publish.

## Ready-to-use tool

A standalone, config-and-run monitor bot built on this harness —
watch any Polymarket market, alert on price thresholds (Slack/Discord/Telegram),
no keys, never trades:
**https://github.com/zhangliyun2023/polymarket-market-monitor**

## Stack

Python 3.12 · `py-clob-client-v2` (official Polymarket V2 SDK) · Binance
public data API · launchd + GitHub Actions · zero LLM calls at runtime.

## Honest limitations

- Retail latency (hundreds of ms) — in-play/HFT lanes are structurally
  closed; the design targets slower information and structural edges.
- Efficient large markets offer no edge to public-information analysis;
  three independent estimates in testing landed within 0.3pp of market
  price. The framework's value is knowing this *before* betting.
- Small-sample anything is noise. The tooling says so, repeatedly.

## Disclaimer

Research software. Not financial advice. Prediction-market trading can lose
money; every safety rail in this repo exists because it must.
