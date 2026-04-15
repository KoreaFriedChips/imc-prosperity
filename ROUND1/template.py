"""
Barebone Round 1 trader scaffold.

This file keeps the same overall architecture as `market-maker.py`:
  * shared ProductTrader base class
  * one class per product
  * persistent traderData storage between timestamps
  * central Trader.run() entry point

The strategy logic is intentionally minimal so you can iterate from a blank
state without carrying over the market-making behavior.
"""

import json
from typing import Any, Dict, List, Tuple

from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState


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


OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

POSITION_LIMITS: Dict[str, int] = {
    OSMIUM: 80,
    PEPPER: 80,
}

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

    def get_orders(self) -> List[Order]:
        return self.orders


class OsmiumTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(OSMIUM, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        mid = self.mid_price()
        self.set_trader_data("last_mid", mid)

        # TODO: Add ASH_COATED_OSMIUM logic here.
        # Example ideas:
        # - estimate fair value
        # - place passive quotes
        # - take liquidity when the book is mispriced
        # - manage inventory skew

        return self.orders


class PepperTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        mid = self.mid_price()
        self.set_trader_data("last_mid", mid)

        # TODO: Add INTARIAN_PEPPER_ROOT logic here.
        # Example ideas:
        # - trend / drift model
        # - accumulation logic
        # - passive spread capture
        # - end-of-session inventory handling

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

        logger.flush(state, result, DEFAULT_CONVERSION, trader_data)
        return result, DEFAULT_CONVERSION, json.dumps(trader_data)
