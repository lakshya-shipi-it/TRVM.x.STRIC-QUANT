"""
================================================================================
EXCHANGE CLIENT — Unified Binance API Client
================================================================================

Handles all communication with Binance (both Spot and Futures APIs).
Includes HMAC-SHA256 authentication, rate-limit-aware request handling,
quantity/price formatting for exchange filters, and paper-mode support.

Refactor: Originally Section 3 of system2.py (~400 lines). Extracted into
a standalone module to separate exchange-specific logic from trading logic.
This separation enables:
    - Swapping Binance for another exchange by implementing the same interface
    - Unit testing trading logic without network calls (mock client)
    - Independent rate-limit tuning per exchange

Security: API credentials are passed in, never hardcoded. In production,
the caller reads them from environment variables via config.CFG.
================================================================================
"""

from __future__ import annotations

import hmac
import hashlib
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional, Tuple

import pandas as pd
import requests

from config import CFG, log


class BinanceClient:
    """
    Unified Binance client supporting both Spot and Futures APIs.

    Design: The client is stateless except for the Session (connection
    pooling) and filter cache. All methods that place orders return
    dicts with standardized keys (orderId, status, executedQty) so
    callers don't need to know which API endpoint was hit.
    """

    SPOT_BASE = "https://api.binance.com"
    FUTURES_BASE = "https://fapi.binance.com"

    # Class-level filter cache shared across instances
    _filter_cache: Dict[str, dict] = {}

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.base_url = self.FUTURES_BASE if CFG.USE_FUTURES else self.SPOT_BASE

    # ─── Authentication ──────────────────────────────────────────────────────

    def _sign(self, query_string: str) -> str:
        """HMAC-SHA256 signature for authenticated requests."""
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ─── Rate-Limit Handling ─────────────────────────────────────────────────

    def _check_response(self, resp: requests.Response) -> None:
        """
        Detect 429/418 and back off before raising.

        Critical fix: Without this, a 429 response is treated as a generic
        HTTP error. The except block catches it, logs it, and the next
        request fires IMMEDIATELY. Binance sees continued requests during
        a 429 window as abuse and escalates to a 418 IP ban.

        429 → read Retry-After header, sleep, then raise.
        418 → sleep 5 minutes unconditionally, then raise.
        Other non-2xx → raise_for_status() as before.
        """
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            log.warning(
                "[RateLimit] Binance RATE LIMITED (429) — sleeping %ds. "
                "If this recurs, check for duplicate bot instances.",
                retry_after,
            )
            time.sleep(retry_after)
            raise requests.exceptions.HTTPError(
                f"429 Rate Limited — backed off {retry_after}s", response=resp
            )
        if resp.status_code == 418:
            log.error(
                "[RateLimit] Binance IP BANNED (418) — sleeping 300s. "
                "Check for duplicate bot instances or a scan loop firing too fast."
            )
            time.sleep(300)
            raise requests.exceptions.HTTPError(
                "418 IP Banned — backed off 300s", response=resp
            )
        resp.raise_for_status()

    # ─── Market Data ─────────────────────────────────────────────────────────

    def get_data(
        self, symbol: str, interval: str = CFG.INTERVAL, limit: int = 200
    ) -> pd.DataFrame:
        """
        Fetch OHLCV klines. Returns a clean DataFrame indexed by open_time.

        Single function used by both TRVM and Scoring engines to avoid
        duplicate API calls. Includes retry logic with exponential backoff.
        """
        endpoint = "/fapi/v1/klines" if CFG.USE_FUTURES else "/api/v3/klines"
        url = f"{self.base_url}{endpoint}"
        params = {"symbol": symbol, "interval": interval, "limit": limit}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=10)
                self._check_response(resp)
                raw = resp.json()
                break
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as exc:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # exponential backoff: 1s, 2s
                    log.warning(
                        "Kline fetch retry %d/%d for %s: %s (waiting %ds)",
                        attempt + 1, max_retries, symbol, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                log.error("Kline fetch failed for %s after %d retries: %s",
                          symbol, max_retries, exc)
                return pd.DataFrame()
            except Exception as exc:
                log.error("Kline fetch failed for %s: %s", symbol, exc)
                return pd.DataFrame()

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "n_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
        df.set_index("open_time", inplace=True)
        return df

    def get_price(self, symbol: str) -> float:
        """Fetch current price for a symbol. Returns 0.0 on failure."""
        endpoint = (
            "/fapi/v1/ticker/price" if CFG.USE_FUTURES else "/api/v3/ticker/price"
        )
        for attempt in range(3):
            try:
                resp = self.session.get(
                    f"{self.base_url}{endpoint}",
                    params={"symbol": symbol}, timeout=5,
                )
                self._check_response(resp)
                return float(resp.json()["price"])
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as exc:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                log.warning("get_price failed for %s after retries: %s", symbol, exc)
                return 0.0
            except Exception:
                return 0.0
        return 0.0

    # ─── Exchange Filters ────────────────────────────────────────────────────

    def get_symbol_filters(self, symbol: str) -> dict:
        """
        Fetch and cache LOT_SIZE, MIN_QTY, MIN_NOTIONAL, and PRICE_FILTER.

        Returns dict with:
            stepSize     (float) — quantity increment
            minQty       (float) — minimum allowed quantity
            minNotional  (float) — minimum order value in USDT
            tickSize     (float) — price precision
        """
        if symbol in BinanceClient._filter_cache:
            return BinanceClient._filter_cache[symbol]

        defaults = {
            "stepSize": 0.001, "minQty": 0.001,
            "minNotional": 5.0, "tickSize": 0.000001,
        }
        try:
            url = (
                f"{self.FUTURES_BASE}/fapi/v1/exchangeInfo"
                if CFG.USE_FUTURES else
                f"{self.SPOT_BASE}/api/v3/exchangeInfo"
            )
            resp = self.session.get(url, params={"symbol": symbol}, timeout=10)
            self._check_response(resp)
            data = resp.json()

            step_size = 0.001
            min_qty = 0.001
            min_notional = 5.0
            tick_size = 0.000001

            for sym_info in data.get("symbols", []):
                if sym_info["symbol"] != symbol:
                    continue
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                        min_qty = float(f["minQty"])
                    elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                        min_notional = float(
                            f.get("notional", f.get("minNotional", 5.0))
                        )
                    elif f["filterType"] == "PRICE_FILTER":
                        tick_size = float(f.get("tickSize", 0.000001))
                break

            filters = {
                "stepSize": step_size,
                "minQty": min_qty,
                "minNotional": max(min_notional, 5.0),
                "tickSize": tick_size,
            }
            BinanceClient._filter_cache[symbol] = filters
            return filters

        except Exception as exc:
            log.warning("Could not fetch filters for %s (%s) — using defaults",
                        symbol, exc)
            BinanceClient._filter_cache[symbol] = defaults
            return defaults

    # ─── Quantity / Price Formatting ─────────────────────────────────────────

    @staticmethod
    def _format_qty(quantity: float, step_size: float) -> str:
        """
        Format quantity to match Binance LOT_SIZE precision.

        Examples:
            stepSize=1.0   → "57"     (not "57.0" — Binance rejects "57.0")
            stepSize=0.001 → "57.471"
        """
        precision = max(0, math.ceil(-math.log10(step_size))) if step_size < 1.0 else 0
        return f"{quantity:.{precision}f}"

    @staticmethod
    def _format_price(price: float, tick_size: float) -> str:
        """Format price to match Binance PRICE_FILTER precision."""
        if tick_size > 0:
            precision = max(0, math.ceil(-math.log10(tick_size))) if tick_size < 1.0 else 0
        else:
            precision = 6
        return f"{price:.{precision}f}"

    def _adjust_quantity(
        self, symbol: str, quantity: float, price: float
    ) -> Optional[Tuple[float, str]]:
        """
        Apply Binance LOT_SIZE / MIN_QTY / MIN_NOTIONAL filters.

        Returns (adjusted_qty_float, qty_string) or None if order
        should be skipped (below minimums).

        Uses Decimal arithmetic to avoid IEEE 754 float drift.
        """
        filters = self.get_symbol_filters(symbol)
        step_size = filters["stepSize"]
        min_qty = filters["minQty"]
        min_notional = filters["minNotional"]

        if step_size > 0:
            d_qty = Decimal(str(quantity))
            d_step = Decimal(str(step_size))
            qty = float(
                (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
            )
        else:
            qty = quantity

        qty_str = self._format_qty(qty, step_size)

        if qty < min_qty:
            log.warning("Skipped %s: qty=%s < minQty=%s", symbol, qty_str, min_qty)
            return None

        notional = qty * price
        if notional < min_notional:
            log.warning("Skipped %s: notional=%.4f < %.4f",
                        symbol, notional, min_notional)
            return None

        return qty, qty_str

    # ─── Order Execution ─────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a futures symbol. No-op in paper/spot mode."""
        if not CFG.USE_FUTURES or CFG.PAPER_MODE:
            return True
        try:
            params = {
                "symbol": symbol,
                "leverage": leverage,
                "timestamp": int(time.time() * 1000),
            }
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            params["signature"] = self._sign(qs)
            resp = self.session.post(
                f"{self.base_url}/fapi/v1/leverage",
                params=params, timeout=10,
            )
            self._check_response(resp)
            log.info("Leverage set to %dx for %s", leverage, symbol)
            return True
        except Exception as exc:
            log.error("Set leverage failed: %s", exc)
            return False

    def place_order(
        self, symbol: str, side: str, quantity: float,
        price: float = 0.0, reduce_only: bool = False,
    ) -> dict:
        """
        Place a market order with filter validation.

        Paper mode: validates quantity and logs but does not hit the API.
        reduce_only: prevents accidentally opening an opposite position
                     when exchange-side SL/TP already closed the position.
        """
        if price <= 0.0:
            price = self.get_price(symbol)

        result = self._adjust_quantity(symbol, quantity, price)
        if result is None:
            return {"error": "quantity_filter_rejected", "status": "SKIPPED"}
        adjusted_qty, qty_str = result

        if CFG.PAPER_MODE:
            log.info("[PAPER] %s %s qty=%s notional=%.4f",
                     side, symbol, qty_str, adjusted_qty * price)
            return {"orderId": "PAPER", "status": "FILLED", "executedQty": qty_str}

        endpoint = "/fapi/v1/order" if CFG.USE_FUTURES else "/api/v3/order"
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty_str,
            "timestamp": int(time.time() * 1000),
        }
        if reduce_only and CFG.USE_FUTURES:
            params["reduceOnly"] = "true"
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = self._sign(qs)
        try:
            resp = self.session.post(
                f"{self.base_url}{endpoint}", params=params, timeout=10
            )
            self._check_response(resp)
            return resp.json()
        except Exception as exc:
            log.error("Order placement failed: %s qty=%s symbol=%s", exc, qty_str, symbol)
            return {"error": str(exc)}

    # ─── Protective Orders (SL/TP) ──────────────────────────────────────────

    def place_protective_orders(
        self, symbol: str, side: str, qty_str: str,
        stop_price: float, tp_price: float,
    ) -> dict:
        """
        Place exchange-side STOP_MARKET (SL) and TAKE_PROFIT_MARKET (TP).

        These orders live on the exchange and trigger even if the bot
        is offline — providing hard protection that polling-based
        monitoring cannot guarantee.

        Only called in LIVE Futures mode. Silently skipped in paper/spot.
        """
        if CFG.PAPER_MODE or not CFG.USE_FUTURES:
            return {"status": "SKIPPED_PAPER_OR_SPOT"}

        close_side = "SELL" if side == "BUY" else "BUY"
        results = {}
        tick = self.get_symbol_filters(symbol).get("tickSize", 0.000001)

        for order_type, price in (("STOP_MARKET", stop_price),
                                   ("TAKE_PROFIT_MARKET", tp_price)):
            params = {
                "symbol": symbol,
                "side": close_side,
                "type": order_type,
                "stopPrice": self._format_price(price, tick),
                "quantity": qty_str,
                "reduceOnly": "true",
                "timestamp": int(time.time() * 1000),
            }
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            params["signature"] = self._sign(qs)
            try:
                resp = self.session.post(
                    f"{self.base_url}/fapi/v1/order", params=params, timeout=10
                )
                self._check_response(resp)
                results[order_type] = resp.json()
                log.info("Protective %s placed: %s price=%.6f qty=%s",
                         order_type, symbol, price, qty_str)
            except Exception as exc:
                log.error("Failed to place %s for %s: %s", order_type, symbol, exc)
                results[order_type] = {"error": str(exc)}

        return results

    def cancel_protective_orders(self, symbol: str) -> None:
        """
        Cancel all open protective orders for a symbol.

        Called when the bot closes a trade itself so remaining
        STOP_MARKET / TAKE_PROFIT_MARKET orders don't trigger on an
        already-closed position and open an unwanted opposite trade.
        """
        if CFG.PAPER_MODE or not CFG.USE_FUTURES:
            return
        try:
            params = {"symbol": symbol, "timestamp": int(time.time() * 1000)}
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            params["signature"] = self._sign(qs)
            resp = self.session.delete(
                f"{self.base_url}/fapi/v1/allOpenOrders",
                params=params, timeout=10,
            )
            self._check_response(resp)
            log.info("Cancelled all open orders for %s", symbol)
        except Exception as exc:
            log.error("Failed to cancel orders for %s: %s", symbol, exc)
