# Verdict: range-forecast sizing on Apex Master v2 — NULL (2026-11-06)

Pre-registration: `PREREG.md` (sha256 in `PREREG.sha256`, written before any result). Code: `ab.py`. Raw output: `results.json`. Research only: nothing in `trading_ops/` or any bot was changed, in line with `docs/FREEZE_EXCEPTIONS.md`.

## Results
| Check | Result | Bar | Pass |
|---|---|---|---|
| Reproduce the v2 trade series (SPY/QQQ/IWM, VIX >= 20, IBS < 0.10, open to close, 2 bp) | n = 734, mean +18.19 bp, 1996-07-16 to 2026-11-03 | within 5% of 734 and 3 bp of 18.19 | yes, exact |
| Forecast accuracy vs "tomorrow equals today", out of sample 2012 on | SPY +20.0%, QQQ +20.5%, IWM +20.9% lower mean range error | >= 15% each | yes |
| 80% band coverage | SPY 78.9%, QQQ 77.0%, IWM 80.0% | 76-86% | yes |
| Information-ratio change, range-forecast sizing (A) vs equal size, 542 trades with a forecast (1999-08 on) | +0.0206 (baseline 0.1375), 95% CI -0.0069 to +0.0479 | CI lower bound > 0 | **no** |
| Control, VIX-only sizing (C) | -0.0076, CI -0.0346 to +0.0192 | n/a | n/a |
| A minus C | +0.0282, CI +0.0174 to +0.0387 | CI lower bound > 0 | yes |

**Decision: NULL.** The primary bar (A improves risk-adjusted return with a CI above zero) failed, so the module is not merged and nothing is proposed for the bot.

## Reading
- The forecaster is genuinely better than naive on all three ETFs; that part of the creator's claim holds.
- As a position-size scaler on v2 it adds about +0.02 to a per-trade IR of 0.14 (a 15% relative gain), but the CI includes zero, mean bp per unit of weight falls slightly (20.9 vs 21.5 bp), and max drawdown per unit of exposure is a little worse (-2,066 vs -1,929 bp).
- It beats VIX-only scaling, which is slightly negative, matching Addendum 3's null for VIX-scaled sizing. That says the range features carry some information beyond VIX, not that scaling v2 helps.
- By year the result is mixed (better in 2012, 2014, 2020-21, 2026; worse in 2015, 2018-19, 2024-25). Since 2015: dIR_A +0.029, dIR_C +0.004 (descriptive).

## Limits
Yahoo daily bars. Long open-to-close with no stop, so this tests size only: stop-width scaling needs intraday data and was not tested. 192 early trades lack a forecast (1,500-day warm-up), ~700 trades are concentrated in a few crises, so power is low: an improvement of up to +0.05 IR cannot be ruled out.

## What would change this
Intraday (5-minute) bars to test stop-width scaling on the intraday bots, or the bots' own forward-test trades once n >= 40, after the freeze ends on 2027-02-03.
