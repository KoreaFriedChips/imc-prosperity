"""
Round 1 market maker.

PEPPER is a simple drift-capture strategy.
OSMIUM is a reversion-aware market maker:
  * track a smooth fair value with an EMA
  * predict a short-term bounce after the last move
  * quote one tick inside the spread when possible
  * only take aggressively when the spread is tight enough
"""

import json
from typing import Any, Dict, List, Tuple

from datamodel import Order, TradingState


OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

POSITION_LIMITS: Dict[str, int] = {
    OSMIUM: 80,
    PEPPER: 80,
}

SESSION_LENGTH_TS = 1_000_000
PEPPER_DRIFT_PER_TICK = 0.001
PEPPER_SPIKE_PREMIUM = 3
PEPPER_EARLY_WINDOW = 5_000
PEPPER_EARLY_EXTRA_PREMIUM = 8
PEPPER_FAST_FILL_PREMIUM = 3

OSMIUM_EMA_ALPHA = 0.05
OSMIUM_REVERSION_BETA = 0.9
OSMIUM_TAKE_SPREAD_MAX = 8
OSMIUM_TAKE_EDGE = 1
OSMIUM_BASE_SIZE = 12

DEFAULT_CONVERSION = 0


class ProductTrader:
    def __init__(
        self,
        product: str,
        state: TradingState,
        trader_data: Dict[str, Any],
    ) -> None:
        self.product = product
        self.state = state
        self.trader_data = trader_data
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
    def has_bids(self) -> bool:
        return bool(self.order_depth and self.order_depth.buy_orders)

    @property
    def has_asks(self) -> bool:
        return bool(self.order_depth and self.order_depth.sell_orders)

    @property
    def buy_capacity(self) -> int:
        return self.position_limit - self.position

    @property
    def sell_capacity(self) -> int:
        return self.position_limit + self.position

    def get_trader_data(self, key: str, default: Any) -> Any:
        return self.trader_data.get(f"{self.product}_{key}", default)

    def set_trader_data(self, key: str, value: Any) -> None:
        self.trader_data[f"{self.product}_{key}"] = value

    def best_bid(self) -> int:
        assert self.order_depth is not None and self.order_depth.buy_orders
        return max(self.order_depth.buy_orders)

    def best_ask(self) -> int:
        assert self.order_depth is not None and self.order_depth.sell_orders
        return min(self.order_depth.sell_orders)

    def best_bid_ask(self) -> Tuple[int, int]:
        return self.best_bid(), self.best_ask()

    def mid_price(self) -> float:
        best_bid, best_ask = self.best_bid_ask()
        return (best_bid + best_ask) / 2.0

    def micro_price(self) -> float:
        assert self.order_depth is not None
        best_bid, best_ask = self.best_bid_ask()
        bid_volume = self.order_depth.buy_orders[best_bid]
        ask_volume = abs(self.order_depth.sell_orders[best_ask])
        total_volume = bid_volume + ask_volume

        if total_volume == 0:
            return self.mid_price()

        return (best_bid * ask_volume + best_ask * bid_volume) / total_volume

    @staticmethod
    def ema(previous: float, observation: float, alpha: float) -> float:
        return (1 - alpha) * previous + alpha * observation

    def take_asks(self, max_price: float, capacity: int) -> int:
        if not self.has_asks or capacity <= 0:
            return capacity

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
        if not self.has_bids or capacity <= 0:
            return capacity

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
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.order_depth:
            return self.orders

        if self.has_book:
            best_bid, best_ask = self.best_bid_ask()
            reference_price = self.mid_price()
        elif self.has_asks:
            best_ask = self.best_ask()
            best_bid = best_ask - 1
            reference_price = float(best_ask)
        elif self.has_bids:
            best_bid = self.best_bid()
            best_ask = best_bid + 1
            reference_price = float(best_bid)
        else:
            return self.orders

        remaining_time = max(0, SESSION_LENGTH_TS - self.state.timestamp)
        forward_fair_value = reference_price + PEPPER_DRIFT_PER_TICK * remaining_time

        self.set_trader_data("last_forward_fair_value", forward_fair_value)

        max_buy_price = forward_fair_value
        if self.state.timestamp <= PEPPER_EARLY_WINDOW:
            max_buy_price += PEPPER_EARLY_EXTRA_PREMIUM

        remaining_buy_capacity = self.take_asks(max_buy_price, self.buy_capacity)
        self.take_bids(forward_fair_value + PEPPER_SPIKE_PREMIUM, self.sell_capacity)

        if remaining_buy_capacity > 0:
            if self.state.timestamp <= PEPPER_EARLY_WINDOW:
                aggressive_bid = min(best_ask + PEPPER_FAST_FILL_PREMIUM, int(max_buy_price))
                if aggressive_bid >= best_ask:
                    self.orders.append(Order(self.product, aggressive_bid, remaining_buy_capacity))
            elif best_bid + 1 < best_ask:
                passive_bid = min(best_bid + 1, int(max_buy_price) - 1)
                if passive_bid > best_bid:
                    self.orders.append(Order(self.product, passive_bid, remaining_buy_capacity))

        return self.orders


class OsmiumTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(OSMIUM, state, trader_data)

    def inventory_skew(self) -> int:
        return -int(round(self.position / 20.0))

    def quote_size(self, signal_strength: float) -> int:
        if signal_strength >= 4:
            return min(20, self.position_limit)
        if signal_strength >= 2:
            return min(16, self.position_limit)
        return min(OSMIUM_BASE_SIZE, self.position_limit)

    def get_orders(self) -> List[Order]:
        if not self.order_depth:
            return self.orders

        if self.has_book:
            reference_price = self.micro_price()
            mid_price = self.mid_price()
        elif self.has_bids:
            reference_price = float(self.best_bid())
            mid_price = reference_price
        elif self.has_asks:
            reference_price = float(self.best_ask())
            mid_price = reference_price
        else:
            return self.orders

        previous_ema = self.get_trader_data("ema", reference_price)
        ema = self.ema(previous_ema, reference_price, OSMIUM_EMA_ALPHA)
        self.set_trader_data("ema", ema)

        previous_mid = self.get_trader_data("prev_mid", mid_price)
        last_change = mid_price - previous_mid
        self.set_trader_data("prev_mid", mid_price)

        predicted_reversion = -OSMIUM_REVERSION_BETA * last_change
        signal_strength = abs(predicted_reversion)

        fair_value = ema + predicted_reversion
        fair_value_int = int(round(fair_value))
        skew = self.inventory_skew()
        quote_center = fair_value_int + skew

        remaining_buy_capacity = self.buy_capacity
        remaining_sell_capacity = self.sell_capacity

        if self.has_book:
            best_bid, best_ask = self.best_bid_ask()
            spread = best_ask - best_bid

            if spread <= OSMIUM_TAKE_SPREAD_MAX:
                remaining_buy_capacity = self.take_asks(
                    fair_value - OSMIUM_TAKE_EDGE,
                    remaining_buy_capacity,
                )
                remaining_sell_capacity = self.take_bids(
                    fair_value + OSMIUM_TAKE_EDGE,
                    remaining_sell_capacity,
                )

            quote_size = min(self.quote_size(signal_strength), remaining_buy_capacity)
            bid_price = min(quote_center - 1, best_bid + 1)
            if quote_size > 0 and bid_price < best_ask:
                self.orders.append(Order(self.product, bid_price, quote_size))

            quote_size = min(self.quote_size(signal_strength), remaining_sell_capacity)
            ask_price = max(quote_center + 1, best_ask - 1)
            if quote_size > 0 and ask_price > best_bid:
                self.orders.append(Order(self.product, ask_price, -quote_size))

            return self.orders

        if self.has_asks:
            remaining_buy_capacity = self.take_asks(
                fair_value - OSMIUM_TAKE_EDGE,
                remaining_buy_capacity,
            )

        if self.has_bids:
            self.take_bids(
                fair_value + OSMIUM_TAKE_EDGE,
                remaining_sell_capacity,
            )

        return self.orders


class Trader:
    @staticmethod
    def _load_trader_data(state: TradingState) -> Dict[str, Any]:
        if not state.traderData:
            return {}

        try:
            return json.loads(state.traderData)
        except Exception:
            return {}

    def run(self, state: TradingState):
        trader_data = self._load_trader_data(state)
        traders = [
            PepperTrader(state, trader_data),
            OsmiumTrader(state, trader_data),
        ]

        result: Dict[str, List[Order]] = {
            product: [] for product in state.order_depths
        }

        for product_trader in traders:
            result[product_trader.product] = product_trader.get_orders()

        return result, DEFAULT_CONVERSION, json.dumps(trader_data)
