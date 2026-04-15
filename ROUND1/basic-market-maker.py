"""
Basic Round 1 market maker.

This is a minimal baseline built on the barebone architecture:
  * use the current mid-price as fair value
  * place one passive bid and one passive ask
  * apply a small inventory skew so the trader leans back toward flat

It is intentionally simple so you can iterate from a clean starting point.
"""

import json
from typing import Any, Dict, List, Tuple

from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState


OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

POSITION_LIMITS: Dict[str, int] = {
    OSMIUM: 80,
    PEPPER: 80,
}

HALF_SPREADS: Dict[str, int] = {
    OSMIUM: 1,
    PEPPER: 1,
}

QUOTE_SIZES: Dict[str, int] = {
    OSMIUM: 10,
    PEPPER: 10,
}

SESSION_LENGTH_TS = 1_000_000_000_000
PEPPER_DRIFT_PER_TICK = 0.001
PEPPER_REVERSION_PREMIUM = 3

DEFAULT_CONVERSION = 0


class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(
            self.to_json(
                [
                    self.compress_state(state, ""),
                    self.compress_orders(orders),
                    conversions,
                    "",
                    "",
                ]
            )
        )

        # We truncate state.traderData, trader_data, and self.logs to the same max. length to fit the log limit
        max_item_length = (self.max_log_length - base_length) // 3

        print(
            self.to_json(
                [
                    self.compress_state(state, self.truncate(
                        state.traderData, max_item_length)),
                    self.compress_orders(orders),
                    conversions,
                    self.truncate(trader_data, max_item_length),
                    self.truncate(self.logs, max_item_length),
                ]
            )
        )

        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp,
            trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        compressed = []
        for listing in listings.values():
            compressed.append(
                [listing.symbol, listing.product, listing.denomination])

        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [
                order_depth.buy_orders, order_depth.sell_orders]

        return compressed

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for trade in arr:
                compressed.append(
                    [
                        trade.symbol,
                        trade.price,
                        trade.quantity,
                        trade.buyer,
                        trade.seller,
                        trade.timestamp,
                    ]
                )

        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        for product, observation in observations.conversionObservations.items():
            conversion_observations[product] = [
                observation.bidPrice,
                observation.askPrice,
                observation.transportFees,
                observation.exportTariff,
                observation.importTariff,
                observation.sugarPrice,
                observation.sunlightIndex,
            ]

        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for order in arr:
                compressed.append([order.symbol, order.price, order.quantity])

        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        lo, hi = 0, min(len(value), max_length)
        out = ""

        while lo <= hi:
            mid = (lo + hi) // 2

            candidate = value[:mid]
            if len(candidate) < len(value):
                candidate += "..."

            encoded_candidate = json.dumps(candidate)

            if len(encoded_candidate) <= max_length:
                out = candidate
                lo = mid + 1
            else:
                hi = mid - 1

        return out


logger = Logger()

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
    def buy_capacity(self) -> int:
        return self.position_limit - self.position

    @property
    def sell_capacity(self) -> int:
        return self.position_limit + self.position

    def get_trader_data(self, key: str, default: Any) -> Any:
        return self.trader_data.get(f"{self.product}_{key}", default)

    def set_trader_data(self, key: str, value: Any) -> None:
        self.trader_data[f"{self.product}_{key}"] = value

    def best_bid_ask(self) -> Tuple[int, int]:
        assert self.order_depth is not None
        return max(self.order_depth.buy_orders), min(self.order_depth.sell_orders)

    def mid_price(self) -> float:
        best_bid, best_ask = self.best_bid_ask()
        return (best_bid + best_ask) / 2.0

    def fair_value(self) -> int:
        return int(round(self.mid_price()))

    def inventory_skew(self) -> int:
        if self.position_limit <= 0:
            return 0

        inventory_ratio = self.position / self.position_limit
        return -int(round(inventory_ratio * 2))

    def quote_size(self) -> int:
        base_size = QUOTE_SIZES.get(self.product, 5)
        return max(1, base_size)

    def make_basic_market(self) -> None:
        if not self.has_book:
            return

        fair_value = self.fair_value()
        self.set_trader_data("last_fair_value", fair_value)

        half_spread = HALF_SPREADS.get(self.product, 1)
        skew = self.inventory_skew()
        quote_center = fair_value + skew

        bid_price = quote_center - half_spread
        ask_price = quote_center + half_spread

        best_bid, best_ask = self.best_bid_ask()
        if best_bid + 1 < best_ask:
            bid_price = min(bid_price, best_bid + 1)
            ask_price = max(ask_price, best_ask - 1)
        else:
            bid_price = min(bid_price, best_bid)
            ask_price = max(ask_price, best_ask)

        if bid_price >= ask_price:
            bid_price = best_bid
            ask_price = best_ask

        buy_quantity = min(self.quote_size(), self.buy_capacity)
        sell_quantity = min(self.quote_size(), self.sell_capacity)

        if buy_quantity > 0:
            self.orders.append(Order(self.product, bid_price, buy_quantity))
        if sell_quantity > 0:
            self.orders.append(Order(self.product, ask_price, -sell_quantity))

    def get_orders(self) -> List[Order]:
        return self.orders


class OsmiumTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(OSMIUM, state, trader_data)

    def get_orders(self) -> List[Order]:
        self.make_basic_market()
        return self.orders


class PepperTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, trader_data)

    def get_orders(self) -> List[Order]:
        # self.make_basic_market()
        if not self.has_book:
            return self.orders

        best_bid, best_ask = self.best_bid_ask()
        mid_price = self.mid_price()
        remaining_time = max(0, SESSION_LENGTH_TS - self.state.timestamp)
        forward_fair_value = mid_price + PEPPER_DRIFT_PER_TICK * remaining_time

        self.set_trader_data("last_forward_fair_value", forward_fair_value)

        assert self.order_depth is not None

        remaining_buy_capacity = self.buy_capacity
        for ask_price in sorted(self.order_depth.sell_orders):
            if remaining_buy_capacity <= 0 or ask_price > forward_fair_value:
                break

            available = -self.order_depth.sell_orders[ask_price]
            quantity = min(available, remaining_buy_capacity)
            if quantity > 0:
                self.orders.append(Order(self.product, ask_price, quantity))
                remaining_buy_capacity -= quantity

        remaining_sell_capacity = self.sell_capacity
        sell_threshold = forward_fair_value + PEPPER_REVERSION_PREMIUM
        for bid_price in sorted(self.order_depth.buy_orders, reverse=True):
            if remaining_sell_capacity <= 0 or bid_price < sell_threshold:
                break

            available = self.order_depth.buy_orders[bid_price]
            quantity = min(available, remaining_sell_capacity)
            if quantity > 0:
                self.orders.append(Order(self.product, bid_price, -quantity))
                remaining_sell_capacity -= quantity

        if remaining_buy_capacity > 0 and best_bid + 1 < best_ask:
            passive_bid = min(best_bid + 1, int(forward_fair_value) - 1)
            if passive_bid > best_bid:
                quantity = min(self.quote_size(), remaining_buy_capacity)
                self.orders.append(Order(self.product, passive_bid, quantity))

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

        trader_data_json = json.dumps(trader_data)
        logger.flush(state, result, DEFAULT_CONVERSION, trader_data_json)
        return result, DEFAULT_CONVERSION, trader_data_json
