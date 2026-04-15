"""
Round 1 Trader - ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT (v3 "ultra")

V2 live result: ~10,500 PnL (pepper ~7,286 + osmium ~2,768).
Pepper is near-optimal (90% of theoretical max). The remaining upside is
almost entirely on the OSMIUM scalper:
  - v2 fills cluster at 9994/10003 (one tick inside top-of-book), capturing
    ~6 PnL per round-trip × 472 trades = 2,768.
  - The passive fill RATE is the bottleneck, not the edge per fill.

v3 changes (more aggressive)
----------------------------
OSMIUM
  * Quote at FIXED 9999 bid / 10001 ask (the tightest possible around 10000
    mean), instead of penny-jumping the best_bid/best_ask. This maximises
    the number of times the book trades through our resting orders.
  * Multi-level resting quotes: post extra size 2 and 4 ticks deeper on
    each side to catch bigger mean-reversion swings.
  * take_edge = 0 (grab any ask <= 9999 or bid >= 10001).
  * Keep inventory skew so we don't over-accumulate a side.

PEPPER
  * Keep the aggressive buy-and-hold (already near-optimal) but widen the
    max_buy_price cap so we'll chase a sudden ramp. Raise premium cap to 40.
  * Instead of passive bid at best_bid+1, pyramid-bid: stack resting bids
    at best_bid+1, best_bid, and one tick below so we scoop any dip.
"""

import json
from typing import Any, Dict, List

from datamodel import Order, OrderDepth, TradingState


POSITION_LIMIT: Dict[str, int] = {
    "ASH_COATED_OSMIUM": 80,
    "INTARIAN_PEPPER_ROOT": 80,
}

# ----------------------------------------------------------------------------
# Configuration constants
# ----------------------------------------------------------------------------
# Assume a generous session length so our drift forecast is always positive.
# Even if the real day is shorter, we'll simply be slightly less aggressive
# near the tail, which is fine.
SESSION_LENGTH_TS = 1_000_000

# Pepper drift per timestamp (confirmed from historical fit).
PEPPER_DRIFT = 0.001

# Osmium long-run mean.
OSMIUM_ANCHOR = 10000.0


class Trader:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _micro_price(od: OrderDepth) -> float:
        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)
        bid_vol = od.buy_orders[best_bid]
        ask_vol = abs(od.sell_orders[best_ask])
        tot = bid_vol + ask_vol
        if tot == 0:
            return (best_bid + best_ask) / 2.0
        return (best_bid * ask_vol + best_ask * bid_vol) / tot

    @staticmethod
    def _ema(prev: float, obs: float, alpha: float) -> float:
        return (1 - alpha) * prev + alpha * obs

    # ------------------------------------------------------------------
    # Per-product trading logic
    # ------------------------------------------------------------------
    def _trade_pepper(self, od: OrderDepth, position: int, limit: int,
                      memory: Dict[str, Any], timestamp: int) -> List[Order]:
        """Aggressive buy-and-hold with opportunistic sells on spikes."""
        product = "INTARIAN_PEPPER_ROOT"
        orders: List[Order] = []

        # Denoised current mid (EMA of micro-price).
        micro = self._micro_price(od)
        prev_ema = memory.get(f"ema_{product}", micro)
        ema = self._ema(prev_ema, micro, 0.5)
        memory[f"ema_{product}"] = ema

        # Forward-looking fair value: current mid + expected drift to close.
        remaining = max(0, SESSION_LENGTH_TS - timestamp)
        # Use 60% of the expected drift to keep a safety buffer.
        forward_drift = PEPPER_DRIFT * remaining * 0.6
        fwd_fv = ema + forward_drift

        # Cap how far above the current mid we are willing to chase.
        # v3: widened cap from 25 -> 40 to grab early asks when drift
        # dominates.
        max_premium = min(forward_drift, 40.0)
        max_buy_price = ema + max_premium

        buy_capacity = limit - position
        sell_capacity = limit + position

        # 1) AGGRESSIVELY take asks cheaper than our max_buy_price.
        for ask_price in sorted(od.sell_orders):
            if buy_capacity <= 0 or ask_price > max_buy_price:
                break
            available = -od.sell_orders[ask_price]
            qty = min(available, buy_capacity)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                buy_capacity -= qty

        # 2) Only sell if a bid is WELL above forward fair value
        #    (captures rare over-shoots). Require a 3-tick margin.
        for bid_price in sorted(od.buy_orders, reverse=True):
            if sell_capacity <= 0 or bid_price < fwd_fv + 3:
                break
            available = od.buy_orders[bid_price]
            qty = min(available, sell_capacity)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                sell_capacity -= qty

        # 3) Pyramid of passive resting bids. We deliberately do NOT post a
        #    passive ask: we want to HOLD the inventory for the drift.
        #    Split buy_capacity across 3 price levels to catch any dip.
        if buy_capacity > 0 and od.buy_orders:
            best_bid = max(od.buy_orders)
            top_bid = min(best_bid + 1, int(ema))  # never above mid
            # Split: 50% at top_bid, 30% one tick lower, 20% two lower.
            a = buy_capacity // 2
            b = (buy_capacity * 3) // 10
            c = buy_capacity - a - b
            if a > 0:
                orders.append(Order(product, top_bid, a))
            if b > 0 and top_bid - 1 >= 1:
                orders.append(Order(product, top_bid - 1, b))
            if c > 0 and top_bid - 2 >= 1:
                orders.append(Order(product, top_bid - 2, c))

        return orders

    def _trade_osmium(self, od: OrderDepth, position: int, limit: int,
                      memory: Dict[str, Any]) -> List[Order]:
        """Mean-reverting market maker with static anchor at 10000."""
        product = "ASH_COATED_OSMIUM"
        orders: List[Order] = []

        micro = self._micro_price(od)
        prev_ema = memory.get(f"ema_{product}", micro)
        ema = self._ema(prev_ema, micro, 0.08)  # slow filter matches v1
        memory[f"ema_{product}"] = ema

        # Heavier weight on the static anchor ensures we reliably trade
        # AGAINST deviations from 10000 (the historical mean).
        anchor_weight = 0.8
        fv = (1 - anchor_weight) * ema + anchor_weight * OSMIUM_ANCHOR
        fv_int = int(round(fv))

        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)

        buy_capacity = limit - position
        sell_capacity = limit + position

        # v3: take at fair value (edge = 0) to grab any opportunistic
        # crosses. A cross at exactly fv still reverts back to mean on avg.
        take_edge = 0

        # Take with edge.
        for ask_price in sorted(od.sell_orders):
            if buy_capacity <= 0 or ask_price > fv_int - take_edge:
                break
            available = -od.sell_orders[ask_price]
            qty = min(available, buy_capacity)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                buy_capacity -= qty

        for bid_price in sorted(od.buy_orders, reverse=True):
            if sell_capacity <= 0 or bid_price < fv_int + take_edge:
                break
            available = od.buy_orders[bid_price]
            qty = min(available, sell_capacity)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                sell_capacity -= qty

        # v3: Post a LADDER of passive quotes around the anchor so we catch
        # both tight mean reversions and wider swings. Quote at fixed
        # anchor-relative levels (not tied to best_bid/ask), because
        # top-of-book is usually 9998/10002 and we want to be INSIDE that
        # at 9999/10001 as much as possible.
        bid_levels = [(fv_int - 1, 0.5), (fv_int - 2, 0.3), (fv_int - 4, 0.2)]
        ask_levels = [(fv_int + 1, 0.5), (fv_int + 2, 0.3), (fv_int + 4, 0.2)]

        # Inventory skew: if we're long, pull bids back & lean asks in.
        pos_frac = position / limit if limit else 0.0
        if pos_frac > 0.5:
            bid_levels = [(p - 1, w) for p, w in bid_levels]
        elif pos_frac > 0.25:
            bid_levels = [(p - (1 if i > 0 else 0), w) for i, (p, w) in enumerate(bid_levels)]
        if pos_frac < -0.5:
            ask_levels = [(p + 1, w) for p, w in ask_levels]
        elif pos_frac < -0.25:
            ask_levels = [(p + (1 if i > 0 else 0), w) for i, (p, w) in enumerate(ask_levels)]

        if buy_capacity > 0:
            remaining = buy_capacity
            placed = 0
            for i, (price, weight) in enumerate(bid_levels):
                if price >= fv_int:
                    continue
                if i == len(bid_levels) - 1:
                    qty = remaining - placed
                else:
                    qty = max(1, int(round(buy_capacity * weight)))
                    qty = min(qty, remaining - placed)
                if qty > 0:
                    orders.append(Order(product, price, qty))
                    placed += qty
                if placed >= remaining:
                    break

        if sell_capacity > 0:
            remaining = sell_capacity
            placed = 0
            for i, (price, weight) in enumerate(ask_levels):
                if price <= fv_int:
                    continue
                if i == len(ask_levels) - 1:
                    qty = remaining - placed
                else:
                    qty = max(1, int(round(sell_capacity * weight)))
                    qty = min(qty, remaining - placed)
                if qty > 0:
                    orders.append(Order(product, price, -qty))
                    placed += qty
                if placed >= remaining:
                    break

        return orders

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        memory: Dict[str, Any] = {}
        if state.traderData:
            try:
                memory = json.loads(state.traderData)
            except Exception:
                memory = {}

        for product, od in state.order_depths.items():
            if not od.buy_orders or not od.sell_orders:
                result[product] = []
                continue

            position = state.position.get(product, 0)
            limit = POSITION_LIMIT.get(product, 20)

            if product == "INTARIAN_PEPPER_ROOT":
                result[product] = self._trade_pepper(
                    od, position, limit, memory, state.timestamp
                )
            elif product == "ASH_COATED_OSMIUM":
                result[product] = self._trade_osmium(od, position, limit, memory)
            else:
                result[product] = []

        return result, 0, json.dumps(memory)