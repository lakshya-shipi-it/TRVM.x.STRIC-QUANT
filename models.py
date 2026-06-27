"""
================================================================================
DATA MODELS — Core Domain Types
================================================================================

All dataclasses and enums used across the system. Centralizing these
eliminates circular imports and provides a single import point for type
annotations.

Refactor: Previously split between Sections 2 and 0.5 of system2.py.
ScanResult was buried in the scanner section while ActivePosition was
in the main bot class. Now all domain types live here.
================================================================================
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Direction(Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass
class ScanResult:
    """Per-symbol scanner result with score breakdown and verdict."""
    symbol: str
    price: float
    vol24h_usdt: float
    atr_ratio: float
    vol_ratio: float
    rsi: float
    trend: str          # "bullish" / "bearish" / "flat"
    macd_bull: bool
    total_score: float
    pass_atr: bool
    pass_volume: bool
    pass_rsi: bool
    pass_trend: bool
    verdict: str        # "READY" / "WATCH" / "SKIP"
    fail_reason: str


@dataclass
class TRVMReport:
    """Detailed TRVM signal breakdown."""
    signal: Direction = Direction.NONE
    trend_ok: bool = False
    rsi_ok: bool = False
    volatility_ok: bool = False
    volume_ok: bool = False
    atr: float = 0.0
    rsi_value: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    volume_ratio: float = 0.0
    reason: str = ""


@dataclass
class ScoreReport:
    """Detailed strict-scoring breakdown."""
    total_score: float = 0.0
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volatility_score: float = 0.0
    volume_score: float = 0.0
    direction: Direction = Direction.NONE
    confidence: float = 0.0
    entry_trigger: str = ""


@dataclass
class HybridDecision:
    """Combined decision from both engines."""
    execute: bool = False
    direction: Direction = Direction.NONE
    trvm_signal: Direction = Direction.NONE
    score: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_size: float = 0.0
    atr: float = 0.0


@dataclass
class ActivePosition:
    """Tracks an open position across scan cycles."""
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    risk_amount: float
    entry_time: datetime.datetime
    score: float
    confidence: float
    highest_price: float
    lowest_price: float
    current_price: float = 0.0
    pnl: float = 0.0
    peak_r: float = 0.0
    original_sl: float = 0.0
    breakeven_moved: bool = False
    trail_active: bool = False
    last_monitored: Optional[datetime.datetime] = None

    def unrealized_pnl(self) -> float:
        """Live unrealized P&L in quote currency."""
        if self.current_price == 0.0:
            return 0.0
        if self.direction == Direction.LONG:
            return (self.current_price - self.entry_price) * self.position_size
        return (self.entry_price - self.current_price) * self.position_size

    def unrealized_pnl_pct(self) -> float:
        """Unrealized P&L as % of entry notional."""
        notional = self.entry_price * self.position_size
        return (self.unrealized_pnl() / notional * 100) if notional > 0 else 0.0

    def progress_to_tp(self) -> float:
        """% progress from entry toward TP (+100 = at TP, -100 = at SL)."""
        tp_dist = abs(self.take_profit - self.entry_price)
        if tp_dist == 0 or self.current_price == 0.0:
            return 0.0
        if self.direction == Direction.LONG:
            moved = self.current_price - self.entry_price
        else:
            moved = self.entry_price - self.current_price
        return max(-100.0, min(100.0, moved / tp_dist * 100))

    def hold_duration(self) -> str:
        """Human-readable hold time, e.g. '2h 15m'."""
        elapsed = datetime.datetime.utcnow() - self.entry_time
        h, rem = divmod(int(elapsed.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m:02d}m"
