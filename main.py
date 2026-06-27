"""
================================================================================
MAIN ENTRY POINT — CLI Interface and Execution Orchestration
================================================================================

Parses command-line arguments, configures the system, and launches the
desired mode (auto, backtest, or single scan).

Usage:
    python main.py --mode paper --auto --top 5 --futures
    python main.py --mode backtest --symbol BTCUSDT --futures
    python main.py --mode paper --symbols BTCUSDT ETHUSDT

Refactor: Originally embedded at the end of system2.py. Extracted to
provide a clean separation between the engine and the launcher.
================================================================================
"""

import argparse
import sys

from config import CFG, log
from monitor import HybridBot


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Hybrid Trading Bot — TRVM x Strict Scoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --mode paper --auto --top 5 --futures
  %(prog)s --mode paper --auto --top 5 --spot
  %(prog)s --mode backtest --symbol BTCUSDT --futures
  %(prog)s --mode paper --symbols BTCUSDT ETHUSDT
        """,
    )

    parser.add_argument(
        "--mode", choices=["paper", "live", "backtest"], default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Fully automatic scan-trade-rescan loop",
    )
    parser.add_argument(
        "--top", type=int, default=3, dest="top_n",
        help="Number of top symbols to trade per cycle (default: 3)",
    )
    parser.add_argument(
        "--futures", action="store_true", default=True,
        help="Use Binance Futures (default)",
    )
    parser.add_argument(
        "--spot", action="store_true",
        help="Use Binance Spot (no leverage, no shorts)",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Fixed symbol list (e.g. BTCUSDT ETHUSDT)",
    )
    parser.add_argument(
        "--symbol", type=str, default="BTCUSDT",
        help="Single symbol for backtest (default: BTCUSDT)",
    )
    parser.add_argument(
        "--capital", type=float, default=None,
        help="Starting capital in USDT",
    )
    parser.add_argument(
        "--leverage", type=int, default=None,
        help="Futures leverage (default: from config)",
    )
    parser.add_argument(
        "--risk", type=float, default=None, dest="risk_pct",
        help="Risk per trade as decimal (e.g. 0.02 = 2%%)",
    )
    parser.add_argument(
        "--interval", type=str, default=None,
        help="Candle interval (default: 1h)",
    )

    return parser.parse_args()


def apply_overrides(args: argparse.Namespace) -> None:
    """Apply CLI argument overrides to the global config."""
    CFG.PAPER_MODE = (args.mode == "paper")
    CFG.USE_FUTURES = not args.spot

    if args.symbols:
        CFG.SYMBOLS = args.symbols
    if args.capital is not None:
        CFG.STARTING_CAPITAL = args.capital
    if args.leverage is not None:
        CFG.LEVERAGE = args.leverage
    if args.risk_pct is not None:
        CFG.RISK_PERCENT = args.risk_pct
        CFG.RISK_PER_TRADE = args.risk_pct
    if args.interval is not None:
        CFG.INTERVAL = args.interval
    if args.top_n is not None:
        CFG.TOP_N_SYMBOLS = args.top_n

    log.info("Configuration applied | Mode: %s | Futures: %s | Capital: %.2f | Leverage: %dx",
             "PAPER" if CFG.PAPER_MODE else "LIVE",
             CFG.USE_FUTURES, CFG.STARTING_CAPITAL, CFG.LEVERAGE)


def main() -> int:
    """Main entry point."""
    args = parse_args()
    apply_overrides(args)

    if args.mode == "backtest":
        log.info("Backtest mode not yet implemented in refactored version.")
        log.info("Use the auto scanner with paper mode to evaluate performance.")
        return 0

    bot = HybridBot()

    if args.auto:
        bot.run_auto()
    else:
        bot.run_once()

    return 0


if __name__ == "__main__":
    sys.exit(main())
