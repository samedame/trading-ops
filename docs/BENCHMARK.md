# Benchmark ledger: are you actually beating the S&P 500?

Roadmap item R3. **Gate (frozen 2026-11-16): no "beating SPY" claim unless your excess over a shadow VOO is positive after fees and tax.** Nothing in this project may use that sentence until `python -m trading_ops benchmark` says `CLAIM_PERMITTED_UNPROVEN` or `CLAIM_SUPPORTED`.

## Keep your numbers out of git

This repo is public. Put your account files in `ledger/benchmark/` (git-ignored). The empty templates are in `templates/benchmark/`.

## The four files

| File | Columns | What goes in it |
|---|---|---|
| `flows.csv` | `date,amount,note` | Money you moved into (+) or out of (-) the account. Not trades, not dividends. |
| `equity.csv` | `date,equity` | End-of-day account value, net of every fee the broker charged. One row per day is best; weekly works; monthly is the minimum. The first row must be on or before your first deposit (use 0 for an empty account). |
| `costs.csv` (optional) | `date,amount,note` | Money spent OUTSIDE the account to run it: data subscriptions, hosting, a Robinhood Gold fee paid from your bank. Positive numbers. |
| `realized.csv` | `date,short_term,long_term` | Realised gains or losses in dollars, net of the fees on those trades (your broker's year-end 1099-B or monthly statement). **Without this file tax is unknown and the claim stays closed.** |

## Run it

```
python scripts/fetch_benchmark_prices.py              # on your own computer; Yahoo refuses most cloud IPs
python -m trading_ops benchmark                       # markdown report
python -m trading_ops benchmark --json                # same, machine-readable
python -m trading_ops benchmark --st-rate 0.24 --lt-rate 0.15
```

`scripts/fetch_benchmark_prices.py --allow-proxy` falls back to SPY (plus 6.45 bp a year to offset its higher fee) if Yahoo fails. The report prints which source it used.

## What the report means

* **Time-weighted return** is how your investments did, with the timing of your deposits removed. It is the fair comparison of skill.
* **Ending value and IRR** are what you actually have versus what the same dollars, deposited on the same dates, would be worth in VOO. The gate uses this, because it is the question that matters to you.
* **Costs** paid outside the account are treated as extra money the shadow also invests: the honest alternative to running bots is to put those dollars in VOO.
* **T-bill line**: whether you even beat cash.
* **Tax** is an estimate: both sides are sold on the last day; the shadow's lots are short-term at 365 days or less; your realised gains come from `realized.csv` and the rest of your gain is treated as short-term. One netting for the whole window, no loss carry-forward, no state tax. It will be replaced by the R9 tax module, and it errs against making the claim.

## Labels

| Label | Meaning |
|---|---|
| `NO_CLAIM_TAX_UNKNOWN` | No `realized.csv`. |
| `NO_CLAIM_BEHIND_VOO` | Ending wealth after fees and tax is not above the shadow's (a difference under half a cent counts as a tie). |
| `CLAIM_PERMITTED_UNPROVEN` | Ahead after fees and tax, which meets the frozen gate, but fewer than 24 month-end points or the 95% lower bound on the monthly excess is not above zero. A lucky quarter looks like this. |
| `CLAIM_SUPPORTED` | The above, plus 24 or more month-end points and a positive 95% lower bound (circular block bootstrap, block 3, 5,000 draws, seed 20261117). |

Roadmap reality check: at a 10% tracking error, detecting a true 3% a year edge takes about 69 years of live data (the report prints this from your own tracking error). Treat `CLAIM_PERMITTED_UNPROVEN` as "not behind", never as "proven".

## Conventions

End-of-day equity; a flow happens at the close of its date; the shadow trades at the last close on or before the flow date in fractional units; interval returns use Modified Dietz (effectively exact with daily rows); 365-day years; annualised numbers under one year are flagged as not evidence.

## Limits

Single price vendor. No dividends beyond what adjusted close carries. No loss carry-forward or state tax. Margin interest is only in the account's own equity if the broker charges it there. If your broker's equity row includes unsettled or pending funds, the first and last rows may be off by that amount.
