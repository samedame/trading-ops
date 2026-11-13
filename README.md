# trading-ops

Measurement, sizing and journaling for your bot fleet. It records and checks; it never places an order.

| Piece | File | What it does |
|---|---|---|
| Ledger | `trading_ops/ledger.py` | One SQLite file for signals, trades, fills (with slippage), chain snapshots and events. |
| Sizing | `trading_ops/sizing.py` | Fixed-fractional contract counts, cash-secured collateral, whole-share rounding. Reads `ACCOUNT_CAPITAL_USD`, else the account total in `config/capital.json`; refuses to run if neither is set. |
| Snapshotter | `trading_ops/snapshot.py` | Stores SPY/QQQ/IWM option chains (yfinance or Tradier) and reads the H8 put ladder in bp of notional. Manual only: options-data-journal is the daily snapshotter. |
| IBS import | `trading_ops/ibs_import.py` | Loads the IBS callout bot's closed trades into the ledger in bp, valued at the next open as the bot's own rule fills them. |
| Scorecard | `trading_ops/scorecard.py` | Per-bot live-vs-backtest verdict using the thresholds your own verdicts set. |
| Cost priors | `config/cost_priors.json` | Expected bid/ask crossing cost per one-lot by structure (from real SPY chains, 2010-2023). Fills carry `expected_cost_per_lot`; the scorecard compares actual vs prior and shows break-even win rate and expectancy in R. |
| Freeze | `trading_ops/freeze.py` | 90-day build-freeze tracker with an auditable exception log. |
| Docs | `docs/` | Evidence gates, redundancy map, runbook. |

Start with `docs/RUNBOOK.md`.

```
pip install -r requirements.txt
python -m pytest -q
python -m trading_ops init
python -m trading_ops scorecard
```

Python 3.10+. Only the snapshotter needs third-party packages.

Not investment advice. The thresholds are copied from your project's verdicts; they are minimum bars, not promises.
