"""Live-vs-backtest scorecard and kill-switch checks.

Reads closed trades from the ledger and judges each bot against the rules written in config/bots.json.
Every rule in the config is copied from a verdict document in your project (see each bot's `source`), so the
scorecard enforces thresholds you already committed to rather than inventing new ones.

It never sizes or places anything. Statuses, in priority order:
  BLOCKED, PAUSE, RETIRE, INSUFFICIENT DATA, UNDERPERFORMING, ON TRACK, INCONCLUSIVE
"""
from __future__ import annotations

import json
import random
import statistics
from datetime import date, datetime, timezone
from typing import Optional

from . import ledger


def summarize(pnls: list[float]) -> dict:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "profit_factor": None,
                "cum": 0.0, "max_drawdown": 0.0, "avg_win": None, "avg_loss": None,
                "breakeven_win_rate": None, "expectancy_R": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = abs(sum(losses))
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None   # magnitude, positive
    # Win rate at which this payoff shape breaks even: avg_loss / (avg_win + avg_loss). Compare it with win_rate.
    breakeven = (avg_loss / (avg_win + avg_loss)) if (avg_win is not None and avg_loss is not None) else None
    # Expectancy in units of the average losing trade ("R"): mean P&L / avg_loss. Undefined with no losing trade.
    expectancy_r = ((sum(pnls) / n) / avg_loss) if avg_loss else None
    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {"n": n, "mean": sum(pnls) / n, "median": statistics.median(pnls), "win_rate": len(wins) / n,
            "profit_factor": (sum(wins) / gross_loss) if gross_loss else None, "cum": cum, "max_drawdown": max_dd,
            "avg_win": avg_win, "avg_loss": avg_loss, "breakeven_win_rate": breakeven, "expectancy_R": expectancy_r}


def bootstrap_ci(pnls: list[float], draws: int = 5000, seed: int = 20261104, level: float = 0.90):
    """Percentile bootstrap CI of the mean. Seeded so a re-run gives the same answer."""
    if len(pnls) < 2:
        return None
    rng = random.Random(seed)
    n = len(pnls)
    means = sorted(sum(pnls[rng.randrange(n)] for _ in range(n)) / n for _ in range(draws))
    lo = means[int(((1 - level) / 2) * draws)]
    hi = means[min(draws - 1, int((1 - (1 - level) / 2) * draws))]
    return lo, hi


def evaluate(name: str, cfg: dict, trades: list, glob: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    pnls = [t["pnl"] for t in trades]
    s = summarize(pnls)
    ci = bootstrap_ci(pnls, glob.get("bootstrap_draws", 5000), glob.get("seed", 20261104)) if pnls else None
    min_n = cfg.get("min_n", glob.get("min_n", 30))
    bt = cfg.get("backtest_mean")
    expected = None if bt is None else bt * (1 - cfg.get("haircut", 0.0))
    reasons: list[str] = []
    flags: list[str] = []

    status = None
    if cfg.get("blocked"):
        status, _ = "BLOCKED", reasons.append(cfg["blocked"])
    if status is None and cfg.get("pause_cum_floor") is not None and s["cum"] <= cfg["pause_cum_floor"]:
        status = "PAUSE"
        reasons.append(f"cumulative {s['cum']:.2f} {cfg.get('unit', '')} is at or below the pre-registered floor "
                       f"{cfg['pause_cum_floor']}")
    if status is None and cfg.get("max_dd_floor") is not None and s["max_drawdown"] >= abs(cfg["max_dd_floor"]):
        status = "PAUSE"
        reasons.append(f"max drawdown {s['max_drawdown']:.2f} breaches the pre-registered bar {cfg['max_dd_floor']}")
    if status is None and cfg.get("retire_at_n") and s["n"] >= cfg["retire_at_n"] \
            and s["mean"] is not None and s["mean"] <= cfg.get("retire_if_mean_le", 0):
        status = "RETIRE"
        reasons.append(f"n={s['n']} >= {cfg['retire_at_n']} and mean {s['mean']:.2f} <= {cfg.get('retire_if_mean_le', 0)}")
    if status is None and s["n"] < min_n:
        status = "INSUFFICIENT DATA"
        reasons.append(f"{s['n']} of {min_n} closed trades; no verdict allowed yet")
    if status is None:
        lo, hi = ci if ci else (None, None)
        if lo is not None and ((expected is None and hi < 0) or (expected is not None and hi < expected)):
            status = "UNDERPERFORMING"
            reasons.append(f"90% CI of mean [{lo:.2f}, {hi:.2f}] lies below the expected {expected if expected is not None else 0:.2f}")
        elif lo is not None and lo > 0 and (expected is None or hi >= expected):
            status = "ON TRACK"
            reasons.append(f"90% CI of mean [{lo:.2f}, {hi:.2f}] is above zero and consistent with the expected "
                           f"{expected if expected is not None else 'n/a'}")
        else:
            status = "INCONCLUSIVE"
            reasons.append("CI straddles zero or the expected value; keep collecting")

    rv = cfg.get("review_after")
    if rv and today >= date.fromisoformat(rv):
        flags.append(f"REVIEW DUE ({rv}): {cfg.get('review_note', '')}".strip())
    return {"bot": name, "status": status, "reasons": reasons, "flags": flags, "unit": cfg.get("unit"),
            "stats": s, "ci90": ci, "expected_mean_after_haircut": expected, "backtest_mean": bt,
            "source": cfg.get("source"), "notes": cfg.get("notes")}


def run(conn, config: dict, today: Optional[date] = None) -> dict:
    glob = config.get("global", {})
    today = today or date.today()
    results = []
    for name, cfg in config["bots"].items():
        trades = ledger.closed_trades(conn, name)
        res = evaluate(name, cfg, trades, glob, today)
        res["modes"] = sorted({t["mode"] for t in trades})
        last = conn.execute("SELECT MAX(ts) AS t FROM signals WHERE bot=?", (name,)).fetchone()["t"]
        res["last_signal"] = last
        stale = cfg.get("stale_days")
        if stale and last:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).days
            if age > stale:
                res["flags"].append(f"STALE: no signal logged for {age} days (limit {stale}); check the bot is running")
        res["slippage"] = ledger.slippage_summary(conn, name)
        res["cost_vs_prior"] = ledger.cost_vs_prior(conn, name)
        results.append(res)

    usd = sorted((t for name in config["bots"] for t in ledger.closed_trades(conn, name) if t["pnl_unit"] == "usd"),
                 key=lambda t: (t["closed_at"], t["id"]))
    combined = summarize([t["pnl"] for t in usd])
    floor = glob.get("combined_dd_floor_usd")
    combined["breach"] = bool(floor and combined["max_drawdown"] >= abs(floor))
    return {"as_of": today.isoformat(), "results": results, "combined_usd": combined, "combined_floor": floor}


def _fmt(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_markdown(report: dict) -> str:
    lines = [f"# Bot scorecard, as of {report['as_of']}", "",
             "| Bot | Status | n | Mean | 90% CI of mean | Expected (after haircut) | Win | Break-even win | Expectancy (R) | Cum | Max DD | Unit |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        s, ci = r["stats"], r["ci90"]
        ci_txt = "n/a" if not ci else "[%.2f, %.2f]" % ci
        win_txt = "n/a" if s["win_rate"] is None else "%.0f%%" % (100 * s["win_rate"])
        be = s.get("breakeven_win_rate")
        be_txt = "n/a" if be is None else "%.0f%%" % (100 * be)
        lines.append(f"| {r['bot']} | **{r['status']}** | {s['n']} | {_fmt(s['mean'])} | {ci_txt} | "
                     f"{_fmt(r['expected_mean_after_haircut'])} | {win_txt} | {be_txt} | {_fmt(s.get('expectancy_R'))} | "
                     f"{_fmt(s['cum'])} | {_fmt(s['max_drawdown'])} | {r['unit'] or 'unset'} |")
    lines += ["", "## Why each status", ""]
    for r in report["results"]:
        lines.append(f"- **{r['bot']}**: " + "; ".join(r["reasons"]))
        for f in r["flags"]:
            lines.append(f"  - FLAG: {f}")
        for sl in r["slippage"]:
            lines.append(f"  - slippage ({sl['action']}, n={sl['n']}): avg {sl['avg_per_unit']:.4f}/unit, "
                         f"total ${sl['total_usd']:.2f}")
        for cp in r.get("cost_vs_prior", []):
            lines.append(f"  - cost vs prior ({cp['structure']} {cp['action']}, n={cp['n']}): actual ${cp['avg_actual_cost_per_lot']:.2f}/lot "
                         f"vs prior ${cp['avg_expected_cost_per_lot']:.2f}/lot (excess {cp['avg_excess_vs_prior_per_lot']:+.2f}; "
                         f"{100 * cp['share_worse_than_prior']:.0f}% of fills worse than the prior)")
    c = report["combined_usd"]
    lines += ["", "## Combined USD book", "",
              f"{c['n']} closed USD trades, cumulative ${c['cum']:.2f}, max drawdown ${c['max_drawdown']:.2f}"
              + (f" (floor ${abs(report['combined_floor']):.2f}: **BREACHED**)" if c["breach"] else "")]
    lines += ["", "Statuses apply the pre-registered rules in config/bots.json. Backtest numbers are not guarantees; "
                  "this is not investment advice."]
    return "\n".join(lines)
