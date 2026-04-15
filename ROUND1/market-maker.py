# """
# IM CANNING THIS ONE FOR NOW, TS IS TOO LONG TO READ
# Round 1 Market Maker - ASH_COATED_OSMIUM and INTARIAN_PEPPER_ROOT

# Unlike trader.py and agro.py (buy-and-hold / aggressive accumulation),
# this strategy profits by quoting BOTH sides of the book and earning the
# spread, while using signals to skew quotes and manage inventory risk.

# Strategy summary
# ----------------
# ASH_COATED_OSMIUM:
#   * Two-sided market making around a dynamic fair value (EMA + autocorrelation).
#   * Half-spread of 2 ticks (quote at fv-2 / fv+2) to capture 4 ticks per
#     round-trip while staying inside the typical 16-tick bot spread.
#   * Inventory skew: shift quotes toward flat to avoid directional risk.
#   * Aggressive taking when autocorrelation signal predicts a reversion
#     larger than the spread cost.
#   * Multi-level ladder for larger size, capturing wider swings.

# INTARIAN_PEPPER_ROOT:
#   * Drift-aware market making. The +0.001/ts drift means fair value rises
#     steadily, so we skew bids UP (eager to buy) and asks UP (reluctant to
#     sell cheaply). Still quote both sides to earn spread on noise.
#   * Sell inventory only when price overshoots trend significantly.
#   * Accumulate long bias like a market maker who knows the asset appreciates.
# """

# import json
# from typing import Any, Dict, List, Sequence, Tuple

# from datamodel import Order, OrderDepth, TradingState


# OSMIUM = "ASH_COATED_OSMIUM"
# PEPPER = "INTARIAN_PEPPER_ROOT"

# POSITION_LIMITS: Dict[str, int] = {
#     OSMIUM: 80,
#     PEPPER: 80,
# }

# SESSION_LENGTH_TS = 1_000_000
# PEPPER_DRIFT = 0.001

# OSMIUM_EMA_ALPHA = 0.03
# OSMIUM_AUTOCORR = -0.50
# OSMIUM_HALF_SPREAD = 2
# OSMIUM_TAKE_THRESHOLD = 3

# DEFAULT_CONVERSION = 0


# class ProductTrader:
#     def __init__(
#         self,
#         product: str,
#         state: TradingState,
#         traderData: Dict[str, Any],
#     ) -> None:
#         self.product = product
#         self.state = state
#         self.traderData = traderData
#         self.order_depth = state.order_depths.get(product)
#         self.position = state.position.get(product, 0)
#         self.position_limit = POSITION_LIMITS.get(product, 20)
#         self.orders: List[Order] = []

#     @property
#     def has_book(self) -> bool:
#         return bool(
#             self.order_depth
#             and self.order_depth.buy_orders
#             and self.order_depth.sell_orders
#         )

#     @property
#     def buy_capacity(self) -> int:
#         return self.position_limit - self.position

#     @property
#     def sell_capacity(self) -> int:
#         return self.position_limit + self.position

#     def get_trader_data(self, key: str, default: Any) -> Any:
#         return self.traderData.get(f"{self.product}_{key}", default)

#     def set_trader_data(self, key: str, value: Any) -> None:
#         self.traderData[f"{self.product}_{key}"] = value

#     def best_bid_ask(self) -> Tuple[int, int]:
#         assert self.order_depth is not None
#         return max(self.order_depth.buy_orders), min(self.order_depth.sell_orders)

#     def micro_price(self) -> float:
#         assert self.order_depth is not None
#         best_bid, best_ask = self.best_bid_ask()
#         bid_volume = self.order_depth.buy_orders[best_bid]
#         ask_volume = abs(self.order_depth.sell_orders[best_ask])
#         total_volume = bid_volume + ask_volume

#         if total_volume == 0:
#             return (best_bid + best_ask) / 2.0

#         return (best_bid * ask_volume + best_ask * bid_volume) / total_volume

#     @staticmethod
#     def ema(previous: float, observation: float, alpha: float) -> float:
#         return (1 - alpha) * previous + alpha * observation

#     def place_weighted_ladder(
#         self,
#         levels: Sequence[Tuple[int, float]],
#         total_capacity: int,
#         is_buy: bool,
#     ) -> None:
#         if total_capacity <= 0:
#             return

#         remaining = total_capacity

#         for index, (price, weight) in enumerate(levels):
#             if index == len(levels) - 1:
#                 quantity = remaining
#             else:
#                 quantity = max(1, int(round(total_capacity * weight)))
#                 quantity = min(quantity, remaining)

#             if quantity > 0:
#                 signed_quantity = quantity if is_buy else -quantity
#                 self.orders.append(Order(self.product, price, signed_quantity))
#                 remaining -= quantity

#             if remaining <= 0:
#                 break

#     def get_orders(self) -> List[Order]:
#         return self.orders


# class OsmiumTrader(ProductTrader):
#     def __init__(self, state: TradingState, memory: Dict[str, Any]) -> None:
#         super().__init__(OSMIUM, state, memory)

#     def get_orders(self) -> List[Order]:
#         if not self.has_book:
#             return self.orders

#         micro = self.micro_price()
#         previous_ema = self.get_trader_data("ema", micro)
#         ema = self.ema(previous_ema, micro, OSMIUM_EMA_ALPHA)
#         self.set_trader_data("ema", ema)

#         previous_mid = self.get_trader_data("prev_mid", micro)
#         last_change = micro - previous_mid
#         predicted_reversion = OSMIUM_AUTOCORR * last_change
#         self.set_trader_data("prev_mid", micro)

#         fair_value = ema + predicted_reversion
#         fair_value_int = int(round(fair_value))

#         remaining_buy_capacity = self.buy_capacity
#         remaining_sell_capacity = self.sell_capacity

#         if abs(predicted_reversion) >= OSMIUM_TAKE_THRESHOLD:
#             assert self.order_depth is not None
#             if predicted_reversion > 0:
#                 for ask_price in sorted(self.order_depth.sell_orders):
#                     if remaining_buy_capacity <= 0 or ask_price > fair_value_int:
#                         break

#                     available = -self.order_depth.sell_orders[ask_price]
#                     quantity = min(available, remaining_buy_capacity)
#                     if quantity > 0:
#                         self.orders.append(Order(self.product, ask_price, quantity))
#                         remaining_buy_capacity -= quantity
#             else:
#                 for bid_price in sorted(self.order_depth.buy_orders, reverse=True):
#                     if remaining_sell_capacity <= 0 or bid_price < fair_value_int:
#                         break

#                     available = self.order_depth.buy_orders[bid_price]
#                     quantity = min(available, remaining_sell_capacity)
#                     if quantity > 0:
#                         self.orders.append(Order(self.product, bid_price, -quantity))
#                         remaining_sell_capacity -= quantity

#         inventory_skew = 0
#         if self.position_limit > 0:
#             inventory_ratio = self.position / self.position_limit
#             inventory_skew = -int(round(inventory_ratio * 3))

#         quote_center = fair_value_int + inventory_skew
#         bid_levels = [
#             (quote_center - OSMIUM_HALF_SPREAD, 0.50),
#             (quote_center - OSMIUM_HALF_SPREAD - 2, 0.30),
#             (quote_center - OSMIUM_HALF_SPREAD - 4, 0.20),
#         ]
#         ask_levels = [
#             (quote_center + OSMIUM_HALF_SPREAD, 0.50),
#             (quote_center + OSMIUM_HALF_SPREAD + 2, 0.30),
#             (quote_center + OSMIUM_HALF_SPREAD + 4, 0.20),
#         ]

#         self.place_weighted_ladder(bid_levels, remaining_buy_capacity, is_buy=True)
#         self.place_weighted_ladder(ask_levels, remaining_sell_capacity, is_buy=False)

#         return self.orders


# class PepperTrader(ProductTrader):
#     def __init__(self, state: TradingState, memory: Dict[str, Any]) -> None:
#         super().__init__(PEPPER, state, memory)

#     def get_orders(self) -> List[Order]:
#         if not self.has_book:
#             return self.orders

#         micro = self.micro_price()
#         previous_ema = self.get_trader_data("ema", micro)
#         ema = self.ema(previous_ema, micro, 0.1)
#         self.set_trader_data("ema", ema)

#         remaining_time = max(0, SESSION_LENGTH_TS - self.state.timestamp)
#         forward_drift = PEPPER_DRIFT * remaining_time
#         forward_fair_value = ema + forward_drift

#         best_bid, _ = self.best_bid_ask()
#         remaining_buy_capacity = self.buy_capacity
#         remaining_sell_capacity = self.sell_capacity

#         assert self.order_depth is not None
#         for ask_price in sorted(self.order_depth.sell_orders):
#             if remaining_buy_capacity <= 0 or ask_price > forward_fair_value:
#                 break

#             available = -self.order_depth.sell_orders[ask_price]
#             quantity = min(available, remaining_buy_capacity)
#             if quantity > 0:
#                 self.orders.append(Order(self.product, ask_price, quantity))
#                 remaining_buy_capacity -= quantity

#         spike_threshold = forward_fair_value + 5
#         for bid_price in sorted(self.order_depth.buy_orders, reverse=True):
#             if remaining_sell_capacity <= 0 or bid_price < spike_threshold:
#                 break

#             available = self.order_depth.buy_orders[bid_price]
#             quantity = min(available, remaining_sell_capacity)
#             if quantity > 0:
#                 self.orders.append(Order(self.product, bid_price, -quantity))
#                 remaining_sell_capacity -= quantity

#         passive_bid = min(best_bid + 1, int(ema))
#         passive_ask = int(ema) + 5

#         if remaining_time < 200_000:
#             passive_ask = int(ema) + 3
#         if remaining_time < 50_000:
#             passive_ask = int(ema) + 2

#         if remaining_buy_capacity > 0:
#             self.orders.append(Order(self.product, passive_bid, remaining_buy_capacity))
#         if remaining_sell_capacity > 0 and passive_ask > passive_bid:
#             self.orders.append(Order(self.product, passive_ask, -remaining_sell_capacity))

#         return self.orders


# class Trader:
#     @staticmethod
#     def _load_data(state: TradingState) -> Dict[str, Any]:
#         if not state.traderData:
#             return {}

#         try:
#             return json.loads(state.traderData)
#         except Exception:
#             return {}

#     def run(self, state: TradingState):
#         traderData = self._load_data(state)
#         traders = [
#             PepperTrader(state, traderData),
#             OsmiumTrader(state, traderData),
#         ]

#         result: Dict[str, List[Order]] = {
#             product: [] for product in state.order_depths
#         }

#         for product_trader in traders:
#             result[product_trader.product] = product_trader.get_orders()

#         return result, DEFAULT_CONVERSION, json.dumps(traderData)
