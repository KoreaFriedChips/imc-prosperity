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
  * Compute a forward-looking fair value: mid + drift * remaining_time.
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
from typing import Any, Dict, List, Tuple

from datamodel import Order, OrderDepth, TradingState


OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

POSITION_LIMITS: Dict[str, int] = {
    OSMIUM: 80,
    PEPPER: 80,
}

SESSION_LENGTH_TS = 1_000_000
PEPPER_DRIFT = 0.001
OSMIUM_ANCHOR = 10000.0

DEFAULT_CONVERSION = 0


class ProductTrader:
    def __init__(
        self,
        product: str,
        state: TradingState,
        memory: Dict[str, Any],
    ) -> None:
        self.product = product
        self.state = state
        self.memory = memory
        self.order_depth = state.order_depths.get(product)
        self.position = state.position.get(product, 0)
        self.position_limit = POSITION_LIMITS.get(product, 20)
        self.orders: List[Order] = []

    @property
    def has_book(self) -> bool:
        return bool(
            self.order_depth
            and self.order_depth.buy_orders
            and self.order_depth.sell_orders
        )

    @property
    def buy_capacity(self) -> int:
        return self.position_limit - self.position

    @property
    def sell_capacity(self) -> int:
        return self.position_limit + self.position

    def get_memory(self, key: str, default: Any) -> Any:
        return self.memory.get(f"{self.product}_{key}", default)

    def set_memory(self, key: str, value: Any) -> None:
        self.memory[f"{self.product}_{key}"] = value

    def best_bid_ask(self) -> Tuple[int, int]:
        assert self.order_depth is not None
        return max(self.order_depth.buy_orders), min(self.order_depth.sell_orders)

    def micro_price(self) -> float:
        assert self.order_depth is not None
        best_bid, best_ask = self.best_bid_ask()
        bid_volume = self.order_depth.buy_orders[best_bid]
        ask_volume = abs(self.order_depth.sell_orders[best_ask])
        total_volume = bid_volume + ask_volume

        if total_volume == 0:
            return (best_bid + best_ask) / 2.0

        return (best_bid * ask_volume + best_ask * bid_volume) / total_volume

    @staticmethod
    def ema(previous: float, observation: float, alpha: float) -> float:
        return (1 - alpha) * previous + alpha * observation

    def take_asks(self, max_price: float, capacity: int) -> int:
        assert self.order_depth is not None
        remaining_capacity = capacity

        for ask_price in sorted(self.order_depth.sell_orders):
            if remaining_capacity <= 0 or ask_price > max_price:
                break

            available = -self.order_depth.sell_orders[ask_price]
            quantity = min(available, remaining_capacity)
            if quantity > 0:
                self.orders.append(Order(self.product, ask_price, quantity))
                remaining_capacity -= quantity

        return remaining_capacity

    def take_bids(self, min_price: float, capacity: int) -> int:
        assert self.order_depth is not None
        remaining_capacity = capacity

        for bid_price in sorted(self.order_depth.buy_orders, reverse=True):
            if remaining_capacity <= 0 or bid_price < min_price:
                break

            available = self.order_depth.buy_orders[bid_price]
            quantity = min(available, remaining_capacity)
            if quantity > 0:
                self.orders.append(Order(self.product, bid_price, -quantity))
                remaining_capacity -= quantity

        return remaining_capacity

    def get_orders(self) -> List[Order]:
        return self.orders


class PepperTrader(ProductTrader):
    def __init__(self, state: TradingState, memory: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, memory)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        micro = self.micro_price()
        previous_ema = self.get_memory("ema", micro)
        ema = self.ema(previous_ema, micro, 0.5)
        self.set_memory("ema", ema)

        remaining_time = max(0, SESSION_LENGTH_TS - self.state.timestamp)
        forward_drift = PEPPER_DRIFT * remaining_time * 0.6
        forward_fair_value = ema + forward_drift

        max_premium = min(forward_drift, 25.0)
        max_buy_price = ema + max_premium

        remaining_buy_capacity = self.take_asks(max_buy_price, self.buy_capacity)
        self.take_bids(forward_fair_value + 3, self.sell_capacity)

        if remaining_buy_capacity > 0:
            best_bid, _ = self.best_bid_ask()
            passive_bid = min(best_bid + 1, int(ema))
            self.orders.append(Order(self.product, passive_bid, remaining_buy_capacity))

        # return []
        return self.orders


class OsmiumTrader(ProductTrader):
    def __init__(self, state: TradingState, memory: Dict[str, Any]) -> None:
        super().__init__(OSMIUM, state, memory)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        micro = self.micro_price()
        previous_ema = self.get_memory("ema", micro)
        ema = self.ema(previous_ema, micro, 0.08)
        self.set_memory("ema", ema)

        anchor_weight = 0.8
        fair_value = (1 - anchor_weight) * ema + anchor_weight * OSMIUM_ANCHOR
        fair_value_int = int(round(fair_value))

        best_bid, best_ask = self.best_bid_ask()
        take_edge = 1

        remaining_buy_capacity = self.take_asks(
            fair_value_int - take_edge, self.buy_capacity
        )
        remaining_sell_capacity = self.take_bids(
            fair_value_int + take_edge, self.sell_capacity
        )

        target_bid = fair_value_int - 1
        target_ask = fair_value_int + 1

        passive_bid = min(best_bid + 1, target_bid)
        passive_ask = max(best_ask - 1, target_ask)

        if self.position > self.position_limit * 0.5:
            passive_bid -= 1
        elif self.position < -self.position_limit * 0.5:
            passive_ask += 1

        if passive_bid >= passive_ask:
            passive_bid = fair_value_int - 1
            passive_ask = fair_value_int + 1

        if remaining_buy_capacity > 0 and passive_bid < fair_value:
            self.orders.append(Order(self.product, passive_bid, remaining_buy_capacity))
        if remaining_sell_capacity > 0 and passive_ask > fair_value:
            self.orders.append(Order(self.product, passive_ask, -remaining_sell_capacity))

        return self.orders


class Trader:
    @staticmethod
    def _load_memory(state: TradingState) -> Dict[str, Any]:
        if not state.traderData:
            return {}

        try:
            return json.loads(state.traderData)
        except Exception:
            return {}

    def run(self, state: TradingState):
        memory = self._load_memory(state)
        traders = [
            PepperTrader(state, memory),
            OsmiumTrader(state, memory),
        ]

        result: Dict[str, List[Order]] = {
            product: [] for product in state.order_depths
        }

        for product_trader in traders:
            result[product_trader.product] = product_trader.get_orders()

        return result, DEFAULT_CONVERSION, json.dumps(memory)
