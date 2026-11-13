# Redundancy map and consolidation

The goal is fewer things to monitor, not more. Every row names what to keep, what to park, and why.

## Short-premium cluster (all short the same SPY/QQQ premium)

| Bot | Decision | Reason |
|---|---|---|
| Premium sleeve (`samedame/Premium-Sleeve`) | **Keep as the only short-premium vehicle** | Already replaces options scanner S1, S2, spy-bps-bot and the condor leg. One position is easier to size and measure than four. |
| Options scanner S1/S2, spy-bps-bot | **Park (alerts off)** | Superseded by the sleeve. Your 2026-09-12 test showed the RSI entry timing added nothing over random days; the edge is the premium itself. |
| Condor (`daytrading_bot`) | **Paper only; merge PR #3** | Economics fail the real-credit check; fixing reliability does not fix the economics. |
| tier1-bot (H8) | **Do not fund** | Strike bug (1-delta put); needs roughly $23k-$72k of capital per contract. |
| Vol-regime gate | **Demote to a risk preference** | Audited Tier D/E; gate (a) harmful. Do not describe it as an edge. |

## Intraday cluster

| Bot | Decision | Reason |
|---|---|---|
| Apex Chronos / Master v1 | **Paper only; no new modules** | 108-rule scan found 0 survivors after Holm correction; v1's live rule is indistinguishable from picking at random. |
| Master v2 | **Keep as the one Apex line** | Daily-bar rule with 33 years of data and a written forward test. It fires about 24 times a year. |

## Daily-bar cluster

| Bot | Decision | Reason |
|---|---|---|
| IBS callout bot | **Keep (core)** | Only strategy to pass every gate. |
| Momentum sleeve | **Keep in shadow** | Review due 2027-09-15. |
| FRONTIER-5 | **Keep in paper; give it a benchmark** | Add its verdict numbers to `config/bots.json`. |

## Meta layers

| Layer | Decision | Reason |
|---|---|---|
| Bot selector | **Log only** | No demonstrated value yet; its own build note says so. |
| Regime engine (daily 1-100 score) | **Keep as a morning read, not a trade trigger** | Judge it by the same scorecard once its scores are logged as signals. |
| BotDeck dashboard | **Keep; add the scorecard JSON** | `python -m trading_ops scorecard --json` is built to be read by it. |

## Data tooling

| Tool | Decision | Reason |
|---|---|---|
| options-data-journal snapshots | **Keep as the only daily chain snapshotter** | Two slots a day, idempotent per ticker/date/slot, schema-checked, and it carries the fill journal. |
| trading-ops `snapshot.yml` | **Manual only since 2026-11-13** | It stored the same SPY/QQQ/IWM chains a second time. Kept as a manual run for the H8 put ladder. |

Outcome: from every bot above to **4 active measurements** (IBS, Master v2, premium sleeve in paper, momentum in shadow) plus one data snapshotter (options-data-journal).
