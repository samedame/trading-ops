import csv
import json
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_ops import benchmark as bm
from trading_ops.cli import main

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_benchmark_prices as fetcher  # noqa: E402

D0 = date(2025, 1, 6)           # a Monday


def weekdays(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(n=300, seed=7, start_px=100.0):
    rng = random.Random(seed)
    px, pts = start_px, {}
    for d in weekdays(D0, n):
        px *= 1.0 + rng.gauss(0.0004, 0.01)
        pts[d] = px
    return pts


def holder_equity(pts, flows, days):
    """INDEPENDENT of benchmark.Shadow: an account that is simply long VOO, units tracked by hand."""
    ds = sorted(pts)
    out = []
    for d in days:
        units = 0.0
        for fd, a in flows:
            if fd <= d:
                last = max(x for x in ds if x <= fd)
                units += a / pts[last]
        last = max(x for x in ds if x <= d)
        out.append((d, units * pts[last]))
    return out


def write_csv(path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# ------------------------------------------------------------------------------------------------ invariants
def test_account_that_is_just_voo_has_zero_excess_and_equal_returns():
    pts = walk()
    days = weekdays(D0, 200)
    flows = [(days[0], 1000.0), (days[20], 500.0), (days[60], -300.0)]
    eq = holder_equity(pts, flows, days)
    rep = bm.run(flows, eq, bm.Series(pts), realized=[])
    assert rep["account"]["twr"] == pytest.approx(rep["voo"]["twr"], abs=1e-12)
    assert rep["excess"]["usd_after_fees"] == pytest.approx(0.0, abs=1e-6)
    assert rep["account"]["irr"] == pytest.approx(rep["voo"]["irr"], abs=1e-9)
    assert rep["excess"]["usd_after_fees_and_tax"] == pytest.approx(0.0, abs=1e-6)
    assert rep["gate"]["label"] == "NO_CLAIM_BEHIND_VOO"          # equal is not ahead


def test_voo_twr_equals_the_plain_price_return_when_equity_is_daily():
    pts = walk()
    days = weekdays(D0, 150)
    flows = [(days[0], 1000.0), (days[10], 700.0), (days[50], -400.0), (days[90], 250.0)]
    eq = holder_equity(pts, flows, days)
    rep = bm.run(flows, eq, bm.Series(pts), realized=[])
    assert rep["voo"]["twr"] == pytest.approx(pts[days[-1]] / pts[days[0]] - 1, abs=1e-12)


def test_cash_account_loses_to_a_rising_voo_and_is_refused_the_claim():
    pts = {d: 100.0 + i for i, d in enumerate(weekdays(D0, 250))}     # steadily rising
    days = weekdays(D0, 200)
    flows = [(days[0], 1000.0)]
    eq = [(d, 1000.0) for d in days]
    rep = bm.run(flows, eq, bm.Series(pts), realized=[])
    assert rep["account"]["twr"] == 0.0
    assert rep["voo"]["twr"] > 0.5
    assert rep["excess"]["usd_after_fees"] < 0
    assert rep["gate"]["label"] == "NO_CLAIM_BEHIND_VOO" and rep["gate"]["claim_permitted"] is False


def test_external_costs_are_invested_in_the_shadow_and_hurt_the_account_exactly_that_much():
    pts = walk()
    days = weekdays(D0, 200)
    flows = [(days[0], 1000.0)]
    eq = holder_equity(pts, flows, days)
    costs = [(days[10], 100.0)]
    rep = bm.run(flows, eq, bm.Series(pts), costs=costs, realized=[])
    expected = -100.0 * pts[days[-1]] / pts[days[10]]
    assert rep["excess"]["usd_after_fees"] == pytest.approx(expected, rel=1e-9)
    assert rep["account"]["external_costs"] == 100.0
    assert rep["account"]["twr"] == pytest.approx(rep["voo"]["twr"], abs=1e-12)     # costs never enter the TWR
    # money-weighted: the account's cash flows include the cost outlay, so its IRR is below VOO's
    by_hand = bm.xirr([(days[0], -1000.0), (days[10], -100.0), (days[-1], eq[-1][1])])
    assert rep["account"]["irr"] == pytest.approx(by_hand, abs=1e-12)
    assert rep["account"]["irr"] < rep["voo"]["irr"]


def test_ahead_but_short_history_is_permitted_but_unproven():
    pts = {d: 100.0 for d in weekdays(D0, 250)}                      # flat VOO
    days = weekdays(D0, 120)
    flows = [(days[0], 1000.0)]
    eq = [(d, 1000.0 + 2.0 * i) for i, d in enumerate(days)]         # account drifts up
    rep = bm.run(flows, eq, bm.Series(pts), realized=[])
    assert rep["excess"]["usd_after_fees"] > 0
    assert rep["gate"]["label"] == "CLAIM_PERMITTED_UNPROVEN"
    assert rep["gate"]["claim_permitted"] is True
    assert any("under one year" in w for w in rep["warnings"])


def test_tax_unknown_blocks_the_claim_even_when_ahead():
    pts = {d: 100.0 for d in weekdays(D0, 250)}
    days = weekdays(D0, 120)
    flows = [(days[0], 1000.0)]
    eq = [(d, 1000.0 + 5.0 * i) for i, d in enumerate(days)]
    rep = bm.run(flows, eq, bm.Series(pts), realized=None)
    assert rep["excess"]["usd_after_fees"] > 0
    assert rep["excess"]["usd_after_fees_and_tax"] is None
    assert rep["gate"]["label"] == "NO_CLAIM_TAX_UNKNOWN" and rep["gate"]["claim_permitted"] is False
    assert rep["tax"]["status"] == "UNKNOWN"


def test_ahead_before_tax_can_be_behind_after_tax():
    # Same pre-tax end value. The account is taxed short-term on its unrealised gain; the shadow held 400 days, so long-term.
    s = bm.Series({D0: 100.0, D0 + timedelta(days=400): 150.0})
    flows = [(D0, 1000.0)]
    eq = [(D0, 1000.0), (D0 + timedelta(days=400), 1500.0)]
    rep = bm.run(flows, eq, s, realized=[])
    assert rep["tax"]["shadow_tax"] == pytest.approx(500 * 0.15)
    assert rep["tax"]["account_tax"] == pytest.approx(500 * 0.24)
    assert rep["excess"]["usd_after_fees"] == pytest.approx(0.0)
    assert rep["excess"]["usd_after_fees_and_tax"] == pytest.approx(-(120 - 75))
    assert rep["gate"]["label"] == "NO_CLAIM_BEHIND_VOO"


def test_shadow_lot_is_short_term_at_365_days_and_long_term_after():
    for held, rate in ((365, 0.24), (366, 0.15)):
        s = bm.Series({D0: 100.0, D0 + timedelta(days=held): 150.0})
        sh = bm.build_shadow(s, [(D0, 1000.0)])
        st, lt = sh.unrealized(D0 + timedelta(days=held))
        assert bm.tax_due(st, lt, 0.24, 0.15) == pytest.approx(500 * rate)


def test_withdrawal_sells_oldest_lots_first_and_realises_the_right_bucket():
    d1, d2 = D0 + timedelta(days=10), D0 + timedelta(days=500)
    s = bm.Series({D0: 100.0, d1: 100.0, d2: 200.0})
    sh = bm.build_shadow(s, [(D0, 1000.0), (d1, 1000.0), (d2, -3000.0)])
    # 15 units sold: 10 from the first lot (400 days+, LT gain 1000) and 5 from the second (490 days, LT gain 500)
    assert sh.realized_lt == pytest.approx(1500.0) and sh.realized_st == 0.0
    assert sh.units_at(d2) == pytest.approx(5.0)


def test_sale_of_a_lot_held_exactly_366_days_is_long_term_and_365_is_short_term():
    for held, bucket in ((365, "st"), (366, "lt")):
        sell = D0 + timedelta(days=held)
        sh = bm.build_shadow(bm.Series({D0: 100.0, sell: 120.0}), [(D0, 1000.0), (sell, -600.0)])   # sells 5 units
        assert getattr(sh, "realized_" + bucket) == pytest.approx(100.0)
        assert getattr(sh, "realized_" + ("lt" if bucket == "st" else "st")) == 0.0


def test_withdrawal_larger_than_the_shadow_holds_is_an_error():
    s = bm.Series({D0: 100.0, D0 + timedelta(days=5): 100.0})
    with pytest.raises(bm.BenchmarkError, match="cannot fund"):
        bm.build_shadow(s, [(D0, 100.0), (D0 + timedelta(days=5), -500.0)])


# ------------------------------------------------------------------------------------------------ maths pieces
def test_modified_dietz_matches_the_hand_calculation():
    d0 = date(2025, 1, 1)
    obs = [(d0, 100.0), (d0 + timedelta(days=10), 160.0)]
    rets, undef = bm.dietz_returns(obs, [(d0 + timedelta(days=5), 40.0)])
    assert undef == 0 and rets[0] == pytest.approx(20.0 / 120.0)


def test_flow_on_the_last_day_of_an_interval_earns_nothing_in_it():
    d0 = date(2025, 1, 1)
    obs = [(d0, 100.0), (d0 + timedelta(days=1), 150.0)]
    rets, _ = bm.dietz_returns(obs, [(d0 + timedelta(days=1), 50.0)])
    assert rets[0] == pytest.approx(0.0)


def test_xirr_known_answers():
    d0 = date(2025, 1, 1)
    assert bm.xirr([(d0, -100.0), (d0 + timedelta(days=365), 110.0)]) == pytest.approx(0.10, abs=1e-9)
    assert bm.xirr([(d0, -100.0), (d0 + timedelta(days=730), 121.0)]) == pytest.approx(0.10, abs=1e-9)
    assert bm.xirr([(d0, -100.0), (d0 + timedelta(days=365), 50.0)]) == pytest.approx(-0.50, abs=1e-9)
    assert bm.xirr([(d0, -100.0)]) is None and bm.xirr([(d0, 100.0), (d0 + timedelta(days=9), 5.0)]) is None


def test_net_gains_netting_rules():
    assert bm.net_gains(100, 50) == (100, 50)
    assert bm.net_gains(-100, -50) == (0.0, 0.0)
    assert bm.net_gains(-40, 100) == (0.0, 60)
    assert bm.net_gains(100, -40) == (60, 0.0)
    assert bm.net_gains(-100, 40) == (0.0, 0.0)
    assert bm.tax_due(-100, 40, 0.24, 0.15) == 0.0


def test_gate_labels():
    g = bm.gate
    assert g(10.0, False, 0.01, 30)["label"] == "NO_CLAIM_TAX_UNKNOWN"
    assert g(None, True, 0.01, 30)["label"] == "NO_CLAIM_BEHIND_VOO"
    assert g(-1.0, True, 0.01, 30)["label"] == "NO_CLAIM_BEHIND_VOO"
    assert g(0.0, True, 0.01, 30)["label"] == "NO_CLAIM_BEHIND_VOO"
    assert g(1e-9, True, 0.01, 30)["label"] == "NO_CLAIM_BEHIND_VOO"          # float noise is not an edge
    assert g(0.01, True, 0.01, 30)["label"] == "CLAIM_SUPPORTED"
    assert g(10.0, True, 0.01, 30)["label"] == "CLAIM_SUPPORTED"
    assert g(10.0, True, 0.01, 23)["label"] == "CLAIM_PERMITTED_UNPROVEN"
    assert g(10.0, True, -0.001, 30)["label"] == "CLAIM_PERMITTED_UNPROVEN"
    assert g(10.0, True, None, 30)["label"] == "CLAIM_PERMITTED_UNPROVEN"


def test_years_to_detect_matches_the_roadmap_scale():
    assert bm.years_to_detect(0.10, 0.03) == pytest.approx(((1.645 + 0.84) * 0.10 / 0.03) ** 2)
    assert bm.years_to_detect(0.10, 0.03) > 60
    assert bm.years_to_detect(0.10, 0.0) == math.inf


def test_bootstrap_lower_bound():
    assert bm.bootstrap_lower([0.01] * 11) is None
    assert bm.bootstrap_lower([0.01] * 24) == pytest.approx(0.01)
    noisy = [0.05, -0.05] * 12
    assert bm.bootstrap_lower(noisy) < 0
    assert bm.bootstrap_lower(noisy) == bm.bootstrap_lower(noisy)          # seeded, repeatable


def test_month_grid_keeps_first_point_and_each_month_end():
    days = weekdays(date(2025, 1, 27), 30)
    grid = bm.month_grid([(d, 1.0) for d in days])
    assert grid[0][0] == days[0]
    ends = [d for d, _ in grid[1:]]
    assert ends[0] == date(2025, 1, 31) and ends[1] == date(2025, 2, 28)
    assert all((a.year, a.month) != (b.year, b.month) for a, b in zip(ends, ends[1:]))


def test_bill_value_compounds_the_latest_yield_daily():
    d0 = date(2025, 1, 1)
    v = bm.bill_value({d0: 3.65}, [(d0, 100.0)], d0 + timedelta(days=365))
    assert v == pytest.approx(100.0 * (1 + 0.0365 / 365) ** 365)
    assert bm.bill_value({d0: 3.65}, [(d0, -100.0)], d0 + timedelta(days=365)) == pytest.approx(-v)


def test_price_series_forward_fills_and_refuses_stale_or_early_dates():
    s = bm.Series({date(2025, 1, 3): 10.0, date(2025, 1, 6): 11.0})
    assert s.asof(date(2025, 1, 5)) == 10.0                                  # Sunday uses Friday
    with pytest.raises(bm.BenchmarkError, match="no price on or before"):
        s.asof(date(2025, 1, 2))
    with pytest.raises(bm.BenchmarkError, match="series ends"):
        s.asof(date(2025, 3, 1))


def test_proxy_uplift_is_a_steady_annual_drift():
    s = bm.Series({D0: 100.0, D0 + timedelta(days=365): 100.0}, uplift_bp=100.0)
    assert s.asof(D0 + timedelta(days=365)) == pytest.approx(101.0)


def test_weekend_flow_executes_at_the_prior_close_without_error():
    pts = walk()
    sat = date(2025, 1, 11)
    sh = bm.build_shadow(bm.Series(pts), [(sat, 1000.0)])
    assert sh.units_at(sat) == pytest.approx(1000.0 / pts[date(2025, 1, 10)])


# ------------------------------------------------------------------------------------------------ input checks
def test_first_equity_row_after_first_deposit_is_rejected():
    s = bm.Series({D0: 100.0, D0 + timedelta(days=30): 100.0})
    with pytest.raises(bm.BenchmarkError, match="first equity row"):
        bm.run([(D0, 100.0)], [(D0 + timedelta(days=5), 100.0), (D0 + timedelta(days=9), 101.0)], s)


def test_flow_after_last_equity_row_is_rejected():
    s = bm.Series({D0: 100.0, D0 + timedelta(days=30): 100.0})
    with pytest.raises(bm.BenchmarkError, match="after the last equity row"):
        bm.run([(D0, 100.0), (D0 + timedelta(days=20), 10.0)], [(D0, 100.0), (D0 + timedelta(days=9), 101.0)], s)


def test_loaders_reject_bad_files(tmp_path):
    p = tmp_path / "e.csv"
    write_csv(p, ["date", "equity"], [["2025-01-01", "100"], ["2025-01-01", "101"]])
    with pytest.raises(bm.BenchmarkError, match="two equity rows"):
        bm.load_equity(p)
    write_csv(p, ["date", "equity"], [["2025-01-01", "-5"]])
    with pytest.raises(bm.BenchmarkError, match="negative"):
        bm.load_equity(p)
    write_csv(p, ["day", "equity"], [["2025-01-01", "5"]])
    with pytest.raises(bm.BenchmarkError, match="missing column"):
        bm.load_equity(p)
    write_csv(p, ["date", "amount"], [["01/02/2025", "5"]])
    with pytest.raises(bm.BenchmarkError, match="bad date"):
        bm.load_flows(p)
    write_csv(p, ["date", "amount"], [["2025-01-02", "0"]])
    with pytest.raises(bm.BenchmarkError, match="not a flow"):
        bm.load_flows(p)
    write_csv(p, ["date", "amount"], [["2025-01-02", "-5"]])
    with pytest.raises(bm.BenchmarkError, match="positive"):
        bm.load_costs(p)
    with pytest.raises(bm.BenchmarkError, match="not found"):
        bm.load_flows(tmp_path / "nope.csv")


def test_flows_on_the_same_day_are_summed_and_dollar_signs_accepted(tmp_path):
    p = tmp_path / "f.csv"
    write_csv(p, ["Date", "Amount", "Note"], [["2025-01-02", "$1,000", "a"], ["2025-01-02", "-250", "b"]])
    assert bm.load_flows(p) == [(date(2025, 1, 2), 750.0)]


def test_fred_style_rates_file_loads(tmp_path):
    p = tmp_path / "r.csv"
    write_csv(p, ["observation_date", "DTB3"], [["2025-01-02", "4.30"], ["2025-01-03", "."], ["2025-01-06", "4.31"]])
    assert bm.load_rates(p) == {date(2025, 1, 2): 4.30, date(2025, 1, 6): 4.31}


# ------------------------------------------------------------------------------------------------ CLI + fetch
def _scenario(tmp_path, ahead=True):
    pts = {d: 100.0 for d in weekdays(D0, 250)}
    days = weekdays(D0, 120)
    write_csv(tmp_path / "flows.csv", ["date", "amount", "note"], [[days[0].isoformat(), "1000", "first"]])
    write_csv(tmp_path / "equity.csv", ["date", "equity"],
              [[d.isoformat(), f"{1000 + (3.0 if ahead else -3.0) * i:.2f}"] for i, d in enumerate(days)])
    write_csv(tmp_path / "prices.csv", ["date", "adjclose"], [[d.isoformat(), f"{p:.4f}"] for d, p in pts.items()])
    write_csv(tmp_path / "realized.csv", ["date", "short_term", "long_term"], [[days[5].isoformat(), "0", "0"]])
    return {k: str(tmp_path / f"{k}.csv") for k in ("flows", "equity", "prices", "realized")}


def test_cli_markdown_and_json(tmp_path, capsys):
    f = _scenario(tmp_path, ahead=True)
    base = ["--db", str(tmp_path / "t.db"), "benchmark", "--flows", f["flows"], "--equity", f["equity"],
            "--prices", f["prices"], "--realized", f["realized"]]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "Gate: CLAIM_PERMITTED_UNPROVEN" in out and "Shadow VOO" in out and "under one year" in out
    assert main(base + ["--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["gate"]["label"] == "CLAIM_PERMITTED_UNPROVEN" and rep["tax"]["status"] == "estimated"


def test_cli_behind_case_and_missing_file(tmp_path, capsys):
    f = _scenario(tmp_path, ahead=False)
    args = ["--db", str(tmp_path / "t.db"), "benchmark", "--flows", f["flows"], "--equity", f["equity"],
            "--prices", f["prices"], "--realized", f["realized"]]
    assert main(args) == 0
    assert "Gate: NO_CLAIM_BEHIND_VOO" in capsys.readouterr().out
    assert main(args[:-1] + [str(tmp_path / "missing.csv")]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_without_realized_file_closes_the_gate(tmp_path, capsys):
    f = _scenario(tmp_path, ahead=True)
    args = ["--db", str(tmp_path / "t.db"), "benchmark", "--flows", f["flows"], "--equity", f["equity"],
            "--prices", f["prices"]]
    assert main(args) == 0
    assert "NO_CLAIM_TAX_UNKNOWN" in capsys.readouterr().out


def test_run_from_files_applies_the_source_uplift_and_names_the_source(tmp_path):
    f = _scenario(tmp_path)
    (tmp_path / "SOURCE.json").write_text(json.dumps({"source": "SPY proxy", "uplift_bp": 100.0}))
    paths = {**f, "source": str(tmp_path / "SOURCE.json"), "costs": None, "rates": None}
    rep = bm.run_from_files(paths)
    assert rep["voo"]["source"] == "SPY proxy"
    assert rep["voo"]["twr"] > 0           # flat prices plus uplift can only be positive


def test_fetch_builds_files_and_never_hides_a_proxy(tmp_path):
    prices = {f"2020-01-{i:02d}": 100.0 + i for i in range(1, 29)}
    big = {(date(2015, 1, 1) + timedelta(days=i)).isoformat(): 100.0 + i * 0.01 for i in range(1500)}
    bills = {"2025-01-02": 4.3, "2025-01-03": 4.31}
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    meta = fetcher.build(tmp_path / "a", False, fetch_voo=lambda: big, fetch_bills=lambda: bills, now=now)
    assert meta["uplift_bp"] == 0.0 and "Yahoo" in meta["source"]
    assert (tmp_path / "a" / "voo.csv").read_text().startswith("date,adjclose")
    assert bm.load_rates(tmp_path / "a" / "dtb3.csv") == {date(2025, 1, 2): 4.3, date(2025, 1, 3): 4.31}

    def boom():
        raise RuntimeError("429")

    with pytest.raises(SystemExit, match="VOO download failed"):
        fetcher.build(tmp_path / "b", False, fetch_voo=boom, fetch_spy=lambda: big, fetch_bills=lambda: bills)
    meta = fetcher.build(tmp_path / "c", True, fetch_voo=boom, fetch_spy=lambda: big, fetch_bills=lambda: bills)
    assert meta["uplift_bp"] == fetcher.PROXY_UPLIFT_BP and "proxy" in meta["source"] and "429" in meta["source"]
    assert prices                      # keep the fixture honest: unused small dict is not read


def test_money_formatting_puts_the_sign_before_the_dollar_sign():
    assert bm._usd(-62.4) == "-$62.40" and bm._usd(1234.5) == "$1,234.50" and bm._usd(None) == "n/a"
    assert bm._plain(0.1187) == "11.87%"
