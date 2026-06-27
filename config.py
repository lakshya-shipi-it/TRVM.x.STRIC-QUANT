"""
================================================================================
CONFIGURATION — Single Source of Truth for All System Parameters
================================================================================

All trading parameters, risk thresholds, and system settings are centralized
here. No magic numbers exist in other modules — every tunable constant
references this configuration.

Security:
    API credentials are read from environment variables. Hardcoded keys are
    stripped and replaced with placeholders. Never commit real credentials.

Usage:
    from config import CFG
    atr_limit = CFG.ATR_MAX_RATIO

Refactor note:
    Extracted from the monolithic system2.py (Section 1). Previously,
    parameters were scattered across 5000 lines. Centralizing them revealed
    three inconsistencies in ATR threshold definitions that caused
    scanner/bot mismatches.
================================================================================
"""

import os
import logging
import sys
from dataclasses import dataclass, field
from typing import List

# ─── Logging Configuration ──────────────────────────────────────────────────
_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(message)s"
_LOG_DATE = "%Y-%m-%d %H:%M:%S"

_stream_handler = logging.StreamHandler(
    stream=open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
    if hasattr(sys.stdout, "fileno") else sys.stdout
)
_stream_handler.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE))

_file_handler = logging.FileHandler("hybrid_bot.log", mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FMT, _LOG_DATE))

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])
log = logging.getLogger("HybridBot")


# ─── Trade Log Configuration ────────────────────────────────────────────────
TRADE_LOG_FILE = "trade_log.csv"
TRADE_LOG_FIELDS = [
    "timestamp", "symbol", "side", "entry_price",
    "stop_loss", "take_profit", "pnl", "score",
    "trvm_signal", "exit_reason", "confidence",
]


# ─── HybridConfig ───────────────────────────────────────────────────────────

@dataclass
class HybridConfig:
    """
    Single source of truth for all system parameters.

    Design decisions:
        1. All numeric parameters use explicit types (int/float) — no
           ambiguity between 1 (int) and 1.0 (float) that caused bugs.
        2. API keys read from env vars with empty-string fallbacks.
        3. Risk parameters grouped logically, not alphabetically.
        4. Every parameter has a docstring comment explaining its purpose.

    Refactor: Previously a 200-line class with parameters interleaved
    with DeepSeek settings and market regime filters. Now organized by
    functional domain.
    """

    # ── API Credentials (env-var sourced) ───────────────────────────────────
    # SECURITY: These default to empty strings. Set via environment
    # variables or a .env file. Never hardcode real keys.
    API_KEY:    str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    API_SECRET: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))

    # ── Market Data ─────────────────────────────────────────────────────────
    INTERVAL:       str = "1h"       # Primary trading timeframe
    CANDLES_NEEDED: int = 100       # Minimum candles for indicator warmup

    # ── Symbol Universe ─────────────────────────────────────────────────────
    SYMBOLS: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    # Scanner settings
    AUTO_MODE:        bool  = True    # Run scanner automatically
    TOP_N_SYMBOLS:    int   = 3       # How many top coins to trade per cycle
    MIN_SCAN_SCORE:   float = 70.0    # A+ pre-filter threshold
    RESCAN_DELAY_SEC: int   = 60      # Pause between cycles
    MAX_IDLE_SCANS:   int   = 3       # Max consecutive empty scans before rescan

    # ── Trading Mode ────────────────────────────────────────────────────────
    PAPER_MODE:       bool = True     # Paper trading by default (safety)
    USE_FUTURES:      bool = True     # Futures vs. Spot
    SCAN_INTERVAL_SEC: int = 60      # Seconds between position monitoring scans

    # ── Futures Leverage ────────────────────────────────────────────────────
    LEVERAGE: int = 5                 # Conservative default (production: 3-13x)

    # ── TRVM Signal Engine Parameters ───────────────────────────────────────
    # These define the trend-following filter thresholds.
    EMA_FAST:         int   = 20
    EMA_SLOW:         int   = 50
    RSI_PERIOD:       int   = 14
    ATR_PERIOD:       int   = 14
    VOLUME_MA_PERIOD: int   = 20

    # RSI bands — narrower than standard to avoid overextended entries
    RSI_LONG_MIN:  float = 45.0       # LONG only when momentum is building
    RSI_LONG_MAX:  float = 65.0       # Reject near-overbought entries
    RSI_SHORT_MIN: float = 33.0       # SHORT only in confirmed weakness
    RSI_SHORT_MAX: float = 50.0       # Reject oversold bounce risk

    # Volume filter — must be meaningfully above average
    VOLUME_MULTIPLIER: float = 1.2    # Current vol >= 1.2x the 20-bar MA
    MIN_VOLUME_RATIO:  float = 1.5    # Strict scoring minimum

    # ATR filters — minimum volatility to avoid slow-movers
    ATR_MIN_RATIO: float = 0.015      # 1.5% minimum ATR/price
    ATR_MAX_RATIO: float = 0.045      # 4.5% maximum (extreme vol = skip)

    # Trend strength — EMA gap must be meaningful
    MIN_EMA_GAP_PCT: float = 0.015    # Rejects flat/sideways EMA stacks
    MAX_EMA_GAP_PCT: float = 0.040    # Rejects overextended entries

    # Anti-chaser: max distance from price to EMA20
    MAX_PRICE_EMA_DISTANCE:  float = 0.025   # 2.5% max
    PUMP_REVERSAL_THRESHOLD: float = 0.035   # Wick spike above EMA

    # Post-pump recovery guard
    POST_PUMP_LOOKBACK:  int   = 2
    POST_PUMP_SPIKE_PCT: float = 0.05

    # ── Strict Scoring Engine Parameters ────────────────────────────────────
    SCORE_EMA_FAST:  int   = 9
    SCORE_EMA_SLOW:  int   = 21
    SCORE_EMA_TREND: int   = 50
    MACD_FAST:       int   = 12
    MACD_SLOW:       int   = 26
    MACD_SIGNAL:     int   = 9
    BB_PERIOD:       int   = 20
    BB_STD:        float   = 2.0
    VOLUME_PERIOD:   int   = 20
    MAX_ATR_PERCENT: float = 4.5

    # ── Dual-Confirmation Gate ──────────────────────────────────────────────
    MIN_SCORE_TO_TRADE: float = 80.0  # A+ threshold

    # ── Risk Management ─────────────────────────────────────────────────────
    STARTING_CAPITAL: float = 10000.0   # Starting capital in USDT
    RISK_PERCENT:     float = 0.02     # 2% risk per trade
    RISK_PER_TRADE:   float = 0.02     # Alias for backward compatibility
    MIN_TRADE_SIZE:   float = 6.0      # Minimum $6 notional
    MAX_RISK_PER_TRADE_PCT: float = 3.0
    MAX_RISK_PER_DAY:       float = 5.0      # % daily limit
    TAKER_FEE_PCT:          float = 0.0004   # 0.04% per side (Binance Futures)
    MAX_OPEN_POSITIONS:     int   = 3
    MAX_SAME_DIR_POSITIONS: int   = 2
    MIN_RISK_REWARD:        float = 1.25
    MAX_POSITION_SIZE_PCT:  float = 100.0

    # ATR-based SL/TP multipliers
    ATR_SL_MULTIPLIER: float = 1.5     # Stop-loss = entry ± (ATR × 1.5)
    ATR_TP_MULTIPLIER: float = 3.0     # Take-profit = entry ± (ATR × 3.0)

    # Trailing stop parameters
    TRAILING_STOP_ATR:     float = 1.5  # ATR-based trail distance
    TRAILING_ACTIVATION_R: float = 0.5  # Activate trail at 0.5R profit
    TRAILING_R_DROP:       float = 0.15  # Trail stays 0.15R below peak

    # Position upgrade
    UPGRADE_SCORE_GAP:        float = 5.0   # Min score gap to upgrade
    UPGRADE_MIN_HOLD_MINUTES: int   = 7     # Min hold before upgrade
    BREAKEVEN_R:              float = 0.3   # Move SL to entry at 0.3R

    # Cooldowns
    COOLDOWN_HOURS: float = 0.5        # Global cooldown after loss

    # Daily limits
    DAILY_LOSS_LIMIT_PCT: float = 0.05  # Stop trading at 5% daily loss
    MAX_HOLD_HOURS:       int   = 48    # Max position hold time
    MAX_DAILY_ENTRIES_PER_SYMBOL: int = 3

    # Connection resilience
    CONNECTION_TIMEOUT_MINUTES: int = 5  # Force-close if unmonitored

    # ── Scalper Exit System ─────────────────────────────────────────────────
    SCALPER_QUICK_PROFIT_R:   float = 0.7
    SCALPER_MAX_HOLD_MINUTES: int   = 180
    STAGNANCY_MINUTES:        int   = 30
    STAGNANCY_R_THRESHOLD:    float = 0.15
    MAX_LOSS_R:               float = -0.5
    EARLY_LOSS_PCT:           float = -0.8
    EARLY_LOSS_MINUTES:       int   = 10
    EARLY_LOSS_COOLDOWN_MINUTES: int = 60
    EARLY_LOSS_R:             float = -0.3
    EARLY_LOSS_R_MINUTES:     float = 5.0
    STAGNANCY_COOLDOWN_MINUTES: int = 20

    # ── Market Regime Filters ───────────────────────────────────────────────
    # ADX trend strength filter
    ADX_PERIOD:    int   = 14
    ADX_THRESHOLD: float = 21.0       # Skip if ADX < 21 (ranging market)

    # Relative Volatility Expansion (RVE)
    RVE_ENABLED:         bool  = True
    RVE_FAST_PERIOD:     int   = 5
    RVE_SLOW_PERIOD:     int   = 100
    RVE_COMPRESSION_MAX: float = 0.8   # Below = compression (skip)
    RVE_EXHAUSTION_MIN:  float = 3.0   # Above = exhaustion (skip)

    # 24h-High Proximity Filter
    HIGH_24H_FILTER_ENABLED:   bool  = True
    MIN_24H_HIGH_DISTANCE_PCT: float = 0.04   # 4% min distance from 24h high


# Global configuration instance
# Modules import this singleton. For testing, replace with a test config.
CFG = HybridConfig()
