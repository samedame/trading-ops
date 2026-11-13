# Runbook for the 90-day measurement window

Window: 2026-11-05 to 2027-02-03 (`python -m trading_ops freeze status`).

## One-time setup (about 30 minutes)

1. `pip install -r requirements.txt` (only needed for the snapshotter; the rest is standard library).
2. `export TRADING_OPS_DB=~/trading-ops/ledger.db` and `python -m trading_ops init`.
3. **Set `ACCOUNT_CAPITAL_USD`** to the capital you are allocating. This is yours to choose; nothing here guesses it. Also set the `ACCOUNT_CAPITAL_FOR_SIZING` constant in the options scanner's `daily_alert.py` and `ACCOUNT_CAPITAL_USD` in tier1-bot to the same number so their alerts stop sizing to zero.
4. Check feasibility before you trust any alert:
   - `python -m trading_ops size spread --width 5 --credit 1.00 --risk-pct 0.02`
   - `python -m trading_ops size csp --strike 725 --credit 0.15`
   - `python -m trading_ops size shares --price 755 --weight 0.5` (add `--fractional` if your broker allows it)
5. Fill in the `SET BEFORE FIRST TRADE` cells in `docs/GATES.md` and the null benchmarks in `config/bots.json`.

## Tradier token (optional, improves the snapshots)

Get the token from web.tradier.com: your name (top right) -> API Access -> Production Account Access. It gives full access to the account, so never paste it into chat or commit it. Then, from this repo on your own machine with `gh` logged in:

```
python scripts/set_tradier_secret.py            # dry run: shows which repos reference a Tradier secret
python scripts/set_tradier_secret.py --apply    # hidden prompt for the token, verifies it, sets it in each repo
```

The script sets the secret under whatever name each repo's workflows use (for example `TRADIER_TOKEN` or `TRADIER_API_KEY`). The snapshot workflow here uses Tradier automatically when `TRADIER_TOKEN` exists and falls back to yfinance when it doesn't. A repo that reads Tradier credentials some other way is not detected: use `--repos name --secret-name NAME`.

## Every trading day (5 minutes)

- Each alert you see: `signal --bot <name> --symbol SPY --grade <n>`.
- If you take it (or paper it): `open --bot <name> --mode paper|shadow|live ...`, then `fill ... --ref <alert price> --price <what you got>` for each leg, then `close --trade-id <n>`. For a short that expires worthless, log a closing buy at price 0.
- Chain snapshots come from options-data-journal (the fleet's one snapshotter since 2026-11-13). `python -m trading_ops snapshot --symbols SPY,QQQ,IWM --slot close` still works by hand when you want the H8 ladder.
- Each snapshot run also lets you read the H8 ladder: `ladder --date YYYY-MM-DD`.

## Every Sunday (10 minutes)

- `python -m trading_ops import-ibs` first: it loads the IBS bot's closed trades (valued at the next open, in bp) so the scorecard has something to judge. Safe to re-run.
- `python -m trading_ops scorecard`. Read the status column, not the means.
- `python -m trading_ops slippage`. If average slippage per fill is more than a third of the edge you are claiming, the edge is gone.
- Write one line per bot in your notes: status, n, anything flagged.

## What the statuses mean and what to do

| Status | Meaning | Action |
|---|---|---|
| INSUFFICIENT DATA | Fewer closed trades than the minimum | Keep collecting. No verdict is allowed. |
| ON TRACK | 90% CI of the mean is above zero and consistent with the haircut backtest | Eligible for the next gate in GATES.md, not automatically funded. |
| INCONCLUSIVE | CI straddles zero or the expected value | Keep collecting. |
| UNDERPERFORMING | CI sits below the expected value | Stop adding capital; review at the next Sunday. |
| PAUSE | A pre-registered loss floor was hit | Stop trading it. Do not restart without a written reason. |
| RETIRE | Pre-registered retirement rule fired | Turn it off. |
| BLOCKED | A known defect or verdict forbids funding | Fix the cause first. |

## Rules for the freeze

See `config/freeze.json`. If you must break it: `python -m trading_ops freeze exception --reason "..." --reverse-if "..."`. The log is your audit trail.

## Known limits

- The snapshotter on yfinance returns delayed data and zero bid/ask outside market hours. Mid is stored as empty rather than faked. For the H8 ladder, prefer running at the close with Tradier if you have a token (`--provider tradier`, token in `TRADIER_TOKEN`; the Tradier parser is tested against a sample payload but not against the live API).
- The scorecard judges whatever P&L you log. Logging alert prices instead of fills makes every strategy look better than it is.
- A passing statistic on 30 trades is weak evidence. The thresholds are minimums, not guarantees.
