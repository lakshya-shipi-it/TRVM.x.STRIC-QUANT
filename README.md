# Hybrid Trading Bot

> A production-grade algorithmic trading system with dual-signal confirmation, ATR-based risk management, and automatic market scanning.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Mode: Paper-first](https://img.shields.io/badge/default%20mode-paper-orange.svg)]()

<p align="center">
  <img src="docs/screenshots/demo.gif" alt="Hybrid Trading Bot — live terminal demo" width="850">
</p>

## Table of Contents

- [Overview](#overview)
- [Proprietary Signal Engine](#%EF%B8%8F-proprietary-signal-engine)
- [Architecture](#architecture)
- [Module Breakdown](#module-breakdown)
- [Key Features](#key-features)
- [Performance Snapshot](#performance-snapshot)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Engineering Quality](#engineering-quality)
- [License](#license)

## Overview

This repository demonstrates the architecture of a systematic cryptocurrency trading system. It combines two independent signal engines — a trend-following TRVM engine and a multi-factor scoring engine — behind a dual-confirmation gate that dramatically reduces false entries.

**This is a portfolio project.** All proprietary strategy parameters have been replaced with configurable defaults. The system is designed for paper trading by default and requires explicit configuration to run live.

## ⚠️ Proprietary Signal Engine

> The public repository contains a **demo signal engine** for architecture demonstration only.
> The production **TRVM × Strict Scoring** engine is available for review **under NDA during interviews**.

## Architecture

```
                        ┌──────────────────────────────────────┐
                        │         Binance Exchange              │
                        │     (REST + WebSocket APIs)           │
                        └──────────────┬───────────────────────┘
                                       │
                        ┌──────────────▼───────────────────────┐
                        │          exchange.py                  │
                        │    Unified API Client                 │
                        │  ├─ HMAC-SHA256 auth                 │
                        │  ├─ Rate-limit handling              │
                        │  ├─ Order execution                  │
                        │  └─ Protective orders (SL/TP)        │
                        └──────────────┬───────────────────────┘
                                       │
          ┌────────────────────────────┴────────────────────────────┐
          │                                                         │
┌─────────▼──────────┐                            ┌────────────────▼─────┐
│     scanner.py      │                            │    indicators.py      │
│  Market Scanner     │                            │  Indicator Engine     │
│  ├─ Symbol discovery│                            │  ├─ EMA, RSI, ATR    │
│  ├─ Pre-filtering   │                            │  ├─ MACD, Bollinger  │
│  └─ Quality scoring │                            │  ├─ ADX, RVE         │
└─────────┬───────────┘                            └────────────────┬─────┘
          │                                                         │
          │         ┌───────────────────────────────────────────────┘
          │         │
┌─────────▼─────────▼──────────────────────────────────────────────────────┐
│                          strategy.py                                      │
│    ┌─────────────────────┐    ┌─────────────────────┐                   │
│    │   TRVM Engine       │    │   Scoring Engine    │                   │
│    │   (trend-following) │    │   (multi-factor)    │                   │
│    │   4 filters + 6     │    │   4 factors, 0-100  │                   │
│    │   additional guards │    │   Trend 40%         │                   │
│    │                     │    │   Momentum 30%      │                   │
│    └──────────┬──────────┘    │   Volatility 20%    │                   │
│               │               │   Volume 10%        │                   │
│               └───────────────┴──────────┬──────────┘                   │
│                                          │                              │
│                              ┌───────────▼──────────┐                   │
│                              │  Dual-Confirm Gate   │                   │
│                              │  Score >= 80         │                   │
│                              │  Both agree          │                   │
│                              └───────────┬──────────┘                   │
└──────────────────────────────────────────┼──────────────────────────────┘
                                           │
                        ┌──────────────────▼───────────────────────┐
                        │         risk_manager.py                   │
                        │    ATR-Based Position Sizing              │
                        │  ├─ Dynamic risk scaling                  │
                        │  ├─ R-based trailing stop                │
                        │  ├─ Breakeven protection                 │
                        │  └─ Circuit breakers (8 levels)          │
                        └──────────────────┬───────────────────────┘
                                           │
                        ┌──────────────────▼───────────────────────┐
                        │         monitor.py                        │
                        │    Position Lifecycle Manager             │
                        │  ├─ 7-exit framework                     │
                        │  ├─ Position upgrades                    │
                        │  ├─ Cooldown management                  │
                        │  └─ Auto-scan orchestration              │
                        └──────────────────┬───────────────────────┘
                                           │
                        ┌──────────────────▼───────────────────────┐
                        │         config.py                         │
                        │    Centralized Parameter Store            │
                        │  ├─ All risk thresholds                  │
                        │  ├─ Signal engine params                 │
                        │  ├─ Exchange settings                    │
                        │  └─ Scanner configuration                │
                        └──────────────────────────────────────────┘
```

## Module Breakdown

| Module | Lines | Purpose |
|--------|-------|---------|
| `config.py` | ~200 | All tunable parameters in one place |
| `models.py` | ~100 | Domain dataclasses (ScanResult, ActivePosition, etc.) |
| `indicators.py` | ~200 | Technical indicator calculations (single-pass) |
| `exchange.py` | ~400 | Binance API client with rate-limit resilience |
| `risk_manager.py` | ~150 | Position sizing, trailing stops, circuit breakers |
| `scanner.py` | ~350 | Market scanning and symbol pre-filtering |
| `strategy.py` | ~500 | TRVM + Scoring engines + dual-confirmation gate |
| `monitor.py` | ~550 | Position monitoring, execution, main loop |
| `main.py` | ~100 | CLI entry point |
| **Total** | **~2,550** | |

## Key Features

### Dual-Signal Architecture

The system uses two independent engines whose signals must agree before a trade executes:

- **TRVM Engine** — trend-following with 10 filter layers (EMA alignment, RSI bands, ATR range, volume confirmation, ADX regime filter, no-chaser protection, pump-reversal guard, post-pump recovery, volume-price divergence, and MACD alignment)
- **Scoring Engine** — multi-factor quality scoring (trend 40%, momentum 30%, volatility 20%, volume 10%) producing a 0–100 score
- **Dual-Confirm Gate** — both engines must agree on direction AND score must clear the configured threshold

<p align="center">
  <img src="docs/screenshots/signal-generation.png" alt="Dual-signal gate — TRVM and Scoring engines, accepted and rejected candidates" width="850">
  <br>
  <sub><b>strategy.py</b> — dual-signal gate: accepted A+ trades alongside candidates rejected for chasing, pump-reversal, exhaustion, and low volume</sub>
</p>

### Market Scanner

Every cycle, the scanner pulls all Binance Futures USDT pairs, runs them through indicator pre-filtering, and ranks candidates by score before they ever reach the signal engines.

<p align="center">
  <img src="docs/screenshots/scanner-output.png" alt="Market scanner output — 248 symbols scanned, ranked by score" width="850">
  <br>
  <sub><b>scanner.py</b> — a full market scan cycle: 248 symbols scanned, ranked by score, verdict, and trend</sub>
</p>

### Risk Management Framework

- **ATR-based position sizing** — adapts to market volatility
- **R-based trailing stop** — captures profit proportionally to distance traveled
- **Breakeven protection** — guarantees no loss once price crosses the configured R-threshold
- **8 circuit breaker levels** — daily loss limit, max positions, cooldown, per-symbol caps
- **Exchange-side SL/TP orders** — hard protection even if the bot goes offline

<p align="center">
  <img src="docs/screenshots/risk-management.png" alt="Risk manager — breakeven lock, profit lock, and trailing stop upgrades on a live position" width="850">
  <br>
  <sub><b>risk_manager.py</b> — adaptive position defense: breakeven lock → profit lock → trailing stop upgrade, plus the 7-exit distribution across closed trades</sub>
</p>

### 7-Exit Framework

Every position is monitored against 7 exit conditions checked in priority order:

1. **Quick Profit** — modest profit target booked early
2. **Max Loss R** — early cut before full stop-loss is hit
3. **Stop Loss / Take Profit / Trailing Stop** — the classic exits, R-based
4. **Early-Loss R** — fast detection of an immediately wrong trade
5. **Early-Loss %** — time-based bleed detection
6. **Stagnancy** — frees capital from dead, directionless trades
7. **Time-based exit** — hard max-hold limit; thesis expired

<p align="center">
  <img src="docs/screenshots/monitor.png" alt="Position lifecycle manager — live portfolio snapshot and per-position monitoring" width="850">
  <br>
  <sub><b>monitor.py</b> — position lifecycle manager: portfolio snapshots and live per-position monitoring across scan cycles</sub>
</p>

### Order Execution

Orders are filtered for exchange compliance (`stepSize`, `minQty`, `minNotional`) before submission, then tracked through their full open → monitor → close lifecycle.

<p align="center">
  <img src="docs/screenshots/execution-flow.png" alt="Order execution lifecycle — paper order flow from entry to exit" width="850">
  <br>
  <sub><b>exchange.py</b> — order execution lifecycle: filter compliance, entry, live monitoring, and exit with realized PnL</sub>
</p>

### Trade Log & Results

Every decision is logged to `trade_log.csv` for full auditability.

<p align="center">
  <img src="docs/screenshots/trade-log.png" alt="Trade log — 10 closed paper trades with win rate and profit factor summary" width="850">
  <br>
  <sub><b>trade_log.csv</b> — a 10-trade paper session: full reason-coded exit log with win rate and profit factor summary</sub>
</p>

## Performance Snapshot

Pulled directly from a 10-trade paper session (`trade_log.csv`, $50 starting capital, 13× leverage):

| Metric | Value |
|--------|-------|
| Trades closed | 10 |
| Win / Loss | 8 / 2 |
| Win rate | 80.0% |
| Net PnL | +$6.53 |
| ROI (session) | +13.07% |
| Profit factor | 3.48 |
| Avg win | +$1.15 |
| Avg loss | −$1.32 |

> Paper-trading sample over a short window — not a claim of live, risk-adjusted performance. Included to demonstrate the audit trail the system produces, not as a forward-looking guarantee.

## Quick Start

### Prerequisites

```bash
# Python 3.11+
pip install -r requirements.txt
```

### Set API Credentials (for live data)

```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"
```

### Paper Trading (Default)

```bash
# Auto-scanning mode — finds top 5 symbols, trades them
python main.py --mode paper --auto --top 5 --futures

# Fixed symbols
python main.py --mode paper --symbols BTCUSDT ETHUSDT
```

<p align="center">
  <img src="docs/screenshots/terminal-running.png" alt="Bot running in paper mode — full configuration printout and auto-loop start" width="850">
  <br>
  <sub><b>main.py</b> — paper-mode startup: full configuration printout followed by the auto-scan loop</sub>
</p>

### Live Trading (Use with Caution)

```bash
python main.py --mode live --auto --top 3 --futures --capital 10000
```

## Configuration

All parameters are in `config.py`. Key settings:

| Parameter | Default | Description |
|-----------|---------|--------------|
| `PAPER_MODE` | `True` | Paper trading (no real orders) |
| `RISK_PERCENT` | `0.02` | Risk per trade (2% of capital) |
| `LEVERAGE` | `5` | Futures leverage |
| `MIN_SCORE_TO_TRADE` | `80` | Minimum score to execute |
| `MAX_OPEN_POSITIONS` | `3` | Max concurrent trades |
| `COOLDOWN_HOURS` | `0.5` | Cooldown after loss |

## Project Structure

```
hybrid-trading-bot/
├── config.py              # All system parameters
├── models.py               # Domain dataclasses
├── indicators.py            # Technical indicator engine
├── exchange.py              # Binance API client
├── risk_manager.py          # Position sizing & circuit breakers
├── scanner.py                # Market scanner
├── strategy.py                # Signal & scoring engines
├── monitor.py                 # Position monitoring & main loop
├── main.py                      # CLI entry point
├── requirements.txt             # Dependencies
├── .gitignore
├── LICENSE
├── README.md
├── docs/
│   ├── architecture.md     # Detailed architecture document
│   ├── risk_model.md        # Risk management documentation
│   ├── signals.md            # Signal engine documentation
│   └── screenshots/            # README screenshots and demo gif
└── tests/
    (test files)
```

## Engineering Quality

- **Type annotations** throughout all modules
- **Dataclass-based models** with validation
- **Pure functions** for indicator calculations (testable, parallelizable)
- **Single-pass computation** — all indicators computed once, shared by both engines
- **Rate-limit resilience** — exponential backoff, 429/418 handling, circuit breaker
- **Decimal arithmetic** for exchange filter compliance (avoids float drift)
- **Comprehensive logging** — every decision is auditable via `trade_log.csv`

## License

MIT License — see [LICENSE](LICENSE) for details.
