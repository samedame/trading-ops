# Freeze exceptions

The ledger (`ledger.db`) is gitignored, so each exception logged with `python -m trading_ops freeze exception` is also
written down here, where it is reviewable.

## 2026-11-07: Tradier-first quotes for Premium-Sleeve and Tier1-Bot (fleet reliability pass)

- **Logged first** with `trading_ops.freeze.log_exception` (ledger event 1 in the local fleet-pass ledger, before any code changed).
- **Reason:** both bots quoted SPY option chains from yfinance only; Tier1-Bot stamped a delayed chain with a hard-coded
  quote age of `0.0`. Quote *sourcing and labelling* change (Tradier first, labelled yfinance fallback, true quote age,
  notice instead of alert on a dead quote). Strike selection, parameters and sizing are untouched; fixtures must select
  identical strikes.
- **Reverse if:** any existing fixture selects a different strike; OR 5 live sessions show Tradier-vs-yfinance strikes
  differing for reasons other than data delay; OR the `Tradier check (manual)` workflow FAILs for either repo.
- **Not an exception:** the yfinance feeds in Apex_Chronos, Apex-Sniper, Apex_Omega, Apex-Master, spy-bps-bot and
  frontier5-bot were deliberately NOT changed (they match their backtests).
