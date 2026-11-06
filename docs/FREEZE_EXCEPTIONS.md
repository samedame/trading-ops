# Freeze exceptions (committed audit trail)

The ledger database is gitignored, so exceptions are also recorded here. Format follows `python -m trading_ops freeze exception`.

## 2026-11-06: range-forecast sizing A/B (research only)

- **Reason:** Four Instagram creator reviews produced one replicable tool: a next-day range forecaster (walk-forward HAR-X, 22.5% lower range error than "tomorrow equals today" on the Nasdaq-100). This exception covers one offline, pre-registered A/B of range-forecast position scaling on the Apex Master v2 trade series, in `research/range_forecast/` on a branch. It touches no bot, no parameter, no sizing, no forward test, and nothing is wired into any live path.
- **Reverse if / stop condition:** If the A/B fails its pre-registered bar (see `research/range_forecast/PREREG.md`), the work stays as a documented null result and nothing is merged into `trading_ops/`. If it passes, merging the module or applying it to any bot still requires a separate logged exception or waiting until the freeze ends on 2027-02-03.
- **Prior evidence acknowledged:** Apex Master v2 Addendum 3 (2026-11-04) found VIX-proportional sizing null (change in information ratio +0.008 and -0.012, CIs include 0). This test asks whether a range forecast adds anything beyond VIX scaling.
