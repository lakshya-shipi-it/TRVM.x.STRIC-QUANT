# Signal Engine Documentation

## TRVM Engine (Trend-Following)

A 4-filter signal engine with AND logic — all filters must pass:

### Filter 1: Trend
- EMA fast > EMA slow (bullish) or EMA fast < EMA slow (bearish)
- Price above/below EMA fast
- EMA gap between 1.5% and 4.0%
- MACD aligned with trend

### Filter 2: Momentum (RSI)
- LONG: RSI between 45 and 65 (building momentum, not overbought)
- SHORT: RSI between 33 and 50 (confirmed weakness, not oversold)

### Filter 3: Volatility (ATR)
- ATR ratio between 1.5% and 4.5%
- Below 1.5% = no movement, above 4.5% = too volatile

### Filter 4: Volume
- Current volume ≥ 1.2x 20-period average
- Confirms market participation

### Additional Guards
1. **No-Chaser**: Price within 2.5% of EMA20
2. **Pump-Reversal**: Detect wick spikes above EMA with reversal candles
3. **Post-Pump Recovery**: Check last 2 candles for pump+reversal
4. **Volume-Price Divergence**: Higher high on declining volume = skip
5. **ADX Regime**: ADX ≥ 21 required (no ranging markets)
6. **RVE Filter**: ATR compression (< 0.8) or exhaustion (> 3.0) = skip

## Strict Scoring Engine (Multi-Factor)

Independent quality scoring on 4 factors (0-100 total):

| Factor | Weight | Criteria |
|--------|--------|----------|
| Trend | 40% | EMA stack alignment (fast > slow > trend) |
| Momentum | 30% | RSI sweet spot + MACD alignment |
| Volatility | 20% | ATR within range + Bollinger position |
| Volume | 10% | Volume ratio vs 20-period MA |

### Direction Assignment
- Structural bias (EMA stack + RSI zone) is primary gate
- TRVM signal as fallback for high scores
- Score ≥ 80 required for execution

## Dual-Confirmation Gate

EXECUTE conditions (ALL must be true):
1. TRVM signal = LONG or SHORT
2. Scoring direction matches TRVM
3. Score ≥ MIN_SCORE_TO_TRADE (default 80)
4. TRVM passed all its own filters

Result: Both engines must independently reach the same conclusion.
