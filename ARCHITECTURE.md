# Trading Bot Architecture Pattern

Based on FrankfurtHedgehogs framework (github.com/TimoDiehm/imc-prosperity-3).

---

## 1. Module-Level Constants

Group constants by section using banner comments:

```python
####### GENERAL ####### GENERAL ####### GENERAL #######

STATIC_SYMBOL = 'RAINFOREST_RESIN'
DYNAMIC_SYMBOL = 'KELP'

POS_LIMITS = {
    STATIC_SYMBOL: 50,
    DYNAMIC_SYMBOL: 50,
}

CONVERSION_LIMIT = 10
LONG, NEUTRAL, SHORT = 1, 0, -1
INFORMED_TRADER_ID = 'Olivia'

####### ETF ####### ETF ####### ETF ####### ETF #######

ETF_CONSTITUENT_FACTORS = [[6, 3, 1], [4, 2, 0]]
BASKET_THRESHOLDS = [80, 50]
```

---

## 2. ProductTrader Base Class

Handles all common plumbing so individual traders stay focused on strategy logic.

```python
class ProductTrader:
    def __init__(self, name, state, prints, new_trader_data, product_group=None):
        self.orders = []
        self.name = name
        self.state = state
        self.prints = prints
        self.new_trader_data = new_trader_data
        self.product_group = name if product_group is None else product_group

        self.last_traderData = self.get_last_traderData()

        self.position_limit = POS_LIMITS.get(self.name, 0)
        self.initial_position = self.state.position.get(self.name, 0)
        self.expected_position = self.initial_position

        self.mkt_buy_orders, self.mkt_sell_orders = self.get_order_depth()
        self.bid_wall, self.wall_mid, self.ask_wall = self.get_walls()
        self.best_bid, self.best_ask = self.get_best_bid_ask()

        self.max_allowed_buy_volume, self.max_allowed_sell_volume = self.get_max_allowed_volume()
        self.total_mkt_buy_volume, self.total_mkt_sell_volume = self.get_total_market_buy_sell_volume()
```

### Built-in utilities

| Method | Purpose |
|---|---|
| `get_order_depth()` | Parse & sort order book into buy/sell dicts |
| `get_walls()` | Widest bid (bid_wall), widest ask (ask_wall), midpoint |
| `get_best_bid_ask()` | Tightest bid and ask |
| `get_max_allowed_volume()` | Remaining buy/sell capacity given position limits |
| `get_total_market_buy_sell_volume()` | Total volume on each side of the book |
| `bid(price, volume)` | Place buy order, auto-decrement buy capacity |
| `ask(price, volume)` | Place sell order, auto-decrement sell capacity |
| `log(kind, message)` | Structured JSON logging under product group |
| `check_for_informed()` | Track Olivia's trades, return direction + timestamps |
| `get_orders()` | Override in subclass — returns `{symbol: [orders]}` |

---

## 3. Per-Product Trader Subclasses

### Simple (single product) — inherit ProductTrader directly

```python
class StaticTrader(ProductTrader):
    def __init__(self, state, prints, new_trader_data):
        super().__init__(STATIC_SYMBOL, state, prints, new_trader_data)

    def get_orders(self):
        # Strategy logic using self.wall_mid, self.mkt_sell_orders, etc.
        self.bid(price, volume)
        self.ask(price, volume)
        return {self.name: self.orders}
```

### Complex (multi-product) — compose multiple ProductTrader instances

```python
class EtfTrader:
    def __init__(self, state, prints, new_trader_data):
        self.baskets = [ProductTrader(s, state, prints, new_trader_data, product_group='ETF')
                        for s in ETF_BASKET_SYMBOLS]
        self.constituents = [ProductTrader(s, state, prints, new_trader_data, product_group='ETF')
                             for s in ETF_CONSTITUENT_SYMBOLS]
        # ...

    def get_orders(self):
        return {
            **self.get_basket_orders(),
            **self.get_constituent_orders()
        }
```

---

## 4. Trader.run() — Centralized Dispatch

```python
class Trader:
    def run(self, state: TradingState):
        result = {}
        new_trader_data = {}
        prints = {
            "GENERAL": {
                "TIMESTAMP": state.timestamp,
                "POSITIONS": state.position
            },
        }

        product_traders = {
            STATIC_SYMBOL: StaticTrader,
            DYNAMIC_SYMBOL: DynamicTrader,
            COMMODITY_SYMBOL: CommodityTrader,
        }

        conversions = 0
        for symbol, trader_class in product_traders.items():
            if symbol in state.order_depths:
                try:
                    trader = trader_class(state, prints, new_trader_data)
                    result.update(trader.get_orders())

                    if symbol == COMMODITY_SYMBOL:
                        conversions = trader.get_conversions()
                except: pass

        try: final_trader_data = json.dumps(new_trader_data)
        except: final_trader_data = ''

        try: print(json.dumps(prints))
        except: pass

        return result, conversions, final_trader_data
```

---

## 5. State Persistence

- Each trader reads prior state from `self.last_traderData` (parsed from `state.traderData` JSON)
- Each trader writes new state to `self.new_trader_data[key]`
- Keys are product names or custom identifiers (e.g. `'ETF_0_P'`, `'SA'`, `'LA'`)
- `Trader.run()` serializes the merged `new_trader_data` dict as the return value

---

## 6. Key Conventions

- Bare `except: pass` for non-critical failures (resilience over verbosity)
- `try/except` wrapping each trader in the main loop so one crash doesn't kill others
- `product_group` parameter groups related products under one log section
- Volume limits auto-decrement on `bid()`/`ask()` — no manual tracking needed
- Direction constants `LONG=1, NEUTRAL=0, SHORT=-1` for informed trader signals
