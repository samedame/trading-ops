# Evidence gates: when a bot may touch real money

Every number below comes from a verdict in your project. Where a verdict gave no threshold, the cell says **SET BEFORE FIRST TRADE**: write the number down before you see live results, never after.

A bot may only be funded with real capital when ALL of these are true:

1. Its scorecard status is **ON TRACK** (never INSUFFICIENT DATA, INCONCLUSIVE, UNDERPERFORMING, PAUSE, RETIRE or BLOCKED).
2. `ACCOUNT_CAPITAL_USD` is set (the account total is $1,500 in `config/capital.json` since 2026-11-13) and `python -m trading_ops size ...` shows at least one tradable unit at your risk budget.
3. At least 20 fills are logged and measured slippage has been subtracted from the live mean (the scorecard's mean is only as honest as the P&L you log: log real fill prices, not alert prices).
4. The bot has run through at least one VIX > 25 episode, or you have written down that you accept not having seen one.
5. You have written down, in advance, the cumulative loss at which you stop.

## Per-bot status and next gate

| Bot | Evidence today | What it must show next | Stop / retire rule |
|---|---|---|---|
| **IBS (index mean reversion)** | Only strategy that passed every pre-registered gate (frozen pre-registration, OOS split, multiple-testing correction, independent re-implementation). Benchmark in `config/bots.json`: **+68 bp a trade**, next open to next open, net of 1.5 bp a side (OOS 2019-01 to 2026-09). | 30 closed trades before any status; then live results that sit with +68 bp within cost. Load the bot's closed trades with `python -m trading_ops import-ibs` (16 were logged on 2026-11-13). | Verdict kill switch: average trade below +0.2% over 100 alerts, drawdown past 25%, or win rate under 60% over 100 alerts. The scorecard applies the first at 100 trades; the bot's `kill_switch_monitor.py` checks all three weekly. |
| **Momentum sleeve** | 5 of 6 gating checks passed; bootstrap CI on the Sharpe improvement includes zero. Shadow mode. | Re-test the bootstrap around **2027-09-15**; confirm fractional-share support first (whole-share rounding is damaging below ~$5k). | Pre-registered decision rule: shadow continues, not GO, not rejected. |
| **Apex Master v2** (VIX >= 20 and IBS < 0.10) | +18.2 bp/trade, 95% CI +5.4 to +31.3 over 734 trades since 1993; haircut about 50% (post-hoc VIX split, ~130 tests); VIX >= 25 contributes 69.5% of profit. | 40 paper trades. Roughly 24 signals/year, so ~20 months: this is a slow bot. Alerts only. Quote gate before any options version: only take it if the real price beats the empirical fair value by a margin. | Pause below -1,600 bp cumulative. Retire at 40 trades if mean <= 0. Test the IWM exclusion once at 40 trades. |
| **Apex Master v1** | Published +0.306R depends on hindsight pick; live first_fire rule is -0.020R with max drawdown -12.8R, breaching the -8R bar. | A causal pick rule that is actually tested (wait-for-window variants all lost). Decide profile (reliability vs runner) before the first trade. | -8R max drawdown (Section 9). |
| **Apex Chronos (intraday)** | Audit found no edge on hourly data: 108-rule scan, 0 Holm survivors. Forward model record 15 Oct to 12 Nov (120 trades, model prices, not fills): P1 74 trades -3.4%, P2 40 trades +3.3% with 72% winners, P3 6 trades -10.3%. | P1 and P3 alerts are muted (still logged). **P2 only:** 100 new model trades dated 2026-11-16 or later, about 2 a session, so roughly February 2027. | Written 2026-11-13 in `Apex_Chronos/docs/P2_DECISION.md`: at 100 new P2 trades KEEP only if the mean is above zero and the day-clustered one-sided 95% lower bound is above zero, otherwise RETIRE; stop early at -100% of collateral cumulative. |
| **tier1-bot (H8)** | BLOCKED. Strike rule picks ~2.21% OTM (a ~1-delta put): -$0.59/trade over 33 years. Beats T-bills only in the 2022-25 window. | Re-specify as a fixed 3.5-4.0% OTM distance; log the ladder (snapshotter does) for 20 sessions; re-run the comparison on real credits. Needs roughly $23k-$72k of capital per contract (the verdict's capital-per-contract figures, up to a fully cash-secured put at today's price): not feasible on a small account. | n/a until re-specified |
| **Condor / daytrading_bot** | 0 of 7 real credit checks reached OK ($0.03-$0.27 vs $0.44 bar). 0 logged fills. | Merge draft PR #3 on `daytrading_bot` first. Then 20+ paper fills. | Kill-switch floor scales with real credit (patched in PR #3, not merged). |
| **Premium sleeve (SPY short premium)** | Evidence is index-level PUT only: holdout Sharpe 0.56, CAGR 6.9%, max DD -29%. The audited gate in front of it is Tier D/E. **At the $1,500 account setting it sizes to 0 contracts** (5% per-position cap against a 10-wide spread; floor near $20,000), so it has no position and cannot build a record. | 60+ sessions of logged fills spanning at least one VIX > 25 episode, on the actual PT/SL spread structure. Not reachable until the account can fit one contract. | In the bot's config since launch: halt entries at a 15% drawdown from the sleeve's peak, restart after 20 sessions with VIX/VIX3M below 1.00; per position 50% of credit, 2x credit stop, 21 DTE. |
| **Pre-earnings straddle** | No evidence in the project. Paper only. | Needs its own pre-registered backtest. | n/a |
| **Bot selector** | Replay: 39 pick changes, n=5 comparable, pick -29 pts vs alternatives, CI includes 0. | Log decisions only; judge after n >= 30 against same-board alternatives. | Do not trade from it. |

## The gate that is not a gate

Your vol-regime gate audit (2026-11-01) returned **Tier D, not supported** (gate (a) Tier E, harmful). Gate (a), VIX3M/VIX >= 1.0, was negative in 9 of 9 holdout years it acted and the audit says to turn it off. Gate (b1) is indistinguishable from random blocking and rests on one event (March 2020). It can stay as a stated risk-appetite preference, not as an edge claim. What the audit did find is real: VIX level predicts the next cycle's short-premium return (t = +3.63, not decayed in 17 years), which points to a **VIX-scaled sizing rule** as a separate candidate that must be pre-registered and audited on its own.

## What would change a verdict (copied from the audit, kept here so it cannot drift)

A matched-frequency random-regime control, on a window excluding 2020, where the real gate beats at least 95% of random gates on both delta-Sharpe and delta-drawdown. Forward-logged fills on the actual spread structure would also reopen it (60+ sessions, spanning at least one VIX > 25 episode).
