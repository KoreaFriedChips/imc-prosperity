"""
Round 1 Dip Buyer - Simple mean-reversion + drift capture strategy.

Strategy:
---------
OSMIUM (Mean Reversion):
  * Track the highest price seen each day (peak_price)
  * When price drops 20+ ticks from peak: BUY (expect bounce back)
  * When price spikes 10+ ticks above peak: SELL (take profits)
  * Simple: buy dips, sell spikes

INTARIAN_PEPPER_ROOT (Drift Capture):
  * Aggressively accumulate 70-80 units in first 200k timestamps
  * Hold the entire day
  * Dump everything at end of session
  * Let the drift (+1000 ticks/day) do the work
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

SESSION_LENGTH_TS = 1_000_000
DIP_THRESHOLD = 10  # Buy when price is 10 ticks below peak (happens ~97% of time)
SPIKE_THRESHOLD = 5  # Sell when price is 5 ticks above peak (tight profit taking)
PEPPER_ACCUMULATION_WINDOW = 200_000  # First 20% of session

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

        current_price = int(self.mid_price())

        # Skip if price is 0 (missing data)
        if current_price == 0:
            return self.orders

        peak_price = self.get_trader_data("peak_price", current_price)

        # Update peak price (only from valid prices)
        if current_price > peak_price:
            peak_price = current_price
            self.set_trader_data("peak_price", peak_price)

        best_bid, best_ask = self.best_bid_ask()

        # Buy when price dips 10+ ticks from peak
        if current_price <= peak_price - DIP_THRESHOLD and self.buy_capacity > 0:
            quantity = min(15, self.buy_capacity)
            self.orders.append(Order(self.product, best_ask, quantity))

        # Sell when price spikes 5+ ticks above peak
        if current_price >= peak_price + SPIKE_THRESHOLD and self.sell_capacity > 0:
            quantity = min(15, self.sell_capacity)
            self.orders.append(Order(self.product, best_bid, -quantity))

        return self.orders


class PepperTrader(ProductTrader):
    def __init__(self, state: TradingState, trader_data: Dict[str, Any]) -> None:
        super().__init__(PEPPER, state, trader_data)

    def get_orders(self) -> List[Order]:
        if not self.has_book:
            return self.orders

        # Accumulation window: first 20% of session
        if self.state.timestamp < PEPPER_ACCUMULATION_WINDOW:
            # Buy aggressively at market (take all asks)
            if self.buy_capacity > 0:
                remaining_capacity = self.buy_capacity

                for ask_price in sorted(self.order_depth.sell_orders):
                    if remaining_capacity <= 0:
                        break

                    available = -self.order_depth.sell_orders[ask_price]
                    quantity = min(available, remaining_capacity)
                    if quantity > 0:
                        self.orders.append(Order(self.product, ask_price, quantity))
                        remaining_capacity -= quantity

        # End of session: dump everything
        elif self.state.timestamp > 900_000:
            if self.position > 0:
                best_bid, best_ask = self.best_bid_ask()
                self.orders.append(Order(self.product, best_bid, -self.position))

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
