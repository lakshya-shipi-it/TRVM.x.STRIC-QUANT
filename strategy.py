from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

from config import CFG, log
from models import Direction, ScoreReport, TRVMReport


# ─── Helper: Candles per 24h ────────────────────────────────────────────────

def _candles_per_24h(interval: str) -> int:
    """Convert Binance interval to candles per 24h window."""
    try:
        unit = interval[-1]
        value = int(interval[:-1])
        if unit == "m":
            minutes = value
        elif unit == "h":
            minutes = value * 60
        elif unit == "d":
            minutes = value * 60 * 24
        else:
            minutes = 60
        return max(1, round(1440 / minutes))
    except (ValueError, IndexError, ZeroDivisionError):
        return 24


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — TRVM SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_trvm(df: pd.DataFrame) -> TRVMReport:
    """
    TRVM 4-filter signal engine (AND logic — all must pass).

    Filter 1 — Trend:      EMA alignment + gap strength
    Filter 2 — Momentum:   RSI in A+ band
    Filter 3 — Volatility: ATR within range
    Filter 4 — Volume:     Above-average participation

    Plus 6 additional guards:
        1c. No-Chaser (price vs EMA20 distance)
        1d. Pump-Reversal Guard
        1e. Post-Pump Recovery Guard
        1f. Volume-Price Divergence
        ADX. Market Regime Filter
        RVE. Relative Volatility Expansion

    Returns TRVMReport with full breakdown.
    """
    if len(df) < CFG.CANDLES_NEEDED:
        return TRVMReport(reason="Insufficient data")

    last = df.iloc[-1]
    prev = df.iloc[-2]

    report = TRVMReport(
        atr=last["trvm_atr"],
        rsi_value=last["trvm_rsi"],
        ema_fast=last["trvm_ema_fast"],
        ema_slow=last["trvm_ema_slow"],
        volume_ratio=last["trvm_vol_ratio"],
    )

    # ── Market Regime: ADX ──────────────────────────────────────────────────
    if CFG.ADX_THRESHOLD > 0 and "adx" in df.columns:
        adx_val = float(last["adx"]) if not np.isnan(last["adx"]) else 0.0
        if adx_val < CFG.ADX_THRESHOLD:
            report.reason = f"RANGING MARKET: ADX={adx_val:.1f} < {CFG.ADX_THRESHOLD}"
            return report

    # ── Relative Volatility Expansion ────────────────────────────────────────
    if CFG.RVE_ENABLED and "rve_ratio" in df.columns:
        rve_val = float(last["rve_ratio"]) if not np.isnan(last["rve_ratio"]) else 1.0
        if rve_val < CFG.RVE_COMPRESSION_MAX:
            report.reason = f"COMPRESSION: RVE={rve_val:.2f} < {CFG.RVE_COMPRESSION_MAX}"
            return report
        if rve_val > CFG.RVE_EXHAUSTION_MIN:
            report.reason = f"EXHAUSTION: RVE={rve_val:.2f} > {CFG.RVE_EXHAUSTION_MIN}"
            return report

    # ── 24h-High Proximity Filter ────────────────────────────────────────────
    if CFG.HIGH_24H_FILTER_ENABLED:
        lookback = min(len(df), _candles_per_24h(CFG.INTERVAL))
        high_24h = float(df["high"].iloc[-lookback:].max())
        last_close = float(last["close"])
        if high_24h > 0:
            dist = (high_24h - last_close) / high_24h
            if dist < CFG.MIN_24H_HIGH_DISTANCE_PCT:
                report.reason = f"NEAR_24H_PEAK: {dist:.2%} below high"
                return report

    # ── Hard RSI rejects ─────────────────────────────────────────────────────
    rsi = last["trvm_rsi"]
    if rsi > 70:
        report.reason = f"RSI overbought: {rsi:.1f} > 70"
        return report
    if rsi < 30:
        report.reason = f"RSI oversold: {rsi:.1f} < 30"
        return report

    # ── Filter 3: Volatility ─────────────────────────────────────────────────
    atr_ratio = last["trvm_atr_ratio"]
    if atr_ratio < CFG.ATR_MIN_RATIO:
        report.reason = f"ATR too low: {atr_ratio:.2%} < {CFG.ATR_MIN_RATIO}"
        return report
    if atr_ratio > CFG.ATR_MAX_RATIO:
        report.reason = f"ATR too high: {atr_ratio:.2%}"
        return report
    report.volatility_ok = True

    # ── Filter 4: Volume ─────────────────────────────────────────────────────
    vol_ratio = last["trvm_vol_ratio"]
    if vol_ratio < CFG.VOLUME_MULTIPLIER:
        report.reason = f"Volume low: {vol_ratio:.2f}x < {CFG.VOLUME_MULTIPLIER}x"
        return report
    report.volume_ok = True

    # ── Filter 1: Trend + EMA gap ────────────────────────────────────────────
    ema_f = last["trvm_ema_fast"]
    ema_s = last["trvm_ema_slow"]
    ema_gap_pct = abs(ema_f - ema_s) / ema_s if ema_s > 0 else 0.0
    ema_bullish = ema_f > ema_s
    ema_bearish = ema_f < ema_s
    price_above = last["close"] > ema_f
    price_below = last["close"] < ema_f
    trend_long = ema_bullish and price_above
    trend_short = ema_bearish and price_below

    if ema_gap_pct < CFG.MIN_EMA_GAP_PCT:
        report.reason = f"EMA gap too small: {ema_gap_pct:.3%}"
        return report
    if ema_gap_pct > CFG.MAX_EMA_GAP_PCT:
        report.reason = f"EMA gap too large: {ema_gap_pct:.3%}"
        return report
    report.trend_ok = True

    # ── Filter 1b: MACD alignment ────────────────────────────────────────────
    macd_bull = last["sc_macd"] > last["sc_macd_sig"]
    macd_bear = last["sc_macd"] < last["sc_macd_sig"]

    # ── Filter 1c: No-Chaser ─────────────────────────────────────────────────
    if trend_long:
        price_ema_dist = (last["close"] - ema_f) / ema_f
        if price_ema_dist > CFG.MAX_PRICE_EMA_DISTANCE:
            report.reason = f"CHASING: {price_ema_dist:.2%} above EMA20"
            return report
    elif trend_short:
        price_ema_dist = (ema_f - last["close"]) / ema_f
        if price_ema_dist > CFG.MAX_PRICE_EMA_DISTANCE:
            report.reason = f"CHASING: {price_ema_dist:.2%} below EMA20"
            return report

    # ── Filter 1d: Pump-Reversal Guard ───────────────────────────────────────
    if trend_long:
        candle_high_dist = (last["high"] - ema_f) / ema_f
        if (candle_high_dist > CFG.PUMP_REVERSAL_THRESHOLD
                and last["close"] < last["open"]):
            report.reason = f"PUMP_REVERSAL: high {candle_high_dist:.2%} above EMA, red candle"
            return report
    elif trend_short:
        candle_low_dist = (ema_f - last["low"]) / ema_f
        if (candle_low_dist > CFG.MAX_PRICE_EMA_DISTANCE
                and last["close"] > last["open"]):
            report.reason = f"PUMP_REVERSAL: low {candle_low_dist:.2%} below EMA, green candle"
            return report

    # ── Filter 1e: Post-Pump Recovery Guard ──────────────────────────────────
    if CFG.POST_PUMP_LOOKBACK > 0 and len(df) >= CFG.POST_PUMP_LOOKBACK + 2:
        lookback = min(CFG.POST_PUMP_LOOKBACK, len(df) - 2)
        if trend_long:
            for i in range(1, lookback + 1):
                prev_c = df.iloc[-(i + 1)]
                prev_high_dist = (prev_c["high"] - ema_f) / ema_f
                if (prev_high_dist > CFG.POST_PUMP_SPIKE_PCT
                        and prev_c["close"] < prev_c["open"]):
                    report.reason = f"POST_PUMP: candle[-{i}] spike + red"
                    return report
        elif trend_short:
            for i in range(1, lookback + 1):
                prev_c = df.iloc[-(i + 1)]
                prev_low_dist = (ema_f - prev_c["low"]) / ema_f
                if (prev_low_dist > CFG.POST_PUMP_SPIKE_PCT
                        and prev_c["close"] > prev_c["open"]):
                    report.reason = f"POST_DUMP: candle[-{i}] drop + green"
                    return report

    # ── Filter 1f: Volume-Price Divergence ───────────────────────────────────
    if trend_long:
        if last["high"] > prev["high"] and last["volume"] < prev["volume"] * 0.8:
            report.reason = "BEARISH DIVERGENCE: higher high on declining vol"
            return report
    elif trend_short:
        if last["low"] < prev["low"] and last["volume"] < prev["volume"] * 0.8:
            report.reason = "BULLISH DIVERGENCE: lower low on declining vol"
            return report

    # ── Filter 2: RSI ────────────────────────────────────────────────────────
    rsi_long = CFG.RSI_LONG_MIN <= rsi <= CFG.RSI_LONG_MAX
    rsi_short = CFG.RSI_SHORT_MIN <= rsi <= CFG.RSI_SHORT_MAX

    # AND gate: trend + RSI + MACD must all align
    if trend_long and rsi_long and macd_bull:
        report.rsi_ok = True
        report.signal = Direction.LONG
        report.reason = (
            f"TRVM LONG: EMA{CFG.EMA_FAST}>{CFG.EMA_SLOW} "
            f"gap={ema_gap_pct:.2%}, RSI={rsi:.1f}, vol={vol_ratio:.2f}x"
        )
    elif trend_short and rsi_short and macd_bear:
        report.rsi_ok = True
        report.signal = Direction.SHORT
        report.reason = (
            f"TRVM SHORT: EMA{CFG.EMA_FAST}<{CFG.EMA_SLOW} "
            f"gap={ema_gap_pct:.2%}, RSI={rsi:.1f}, vol={vol_ratio:.2f}x"
        )
    else:
        report.rsi_ok = rsi_long or rsi_short
        trend_label = "bullish" if ema_bullish else ("bearish" if ema_bearish else "flat")
        macd_label = "bullish" if macd_bull else ("bearish" if macd_bear else "neutral")
        rsi_label = f"{rsi:.1f}" + (" (ok)" if (rsi_long or rsi_short) else " (out of band)")
        if trend_long and not macd_bull:
            report.reason = f"bull↓ divergence: trend={trend_label}, RSI={rsi_label}"
        elif trend_short and not macd_bear:
            report.reason = f"bear↑ divergence: trend={trend_label}, RSI={rsi_label}"
        else:
            report.reason = f"No alignment: trend={trend_label}, RSI={rsi_label}, MACD={macd_label}"

    return report


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — STRICT SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _trend_score(last: pd.Series) -> float:
    """Trend Score (0-40 points)."""
    score = 0.0
    if last["sc_ema_fast"] > last["sc_ema_slow"]:
        score += 20
    if last["sc_ema_slow"] > last["sc_ema_trend"]:
        score += 10
    if last["close"] > last["sc_ema_fast"]:
        score += 10
    return score


def _momentum_score(last: pd.Series, prev: pd.Series) -> float:
    """Momentum Score (0-30 points)."""
    score = 0.0
    rsi = last.get("trvm_rsi", last.get("sc_rsi", 50))
    if 40 <= rsi <= 65:
        score += 20
    elif 30 <= rsi < 40 or 65 < rsi <= 70:
        score += 10
    if last["sc_macd"] > last["sc_macd_sig"]:
        score += 10
    elif last["sc_macd_hist"] > prev["sc_macd_hist"]:
        score += 5
    return score


def _volatility_score(last: pd.Series) -> float:
    """Volatility Score (0-20 points)."""
    score = 0.0
    atr_pct = last["sc_atr_pct"]
    bb_range = last["sc_bb_upper"] - last["sc_bb_lower"]
    bb_pos = ((last["close"] - last["sc_bb_lower"]) / bb_range
              if bb_range > 0 else 0.5)
    if atr_pct <= CFG.MAX_ATR_PERCENT:
        score += 10
    if 0.3 <= bb_pos <= 0.7:
        score += 10
    elif 0.2 <= bb_pos < 0.3 or 0.7 < bb_pos <= 0.8:
        score += 5
    return score


def _volume_score(last: pd.Series) -> float:
    """Volume Score (0-10 points)."""
    ratio = last["sc_vol_ratio"]
    if ratio >= CFG.MIN_VOLUME_RATIO:
        return 10.0
    elif ratio >= 1.0:
        return 5.0
    return 0.0


def calculate_score(
    df: pd.DataFrame,
    trvm_signal: Direction = Direction.NONE,
) -> ScoreReport:
    """
    Multi-factor scoring engine (0-100).

    Weights: Trend=40, Momentum=30, Volatility=20, Volume=10.

    Direction assignment uses structural bias (EMA stack + RSI zone)
as the primary gate, with TRVM signal as fallback for high scores.
    """
    if len(df) < 2 or pd.isna(df.iloc[-1].get("sc_ema_trend", np.nan)):
        return ScoreReport()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    trend_s = _trend_score(last)
    momentum_s = _momentum_score(last, prev)
    volat_s = _volatility_score(last)
    volume_s = _volume_score(last)
    total = min(100, max(0, trend_s + momentum_s + volat_s + volume_s))

    # Structural direction
    ema_fast_gt_slow = last["sc_ema_fast"] > last["sc_ema_slow"]
    ema_slow_gt_tnd = last["sc_ema_slow"] > last["sc_ema_trend"]
    ema_fast_lt_slow = last["sc_ema_fast"] < last["sc_ema_slow"]
    ema_slow_lt_tnd = last["sc_ema_slow"] < last["sc_ema_trend"]

    structure_long = ema_fast_gt_slow and (last["sc_ema_fast"] > last["sc_ema_trend"]
                      or ema_slow_gt_tnd)
    structure_short = ema_fast_lt_slow and (last["sc_ema_fast"] < last["sc_ema_trend"]
                       or ema_slow_lt_tnd)

    rsi_long_ok = CFG.RSI_LONG_MIN <= last["sc_rsi"] <= CFG.RSI_LONG_MAX
    rsi_short_ok = CFG.RSI_SHORT_MIN <= last["sc_rsi"] <= CFG.RSI_SHORT_MAX

    # Trigger patterns (for labelling only)
    ema_cross_long = (prev["sc_ema_fast"] <= prev["sc_ema_slow"]) and ema_fast_gt_slow
    ema_cross_short = (prev["sc_ema_fast"] >= prev["sc_ema_slow"]) and ema_fast_lt_slow
    price_above_emas = last["close"] > last["sc_ema_fast"] > last["sc_ema_slow"]
    price_below_emas = last["close"] < last["sc_ema_fast"] < last["sc_ema_slow"]
    macd_bull = (last["sc_macd"] > last["sc_macd_sig"]
                 and last["sc_macd_hist"] > prev["sc_macd_hist"])
    macd_bear = (last["sc_macd"] < last["sc_macd_sig"]
                 and last["sc_macd_hist"] < prev["sc_macd_hist"])

    # Assign direction
    direction = Direction.NONE
    trigger = ""
    confidence = round(total, 2)

    if total >= CFG.MIN_SCORE_TO_TRADE and structure_long and rsi_long_ok:
        direction = Direction.LONG
        if ema_cross_long:
            trigger = "EMA_CROSS"
            confidence = min(100.0, total + 15)
        elif price_above_emas and macd_bull:
            trigger = "TREND_CONT"
        else:
            trigger = "TREND_ALIGN"

    elif total >= CFG.MIN_SCORE_TO_TRADE and structure_short and rsi_short_ok:
        direction = Direction.SHORT
        if ema_cross_short:
            trigger = "EMA_CROSS"
            confidence = min(100.0, (100 - total) + 15)
        elif price_below_emas and macd_bear:
            trigger = "TREND_CONT"
        else:
            trigger = "TREND_ALIGN"

    # Fallback: align with TRVM signal for high scores
    if direction == Direction.NONE and total >= CFG.MIN_SCORE_TO_TRADE:
        if trvm_signal == Direction.LONG:
            direction = Direction.LONG
            trigger = "TRVM_ALIGN"
        elif trvm_signal == Direction.SHORT:
            direction = Direction.SHORT
            trigger = "TRVM_ALIGN"

    return ScoreReport(
        total_score=round(total, 1),
        trend_score=trend_s,
        momentum_score=momentum_s,
        volatility_score=volat_s,
        volume_score=volume_s,
        direction=direction,
        confidence=round(confidence, 1),
        entry_trigger=trigger,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — DUAL-CONFIRMATION GATE
# ═══════════════════════════════════════════════════════════════════════════════

def dual_confirm_gate(
    trvm: TRVMReport,
    score: ScoreReport,
) -> Tuple[bool, Direction, str]:
    """
    A+ Dual-Confirmation Gate — both engines must agree.

    EXECUTE conditions (ALL must be true):
        1. TRVM signal == LONG/SHORT
        2. Score direction matches TRVM
        3. Score >= MIN_SCORE_TO_TRADE
        4. TRVM passed its own filters

    Returns (execute, direction, reason).
    """
    t = trvm.signal
    s = score.direction
    total = score.total_score

    if t == Direction.LONG and s == Direction.LONG and total >= CFG.MIN_SCORE_TO_TRADE:
        reason = (
            f"[A+ LONG] Score={total:.1f}/{CFG.MIN_SCORE_TO_TRADE:.0f} "
            f"Conf={score.confidence:.0f}% Trigger={score.entry_trigger}"
        )
        log.info("A+ LONG | Score=%.1f | Conf=%.0f%% | %s",
                 total, score.confidence, score.entry_trigger)
        return True, Direction.LONG, reason

    if t == Direction.SHORT and s == Direction.SHORT and total >= CFG.MIN_SCORE_TO_TRADE:
        reason = (
            f"[A+ SHORT] Score={total:.1f}/{CFG.MIN_SCORE_TO_TRADE:.0f} "
            f"Conf={score.confidence:.0f}% Trigger={score.entry_trigger}"
        )
        log.info("A+ SHORT | Score=%.1f | Conf=%.0f%% | %s",
                 total, score.confidence, score.entry_trigger)
        return True, Direction.SHORT, reason

    # Determine skip reason
    if t == Direction.NONE:
        skip_reason = trvm.reason
    elif total < CFG.MIN_SCORE_TO_TRADE:
        skip_reason = f"score {total:.1f} < {CFG.MIN_SCORE_TO_TRADE:.0f}"
    elif t != s:
        skip_reason = f"direction conflict: TRVM={t.value} vs Score={s.value}"
    else:
        skip_reason = f"Score direction=NONE despite Score={total:.1f}"

    if t != Direction.NONE:
        log.info("SKIPPED | %s", skip_reason)

    return False, Direction.NONE, f"SKIPPED: {skip_reason}"
