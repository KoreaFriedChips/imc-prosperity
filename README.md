# IMC Prosperity

Local research and backtesting workspace for the IMC Prosperity trading challenge.

This repo contains:

- round-specific trading algorithms under `round1/`
- a lightweight local backtester under `backtest/`
- challenge docs and notes in `DOCUMENTATION.md`, `ARCHITECTURE.md`, and the round writeups

## Repository Layout

```text
.
├── datamodel.py                 # IMC submission datamodel used by trader files
├── backtest/                    # Local backtesting engine and CLI
├── round1/                      # Stuff for round 1
├── DOCUMENTATION.md             # IMC algorithm API notes
└── ARCHITECTURE.md              # Notes on trader structure
```

## Round 1 Strategies

Round 1 trades two products:

- `ASH_COATED_OSMIUM`
- `INTARIAN_PEPPER_ROOT`

Current variants in this repo:

- `round1/trader.py`: balanced round 1 submission combining pepper drift capture with osmium mean reversion
- `round1/market-maker.py`: spread-capture variant that quotes both sides and skews for inventory
- `round1/agro.py`: more aggressive version that pushes harder for osmium fills and pepper accumulation
- `round1/polished.py`: broader framework-style trader that appears to be a later multi-round architecture reference rather than a round 1-only bot

## Backtester

The local backtester loads a Python file containing a `Trader` class, replays historical market data, applies position limits, matches orders, and prints per-day summaries.

The CLI entrypoint is:

```bash
python -m backtest --help
```

Example usage:

```bash
python -m backtest run round1/trader.py 1
python -m backtest run round1/trader.py 1-0
python -m backtest run round1/agro.py 1 --no-vis --print
```

How the day arguments work:

- `1` means all available days for round 1
- `1-0` means only round 1, day 0

By default the backtester reads bundled CSV data from `backtest/resources/`. You can also point it at another data directory with `--data`.

## Setup

This repo includes a `requirements.txt` file for the current backtester and strategy dependencies.

At minimum, the backtester imports:

- `typer`
- `tqdm`
- `ipython`
- `jsonpickle`
- `orjson`

Some strategy files also import:

- `numpy`

One straightforward setup flow is:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Writing a Trader

Your algorithm file must expose a `Trader` class with a `run(state)` method that returns:

```python
orders, conversions, trader_data
```

The key local references are:

- `datamodel.py`: submission-side data structures
- `DOCUMENTATION.md`: challenge API details
- `ARCHITECTURE.md`: project notes on trader organization

## Notes

- `round1/trader.py`, `round1/agro.py`, and `round1/market-maker.py` are self-contained round 1 bots.
- `round1/polished.py` uses a more elaborate product-trader framework and imports extra math/scientific tooling.
- Install dependencies before running the backtester commands above.
