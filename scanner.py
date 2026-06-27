"""
================================================================================
MARKET SCANNER — Symbol Discovery and Pre-Filtering
================================================================================

Scans all active Binance Futures USDT pairs, computes a lightweight
indicator set per symbol, and returns scored candidates sorted by
quality. Uses a dedicated HTTP session (no auth required for public data).

Refactor: Originally Section 0.5 of system2.py (~360 lines). The scanner
was tightly coupled to the trading bot through the global CFG reference.
Now it's a standalone module with explicit parameter injection, making it
usable independently (e.g., for a standalone scanning tool or dashboard).

Performance: ~14 req/s with 0.07s sleep between requests — well within
Binance's rate limits. A full scan of 199 symbols takes ~40 seconds.
================================================================================
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

from config import log
from indicators import IndicatorEngine
from models import ScanResult


# ─── Scanner Constants ──────────────────────────────────────────────────────

_SCANNER_BASE = "https://fapi.binance.com"
_SCANNER_SESSION = requests.Session()
_SCANNER_MIN_PRICE = 0.10         # Reject tokens under 0.10$
_SCANNER_MIN_QUOTE_VOLUME = 5_000_000  # Min 24h USDT volume
_SCANNER_INTERVAL = "1h"
_SCANNER_CANDLES = 100


# ─── Data Fetching ──────────────────────────────────────────────────────────

def _get_futures_symbols(min_quote_volume: float) -> List[dict]:
    """Fetch active USDT-margined futures symbols with volume filter."""
    try:
        info = _SCANNER_SESSION.get(
            f"{_SCANNER_BASE}/fapi/v1/exchangeInfo", timeout=10
        ).json()
        active = {
            s["symbol"]
            for s in info["symbols"]
            if s["status"] == "TRADING" and s["symbol"].endswith("USDT")
        }
        tickers = _SCANNER_SESSION.get(
            f"{_SCANNER_BASE}/fapi/v1/ticker/24hr", timeout=10
        ).json()
        result = []
        for t in tickers:
            sym = t["symbol"]
            if sym not in active:
                continue
            price = float(t["lastPrice"])
            vol24h = float(t["quoteVolume"])
            if price < _SCANNER_MIN_PRICE or vol24h < min_quote_volume:
                continue
            result.append({"symbol": sym, "price": price, "vol24h_usdt": vol24h})
        return sorted(result, key=lambda x: x["vol24h_usdt"], reverse=True)
    except Exception as exc:
        log.error("[Scanner] Failed to fetch symbols: %s", exc)
        return []


def _get_spot_symbols(min_quote_volume: float) -> List[dict]:
    """Fetch active spot symbols with volume filter."""
    spot_base = "https://api.binance.com"
    try:
        info = _SCANNER_SESSION.get(
            f"{spot_base}/api/v3/exchangeInfo", timeout=10
        ).json()
        active = {
            s["symbol"]
            for s in info["symbols"]
            if s["status"] == "TRADING" and s["symbol"].endswith("USDT")
        }
        tickers = _SCANNER_SESSION.get(
            f"{spot_base}/api/v3/ticker/24hr", timeout=10
        ).json()
        result = []
        for t in tickers:
            sym = t["symbol"]
            if sym not in active:
                continue
            price = float(t["lastPrice"])
            vol24h = float(t["quoteVolume"])
            if price < _SCANNER_MIN_PRICE or vol24h < min_quote_volume:
                continue
            result.append({"symbol": sym, "price": price, "vol24h_usdt": vol24h})
        return sorted(result, key=lambda x: x["vol24h_usdt"], reverse=True)
    except Exception as exc:
        log.error("[Scanner] Failed to fetch spot symbols: %s", exc)
        return []


def _get_klines(symbol: str, use_futures: bool = True) -> pd.DataFrame:
    """Fetch OHLCV klines for a single symbol."""
    try:
        url = (
            f"{_SCANNER_BASE}/fapi/v1/klines"
            if use_futures else
            "https://api.binance.com/api/v3/klines"
        )
        resp = _SCANNER_SESSION.get(
            url,
            params={
                "symbol": symbol,
                "interval": _SCANNER_INTERVAL,
                "limit": _SCANNER_CANDLES,
            },
            timeout=8,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            log.warning("[Scanner] Rate limited on %s — sleeping %ds",
                        symbol, retry_after)
            time.sleep(retry_after)
            return pd.DataFrame()
        if resp.status_code == 418:
            log.error("[Scanner] IP BANNED on %s — sleeping 300s", symbol)
            time.sleep(300)
            return pd.DataFrame()
        if resp.status_code != 200:
            return pd.DataFrame()

        raw = resp.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "n_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna()
    except Exception:
        return pd.DataFrame()


# ─── Per-Symbol Scoring ─────────────────────────────────────────────────────

def _format_vol(v: float) -> str:
    """Format volume for display (e.g. $9.0M)."""
    if v >= 1_000_000_000:
        return f"${v / 1e9:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1e6:.1f}M"
    return f"${v / 1e3:.0f}K"


def _scan_symbol(
    symbol: str,
    price: float,
    vol24h: float,
    params: Dict,
) -> Optional[ScanResult]:
    """
    Compute indicators and score a single symbol against filter params.

    Args:
        symbol: Trading pair (e.g. "BTCUSDT")
        price: Current price
        vol24h: 24h volume in USDT
        params: Dictionary of scanner parameters (replaces global CFG)

    Returns:
        ScanResult if symbol passes minimum criteria, None otherwise.
    """
    df = _get_klines(symbol, params.get("USE_FUTURES", True))
    if len(df) < 60:
        return None

    close = df["close"]
    ie = IndicatorEngine

    df["ema_fast"] = ie.sc_ema(close, params.get("EMA_FAST", 20))
    df["ema_slow"] = ie.sc_ema(close, params.get("EMA_SLOW", 50))
    df["atr"] = ie.sc_atr_ema(df, 14)
    df["rsi"] = ie.sc_rsi_wilder(close, 14)
    completed_vol = df["volume"].shift(1)
    df["vol_ma"] = completed_vol.rolling(20).mean()
    df["vol_ratio"] = completed_vol / df["vol_ma"]
    macd_l, macd_s = ie.sc_macd(close)
    df["macd"] = macd_l
    df["macd_sig"] = macd_s
    df = df.dropna()
    if len(df) < 5:
        return None

    last = df.iloc[-1]
    atr_ratio = last["atr"] / last["close"] if last["close"] > 0 else 999
    vol_ratio = last["vol_ratio"]
    rsi = last["rsi"]
    ema_f = last["ema_fast"]
    ema_s = last["ema_slow"]
    macd_bull = last["macd"] > last["macd_sig"]

    # Hard rejects — same gates as TRVM engine
    if rsi > 70 or rsi < 30:
        return None

    ema_gap_pct = abs(ema_f - ema_s) / ema_s if ema_s > 0 else 0.0
    if ema_gap_pct < params.get("MIN_EMA_GAP_PCT", 0.015):
        return None
    if ema_gap_pct > params.get("MAX_EMA_GAP_PCT", 0.040):
        return None

    rsi_long_ok = params.get("RSI_LONG_MIN", 45) <= rsi <= params.get("RSI_LONG_MAX", 65)
    rsi_short_ok = (params.get("RSI_SHORT_MIN", 33) <= rsi <= params.get("RSI_SHORT_MAX", 50))
    if not (rsi_long_ok or rsi_short_ok):
        return None

    # Trend determination
    if ema_f > ema_s and last["close"] > ema_f:
        trend = "bullish"
    elif ema_f < ema_s and last["close"] < ema_f:
        trend = "bearish"
    else:
        trend = "flat"

    # Apply filters
    fail_reasons = []

    pass_atr = params.get("ATR_MIN_RATIO", 0.015) <= atr_ratio <= params.get("ATR_MAX_RATIO", 0.045)
    if not pass_atr:
        fail_reasons.append(f"ATR {atr_ratio:.3%} out of range")

    pass_volume = vol_ratio >= params.get("VOLUME_MULTIPLIER", 1.2)
    if not pass_volume:
        fail_reasons.append(f"low volume ({vol_ratio:.2f}x)")

    pass_rsi = rsi_long_ok or rsi_short_ok
    if not pass_rsi:
        fail_reasons.append(f"RSI {rsi:.1f} out of range")

    pass_trend = trend in ("bullish", "bearish")
    if not pass_trend:
        fail_reasons.append("no clear trend")

    # MACD divergence
    macd_divergence = False
    if trend == "bullish" and not macd_bull:
        macd_divergence = True
        fail_reasons.append("bull MACD divergence")
    elif trend == "bearish" and macd_bull:
        macd_divergence = True
        fail_reasons.append("bear MACD divergence")

    # Score calculation (0-100)
    score = 0.0
    if pass_atr:
        mid = (params.get("ATR_MIN_RATIO", 0.015) + params.get("ATR_MAX_RATIO", 0.045)) / 2
        score += max(0, 30 - abs(atr_ratio - mid) / mid * 30)

    if vol_ratio >= params.get("MIN_VOLUME_RATIO", 1.5):
        score += 25
    elif vol_ratio >= params.get("VOLUME_MULTIPLIER", 1.2):
        score += 15

    if 50 <= rsi <= 65:
        score += 20
    elif pass_rsi:
        score += 12

    if trend == "bullish":
        score += 15
    elif trend == "bearish":
        score += 10

    if macd_bull:
        score += 10

    score = min(100, max(0, score))

    # Verdict
    all_pass = pass_atr and pass_volume and pass_rsi and pass_trend and not macd_divergence
    if all_pass and score >= 60:
        verdict = "READY"
    elif pass_atr and pass_volume and score >= 40 and not macd_divergence:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    return ScanResult(
        symbol=symbol, price=price, vol24h_usdt=vol24h,
        atr_ratio=atr_ratio, vol_ratio=vol_ratio, rsi=rsi,
        trend=trend, macd_bull=macd_bull, total_score=score,
        pass_atr=pass_atr, pass_volume=pass_volume,
        pass_rsi=pass_rsi, pass_trend=pass_trend,
        verdict=verdict,
        fail_reason=" | ".join(fail_reasons) if fail_reasons else "—",
    )


# ─── Public API ─────────────────────────────────────────────────────────────

def scan_market(params: Optional[Dict] = None) -> List[ScanResult]:
    """
    Scan all active Binance Futures USDT pairs.

    Args:
        params: Scanner parameters. If None, uses default values.

    Returns:
        List of ScanResult objects sorted by quality (READY first, then
        by score descending).
    """
    if params is None:
        params = {}

    log.info("[Scanner] Starting market scan...")

    use_futures = params.get("USE_FUTURES", True)
    min_vol = params.get("MIN_QUOTE_VOLUME", _SCANNER_MIN_QUOTE_VOLUME)

    if use_futures:
        candidates = _get_futures_symbols(min_vol)
    else:
        candidates = _get_spot_symbols(min_vol)

    if not candidates:
        log.error("[Scanner] Failed to fetch symbol list")
        return []

    log.info("[Scanner] %d candidates found. Scanning indicators...", len(candidates))

    results: List[ScanResult] = []
    errors = 0

    for i, cand in enumerate(candidates):
        try:
            result = _scan_symbol(cand["symbol"], cand["price"], cand["vol24h_usdt"], params)
            if result is not None and result.total_score >= params.get("MIN_SCAN_SCORE", 60):
                results.append(result)
        except Exception as exc:
            log.debug("[Scanner] Error scanning %s: %s", cand["symbol"], exc)
            errors += 1

        time.sleep(0.07)  # ~14 req/s — within Binance limits

    # Sort: READY first, then by score descending
    results.sort(key=lambda r: (r.verdict != "READY", -r.total_score))

    ready = [r for r in results if r.verdict == "READY"]
    watch = [r for r in results if r.verdict == "WATCH"]
    log.info("[Scanner] Complete: %d scanned, %d ready, %d watch, %d errors",
             len(candidates), len(ready), len(watch), errors)

    return results
