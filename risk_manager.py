"""
================================================================================
RISK MANAGER — ATR-Based Position Sizing and Circuit Breakers
================================================================================

Centralizes all risk calculations: position sizing, stop-loss/take-profit
levels, trailing stops, and trading circuit breakers.

Refactor: Originally Section 7 of system2.py. Extracted because risk
management is a distinct domain that should be independently testable
and auditable. A quant fund's risk committee should be able to review
THIS FILE ONLY to understand all risk parameters.

Design principles:
    1. Every function is pure (inputs -> outputs, no side effects).
    2. All parameters come from CFG — no hardcoded thresholds.
    3. Position sizing uses Decimal arithmetic to avoid float drift.
    4. All edge cases (zero ATR, minimum notional, etc.) are handled.
================================================================================
"""

from __future__ import annotations

import datetime
from typing import Dict, Optional, Tuple

from config import CFG, log
from models import Direction


def calculate_position(
    direction: Direction,
    entry_price: float,
    atr: float,
    capital: float,
) -> Optional[Dict]:
    """
    Calculate all trade parameters using ATR-based risk sizing.

    Dynamic Risk (scales with ANY capital):
        risk_amount   = starting_capital × RISK_PERCENT
        sl_distance   = ATR × ATR_SL_MULTIPLIER
        position_size = risk_amount / sl_distance

    Enforces MIN_TRADE_SIZE. If the computed notional is below minimum,
    scales up and recalculates actual risk. If actual risk exceeds
    1.5x configured risk, returns None (skip trade).

    Args:
        direction: LONG or SHORT
        entry_price: Current market price
        atr: Average True Range (ATR) value
        capital: Current account balance

    Returns:
        Dict with entry_price, stop_loss, take_profit, position_size,
        risk_amount, sl_distance, tp_distance, risk_reward, leverage.
        Returns None if trade should be skipped (risk too high or
        position rounds to zero).
    """
    sl_distance = atr * CFG.ATR_SL_MULTIPLIER
    tp_distance = atr * CFG.ATR_TP_MULTIPLIER

    # Fixed risk: always a % of starting capital (not live balance)
    # Prevents compounding: wins can't inflate future sizes
    risk_amount = CFG.STARTING_CAPITAL * CFG.RISK_PERCENT

    if sl_distance <= 0:
        log.warning("SL distance is zero — skipping trade")
        return None

    position_size = risk_amount / sl_distance

    # Cap at max position size
    max_pos_value = capital * (CFG.MAX_POSITION_SIZE_PCT / 100) * CFG.LEVERAGE
    if entry_price > 0 and position_size * entry_price > max_pos_value:
        position_size = max_pos_value / entry_price

    # Minimum trade size guard
    if entry_price > 0:
        notional = position_size * entry_price
        if 0 < notional < CFG.MIN_TRADE_SIZE:
            position_size = CFG.MIN_TRADE_SIZE / entry_price
            actual_risk_amt = position_size * sl_distance
            actual_risk_pct = (actual_risk_amt / capital) if capital > 0 else 0
            max_allowed_risk = CFG.RISK_PERCENT * 1.5
            if actual_risk_pct > max_allowed_risk:
                log.warning(
                    "Skipped: MIN_TRADE_SIZE forces %.2f%% risk "
                    "(max %.2f%%). Increase capital or reduce MIN_TRADE_SIZE.",
                    actual_risk_pct * 100, max_allowed_risk * 100,
                )
                return None

    # Quantity precision: round based on magnitude
    if position_size > 0:
        if position_size >= 1:
            position_size = round(position_size, 4)
        elif position_size >= 0.01:
            position_size = round(position_size, 5)
        else:
            position_size = round(position_size, 6)

    if direction == Direction.LONG:
        stop_loss = entry_price - sl_distance
        take_profit = entry_price + tp_distance
    else:
        stop_loss = entry_price + sl_distance
        take_profit = entry_price - tp_distance

    rr = tp_distance / sl_distance if sl_distance > 0 else 0

    return {
        "entry_price": round(entry_price, 6),
        "stop_loss": round(stop_loss, 6),
        "take_profit": round(take_profit, 6),
        "position_size": position_size,
        "risk_amount": round(risk_amount, 4),
        "sl_distance": round(sl_distance, 6),
        "tp_distance": round(tp_distance, 6),
        "risk_reward": round(rr, 2),
        "leverage": CFG.LEVERAGE,
    }


def trailing_stop_r(
    direction: Direction,
    peak_r: float,
    entry_price: float,
    sl_distance: float,
) -> float:
    """
    R-Based Trailing Stop — trails by TRAILING_R_DROP below peak R.

    Replaces the old ATR-based trailing stop which was too loose for
    small R gains. The R-based trail always locks in (peak_R - drop)
    of profit.

    Examples (TRAILING_R_DROP=0.15):
        Peak 0.5R → trail at 0.35R → captures 0.35R on pullback
        Peak 1.0R → trail at 0.85R → captures 0.85R on pullback
        Peak 2.0R → trail at 1.85R → captures 1.85R on pullback

    Floor at entry_price guarantees breakeven protection.
    """
    trail_r = peak_r - CFG.TRAILING_R_DROP
    trail_r = max(trail_r, 0.0)  # Floor at breakeven

    if direction == Direction.LONG:
        trail_price = entry_price + trail_r * sl_distance
        return max(trail_price, entry_price)
    else:
        trail_price = entry_price - trail_r * sl_distance
        return min(trail_price, entry_price)


def trailing_stop_atr(
    direction: Direction,
    highest_price: float,
    lowest_price: float,
    atr: float,
    entry_price: float = 0.0,
) -> float:
    """
    ATR-Based Trailing Stop — fallback for large R moves.

    Used as a secondary trail: the effective SL is the TIGHTER of
    the R-based and ATR-based trails.
    """
    trail_dist = atr * CFG.TRAILING_STOP_ATR
    if direction == Direction.LONG:
        trail = highest_price - trail_dist
        if entry_price > 0:
            trail = max(trail, entry_price)
        return trail
    else:
        trail = lowest_price + trail_dist
        if entry_price > 0:
            trail = min(trail, entry_price)
        return trail


def check_circuit_breakers(
    daily_pnl: float,
    last_loss_time: Optional[datetime.datetime],
    open_count: int,
    capital: float,
) -> Tuple[bool, str]:
    """
    Circuit breakers — halt trading when risk thresholds are breached.

    Returns (should_stop, reason). All checks are independent;
    the first triggered breaker wins.

    Breakers (in priority order):
        1. Daily loss limit
        2. Max concurrent positions
        3. Cooldown after loss
    """
    # Daily loss limit
    if daily_pnl <= -(capital * CFG.DAILY_LOSS_LIMIT_PCT):
        return True, f"Daily loss limit hit ({daily_pnl:.2f} USDT)"

    # Max concurrent positions
    if open_count >= CFG.MAX_OPEN_POSITIONS:
        return True, f"Max positions ({CFG.MAX_OPEN_POSITIONS}) reached"

    # Cooldown after loss
    if last_loss_time is not None:
        elapsed_h = (datetime.datetime.utcnow() - last_loss_time).total_seconds() / 3600
        if elapsed_h < CFG.COOLDOWN_HOURS:
            return True, f"Cooldown — {CFG.COOLDOWN_HOURS - elapsed_h:.1f}h remaining"

    return False, ""
