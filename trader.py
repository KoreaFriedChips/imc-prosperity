"""
Round 1 Trader - ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT (v3)

Key changes from v2 (based on data analysis)
---------------------------------------------
INTARIAN_PEPPER_ROOT:
  * Removed 60% safety buffer — drift is perfectly deterministic (std=2),
    so paying even 10+ ticks above mid at start is trivially profitable.
  * Removed passive bid pyramid — with 25 ask-units/row, we fill to 80 in
    3-4 rows by aggressively taking. Passive bids add complexity for ~0 fills.
  * Fill to 80 as fast as physically possible.

ASH_COATED_OSMIUM:
  * Dynamic anchor via EMA instead of static 10000 (true mean drifts ~1.5/day).
  * Exploit the -0.50 lag-1 autocorrelation: predict next mid change as
    -0.5 × last change and shift fair value accordingly.
  * More aggressive taking — old take_edge=1 with rounding created a dead
    zone where we almost never crossed the spread (typical spread is 16).
  * Aggressive taking when autocorrelation signal is strong (big recent move).
  * Concentrate passive quotes at tightest level (fv±1) instead of multi-level.
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
SESSION_LENGTH_TS = 1_000_000

# Pepper drift per timestamp (confirmed from historical fit).
PEPPER_DRIFT = 0.001

# Osmium: EMA alpha for dynamic anchor (replaces static 10000).
# Moderate decay — adapts over ~50 observations but doesn't chase noise.
OSMIUM_EMA_ALPHA = 0.02

# Osmium: autocorrelation coefficient (measured at -0.50).
OSMIUM_AUTOCORR = -0.50


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
        """Fill to 80 ASAP, hold for drift. No safety buffer needed."""
        product = "INTARIAN_PEPPER_ROOT"
        orders: List[Order] = []

        micro = self._micro_price(od)

        # Forward-looking fair value: full drift, no discount.
        # With std=2 noise and ~1000 tick daily drift, any early purchase is profitable.
        remaining = max(0, SESSION_LENGTH_TS - timestamp)
        forward_drift = PEPPER_DRIFT * remaining
        fwd_fv = micro + forward_drift

        buy_capacity = limit - position
        sell_capacity = limit + position

        # Take every ask below forward fair value. No premium cap needed —
        # even paying 50 above mid at timestamp 0 is profitable (drift = 1000).
        for ask_price in sorted(od.sell_orders):
            if buy_capacity <= 0 or ask_price > fwd_fv:
                break
            available = -od.sell_orders[ask_price]
            qty = min(available, buy_capacity)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                buy_capacity -= qty

        # Only sell on rare spikes well above forward FV.
        for bid_price in sorted(od.buy_orders, reverse=True):
            if sell_capacity <= 0 or bid_price < fwd_fv + 3:
                break
            available = od.buy_orders[bid_price]
            qty = min(available, sell_capacity)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                sell_capacity -= qty

        return orders

    def _trade_osmium(self, od: OrderDepth, position: int, limit: int,
                      memory: Dict[str, Any]) -> List[Order]:
        """Mean-reverting scalper with dynamic anchor and autocorrelation signal."""
        product = "ASH_COATED_OSMIUM"
        orders: List[Order] = []

        mid = self._micro_price(od)

        # Dynamic anchor: EMA of mid-price (adapts to drifting mean).
        prev_ema = memory.get(f"ema_{product}", mid)
        ema = self._ema(prev_ema, mid, OSMIUM_EMA_ALPHA)
        memory[f"ema_{product}"] = ema

        # Autocorrelation signal: predict next move as -0.5 × last move.
        prev_mid = memory.get(f"prev_mid_{product}", mid)
        last_change = mid - prev_mid
        predicted_change = OSMIUM_AUTOCORR * last_change
        memory[f"prev_mid_{product}"] = mid

        # Fair value = EMA (dynamic anchor) + predicted reversion.
        fv = ema + predicted_change
        fv_int = int(round(fv))

        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)

        buy_capacity = limit - position
        sell_capacity = limit + position

        # Aggressive taking: buy asks at or below fv, sell bids at or above fv.
        # No edge buffer — the autocorrelation signal IS the edge.
        for ask_price in sorted(od.sell_orders):
            if buy_capacity <= 0 or ask_price > fv_int:
                break
            available = -od.sell_orders[ask_price]
            qty = min(available, buy_capacity)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                buy_capacity -= qty

        for bid_price in sorted(od.buy_orders, reverse=True):
            if sell_capacity <= 0 or bid_price < fv_int:
                break
            available = od.buy_orders[bid_price]
            qty = min(available, sell_capacity)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                sell_capacity -= qty

        # Passive quotes at fv ± 1 (tightest profitable level).
        my_bid = fv_int - 1
        my_ask = fv_int + 1

        # Inventory skew: shift quotes to unwind when heavy.
        inv_ratio = position / limit if limit > 0 else 0
        if inv_ratio > 0.5:
            my_bid -= 1
            my_ask -= 1  # more eager to sell
        elif inv_ratio < -0.5:
            my_bid += 1  # more eager to buy
            my_ask += 1

        if buy_capacity > 0:
            orders.append(Order(product, my_bid, buy_capacity))
        if sell_capacity > 0:
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