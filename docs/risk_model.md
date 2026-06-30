# Risk Management Documentation

## Position Sizing

The system uses ATR-based position sizing with dynamic risk scaling:

```
risk_amount   = STARTING_CAPITAL × RISK_PERCENT
sl_distance   = ATR × ATR_SL_MULTIPLIER
position_size = risk_amount / sl_distance
```

This adapts to market volatility: high ATR → smaller positions, low ATR → larger positions. The risk per trade is constant as a percentage of capital.

### Minimum Notional Handling

For small accounts, minimum notional requirements ($5-10) may force oversizing:

1. Calculate ideal position size
2. If notional < MIN_NOTIONAL, scale up to minimum
3. Recalculate actual risk percentage
4. If actual risk > 1.5x configured risk → skip trade

## Stop Loss Framework

### Entry SL/TP
- **SL**: entry ± (ATR × 1.5)
- **TP**: entry ± (ATR × 3.0)
- Risk:Reward = 1:2

### Breakeven Protection
When price reaches 0.3R profit, SL is moved to entry price + buffer. This guarantees the trade cannot lose money once breakeven is active.

### Trailing Stop
- **Activation**: 0.5R profit
- **Trail distance**: peak_R - 0.15R
- **Floor**: entry_price (never below breakeven)
- **Secondary ATR trail**: Tighter of R-based and ATR-based trails wins

## Circuit Breakers

| Level | Trigger | Action |
|-------|---------|--------|
| 1 | Daily loss ≥ 5% | Stop all trading |
| 2 | Max 3 open positions | Block new entries |
| 3 | Max 2 same-direction | Block directional bias |
| 4 | Cooldown after loss | 30-minute pause |
| 5 | Per-symbol daily loss | Block symbol for rest of day |
| 6 | Max 3 entries/symbol/day | Rate limit per symbol |
| 7 | Connection timeout 5min | Force-close unmonitored positions |
| 8 | Early-loss cooldown | 60-minute symbol ban after early exit |

## Exchange-Side Protection

In live futures mode, the bot places:
- `STOP_MARKET` order at SL price (reduceOnly)
- `TAKE_PROFIT_MARKET` order at TP price (reduceOnly)

These orders survive bot restarts and network outages. They are cancelled when the bot closes a position itself to prevent duplicate execution.
