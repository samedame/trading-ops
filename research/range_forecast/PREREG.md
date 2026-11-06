# Pre-registration: does a next-day range forecast improve Apex Master v2 sizing?

Written and hashed 2026-11-06 BEFORE any trade-level result was computed. Research only; see docs/FREEZE_EXCEPTIONS.md.

## Question
@deltatrendtrading (Instagram) claims a walk-forward range forecast lets you "size stops and positions" better. A reproduction on the Nasdaq-100 found the forecast beats "tomorrow equals today" by 22.5% on mean range error. Does using that forecast to scale position size improve the risk-adjusted result of a real bot rule, beyond what scaling by VIX alone gives?

## Bot rule under test (Apex Master v2, from claude/apex-master-v2-regime-ibs-verdict-2026-11-04.md)
Previous VIX close >= 20 AND previous-day IBS < 0.10, IBS = (C-L)/(H-L). First of SPY, QQQ, IWM (that order) meeting IBS. Long at the open, exit at the close, one trade per day. Net of 2 bp round trip. Daily bars from Yahoo (unadjusted OHLC), ^VIX close.
**Reproduction gate (stop if it fails):** trade count within +-5% of 734 and mean net within +-3 bp of +18.19 using data through 2026-11-05. If it fails, report that and do no A/B.

## Forecaster (fixed)
Per ticker (SPY, QQQ, IWM): target y = log((H-L)/previous close) of the NEXT day. Features at day t: log range_t, log of the 5-day mean range, log of the 22-day mean range, log VIX_t. OLS refit every 21 trading days on an expanding window (minimum 1,500 days), one-step forecast F = exp(fitted) (median). VIX is used for all three tickers so the model differs from the control only by the range features.

## Weights (fixed; inverse-risk scaling, clipped to [0.5, 2])
- Baseline: w = 1.
- A (range forecast): w = clip(M_F / F_D, 0.5, 2), F_D the forecast for the trade day D made at the prior close, M_F the trailing 252-day median of forecasts available before D.
- C (control, VIX only): w = clip(M_V / VIX_{D-1}, 0.5, 2), M_V the trailing 252-day median of VIX through D-1.
Only trades with a forecast available are compared, and all three variants use the same trades.

## Statistics
Per-trade information ratio IR = mean(w*r) / sd(w*r), r the net return in bp. dIR_A = IR(A) - IR(baseline), dIR_C likewise. 10,000 iid bootstrap draws over trades, seed 20261106, 95% percentile CI for dIR_A, dIR_C and dIR_A - dIR_C. Also reported (no pass bar): max drawdown of the weighted cumulative bp divided by mean weight, mean bp per unit of average weight, worst year, and the 2015-2026 subset.

## Forecaster accuracy gate
Out of sample from 2012-01-01 to 2026-11-05, per ticker: mean absolute range error at least 15% below the naive "tomorrow equals today" forecast, and an 80% band (exp(fit +- 1.2816 * training residual sd)) coverage in [76%, 86%].

## Decision rule
PASS (module may be proposed for review after the freeze, or via a separate logged exception) only if ALL hold: reproduction gate; accuracy gate on all three tickers; 95% CI of dIR_A has a lower bound above 0; dIR_A > dIR_C with the 95% CI of dIR_A - dIR_C lower bound above 0.
Otherwise the result is NULL, it is documented, and nothing is merged into trading_ops/. Under any outcome nothing is wired into a bot.

## Known limits (stated now)
Yahoo daily bars, not the bot's feed. Open-to-close long with no stop, so this tests size scaling only; it cannot test stop widening (needs intraday data). Prior evidence: Addendum 3 found VIX-proportional sizing null (+0.008 / -0.012 information-ratio change), so a null here is the expected outcome and would confirm it. One bot rule, ~700 trades concentrated in a few crises (VIX >= 25 gives 69.5% of profit), so power is low.
