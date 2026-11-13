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

## 2026-11-13: Apex Chronos P1 and P3 alerts muted (fleet production pass)

- **Logged here before the change was merged.** Requested by Sam on 2026-11-13 after the fleet scan.
- **Reason:** the forward model record (15 Oct to 12 Nov, 120 trades, model prices) is P1 74 trades at -3.4% each and
  P3 6 trades at -10.3% each; only P2 is positive (40 trades, +3.3%, 72% winners). Muting changes which alerts reach
  Discord. The engine, its parameters, its slot scheduling and the ledger are untouched: all three phases are still
  logged from final bars every evening, so the forward test continues unchanged.
- **Reverse if:** over any 100 consecutive logged P1 trades after 2026-11-16 the mean model ROI is above zero with a
  day-clustered one-sided 95% lower bound above zero (the same bar P2 has to clear), or the P2 decision in
  `Apex_Chronos/docs/P2_DECISION.md` ends in RETIRE, in which case the whole bot goes log-only instead.
- **Not an exception (allowed work):** filling `config/capital.json`, the IBS benchmark in `config/bots.json`, trimming
  duplicate GitHub schedules behind the Cloudflare Worker, and turning off this repo's duplicate chain snapshot.
- **Still unlogged:** the six bot repos created after the freeze began (explosive-scanner, jonah-ledger,
  trend-sleeve-bot, pair-alert, Grand-Tournament, dip-call). Each needs its own "reverse if" from you.
