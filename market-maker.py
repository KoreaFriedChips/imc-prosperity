"""
Round 1 Market Maker — ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT

Unlike trader.py and agro.py (buy-and-hold / aggressive accumulation),
this strategy profits by quoting BOTH sides of the book and earning the
spread, while using signals to skew quotes and manage inventory risk.

Strategy summary
----------------
ASH_COATED_OSMIUM:
  * Two-sided market making around a dynamic fair value (EMA + autocorrelation).
  * Half-spread of 2 ticks (quote at fv-2 / fv+2) to capture 4 ticks per
    round-trip while staying inside the typical 16-tick bot spread.
  * Inventory skew: shift quotes toward flat to avoid directional risk.
  * Aggressive taking when autocorrelation signal predicts a reversion
    larger than the spread cost.
  * Multi-level ladder for larger size, capturing wider swings.

INTARIAN_PEPPER_ROOT:
  * Drift-aware market making. The +0.001/ts drift means fair value rises
    steadily, so we skew bids UP (eager to buy) and asks UP (reluctant to
    sell cheaply). Still quote both sides to earn spread on noise.
  * Sell inventory only when price overshoots trend significantly.
  * Accumulate long bias like a market maker who knows the asset appreciates.
"""

import json
from typing import Any, Dict, List

from datamodel import Order, OrderDepth, TradingState


POSITION_LIMIT: Dict[str, int] = {
    "ASH_COATED_OSMIUM": 80,
    "INTARIAN_PEPPER_ROOT": 80,
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SESSION_LENGTH_TS = 1_000_000
PEPPER_DRIFT = 0.001  # confirmed deterministic drift per timestamp

# Osmium parameters
OSMIUM_EMA_ALPHA = 0.03       # moderate EMA decay for dynamic anchor
OSMIUM_AUTOCORR = -0.50       # lag-1 autocorrelation
OSMIUM_HALF_SPREAD = 2        # quote at fv ± 2
OSMIUM_TAKE_THRESHOLD = 3     # take liquidity when predicted reversion > this


class Trader:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _mid(od: OrderDepth) -> float:
        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)
        return (best_bid + best_ask) / 2.0

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
    # Osmium: Two-sided market maker with autocorrelation edge
    # ------------------------------------------------------------------
    def _trade_osmium(self, od: OrderDepth, position: int, limit: int,
                      memory: Dict[str, Any]) -> List[Order]:
        product = "ASH_COATED_OSMIUM"
        orders: List[Order] = []

        mid = self._micro_price(od)

        # Dynamic fair value via EMA (adapts to drifting mean ~1.5/day).
        prev_ema = memory.get(f"ema_{product}", mid)
        ema = self._ema(prev_ema, mid, OSMIUM_EMA_ALPHA)
        memory[f"ema_{product}"] = ema

        # Autocorrelation signal: predict next move as -0.5 * last move.
        prev_mid = memory.get(f"prev_mid_{product}", mid)
        last_change = mid - prev_mid
        predicted_reversion = OSMIUM_AUTOCORR * last_change
        memory[f"prev_mid_{product}"] = mid

        # Fair value = EMA + predicted reversion.
        fv = ema + predicted_reversion
        fv_int = int(round(fv))

        buy_capacity = limit - position
        sell_capacity = limit + position

        # --- 1) Aggressive taking when autocorrelation signal is strong ---
        # If we predict a big reversion, cross the spread to capture it.
        if abs(predicted_reversion) >= OSMIUM_TAKE_THRESHOLD:
            if predicted_reversion > 0:
                # Price expected to go UP -> buy aggressively
                for ask_price in sorted(od.sell_orders):
                    if buy_capacity <= 0:
                        break
                    # Buy if ask is below where we think price is heading
                    if ask_price <= fv_int:
                        available = -od.sell_orders[ask_price]
                        qty = min(available, buy_capacity)
                        if qty > 0:
                            orders.append(Order(product, ask_price, qty))
                            buy_capacity -= qty
            else:
                # Price expected to go DOWN -> sell aggressively
                for bid_price in sorted(od.buy_orders, reverse=True):
                    if sell_capacity <= 0:
                        break
                    if bid_price >= fv_int:
                        available = od.buy_orders[bid_price]
                        qty = min(available, sell_capacity)
                        if qty > 0:
                            orders.append(Order(product, bid_price, -qty))
                            sell_capacity -= qty

        # --- 2) Passive market making: quote both sides ---
        # Inventory skew: shift the quote center to lean toward unwinding.
        # At max position (±80), shift by up to 3 ticks.
        inv_skew = 0
        if limit > 0:
            inv_ratio = position / limit  # -1 to +1
            inv_skew = -int(round(inv_ratio * 3))  # long -> lower quotes

        quote_center = fv_int + inv_skew

        # Primary level: fv ± half_spread (adjusted for inventory)
        my_bid = quote_center - OSMIUM_HALF_SPREAD
        my_ask = quote_center + OSMIUM_HALF_SPREAD

        # Multi-level ladder: split capacity across levels.
        # Level 1 (tight):  50% of capacity at ±2
        # Level 2 (medium): 30% at ±4
        # Level 3 (wide):   20% at ±6
        bid_levels = [
            (my_bid, 0.50),
            (my_bid - 2, 0.30),
            (my_bid - 4, 0.20),
        ]
        ask_levels = [
            (my_ask, 0.50),
            (my_ask + 2, 0.30),
            (my_ask + 4, 0.20),
        ]

        # Place bid ladder
        if buy_capacity > 0:
            placed = 0
            for i, (price, weight) in enumerate(bid_levels):
                if i == len(bid_levels) - 1:
                    qty = buy_capacity - placed
                else:
                    qty = max(1, int(round(buy_capacity * weight)))
                    qty = min(qty, buy_capacity - placed)
                if qty > 0:
                    orders.append(Order(product, price, qty))
                    placed += qty
                if placed >= buy_capacity:
                    break

        # Place ask ladder
        if sell_capacity > 0:
            placed = 0
            for i, (price, weight) in enumerate(ask_levels):
                if i == len(ask_levels) - 1:
                    qty = sell_capacity - placed
                else:
                    qty = max(1, int(round(sell_capacity * weight)))
                    qty = min(qty, sell_capacity - placed)
                if qty > 0:
                    orders.append(Order(product, price, -qty))
                    placed += qty
                if placed >= sell_capacity:
                    break

        return orders

    # ------------------------------------------------------------------
    # Pepper: Drift-aware market maker
    # ------------------------------------------------------------------
    def _trade_pepper(self, od: OrderDepth, position: int, limit: int,
                      memory: Dict[str, Any], timestamp: int) -> List[Order]:
        product = "INTARIAN_PEPPER_ROOT"
        orders: List[Order] = []

        micro = self._micro_price(od)

        # Track EMA for smoothed mid.
        prev_ema = memory.get(f"ema_{product}", micro)
        ema = self._ema(prev_ema, micro, 0.1)
        memory[f"ema_{product}"] = ema

        # Forward fair value: current level + remaining drift.
        remaining_ts = max(0, SESSION_LENGTH_TS - timestamp)
        forward_drift = PEPPER_DRIFT * remaining_ts
        fwd_fv = ema + forward_drift

        best_bid = max(od.buy_orders)
        best_ask = min(od.sell_orders)

        buy_capacity = limit - position
        sell_capacity = limit + position

        # --- 1) Take asks below forward fair value (accumulate long) ---
        for ask_price in sorted(od.sell_orders):
            if buy_capacity <= 0 or ask_price > fwd_fv:
                break
            available = -od.sell_orders[ask_price]
            qty = min(available, buy_capacity)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                buy_capacity -= qty

        # --- 2) Sell on spikes well above trend ---
        # Only sell if bid exceeds forward FV + noise buffer (std=2, so 5+ ticks).
        spike_threshold = fwd_fv + 5
        for bid_price in sorted(od.buy_orders, reverse=True):
            if sell_capacity <= 0 or bid_price < spike_threshold:
                break
            available = od.buy_orders[bid_price]
            qty = min(available, sell_capacity)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                sell_capacity -= qty

        # --- 3) Two-sided passive quotes ---
        # Bid side: quote aggressively to accumulate (drift is our friend).
        # Place bid at best_bid + 1, capped at current mid.
        passive_bid = min(best_bid + 1, int(ema))

        # Ask side: only sell well above current mid to capture noise spikes
        # while keeping inventory for the drift. Quote at mid + 5.
        passive_ask = int(ema) + 5

        # Late in the session, drift remaining is small — tighten asks to
        # capture more spread since holding value diminishes.
        if remaining_ts < 200_000:
            passive_ask = int(ema) + 3
        if remaining_ts < 50_000:
            passive_ask = int(ema) + 2

        if buy_capacity > 0:
            orders.append(Order(product, passive_bid, buy_capacity))

        if sell_capacity > 0 and passive_ask > passive_bid:
            orders.append(Order(product, passive_ask, -sell_capacity))

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
