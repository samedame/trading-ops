"""Benchmark ledger (roadmap item R3): your real account against a shadow VOO fed the same money.

Gate this module enforces (frozen in the 2026-11-16 research roadmap): do not say you are beating the S&P 500 unless
the excess over a shadow VOO is positive AFTER fees and tax. Nothing else in this project may use that sentence.

What it measures
  * Time-weighted return (TWR): how the account's investments did, independent of when you added money.
  * Money-weighted return (IRR) and ending wealth: what you actually ended up with versus the same dollars put
    in VOO on the same dates. This is the one the gate uses.
  * A T-bill shadow for the same dollars, so you can see whether the account even beat cash.
  * External costs (data subscriptions, hosting, anything paid from outside the account) as extra outlays. The shadow
    invests that money too, because the honest alternative to running bots is to put those dollars in VOO.
  * A tax estimate (see TAX below). If it cannot be computed the gate says so and refuses the claim.

Conventions (all stated so they can be checked)
  * Equity rows are END-OF-DAY account values, net of every fee charged inside the account.
  * A flow dated D happens at the close of D. A deposit is positive, a withdrawal negative.
  * The shadow trades at the last close on or before the flow date (fractional units).
  * Interval returns use Modified Dietz with those flow weights; daily equity rows make that effectively exact.
  * Annualised figures use 365-day years. Under a year of history they are shown but flagged: they are not evidence.

TAX (an approximation, not the R9 tax module)
  * Both sides are valued as if everything were sold on the last day. Shadow lots are short-term if held 365 days or
    less, long-term otherwise. The account's realised gains come from realized.csv; whatever is left of its total gain
    is treated as short-term unrealised. One netting across the whole window, no loss carry-forward, no $3,000
    ordinary-income offset, no state tax. If realized.csv is absent the account side is UNKNOWN and no claim is allowed.

Standard library only.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

LT_DAYS = 365
CLAIM_MIN_MONTHS = 24            # statistical claim needs two years of month-end data
BOOTSTRAP_DRAWS = 5000
BOOTSTRAP_BLOCK = 3
BOOTSTRAP_SEED = 20261117
Z_ALPHA = 1.645                  # one-sided 5%
Z_POWER = 0.84                   # 80% power
EPS_USD = 0.005                  # half a cent: anything smaller is rounding noise, not an edge


class BenchmarkError(ValueError):
    """Bad or inconsistent input. The message says which row or file."""


# ----------------------------------------------------------------------------------------------------- loading
def _d(s: str, where: str) -> date:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise BenchmarkError(f"{where}: bad date {s!r} (use YYYY-MM-DD)") from exc


def _f(s: str, where: str) -> float:
    try:
        v = float(str(s).strip().replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise BenchmarkError(f"{where}: bad number {s!r}") from exc
    if math.isnan(v) or math.isinf(v):
        raise BenchmarkError(f"{where}: number is not finite")
    return v


def _rows(path, required: Iterable[str]) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise BenchmarkError(f"{p}: file not found")
    with p.open(newline="", encoding="utf-8") as fh:
        rdr = csv.DictReader(fh)
        names = [(n or "").strip().lower() for n in (rdr.fieldnames or [])]
        missing = [c for c in required if c not in names]
        if missing:
            raise BenchmarkError(f"{p}: missing column(s) {', '.join(missing)} (found {', '.join(names) or 'none'})")
        out = []
        for i, raw in enumerate(rdr, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            row["_where"] = f"{p.name} line {i}"
            out.append(row)
        return out


def load_flows(path) -> list[tuple[date, float]]:
    """flows.csv: date,amount[,note]. Deposit positive, withdrawal negative. Several rows per day are summed."""
    byday: dict[date, float] = {}
    for r in _rows(path, ["date", "amount"]):
        amt = _f(r["amount"], r["_where"])
        if amt == 0:
            raise BenchmarkError(f"{r['_where']}: a flow of 0 is not a flow; delete the row")
        d = _d(r["date"], r["_where"])
        byday[d] = byday.get(d, 0.0) + amt
    return sorted((d, a) for d, a in byday.items() if abs(a) > 1e-12)


def load_costs(path) -> list[tuple[date, float]]:
    """costs.csv: date,amount[,note]. Money you spent OUTSIDE the account to run it. Positive numbers only."""
    byday: dict[date, float] = {}
    for r in _rows(path, ["date", "amount"]):
        amt = _f(r["amount"], r["_where"])
        if amt <= 0:
            raise BenchmarkError(f"{r['_where']}: a cost must be a positive amount")
        d = _d(r["date"], r["_where"])
        byday[d] = byday.get(d, 0.0) + amt
    return sorted(byday.items())


def load_equity(path) -> list[tuple[date, float]]:
    """equity.csv: date,equity. End-of-day account value, one row per date, oldest first or not."""
    seen: dict[date, float] = {}
    for r in _rows(path, ["date", "equity"]):
        d = _d(r["date"], r["_where"])
        v = _f(r["equity"], r["_where"])
        if v < 0:
            raise BenchmarkError(f"{r['_where']}: equity cannot be negative")
        if d in seen:
            raise BenchmarkError(f"{r['_where']}: two equity rows for {d}")
        seen[d] = v
    return sorted(seen.items())


def load_realized(path) -> list[tuple[date, float, float]]:
    """realized.csv: date,short_term,long_term (realised gain or loss in dollars, net of the fees on those trades)."""
    out = []
    for r in _rows(path, ["date", "short_term", "long_term"]):
        out.append((_d(r["date"], r["_where"]), _f(r["short_term"] or "0", r["_where"]),
                    _f(r["long_term"] or "0", r["_where"])))
    return sorted(out)


class Series:
    """Daily closes with forward-fill. `uplift_bp` adds a steady annual drift (used only for the SPY proxy)."""

    def __init__(self, points: dict[date, float], uplift_bp: float = 0.0):
        if not points:
            raise BenchmarkError("price series is empty")
        self.dates = sorted(points)
        d0 = self.dates[0]
        up = 1.0 + uplift_bp / 1e4
        self.vals = [points[d] * (up ** ((d - d0).days / 365.0)) for d in self.dates]
        if any(v <= 0 for v in self.vals):
            raise BenchmarkError("price series has a non-positive price")

    @property
    def first(self) -> date:
        return self.dates[0]

    @property
    def last(self) -> date:
        return self.dates[-1]

    def asof(self, d: date) -> float:
        i = bisect.bisect_right(self.dates, d) - 1
        if i < 0:
            raise BenchmarkError(f"no price on or before {d} (series starts {self.first})")
        if d > self.last + timedelta(days=10):
            raise BenchmarkError(f"no price near {d} (series ends {self.last}); refresh the price file")
        return self.vals[i]


def load_prices(path, uplift_bp: float = 0.0) -> Series:
    """prices csv with a `date` column and one of adjclose / adj_close / close."""
    p = Path(path)
    if not p.exists():
        raise BenchmarkError(f"{p}: file not found (run scripts/fetch_benchmark_prices.py)")
    with p.open(newline="", encoding="utf-8") as fh:
        rdr = csv.DictReader(fh)
        names = [(n or "").strip().lower() for n in (rdr.fieldnames or [])]
        col = next((c for c in ("adjclose", "adj_close", "close") if c in names), None)
        if "date" not in names or col is None:
            raise BenchmarkError(f"{p}: needs a date column and adjclose (or adj_close or close)")
        pts: dict[date, float] = {}
        for i, raw in enumerate(rdr, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not row.get("date") or not row.get(col):
                continue
            pts[_d(row["date"], f"{p.name} line {i}")] = _f(row[col], f"{p.name} line {i}")
    return Series(pts, uplift_bp)


def load_rates(path) -> dict[date, float]:
    """Annual bill yield in percent by date: a `date` column plus value / dtb3 / close / adjclose. Blank rows skipped."""
    p = Path(path)
    if not p.exists():
        raise BenchmarkError(f"{p}: file not found")
    out: dict[date, float] = {}
    with p.open(newline="", encoding="utf-8") as fh:
        rdr = csv.DictReader(fh)
        names = [(n or "").strip().lower() for n in (rdr.fieldnames or [])]
        col = next((c for c in ("dtb3", "value", "adjclose", "close") if c in names), None)
        dcol = "date" if "date" in names else ("observation_date" if "observation_date" in names else None)
        if col is None or dcol is None:
            raise BenchmarkError(f"{p}: needs a date column and one of dtb3 / value / adjclose / close")
        for i, raw in enumerate(rdr, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if row.get(col) in (None, "", "."):
                continue
            out[_d(row[dcol], f"{p.name} line {i}")] = _f(row[col], f"{p.name} line {i}")
    if not out:
        raise BenchmarkError(f"{p}: no rates")
    return out


# --------------------------------------------------------------------------------------------------- shadow VOO
class Shadow:
    """Fractional-unit shadow holding. Deposits buy at the last close on/before the flow date; withdrawals sell FIFO."""

    def __init__(self, prices: Series):
        self.prices = prices
        self.events: list[tuple[date, float]] = []        # (date, signed units)
        self.lots: list[list] = []                        # [buy_date, units_left, price_paid]
        self.realized_st = 0.0
        self.realized_lt = 0.0

    def apply(self, d: date, amount: float) -> None:
        px = self.prices.asof(d)
        if amount > 0:
            units = amount / px
            self.lots.append([d, units, px])
            self.events.append((d, units))
            return
        need = -amount / px
        held = sum(l[1] for l in self.lots)
        if need > held * (1 + 1e-9):
            raise BenchmarkError(f"shadow VOO cannot fund the {d} withdrawal of ${-amount:,.2f}: it holds "
                                 f"${held * px:,.2f}. Check the flows file.")
        self.events.append((d, -need))
        left = need
        for lot in self.lots:
            if left <= 1e-15:
                break
            take = min(lot[1], left)
            gain = take * (px - lot[2])
            if (d - lot[0]).days > LT_DAYS:
                self.realized_lt += gain
            else:
                self.realized_st += gain
            lot[1] -= take
            left -= take
        self.lots = [l for l in self.lots if l[1] > 1e-15]

    def units_at(self, d: date) -> float:
        return sum(u for ed, u in self.events if ed <= d)

    def value_at(self, d: date) -> float:
        return self.units_at(d) * self.prices.asof(d)

    def unrealized(self, d: date) -> tuple[float, float]:
        """(short-term, long-term) unrealised gain on lots still held at d."""
        px = self.prices.asof(d)
        st = lt = 0.0
        for bd, units, paid in self.lots:
            g = units * (px - paid)
            if (d - bd).days > LT_DAYS:
                lt += g
            else:
                st += g
        return st, lt


def build_shadow(prices: Series, flows: list[tuple[date, float]]) -> Shadow:
    sh = Shadow(prices)
    for d, a in sorted(flows):
        sh.apply(d, a)
    return sh


# --------------------------------------------------------------------------------------------------- returns
def dietz_returns(obs: list[tuple[date, float]], flows: list[tuple[date, float]]) -> tuple[list[float], int]:
    """Chained Modified Dietz over consecutive observations. Returns (interval returns, undefined intervals).

    Flows are at the close of their date, so a flow on the interval's last day has weight 0 and one on the first
    day after the previous observation has weight (L-1)/L.
    """
    rets: list[float] = []
    undefined = 0
    for (t0, e0), (t1, e1) in zip(obs, obs[1:]):
        length = (t1 - t0).days
        inside = [(d, a) for d, a in flows if t0 < d <= t1]
        net = sum(a for _, a in inside)
        weighted = sum(a * (t1 - d).days / length for d, a in inside)
        denom = e0 + weighted
        num = e1 - e0 - net
        if denom <= 1e-12:
            if abs(num) <= 1e-9:
                rets.append(0.0)
            else:
                rets.append(0.0)
                undefined += 1
        else:
            rets.append(num / denom)
    return rets, undefined


def chain(rets: Iterable[float]) -> float:
    g = 1.0
    for r in rets:
        g *= 1.0 + r
    return g - 1.0


def annualize(total: float, days: int) -> Optional[float]:
    if days <= 0 or total <= -1.0:
        return None
    return (1.0 + total) ** (365.0 / days) - 1.0


def xirr(flows: list[tuple[date, float]]) -> Optional[float]:
    """Money-weighted annual rate. `flows`: investor view, outlays negative, money back positive. None if no root."""
    flows = [(d, a) for d, a in flows if abs(a) > 1e-12]
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.999999, 1000.0
    flo, fhi = npv(lo), npv(hi)
    if flo == 0:
        return lo
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if abs(fm) < 1e-10 or (hi - lo) < 1e-13:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def bill_value(rates: dict[date, float], flows: list[tuple[date, float]], end: date) -> float:
    """What the same flows would be worth in T-bills at `end` (daily compounding of the latest known yield)."""
    rd = sorted(rates)
    total = 0.0
    for d, a in flows:
        v, day = a, d
        while day < end:
            i = bisect.bisect_right(rd, day) - 1
            y = rates[rd[i]] if i >= 0 else rates[rd[0]]
            v *= 1.0 + y / 100.0 / 365.0
            day += timedelta(days=1)
        total += v
    return total


def net_gains(st: float, lt: float) -> tuple[float, float]:
    """Standard netting: losses in one bucket offset gains in the other. Returns taxable (short, long) amounts."""
    if st >= 0 and lt >= 0:
        return st, lt
    if st < 0 and lt < 0:
        return 0.0, 0.0
    if st < 0:
        return 0.0, max(0.0, lt + st)
    return max(0.0, st + lt), 0.0


def tax_due(st: float, lt: float, st_rate: float, lt_rate: float) -> float:
    s, l = net_gains(st, lt)
    return s * st_rate + l * lt_rate


def years_to_detect(tracking_error: float, edge: float) -> float:
    """Years of live data to detect a true annual edge at 5% one-sided and 80% power, if excess returns are iid."""
    if edge <= 0 or tracking_error <= 0:
        return math.inf
    return ((Z_ALPHA + Z_POWER) * tracking_error / edge) ** 2


def month_grid(obs: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """First observation, then the last observation of each calendar month."""
    out = [obs[0]]
    for (d, v), nxt in zip(obs[1:], obs[2:] + [None]):
        if nxt is None or (nxt[0].year, nxt[0].month) != (d.year, d.month):
            out.append((d, v))
    dedup, seen = [], set()
    for d, v in out:
        if d not in seen:
            seen.add(d)
            dedup.append((d, v))
    return dedup


def bootstrap_lower(xs: list[float], draws: int = BOOTSTRAP_DRAWS, block: int = BOOTSTRAP_BLOCK,
                    seed: int = BOOTSTRAP_SEED) -> Optional[float]:
    """5th percentile of the circular-block-bootstrap mean (one-sided 95% lower bound). Monthly units in, monthly out."""
    n = len(xs)
    if n < 12:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = []
        while len(pick) < n:
            s = rng.randrange(n)
            pick.extend(xs[(s + k) % n] for k in range(block))
        means.append(sum(pick[:n]) / n)
    means.sort()
    return means[int(0.05 * draws)]


# --------------------------------------------------------------------------------------------------- the report
def gate(excess_after_tax: Optional[float], tax_known: bool, ci_lower_monthly: Optional[float], months: int) -> dict:
    """The frozen roadmap gate, plus a statistical label so a lucky quarter is not mistaken for a result."""
    reasons: list[str] = []
    if not tax_known:
        reasons.append("tax could not be estimated (add realized.csv), so no claim is allowed")
        return {"label": "NO_CLAIM_TAX_UNKNOWN", "claim_permitted": False, "reasons": reasons}
    if excess_after_tax is None or excess_after_tax <= EPS_USD:
        reasons.append("ending wealth after fees and tax is not above the shadow VOO funded with the same dollars")
        return {"label": "NO_CLAIM_BEHIND_VOO", "claim_permitted": False, "reasons": reasons}
    if months >= CLAIM_MIN_MONTHS and ci_lower_monthly is not None and ci_lower_monthly > 0:
        reasons.append("ahead after fees and tax, and the monthly excess has a positive 95% lower bound")
        return {"label": "CLAIM_SUPPORTED", "claim_permitted": True, "reasons": reasons}
    reasons.append("ahead after fees and tax, which meets the roadmap gate")
    if months < CLAIM_MIN_MONTHS:
        reasons.append(f"only {months} month-end points; the statistical read needs {CLAIM_MIN_MONTHS}")
    elif ci_lower_monthly is None or ci_lower_monthly <= 0:
        reasons.append("the 95% lower bound on monthly excess is not above zero: luck cannot be ruled out")
    return {"label": "CLAIM_PERMITTED_UNPROVEN", "claim_permitted": True, "reasons": reasons}


def run(flows: list[tuple[date, float]], equity: list[tuple[date, float]], voo: Series,
        costs: Optional[list[tuple[date, float]]] = None, realized: Optional[list[tuple[date, float, float]]] = None,
        rates: Optional[dict[date, float]] = None, st_rate: float = 0.24, lt_rate: float = 0.15,
        voo_source: str = "VOO") -> dict:
    if not flows:
        raise BenchmarkError("flows is empty: log at least your first deposit")
    if len(equity) < 2:
        raise BenchmarkError("equity needs at least two rows")
    costs = costs or []
    warnings: list[str] = []
    if equity[0][0] > flows[0][0]:
        # nothing before the first equity row can be measured
        raise BenchmarkError(f"first equity row {equity[0][0]} is after the first flow {flows[0][0]}: add an equity row "
                             "on or before your first deposit date (use 0 if the account was empty)")
    if flows[-1][0] > equity[-1][0]:
        raise BenchmarkError(f"a flow on {flows[-1][0]} is dated after the last equity row {equity[-1][0]}")
    start, end = equity[0][0], equity[-1][0]
    days = (end - start).days
    if days <= 0:
        raise BenchmarkError("equity rows span no time")

    # money-in view for the account: deposits and withdrawals only (costs never enter the account)
    sh_dep = build_shadow(voo, flows)
    all_in = sorted(flows + [(d, a) for d, a in costs])
    sh_all = build_shadow(voo, all_in)

    # --- time-weighted returns on every equity row
    acc_obs = equity
    voo_obs = [(d, sh_dep.value_at(d)) for d, _ in equity]
    acc_rets, acc_undef = dietz_returns(acc_obs, flows)
    voo_rets, voo_undef = dietz_returns(voo_obs, flows)
    if acc_undef:
        warnings.append(f"{acc_undef} equity interval(s) had no capital at risk but a non-zero change; counted as 0%")
    twr_a, twr_v = chain(acc_rets), chain(voo_rets)

    # --- money-weighted view, ending wealth
    net_dep = sum(a for _, a in flows)
    cost_total = sum(a for _, a in costs)
    end_equity = equity[-1][1]
    end_voo_all = sh_all.value_at(end)
    end_voo_dep = sh_dep.value_at(end)
    acc_cf = [(d, -a) for d, a in flows] + [(d, -a) for d, a in costs] + [(end, end_equity)]
    voo_cf = [(d, -a) for d, a in all_in] + [(end, end_voo_all)]
    irr_a, irr_v = xirr(acc_cf), xirr(voo_cf)
    excess_usd = end_equity - end_voo_all

    # --- tax
    sh_unreal_st, sh_unreal_lt = sh_all.unrealized(end)
    sh_tax = tax_due(sh_all.realized_st + sh_unreal_st, sh_all.realized_lt + sh_unreal_lt, st_rate, lt_rate)
    tax_known = realized is not None
    acc_tax: Optional[float] = None
    tax_detail: dict = {}
    if tax_known:
        r_st = sum(s for d, s, _ in realized if d <= end)
        r_lt = sum(l for d, _, l in realized if d <= end)
        total_gain = end_equity - net_dep
        unreal = total_gain - (r_st + r_lt)
        acc_tax = tax_due(r_st + unreal, r_lt, st_rate, lt_rate)
        tax_detail = {"realized_short_term": r_st, "realized_long_term": r_lt, "total_gain": total_gain,
                      "unrealized_treated_short_term": unreal}
        if unreal < -1e-6 and abs(unreal) > 0.25 * max(1.0, abs(total_gain)):
            warnings.append("realized.csv gains exceed the account's total gain by a lot: check it covers the same "
                            "dates and is net of fees")
    else:
        warnings.append("no realized.csv: the account's tax is unknown, so the claim gate stays closed")
    excess_after_tax = (end_equity - acc_tax) - (end_voo_all - sh_tax) if tax_known else None

    # --- cash shadow
    bills_end = bill_value(rates, [(d, a) for d, a in all_in], end) if rates else None

    # --- statistics on month-end points
    ga, gv = month_grid(acc_obs), month_grid([(d, sh_dep.value_at(d)) for d, _ in equity])
    ma, _ = dietz_returns(ga, flows)
    mv, _ = dietz_returns(gv, flows)
    ex = [a - v for a, v in zip(ma, mv)]
    months = len(ex)
    stats: dict = {"month_points": months}
    if months >= 2:
        mean = sum(ex) / months
        var = sum((x - mean) ** 2 for x in ex) / (months - 1)
        sd = math.sqrt(var)
        te = sd * math.sqrt(12)
        stats.update({"mean_monthly_excess": mean, "tracking_error_annual": te,
                      "information_ratio": (mean * 12 / te) if te > 0 else None})
        lower = bootstrap_lower(ex)
        stats["bootstrap_lower_95_monthly"] = lower
        stats["bootstrap_lower_95_annualised"] = None if lower is None else (1 + lower) ** 12 - 1
        stats["years_to_detect_3pct_edge"] = years_to_detect(te, 0.03) if te > 0 else None
    else:
        lower = None
    if days < 365:
        warnings.append("under one year of history: annualised numbers are shown but are not evidence of anything")

    verdict = gate(excess_after_tax, tax_known, lower, months)
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "account": {"twr": twr_a, "twr_annualised": annualize(twr_a, days), "end_equity": end_equity,
                    "net_deposits": net_dep, "external_costs": cost_total, "irr": irr_a},
        "voo": {"source": voo_source, "twr": twr_v, "twr_annualised": annualize(twr_v, days),
                "end_value_same_deposits": end_voo_dep, "end_value_deposits_plus_costs": end_voo_all, "irr": irr_v},
        "bills": {"end_value_deposits_plus_costs": bills_end},
        "excess": {"twr_points_annualised": None if None in (annualize(twr_a, days), annualize(twr_v, days))
                   else annualize(twr_a, days) - annualize(twr_v, days),
                   "irr_points": None if None in (irr_a, irr_v) else irr_a - irr_v,
                   "usd_after_fees": excess_usd, "usd_after_fees_and_tax": excess_after_tax},
        "tax": {"status": "estimated" if tax_known else "UNKNOWN", "short_term_rate": st_rate, "long_term_rate": lt_rate,
                "account_tax": acc_tax, "shadow_tax": sh_tax, "account": tax_detail},
        "stats": stats,
        "gate": verdict,
        "warnings": warnings,
    }


# ------------------------------------------------------------------------------------------------------ output
def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _usd(x: Optional[float]) -> str:
    return "n/a" if x is None else (f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}")


def _plain(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


def render_markdown(rep: dict) -> str:
    p, a, v, e, t, s, g = rep["period"], rep["account"], rep["voo"], rep["excess"], rep["tax"], rep["stats"], rep["gate"]
    lines = [
        f"# Benchmark ledger: your account vs shadow VOO ({p['start']} to {p['end']}, {p['days']} days)", "",
        f"**Gate: {g['label']}**. Claim permitted: {'YES' if g['claim_permitted'] else 'NO'}.",
        *[f"- {r}" for r in g["reasons"]], "",
        "| | Your account | Shadow VOO |", "|---|---|---|",
        f"| Time-weighted return | {_pct(a['twr'])} | {_pct(v['twr'])} |",
        f"| Annualised (flag if under 1 year) | {_pct(a['twr_annualised'])} | {_pct(v['twr_annualised'])} |",
        f"| Money-weighted (IRR) | {_pct(a['irr'])} | {_pct(v['irr'])} |",
        f"| Ending value | {_usd(a['end_equity'])} | {_usd(v['end_value_deposits_plus_costs'])} |", "",
        f"Net deposits {_usd(a['net_deposits'])}; costs paid outside the account {_usd(a['external_costs'])} "
        "(the shadow invests those too).", "",
        f"**Excess over shadow VOO:** {_usd(e['usd_after_fees'])} after fees; "
        f"{_usd(e['usd_after_fees_and_tax'])} after fees and tax "
        f"(tax {t['status']}: {t['short_term_rate']:.0%} short-term, {t['long_term_rate']:.0%} long-term, sold on the last day).",
        f"Account tax {_usd(t['account_tax'])}; shadow tax {_usd(t['shadow_tax'])}.", ""]
    b = rep["bills"]["end_value_deposits_plus_costs"]
    if b is not None:
        lines.append(f"T-bills with the same dollars would be worth {_usd(b)}; the account is "
                     f"{_usd(abs(a['end_equity'] - b))} {'above' if a['end_equity'] >= b else 'below'} cash.")
        lines.append("")
    if "mean_monthly_excess" in s:
        lines += [f"Monthly excess over {s['month_points']} points: mean {_pct(s['mean_monthly_excess'])}, tracking error "
                  f"{_plain(s['tracking_error_annual'])} a year"
                  + (f", information ratio {s['information_ratio']:.2f}" if s.get("information_ratio") is not None else "")
                  + ".",
                  ("95% lower bound on the monthly mean: " + _pct(s["bootstrap_lower_95_monthly"])
                   if s.get("bootstrap_lower_95_monthly") is not None else
                   "95% lower bound: needs at least 12 month-end points."),
                  ("At this tracking error, detecting a true 3%/yr edge takes about "
                   f"{s['years_to_detect_3pct_edge']:.0f} years of live data." if s.get("years_to_detect_3pct_edge") else ""),
                  ""]
    if rep["warnings"]:
        lines += ["Warnings:", *[f"- {w}" for w in rep["warnings"]], ""]
    lines.append("Research tooling, not advice. Tax is an approximation: see the module docstring.")
    return "\n".join(x for x in lines if x is not None)


def default_paths(root: Path) -> dict[str, Path]:
    return {"flows": root / "ledger" / "benchmark" / "flows.csv", "equity": root / "ledger" / "benchmark" / "equity.csv",
            "costs": root / "ledger" / "benchmark" / "costs.csv", "realized": root / "ledger" / "benchmark" / "realized.csv",
            "prices": root / "data" / "benchmark" / "voo.csv", "rates": root / "data" / "benchmark" / "dtb3.csv",
            "source": root / "data" / "benchmark" / "SOURCE.json"}


def run_from_files(paths: dict, st_rate: float = 0.24, lt_rate: float = 0.15) -> dict:
    """Load files (costs, realized and rates are optional) and run. `paths` values may be str, Path or None."""
    P = {k: (Path(v) if v else None) for k, v in paths.items()}
    flows, equity = load_flows(P["flows"]), load_equity(P["equity"])
    costs = load_costs(P["costs"]) if P.get("costs") and P["costs"].exists() else []
    realized = load_realized(P["realized"]) if P.get("realized") and P["realized"].exists() else None
    rates = load_rates(P["rates"]) if P.get("rates") and P["rates"].exists() else None
    uplift, label = 0.0, "VOO"
    src = P.get("source")
    if src and src.exists():
        meta = json.loads(src.read_text())
        uplift, label = float(meta.get("uplift_bp", 0.0)), str(meta.get("source", "VOO"))
    prices = load_prices(P["prices"], uplift)
    return run(flows, equity, prices, costs, realized, rates, st_rate, lt_rate, label)
