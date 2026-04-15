# Round 1 Analysis

## Existing Algorithm Assumptions

### Pepper Root
1. **Drift is +0.001/timestamp** — Confirmed exactly: slope=0.001000/ts across all 3 days, with intercept incrementing by 1000/day
2. **Session is 1M timestamps** — Used for forward fair value; actual session is exactly 1M ts (0-999900)
3. **60% safety buffer on drift** — Discounts expected drift to avoid overpaying
4. **Never sell voluntarily** — Only sell if bid exceeds forward FV + 3 ticks
5. **Residual noise is small** — Confirmed: std ~2 ticks around the perfect trend line

### Osmium
6. **True mean is 10000** — **Wrong.** Actual means: day -2 = 9998.2, day -1 = 10000.8, day 0 = 10001.6. The mean shifts ~1.5/day upward.
7. **Mean-reverting** — Confirmed strongly: return autocorrelation = **-0.50** (essentially a textbook mean-reverting process). Only lag-1 matters; all higher lags are ~0.
8. **80% anchor weight** — This is extremely aggressive anchoring to 10000 when the true mean may be elsewhere.
9. **Spread is small enough to penny-jump** — **Wrong.** Typical spread is **16 ticks** (bid ~9994, ask ~10010). The spread only tightens to ≤6 about **2% of the time**.

## Key Data Insights

| Finding | Implication |
|---|---|
| Pepper drift is **perfectly deterministic** at 0.001/ts, residual std=2 | The algo is already near-optimal; the only variable is how fast you fill to 80 |
| Ask volume averages **25 units/row** for pepper | You can fill to 80 in ~3-4 rows (300-400 ts). No need for pyramiding bids — just take everything immediately |
| Osmium has **-0.50 lag-1 autocorrelation** | After any move of +D, expect -D/2 next tick. This is the "hidden pattern" the challenge hints at |
| Osmium mean is **NOT 10000** — it's ~10002 on day 0 and drifting up | Hardcoding 10000 as anchor systematically biases the algo to sell too eagerly and buy too timidly |
| Osmium spread is **16 ticks wide** on average | Posting at 9999/10001 is 5+ ticks inside the book — fills depend on rare book tightenings (only 2% of rows have spread ≤ 6) |
| All bot trades happen **exactly at best bid or ask** | The bots are crossing the book; no midpoint fills exist. Our passive orders only fill when the book walks to us |
| Osmium trades: ~411/day, avg size 5.3 | About 4 trades per 10000 timestamps — very thin |

## Areas for Improvement

### 1. Osmium: Exploit the -0.5 autocorrelation directly (biggest opportunity)
The algo currently ignores this strong signal. Instead of a static anchor, you could **predict the next mid-price change** as -0.5 × last change and shift fair value accordingly. This lets you lean into the direction you expect the price to revert to, and be more aggressive taking liquidity when the price just moved away from fair.

### 2. Osmium: Dynamic anchor instead of static 10000
The true mean drifts day-to-day (9998 → 10001 → 10002). Using an EMA of mid-price with appropriate decay (not 80% anchored to 10000) would adapt to whatever the live-day mean actually is. The current algo will systematically mis-price if the live day mean is, say, 10003.

### 3. Osmium: The take_edge=0 with fv_int rounding creates a dead zone
With `take_edge=0`, the algo only buys asks **strictly below** fv_int (line 173: `ask_price > fv_int - 0`). If fv rounds to 10002, it only buys asks ≤ 10001 — but the best ask is typically 10008-10010. It almost never triggers. The algo's osmium profit likely comes entirely from the ~2% of timestamps where the spread tightens.

### 4. Osmium: Consider aggressive taking when autocorrelation signals are strong
When the mid just jumped +6 ticks, the expected reversion is -3. If the current ask is only +3 from expected reversion target, it's worth crossing the spread to capture that. The current algo doesn't condition on recent price moves at all.

### 5. Pepper: The 60% safety buffer and max_premium cap are overly conservative
With residual std of only 2 ticks and a perfectly linear 1000-tick daily drift, paying even 10 ticks above current mid at the start of the day is trivially profitable (you'll gain ~1000 ticks by end). The algo should fill to 80 as fast as physically possible — within the first few hundred timestamps — and stop worrying about premium.

### 6. Pepper: Passive bid pyramid is wasted effort
With 25 units of ask volume per row, you can fill to 80 by aggressively taking asks in 3-4 rows. The pyramid of passive bids at best_bid+1, best_bid, best_bid-1 adds complexity but nearly zero fills since the bots barely trade with each other (the data shows ~330 pepper trades/day between bots, and our passive orders are last in queue).

### 7. Pepper: Consider selling and re-buying during noise excursions
The noise around the trend (std=2) is small relative to the spread (~14 ticks), so this is probably not worth it. But if the price spikes 5+ ticks above trend, selling and re-buying on the reversion could squeeze out a few extra ticks per unit.

### 8. Osmium: Multi-level quoting might be counterproductive
Posting at fv-4 ties up position limit capacity on a level that rarely fills, and if it does fill, it's during a big move where the price might continue against you. Concentrating quotes at the tightest profitable level (fv-1/fv+1) maximizes fill probability for the most favorable trades.
