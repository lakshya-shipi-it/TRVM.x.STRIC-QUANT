"""
================================================================================
MONITOR — Position Monitoring, Execution, and Bot Orchestration
================================================================================

The main trading engine: monitors open positions with 7 exit conditions,
executes trades, manages position upgrades, and orchestrates the full
auto-scan-trade-rescan loop.

Refactor: Originally Section 10 of system2.py (~1500 lines). The monolithic
HybridBot class handled everything from position tracking to the main loop.
Now organized into focused methods with clear responsibilities:
    - execute_trade: Entry logic
    - monitor_position: 7-exit monitoring
    - close_position: Exit logic with P&L calculation
    - run_auto: Main orchestration loop

Key design decisions preserved:
    1. R-based trailing stop (not ATR-based) — captures profit proportionally
    2. Breakeven protection at 0.3R — guarantees no loss once active
    3. 7 exit conditions checked in priority order every scan cycle
    4. Exchange-side SL/TP orders for protection during bot downtime
================================================================================
"""

from __future__ import annotations

import csv
import datetime
import math
import os
import time
from typing import Dict, List, Optional

import pandas as pd

from config import CFG, TRADE_LOG_FILE, TRADE_LOG_FIELDS, log
from exchange import BinanceClient
from indicators import IndicatorEngine
from models import ActivePosition, Direction, ScanResult
from risk_manager import (
    calculate_position,
    check_circuit_breakers,
    trailing_stop_r,
    trailing_stop_atr,
)
from scanner import scan_market
from strategy import calculate_score, calculate_trvm, dual_confirm_gate


# ─── Trade Logging ──────────────────────────────────────────────────────────

def _ensure_log_file() -> None:
    """Create trade_log.csv with header if it doesn't exist."""
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()


def log_trade(
    symbol: str, side: str, entry_price: float,
    stop_loss: float, take_profit: float, score: float,
    trvm_signal: str, confidence: float,
    pnl: float = 0.0, exit_reason: str = "",
) -> None:
    """Append one row to trade_log.csv."""
    _ensure_log_file()
    row = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "symbol": symbol, "side": side,
        "entry_price": entry_price, "stop_loss": stop_loss,
        "take_profit": take_profit, "pnl": round(pnl, 4),
        "score": score, "trvm_signal": trvm_signal,
        "exit_reason": exit_reason, "confidence": confidence,
    }
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writerow(row)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class HybridBot:
    """
    Main orchestrator for the TRVM x Strict Scoring hybrid system.

    Public API:
        bot = HybridBot()
        bot.run_auto()   # Fully automatic: scan -> trade -> rescan
        bot.run_once()   # Single scan (for dashboard integration)
    """

    def __init__(self):
        self.client = BinanceClient(CFG.API_KEY, CFG.API_SECRET)
        self.capital = CFG.STARTING_CAPITAL
        self.positions: Dict[str, ActivePosition] = {}
        self.daily_pnl = 0.0
        self._symbol_daily_pnl: Dict[str, float] = {}
        self.last_loss_time: Optional[datetime.datetime] = None
        self.scan_count = 0
        self.last_scan: Optional[datetime.datetime] = None
        self._last_reset_day: Optional[datetime.date] = None
        self._cycle_count = 0
        self.early_loss_cooldowns: Dict[str, datetime.datetime] = {}
        self.stagnancy_cooldowns: Dict[str, datetime.datetime] = {}
        self._symbol_daily_entries: Dict[str, int] = {}
        _ensure_log_file()
        log.info("HybridBot initialized | Mode: %s | Capital: %.2f USDT",
                 "PAPER" if CFG.PAPER_MODE else "LIVE", self.capital)

    # ─── Cooldown Helpers ────────────────────────────────────────────────────

    def _is_cooldown(self, symbol: str, cooldowns: Dict, label: str) -> bool:
        """Check if a symbol is within a cooldown window."""
        until = cooldowns.get(symbol)
        if until is None:
            return False
        if datetime.datetime.utcnow() < until:
            remaining = (until - datetime.datetime.utcnow()).total_seconds() / 60
            log.info("%s: %s cooldown (%.0f min remaining)", symbol, label, remaining)
            return True
        del cooldowns[symbol]
        return False

    def _register_cooldown(self, symbol: str, minutes: int, cooldowns: Dict) -> None:
        """Register a cooldown window for a symbol."""
        cooldowns[symbol] = (datetime.datetime.utcnow()
                             + datetime.timedelta(minutes=minutes))

    def _is_early_loss_cooldown(self, symbol: str) -> bool:
        return self._is_cooldown(symbol, self.early_loss_cooldowns, "EARLY_LOSS")

    def _register_early_loss_cooldown(self, symbol: str) -> None:
        self._register_cooldown(symbol, CFG.EARLY_LOSS_COOLDOWN_MINUTES,
                                self.early_loss_cooldowns)

    def _is_stagnancy_cooldown(self, symbol: str) -> bool:
        return self._is_cooldown(symbol, self.stagnancy_cooldowns, "STAGNANCY")

    def _register_stagnancy_cooldown(self, symbol: str) -> None:
        self._register_cooldown(symbol, CFG.STAGNANCY_COOLDOWN_MINUTES,
                                self.stagnancy_cooldowns)

    # ─── Auto-Scanner ────────────────────────────────────────────────────────

    def get_top_symbols(self, scan_results: List[ScanResult],
                        n: Optional[int] = None) -> List[str]:
        """Select top N READY symbols from scan results."""
        n = n or CFG.TOP_N_SYMBOLS
        ready = [r for r in scan_results
                 if r.verdict == "READY" and r.symbol not in self.positions]
        if not ready:
            return []
        return [r.symbol for r in ready[:n]]

    # ─── Per-Symbol Pipeline ─────────────────────────────────────────────────

    def _process_symbol(self, symbol: str) -> dict:
        """
        Full pipeline for one symbol:
            1. Fetch data
            2. Compute indicators
            3. Monitor existing position
            4. Circuit breakers
            5. TRVM signal
            6. Strict score
            7. Dual-confirm gate
            8. Execute or skip
        """
        result = {
            "symbol": symbol, "trvm_signal": "—",
            "score": 0.0, "decision": "—", "action": "SKIP",
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }

        raw_df = self.client.get_data(symbol, CFG.INTERVAL, limit=200)
        if raw_df.empty or len(raw_df) < CFG.CANDLES_NEEDED:
            result["decision"] = "Insufficient data"
            return result

        df = IndicatorEngine.compute_all(raw_df)
        if df.empty:
            result["decision"] = "Indicator computation failed"
            return result

        # Monitor existing position
        if symbol in self.positions:
            self._monitor_position(symbol, df)
            pos = self.positions.get(symbol)
            if pos:
                result["action"] = "MONITORING"
                result["decision"] = (
                    f"Open @ {pos.entry_price:.6f} | "
                    f"Now={pos.current_price:.6f} | "
                    f"Unreal={pos.unrealized_pnl():+.4f}"
                )
            else:
                result["action"] = "CLOSED"
                result["decision"] = "Position closed this scan"
            return result

        # Circuit breakers
        should_stop, reason = check_circuit_breakers(
            self.daily_pnl, self.last_loss_time,
            len(self.positions), self.capital,
        )
        if should_stop:
            result["decision"] = reason
            result["action"] = "BLOCKED"
            return result

        # Per-symbol daily loss limit
        sym_pnl = self._symbol_daily_pnl.get(symbol, 0.0)
        per_sym_limit = self.capital * CFG.DAILY_LOSS_LIMIT_PCT
        if sym_pnl <= -per_sym_limit:
            result["decision"] = f"Per-symbol limit ({sym_pnl:.2f})"
            result["action"] = "BLOCKED"
            return result

        if len(self.positions) >= CFG.MAX_OPEN_POSITIONS:
            result["decision"] = f"Max positions ({CFG.MAX_OPEN_POSITIONS})"
            result["action"] = "BLOCKED"
            return result

        if self._is_early_loss_cooldown(symbol):
            result["decision"] = "EARLY_LOSS cooldown active"
            result["action"] = "BLOCKED"
            return result

        if self._is_stagnancy_cooldown(symbol):
            result["decision"] = "STAGNANCY cooldown active"
            result["action"] = "BLOCKED"
            return result

        # Daily entry cap
        entries_today = self._symbol_daily_entries.get(symbol, 0)
        if entries_today >= CFG.MAX_DAILY_ENTRIES_PER_SYMBOL:
            result["decision"] = f"Daily entry cap ({entries_today}/{CFG.MAX_DAILY_ENTRIES_PER_SYMBOL})"
            result["action"] = "BLOCKED"
            return result

        # Direction counts
        long_count = sum(1 for p in self.positions.values() if p.direction == Direction.LONG)
        short_count = sum(1 for p in self.positions.values() if p.direction == Direction.SHORT)

        # TRVM signal
        trvm = calculate_trvm(df)
        result["trvm_signal"] = trvm.signal.value

        # Strict score
        score = calculate_score(df, trvm.signal)
        result["score"] = score.total_score

        # Dual-confirm gate
        execute, direction, gate_reason = dual_confirm_gate(trvm, score)
        result["decision"] = gate_reason

        if not execute:
            result["action"] = "NO_TRADE"
            return result

        # Spot mode: block shorts
        if not CFG.USE_FUTURES and direction == Direction.SHORT:
            result["decision"] = "Spot mode — SHORT not available"
            result["action"] = "BLOCKED"
            return result

        # Direction cap
        if direction == Direction.LONG and long_count >= CFG.MAX_SAME_DIR_POSITIONS:
            result["decision"] = f"Max {CFG.MAX_SAME_DIR_POSITIONS} LONG positions"
            result["action"] = "BLOCKED"
            return result
        if direction == Direction.SHORT and short_count >= CFG.MAX_SAME_DIR_POSITIONS:
            result["decision"] = f"Max {CFG.MAX_SAME_DIR_POSITIONS} SHORT positions"
            result["action"] = "BLOCKED"
            return result

        # Calculate position
        atr = df.iloc[-1]["sc_atr"]
        entry_price = df.iloc[-1]["close"]
        params = calculate_position(direction, entry_price, atr, self.capital)

        if params is None:
            result["decision"] = "Risk filter — skipped"
            result["action"] = "RISK_FILTER"
            return result

        if params["risk_reward"] < CFG.MIN_RISK_REWARD:
            result["decision"] = f"RR {params['risk_reward']:.2f} < {CFG.MIN_RISK_REWARD}"
            result["action"] = "RR_FILTER"
            return result

        self._execute_trade(symbol, direction, params, score, trvm)
        result["action"] = direction.value
        return result

    # ─── Trade Execution ─────────────────────────────────────────────────────

    def _execute_trade(
        self, symbol: str, direction: Direction,
        params: dict, score, trvm,
    ) -> None:
        """Place order and register position."""
        if CFG.USE_FUTURES:
            self.client.set_leverage(symbol, CFG.LEVERAGE)

        side = "BUY" if direction == Direction.LONG else "SELL"
        order = self.client.place_order(symbol, side, params["position_size"])

        valid_statuses = ("FILLED", "NEW", "PARTIALLY_FILLED", "PAPER")
        if order.get("status") not in valid_statuses and "orderId" not in order:
            log.error("%s: order failed — %s", symbol, order)
            return

        if order.get("avgPrice") and float(order["avgPrice"]) > 0:
            params["entry_price"] = float(order["avgPrice"])

        actual_qty = float(order.get("executedQty")
                           or order.get("origQty")
                           or params["position_size"])

        position = ActivePosition(
            symbol=symbol, direction=direction,
            entry_price=params["entry_price"],
            stop_loss=params["stop_loss"],
            take_profit=params["take_profit"],
            position_size=actual_qty,
            risk_amount=params["risk_amount"],
            entry_time=datetime.datetime.utcnow(),
            score=score.total_score, confidence=score.confidence,
            highest_price=params["entry_price"],
            lowest_price=params["entry_price"],
            original_sl=params["stop_loss"],
            last_monitored=datetime.datetime.utcnow(),
        )
        self.positions[symbol] = position
        self._symbol_daily_entries[symbol] = (
            self._symbol_daily_entries.get(symbol, 0) + 1
        )

        log_trade(
            symbol=symbol, side=side,
            entry_price=params["entry_price"],
            stop_loss=params["stop_loss"],
            take_profit=params["take_profit"],
            score=score.total_score, trvm_signal=trvm.signal.value,
            confidence=score.confidence,
        )
        log.info(
            "ENTRY | %s %s @ %.6f SL=%.6f TP=%.6f | Size=%.6f | RR=%.2f | Score=%.1f",
            direction.value, symbol, params["entry_price"],
            params["stop_loss"], params["take_profit"],
            actual_qty, params["risk_reward"], score.total_score,
        )

        # Exchange-side protective orders (live futures only)
        if not CFG.PAPER_MODE and CFG.USE_FUTURES:
            try:
                filters = self.client.get_symbol_filters(symbol)
                step = filters["stepSize"]
                raw_qty = float(order.get("executedQty")
                                or order.get("origQty")
                                or params["position_size"])
                qty = math.floor(raw_qty / step) * step
                qty_str = self.client._format_qty(qty, step)
                self.client.place_protective_orders(
                    symbol, side, qty_str,
                    params["stop_loss"], params["take_profit"],
                )
            except Exception as exc:
                log.error("Protective order failed for %s: %s", symbol, exc)

    # ─── Position Monitoring ─────────────────────────────────────────────────

    def _monitor_position(self, symbol: str, df: pd.DataFrame) -> None:
        """
        Monitor open position with 7 exit conditions.

        Exit hierarchy:
            1. Quick Profit (r_profit >= SCALPER_QUICK_PROFIT_R)
            2. Max Loss R (r_profit <= MAX_LOSS_R)
            3. SL / TP / Trailing (integrated into stop_loss)
            4. Early-Loss R (fast bleed)
            5. Early-Loss % (time-based bleed)
            6. Stagnancy (dead trade)
            7. Time exit (max hold exceeded)
        """
        pos = self.positions[symbol]
        last = df.iloc[-1]
        high = last["high"]
        low = last["low"]
        close = last["close"]
        atr = last["sc_atr"]

        # Live price
        live_price = 0.0
        for _ in range(3):
            live_price = self.client.get_price(symbol)
            if live_price > 0:
                break
            time.sleep(0.5)
        if live_price <= 0:
            live_price = close

        # Update extremes
        pos.current_price = live_price
        pos.highest_price = max(pos.highest_price, high, live_price)
        pos.lowest_price = min(pos.lowest_price, low, live_price)

        # Calculate R profit
        orig_sl_dist = abs(pos.entry_price - pos.original_sl) if pos.original_sl > 0 else abs(pos.entry_price - pos.stop_loss)
        if orig_sl_dist == 0:
            orig_sl_dist = abs(pos.entry_price - pos.stop_loss)

        if pos.direction == Direction.LONG:
            r_profit = ((live_price - pos.entry_price) / orig_sl_dist
                        if orig_sl_dist > 0 else 0.0)
            r_extreme = ((pos.highest_price - pos.entry_price) / orig_sl_dist
                         if orig_sl_dist > 0 else 0.0)
        else:
            r_profit = ((pos.entry_price - live_price) / orig_sl_dist
                        if orig_sl_dist > 0 else 0.0)
            r_extreme = ((pos.entry_price - pos.lowest_price) / orig_sl_dist
                         if orig_sl_dist > 0 else 0.0)

        pos.peak_r = max(pos.peak_r, r_profit, r_extreme)
        pos.last_monitored = datetime.datetime.utcnow()

        # Breakeven stop
        if not pos.breakeven_moved and pos.peak_r >= CFG.BREAKEVEN_R:
            if pos.direction == Direction.LONG:
                pos.stop_loss = pos.entry_price + (CFG.BREAKEVEN_R * orig_sl_dist)
            else:
                pos.stop_loss = pos.entry_price - (CFG.BREAKEVEN_R * orig_sl_dist)
            pos.breakeven_moved = True
            log.info("BREAKEVEN | %s | SL moved to %.6f", symbol, pos.stop_loss)

        # R-based trailing stop
        trail_r_level = None
        trail_atr_level = None
        if r_profit >= CFG.TRAILING_ACTIVATION_R:
            pos.trail_active = True
            trail_r_level = trailing_stop_r(pos.direction, pos.peak_r,
                                             pos.entry_price, orig_sl_dist)
            trail_atr_level = trailing_stop_atr(
                pos.direction, pos.highest_price, pos.lowest_price,
                atr, entry_price=pos.entry_price,
            )
            if pos.direction == Direction.LONG:
                effective_trail = max(trail_r_level, trail_atr_level)
                if effective_trail > pos.stop_loss:
                    pos.stop_loss = effective_trail
            else:
                effective_trail = min(trail_r_level, trail_atr_level)
                if effective_trail < pos.stop_loss:
                    pos.stop_loss = effective_trail

        # ─── Exit Logic ────────────────────────────────────────────────────────
        should_exit = False
        exit_price = 0.0
        exit_reason = ""
        hold_minutes = (datetime.datetime.utcnow() - pos.entry_time).total_seconds() / 60

        # CHECK 1: Quick Profit
        if not should_exit and r_profit >= CFG.SCALPER_QUICK_PROFIT_R:
            should_exit, exit_price, exit_reason = True, live_price, f"QUICK_PROFIT ({r_profit:.2f}R)"

        # CHECK 2: Max Loss R
        if not should_exit and r_profit <= CFG.MAX_LOSS_R:
            should_exit, exit_price, exit_reason = True, live_price, f"MAX_LOSS_R ({r_profit:.2f}R)"

        # CHECK 3: SL / TP (ALWAYS use live price — candle low/high can ghost-trigger)
        if not should_exit:
            if pos.direction == Direction.LONG:
                if live_price <= pos.stop_loss:
                    should_exit, exit_price = True, pos.stop_loss
                    if pos.trail_active and pos.stop_loss > pos.entry_price * 1.002:
                        exit_reason = "TRAILING_STOP"
                    elif pos.breakeven_moved:
                        exit_reason = "BREAKEVEN_STOP"
                    else:
                        exit_reason = "STOP_LOSS"
                elif live_price >= pos.take_profit:
                    should_exit, exit_price, exit_reason = True, pos.take_profit, "TAKE_PROFIT"
            else:
                if live_price >= pos.stop_loss:
                    should_exit, exit_price = True, pos.stop_loss
                    if pos.trail_active and pos.stop_loss < pos.entry_price * 0.998:
                        exit_reason = "TRAILING_STOP"
                    elif pos.breakeven_moved:
                        exit_reason = "BREAKEVEN_STOP"
                    else:
                        exit_reason = "STOP_LOSS"
                elif live_price <= pos.take_profit:
                    should_exit, exit_price, exit_reason = True, pos.take_profit, "TAKE_PROFIT"

        # CHECK 4a: Early-Loss R
        if not should_exit and hold_minutes <= CFG.EARLY_LOSS_R_MINUTES:
            if r_profit <= CFG.EARLY_LOSS_R:
                should_exit, exit_price, exit_reason = (True, live_price,
                    f"EARLY_LOSS_R ({r_profit:.2f}R in {hold_minutes:.0f}m)")

        # CHECK 4: Early-Loss %
        if not should_exit and hold_minutes >= CFG.EARLY_LOSS_MINUTES:
            if pos.unrealized_pnl_pct() <= CFG.EARLY_LOSS_PCT:
                should_exit, exit_price, exit_reason = (True, live_price,
                    f"EARLY_LOSS ({pos.unrealized_pnl_pct():.2f}% in {hold_minutes:.0f}m)")

        # CHECK 5: Stagnancy
        if not should_exit and hold_minutes >= CFG.STAGNANCY_MINUTES:
            if r_profit < CFG.STAGNANCY_R_THRESHOLD and r_profit < 0.05:
                should_exit, exit_price, exit_reason = (True, live_price,
                    f"STAGNANCY ({hold_minutes:.0f}m, R={r_profit:.2f})")

        # CHECK 6: Time exit
        if not should_exit and hold_minutes >= CFG.SCALPER_MAX_HOLD_MINUTES:
            should_exit, exit_price, exit_reason = (True, live_price,
                f"TIME_EXIT ({hold_minutes:.0f}m)")

        if should_exit:
            self._close_position(symbol, exit_price, exit_reason)

    # ─── Position Closing ────────────────────────────────────────────────────

    def _close_position(self, symbol: str, exit_price: float, reason: str) -> None:
        """Close position, update capital, log result."""
        if symbol not in self.positions:
            return

        self.client.cancel_protective_orders(symbol)
        pos = self.positions[symbol]
        side = "SELL" if pos.direction == Direction.LONG else "BUY"

        close_result = self.client.place_order(symbol, side, pos.position_size,
                                                reduce_only=True)
        if "error" in close_result:
            log.error("Close order failed for %s: %s", symbol, close_result["error"])
            return

        # Calculate P&L
        if pos.direction == Direction.LONG:
            pnl = (exit_price - pos.entry_price) * pos.position_size
        else:
            pnl = (pos.entry_price - exit_price) * pos.position_size

        # Deduct round-trip taker fees
        notional = pos.entry_price * pos.position_size
        fee = notional * CFG.TAKER_FEE_PCT * 2
        pnl -= fee

        self.capital += pnl
        self.daily_pnl += pnl
        self._symbol_daily_pnl[symbol] = self._symbol_daily_pnl.get(symbol, 0.0) + pnl

        # Cooldown management
        if "EARLY_LOSS" in reason or "MAX_LOSS_R" in reason:
            self._register_early_loss_cooldown(symbol)
        elif "STAGNANCY" in reason:
            self._register_stagnancy_cooldown(symbol)
        elif "STOP_LOSS" in reason and "BREAKEVEN" not in reason:
            self.last_loss_time = datetime.datetime.utcnow()
        elif "BREAKEVEN" in reason:
            self.last_loss_time = (datetime.datetime.utcnow()
                                   - datetime.timedelta(hours=CFG.COOLDOWN_HOURS / 2))

        log_trade(
            symbol=symbol, side=side, entry_price=pos.entry_price,
            stop_loss=pos.stop_loss, take_profit=pos.take_profit,
            score=pos.score, trvm_signal=pos.direction.value,
            confidence=pos.confidence, pnl=pnl, exit_reason=reason,
        )
        log.info("EXIT | %s %s | Entry=%.6f Exit=%.6f | PnL=%.4f | %s",
                 pos.direction.value, symbol, pos.entry_price, exit_price, pnl, reason)

        del self.positions[symbol]

    def _close_all_positions(self, reason: str = "MANUAL_CLOSE") -> None:
        """Close ALL open positions. Called on shutdown."""
        for sym in list(self.positions.keys()):
            pos = self.positions.get(sym)
            if pos is None:
                continue
            exit_price = (pos.current_price if pos.current_price > 0
                          else self.client.get_price(sym))
            if exit_price <= 0:
                exit_price = pos.entry_price
            self._close_position(sym, exit_price, reason)

    # ─── Main Loop ───────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        """Single scan across all configured symbols."""
        self._reset_daily_pnl_if_needed()
        results = {}
        for symbol in CFG.SYMBOLS:
            results[symbol] = self._process_symbol(symbol)
        self.scan_count += 1
        self.last_scan = datetime.datetime.utcnow()
        return results

    def run_auto(self) -> None:
        """
        Fully automatic loop:
            [1] scan_market()       — Score all symbols
            [2] get_top_symbols()   — Pick top N
            [3] process each        — Trade until closed
            [4] RESCAN_DELAY_SEC    — Pause
            [5] Repeat
        """
        log.info("Auto-loop started | top_n=%d | delay=%ds",
                 CFG.TOP_N_SYMBOLS, CFG.RESCAN_DELAY_SEC)

        while True:
            try:
                self._cycle_count += 1
                self._reset_daily_pnl_if_needed()
                log.info("=== AUTO CYCLE #%d ===", self._cycle_count)

                # Scan
                scan_results = scan_market(vars(CFG))
                if not scan_results:
                    wait = CFG.RESCAN_DELAY_SEC * 4
                    log.warning("No symbols found. Waiting %ds...", wait)
                    time.sleep(wait)
                    continue

                # Select top N
                symbols = self.get_top_symbols(scan_results, CFG.TOP_N_SYMBOLS)
                if not symbols:
                    time.sleep(CFG.RESCAN_DELAY_SEC)
                    continue

                # Trade each symbol
                for sym in symbols:
                    if sym not in self.positions:
                        self._process_symbol(sym)

                # Monitor existing positions
                for sym in list(self.positions.keys()):
                    self._process_symbol(sym)

                time.sleep(CFG.RESCAN_DELAY_SEC)

            except KeyboardInterrupt:
                log.info("Shutdown requested. Closing positions...")
                self._close_all_positions("MANUAL_CLOSE")
                break
            except Exception as exc:
                log.error("Cycle #%d error: %s", self._cycle_count, exc, exc_info=True)
                time.sleep(30)

    # ─── State Helpers ───────────────────────────────────────────────────────

    def _reset_daily_pnl_if_needed(self) -> None:
        today = datetime.datetime.utcnow().date()
        if self._last_reset_day != today:
            self.daily_pnl = 0.0
            self._symbol_daily_pnl.clear()
            self._symbol_daily_entries.clear()
            self._last_reset_day = today

    def get_status(self) -> dict:
        """Return current bot state for dashboard integration."""
        total_unrealized = sum(p.unrealized_pnl() for p in self.positions.values())
        return {
            "capital": round(self.capital, 2),
            "unrealized_pnl": round(total_unrealized, 4),
            "net_equity": round(self.capital + total_unrealized, 4),
            "daily_pnl": round(self.daily_pnl, 4),
            "open_positions": len(self.positions),
            "cycle_count": self._cycle_count,
            "positions": {
                sym: {
                    "direction": pos.direction.value,
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "unrealized_pnl": round(pos.unrealized_pnl(), 4),
                    "hold_duration": pos.hold_duration(),
                    "score": pos.score,
                }
                for sym, pos in self.positions.items()
            },
            "scan_count": self.scan_count,
            "mode": "PAPER" if CFG.PAPER_MODE else "LIVE",
        }
