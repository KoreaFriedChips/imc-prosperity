"""
Round 1 trader using the shared template architecture.

This keeps the aggressive ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT
strategy logic, but refactors the file to follow `template.py`:
  * shared Logger
  * shared ProductTrader base class
  * product-specific trader classes
  * persistent traderData storage
  * central Trader.run() entry point
"""

import json
from typing import Any, Dict, List, Sequence, Tuple

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

        max_item_length = (self.max_log_length - base_length) // 3

        print(
            self.to_json(
                [
                    self.compress_state(state, self.truncate(state.traderData, max_item_length)),
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
            compressed.append([listing.symbol, listing.product, listing.denomination])
        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [order_depth.buy_orders, order_depth.sell_orders]
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

SESSION_LENGTH_TS = 1_000_000
PEPPER_DRIFT = 0.001
OSMIUM_ANCHOR = 10000.0

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

    def place_weighted_ladder(
        self,
        levels: Sequence[Tuple[int, float]],
        total_capacity: int,
        is_buy: bool,
        skip_at_or_beyond: int | None = None,
        require_strictly_less: bool = True,
    ) -> None:
        if total_capacity <= 0:
            return

        remaining = total_capacity

        for index, (price, weight) in enumerate(levels):
            if skip_at_or_beyond is not None:
                if require_strictly_less and price >= skip_at_or_beyond:
                    continue
                if not require_strictly_less and price <= skip_at_or_beyond:
                    continue

            if index == len(levels) - 1:
                quantity = remaining
            else:
                quantity = max(1, int(round(total_capacity * weight)))
                quantity = min(quantity, remaining)

            if quantity > 0:
                signed_quantity = quantity if is_buy else -quantity
                self.orders.append(Order(self.product, price, signed_quantity))
                remaining -= quantity

            if remaining <= 0:
                break

    def get_orders(self) -> List[Order]:
        return self.orders


class OsmiumTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(OSMIUM, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        micro = self.micro_price()
        previous_ema = self.get_trader_data("ema", micro)
        ema = self.ema(previous_ema, micro, 0.08)
        self.set_trader_data("ema", ema)

        anchor_weight = 0.8
        fair_value = (1 - anchor_weight) * ema + anchor_weight * OSMIUM_ANCHOR
        fair_value_int = int(round(fair_value))

        remaining_buy_capacity = self.take_asks(fair_value_int, self.buy_capacity)
        remaining_sell_capacity = self.take_bids(fair_value_int, self.sell_capacity)

        bid_levels = [
            (fair_value_int - 1, 0.5),
            (fair_value_int - 2, 0.3),
            (fair_value_int - 4, 0.2),
        ]
        ask_levels = [
            (fair_value_int + 1, 0.5),
            (fair_value_int + 2, 0.3),
            (fair_value_int + 4, 0.2),
        ]

        position_ratio = self.position / self.position_limit if self.position_limit else 0.0
        if position_ratio > 0.5:
            bid_levels = [(price - 1, weight) for price, weight in bid_levels]
        elif position_ratio > 0.25:
            bid_levels = [
                (price - (1 if index > 0 else 0), weight)
                for index, (price, weight) in enumerate(bid_levels)
            ]

        if position_ratio < -0.5:
            ask_levels = [(price + 1, weight) for price, weight in ask_levels]
        elif position_ratio < -0.25:
            ask_levels = [
                (price + (1 if index > 0 else 0), weight)
                for index, (price, weight) in enumerate(ask_levels)
            ]

        self.place_weighted_ladder(
            bid_levels,
            remaining_buy_capacity,
            is_buy=True,
            skip_at_or_beyond=fair_value_int,
            require_strictly_less=True,
        )
        self.place_weighted_ladder(
            ask_levels,
            remaining_sell_capacity,
            is_buy=False,
            skip_at_or_beyond=fair_value_int,
            require_strictly_less=False,
        )

        return self.orders


class PepperTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        micro = self.micro_price()
        previous_ema = self.get_trader_data("ema", micro)
        ema = self.ema(previous_ema, micro, 0.5)
        self.set_trader_data("ema", ema)

        remaining_time = max(0, SESSION_LENGTH_TS - self.state.timestamp)
        forward_drift = PEPPER_DRIFT * remaining_time * 0.6
        forward_fair_value = ema + forward_drift

        max_premium = min(forward_drift, 40.0)
        max_buy_price = ema + max_premium

        remaining_buy_capacity = self.take_asks(max_buy_price, self.buy_capacity)
        self.take_bids(forward_fair_value + 3, self.sell_capacity)

        if remaining_buy_capacity > 0:
            best_bid, _ = self.best_bid_ask()
            top_bid = min(best_bid + 1, int(ema))
            bid_levels = [
                (top_bid, 0.5),
                (top_bid - 1, 0.3),
                (top_bid - 2, 0.2),
            ]
            valid_bid_levels = [
                (price, weight) for price, weight in bid_levels if price >= 1
            ]
            self.place_weighted_ladder(
                valid_bid_levels,
                remaining_buy_capacity,
                is_buy=True,
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
            logger.print("Failed to parse traderData; resetting state")
            return {}

    def run(self, state: TradingState):
        trader_data = self._load_trader_data(state)
        traders = [
            OsmiumTrader(state, trader_data),
            PepperTrader(state, trader_data),
        ]

        result: Dict[str, List[Order]] = {
            product: [] for product in state.order_depths
        }

        for product_trader in traders:
            result[product_trader.product] = product_trader.get_orders()

        serialized_trader_data = json.dumps(trader_data)
        logger.flush(state, result, DEFAULT_CONVERSION, serialized_trader_data)
        return result, DEFAULT_CONVERSION, serialized_trader_data
