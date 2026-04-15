"""
Round 1 Trader - ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT (v2)

Lessons from the first live submission
---------------------------------------
* The exchange fills our PASSIVE quotes almost never: non-SUBMISSION bots
  barely trade with each other (0 trades on pepper, 6 on osmium across the
  whole 100k-timestamp session). Every profitable trade has to be initiated
  by us crossing the book.
* Pepper root drifts deterministically at +0.001 per timestamp. Over an
  entire trading session (100k timestamps on the live run, but we size for
  a full 1M-timestamp day to be safe) the drift dominates the bid-ask
  spread (~13), so paying several ticks above mid early in the day is still
  profitable as long as we carry the position to the close.

Revised strategy
----------------
INTARIAN_PEPPER_ROOT (directional, buy-and-hold)
  * Compute a forward-looking fair value:  mid + drift * remaining_time.
  * Aggressively TAKE every ask below that forward fair value (scaled down
    as the session progresses so we don't over-pay near the end).
  * Never voluntarily sell; only unwind if a bid is well above the
    forward fair value (surprising spike).
  * Keep a thin passive bid at best_bid+1 to pick up any stray fills.

ASH_COATED_OSMIUM (mean-reverting scalper)
  * True fair value is the long-run mean (anchored at 10000) with a small
    adjustment from the observed micro-price.
  * Take any ask <= FV - take_edge and any bid >= FV + take_edge.
  * Post tight penny-jumping passive quotes to capture the occasional
    spread from bot activity.
  * Skew quotes when inventory is one-sided.
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
        # Even with infinite drift we never pay more than 25 above mid.
        max_premium = min(forward_drift, 25.0)
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

        # 3) Thin passive resting bid 1 tick above best_bid to pick up any
        #    surprise fills. We deliberately do NOT post a passive ask:
        #    we want to HOLD the inventory for the drift.
        if buy_capacity > 0 and od.buy_orders:
            best_bid = max(od.buy_orders)
            my_bid = best_bid + 1
            my_bid = min(my_bid, int(ema))  # never above mid
            orders.append(Order(product, my_bid, buy_capacity))

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

        take_edge = 1  # cross the book if the price is this far from fv

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

        # Passive market-making (pennies inside top-of-book).
        target_buy = fv_int - 1
        target_sell = fv_int + 1

        my_bid = min(best_bid + 1, target_buy)
        my_ask = max(best_ask - 1, target_sell)

        # Inventory skew.
        if position > limit * 0.5:
            my_bid -= 1
        elif position < -limit * 0.5:
            my_ask += 1

        if my_bid >= my_ask:
            my_bid = fv_int - 1
            my_ask = fv_int + 1

        if buy_capacity > 0 and my_bid < fv:
            orders.append(Order(product, my_bid, buy_capacity))
        if sell_capacity > 0 and my_ask > fv:
            orders.append(Order(product, my_ask, -sell_capacity))

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