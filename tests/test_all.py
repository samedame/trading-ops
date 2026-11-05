import json
import math
import random
from datetime import date
from pathlib import Path

import pytest

from trading_ops import freeze, ledger, scorecard, sizing, snapshot
from trading_ops.cli import main

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config" / "bots.json").read_text())
FREEZE = json.loads((Path(__file__).resolve().parent.parent / "config" / "freeze.json").read_text())


@pytest.fixture
def conn(tmp_path):
    return ledger.connect(str(tmp_path / "t.db"))


# ---------- sizing ----------
def test_capital_never_guessed():
    with pytest.raises(sizing.CapitalNotSet):
        sizing.capital_from_env({})
    with pytest.raises(sizing.CapitalNotSet):
        sizing.capital_from_env({"ACCOUNT_CAPITAL_USD": "abc"})
    with pytest.raises(sizing.CapitalNotSet):
        sizing.capital_from_env({"ACCOUNT_CAPITAL_USD": "-5"})
    assert sizing.capital_from_env({"ACCOUNT_CAPITAL_USD": "$1,250"}) == 1250


def test_spread_math_and_rounding_down():
    assert sizing.spread_max_loss(5, 1.0) == 400
    assert sizing.contracts_for_risk(1000, 0.02, 400) == 0          # $20 budget, $400 risk: zero is correct
    assert sizing.contracts_for_risk(20000, 0.02, 400) == 1         # exactly one
    assert sizing.contracts_for_risk(19999, 0.02, 400) == 0
    f = sizing.feasibility(1000, 0.02, 400)
    assert not f["tradable"] and f["one_contract_risks_pct_of_capital"] == 40 and f["min_capital_for_one_contract"] == 20000
    with pytest.raises(ValueError):
        sizing.spread_max_loss(5, 5)


def test_csp_and_shares():
    assert sizing.csp_collateral(725, 1.0) == 72400
    p = sizing.share_plan(1000, 0.5, 755.72)
    assert p["shares"] == 0 and not p["tradable"]
    q = sizing.share_plan(1000, 0.5, 755.72, fractional=True)
    assert math.isclose(q["actual_weight"], 0.5) and q["tradable"]
    r = sizing.share_plan(10000, 0.5, 755.72)
    assert r["shares"] == 6 and r["weight_error_pct_points"] < 0


# ---------- ledger ----------
def test_slippage_signs():
    assert ledger.slippage_per_unit("sell", 1.00, 0.90) == pytest.approx(0.10)   # sold below ref = cost
    assert ledger.slippage_per_unit("buy", 1.00, 1.05) == pytest.approx(0.05)    # bought above ref = cost
    assert ledger.slippage_per_unit("sell", 1.00, 1.10) == pytest.approx(-0.10)  # price improvement


def test_pnl_from_fills_and_summary(conn):
    t = ledger.open_trade(conn, "spy_bps", "paper", "SPY", 1, "bull put spread 5w")
    ledger.add_fill(conn, t, "open", "sell", 1, 1.00, 0.92, fees=1.3, leg="short")
    ledger.add_fill(conn, t, "close", "buy", 1, 0.50, 0.55, fees=1.3, leg="short")
    pnl = ledger.close_trade(conn, t)
    assert pnl == pytest.approx((0.92 - 0.55) * 100 - 2.6)
    s = {r["action"]: r for r in ledger.slippage_summary(conn, "spy_bps")}
    assert s["open"]["total_usd"] == pytest.approx(8.0) and s["close"]["total_usd"] == pytest.approx(5.0)


def test_invalid_inputs(conn):
    with pytest.raises(ValueError):
        ledger.open_trade(conn, "x", "bogus", "SPY", 1)
    with pytest.raises(ValueError):
        ledger.open_trade(conn, "x", "paper", "SPY", 0)
    t = ledger.open_trade(conn, "x", "paper", "SPY", 1)
    with pytest.raises(ValueError):
        ledger.pnl_from_fills(conn, t)


# ---------- snapshot ----------
class FakeProvider:
    name = "fake"

    def spot(self, s): return 700.0
    def vix(self): return 16.6
    def expirations(self, s): return ["2026-11-05", "2026-11-06", "2027-03-19"]

    def chain(self, s, exp):
        out = []
        for k in range(600, 801, 5):
            out.append(snapshot.Quote(exp, float(k), "P", max(0.0, (700 - k) * 0.0 + 700 * 0.0002 * (k / 700) ** 8),
                                      700 * 0.0002 * (k / 700) ** 8 + 0.01, iv=0.17, volume=10, open_interest=5))
            out.append(snapshot.Quote(exp, float(k), "C", 0.0, 0.0, iv=0.17))
        return out


def test_snapshot_band_dte_and_idempotent(conn):
    n1 = snapshot.snapshot(conn, FakeProvider(), ["SPY"], "close", today=date(2026, 11, 4))
    assert n1 > 0
    assert conn.execute("SELECT COUNT(*) c FROM chain_snapshots WHERE dte>45").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM chain_snapshots WHERE otm_pct<-8.01 OR otm_pct>8.01").fetchone()["c"] == 0
    n2 = snapshot.snapshot(conn, FakeProvider(), ["SPY"], "close", today=date(2026, 11, 4))
    assert n2 == n1 and conn.execute("SELECT COUNT(*) c FROM chain_snapshots").fetchone()["c"] == n1
    r = conn.execute("SELECT * FROM chain_snapshots WHERE right='P' AND strike=680 AND dte=1").fetchone()
    assert r["delta"] is not None and -0.5 < r["delta"] < 0  # computed from the quote's IV
    c = conn.execute("SELECT mid FROM chain_snapshots WHERE right='C' LIMIT 1").fetchone()
    assert c["mid"] is None  # zero quotes are not turned into a fake mid


def test_ladder_uses_bp_of_notional(conn):
    snapshot.snapshot(conn, FakeProvider(), ["SPY"], "close", today=date(2026, 11, 4))
    lad = snapshot.put_ladder_bp(conn, "2026-11-04", "SPY")
    assert [x["target_otm_pct"] for x in lad] == [3.5, 4.0, 4.5]
    assert all(x["dte"] == 1 for x in lad)
    row = conn.execute("SELECT mid, spot FROM chain_snapshots WHERE strike=? AND dte=1 AND right='P'",
                       (lad[0]["strike"],)).fetchone()
    assert lad[0]["mid_bp"] == pytest.approx(row["mid"] / row["spot"] * 1e4)


def test_export_chains_roundtrip(conn, tmp_path):
    import csv
    import gzip

    n = snapshot.snapshot(conn, FakeProvider(), ["SPY"], "close", today=date(2026, 11, 4))
    out = tmp_path / "c.csv.gz"
    assert snapshot.export_chains(conn, "2026-11-04", "close", str(out)) == n
    with gzip.open(out, "rt") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == n and set(rows[0]) == set(snapshot.EXPORT_COLUMNS)
    assert snapshot.export_chains(conn, "2026-11-05", "close", str(tmp_path / "e.csv.gz")) == 0


def test_tradier_parser_handles_single_and_missing_greeks():
    one = {"options": {"option": {"strike": 700, "option_type": "put", "bid": 1.0, "ask": 1.2, "greeks": None}}}
    q = snapshot.parse_tradier_chain(one, "2026-11-05")
    assert len(q) == 1 and q[0].right == "P" and q[0].delta is None
    many = {"options": {"option": [{"strike": 700, "option_type": "call", "bid": 1, "ask": 2,
                                    "greeks": {"delta": 0.4, "mid_iv": 0.2}}]}}
    assert snapshot.parse_tradier_chain(many, "x")[0].delta == 0.4
    assert snapshot.parse_tradier_chain({"options": None}, "x") == []


# ---------- scorecard ----------
def _add(conn, bot, pnls, unit="bp", mode="paper", start_day=1):
    for i, p in enumerate(pnls):
        t = ledger.open_trade(conn, bot, mode, "SPY", 1, opened_at=f"2026-12-{start_day + i % 25:02d}T14:30:00Z")
        ledger.close_trade(conn, t, pnl=p, pnl_unit=unit, closed_at=f"2026-12-{start_day + i % 25:02d}T21:00:00Z")


def _status(conn, bot):
    rep = scorecard.run(conn, CONFIG, today=date(2026, 12, 31))
    return {r["bot"]: r for r in rep["results"]}[bot]


def test_bootstrap_is_deterministic_and_brackets_mean():
    rng = random.Random(1)
    xs = [rng.gauss(10, 50) for _ in range(60)]
    assert scorecard.bootstrap_ci(xs) == scorecard.bootstrap_ci(xs)
    lo, hi = scorecard.bootstrap_ci(xs)
    assert lo < sum(xs) / len(xs) < hi


def test_summary_drawdown():
    s = scorecard.summarize([5, -3, -4, 2, 10])
    assert s["cum"] == 10 and s["max_drawdown"] == 7 and s["win_rate"] == 0.6


def test_insufficient_data_blocks_any_verdict(conn):
    _add(conn, "apex_master_v2", [50] * 10)
    r = _status(conn, "apex_master_v2")
    assert r["status"] == "INSUFFICIENT DATA"


def test_pause_floor_triggers_even_with_few_trades(conn):
    _add(conn, "apex_master_v2", [-900, -800])
    assert _status(conn, "apex_master_v2")["status"] == "PAUSE"


def test_retire_at_40_trades_nonpositive_mean(conn):
    _add(conn, "apex_master_v2", ([20, -22] * 20))   # mean -1, cum -40 (above the -1600 floor)
    r = _status(conn, "apex_master_v2")
    assert r["status"] == "RETIRE" and "n=40" in r["reasons"][0]


def test_on_track_and_underperforming(conn):
    rng = random.Random(7)
    _add(conn, "apex_master_v2", [rng.gauss(30, 40) for _ in range(45)])
    assert _status(conn, "apex_master_v2")["status"] == "ON TRACK"
    _add(conn, "apex_master_v1", [rng.gauss(-0.3, 0.2) for _ in range(35)], unit="R")
    assert _status(conn, "apex_master_v1")["status"] in ("UNDERPERFORMING", "PAUSE")


def test_blocked_beats_everything_and_review_flag(conn):
    _add(conn, "tier1_bot_h8", [100] * 50, unit="usd")
    assert _status(conn, "tier1_bot_h8")["status"] == "BLOCKED"
    r = _status(conn, "momentum_sleeve")
    assert not any("REVIEW DUE" in f for f in r["flags"])  # not due on 2026-12-31
    rep = scorecard.run(conn, CONFIG, today=date(2027, 9, 16))
    ms = {x["bot"]: x for x in rep["results"]}["momentum_sleeve"]
    assert any("REVIEW DUE" in f for f in ms["flags"])


def test_combined_usd_only_counts_usd(conn):
    _add(conn, "condor", [-100, 50, -80], unit="usd")
    _add(conn, "apex_master_v2", [999], unit="bp")
    c = scorecard.run(conn, CONFIG, today=date(2026, 12, 31))["combined_usd"]
    assert c["n"] == 3 and c["cum"] == -130


def test_markdown_renders(conn):
    _add(conn, "condor", [10, -5], unit="usd")
    md = scorecard.render_markdown(scorecard.run(conn, CONFIG, today=date(2026, 12, 31)))
    assert "| condor |" in md and "INSUFFICIENT DATA" in md and "not investment advice" in md


# ---------- freeze ----------
def test_freeze_window():
    assert freeze.status(FREEZE, date(2026, 11, 4))["state"] == "NOT STARTED"
    s = freeze.status(FREEZE, date(2026, 11, 5))
    assert s["state"] == "ACTIVE" and s["day_number"] == 1 and s["end"] == "2027-02-03"
    assert freeze.status(FREEZE, date(2027, 2, 3))["state"] == "ENDED"


def test_freeze_exception_requires_reversal_condition(conn):
    with pytest.raises(ValueError):
        freeze.log_exception(conn, "fix a typo", "")
    freeze.log_exception(conn, "scheduler bug", "bot misses 2 more fires")
    assert freeze.exceptions(conn)[0]["reverse_if"] == "bot misses 2 more fires"


# ---------- CLI end to end ----------
def test_cli_flow(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "cli.db")
    monkeypatch.delenv("ACCOUNT_CAPITAL_USD", raising=False)
    assert main(["--db", db, "init"]) == 0
    main(["--db", db, "signal", "--bot", "condor", "--symbol", "SPY", "--grade", "62"])
    main(["--db", db, "open", "--bot", "condor", "--mode", "paper", "--symbol", "SPY", "--qty", "1", "--signal-id", "1"])
    main(["--db", db, "fill", "--trade-id", "1", "--action", "open", "--side", "sell", "--qty", "1",
          "--ref", "0.44", "--price", "0.40"])
    main(["--db", db, "fill", "--trade-id", "1", "--action", "close", "--side", "buy", "--qty", "1",
          "--ref", "0", "--price", "0"])
    capsys.readouterr()
    main(["--db", db, "close", "--trade-id", "1"])
    assert "40.0000" in capsys.readouterr().out  # sold at 0.40, expired worthless: $40
    with pytest.raises(sizing.CapitalNotSet):
        main(["--db", db, "size", "spread", "--width", "5", "--credit", "1"])
    monkeypatch.setenv("ACCOUNT_CAPITAL_USD", "25000")
    capsys.readouterr()
    main(["--db", db, "size", "spread", "--width", "5", "--credit", "1"])
    assert '"contracts": 1' in capsys.readouterr().out
    main(["--db", db, "scorecard"])
    assert "# Bot scorecard" in capsys.readouterr().out
    main(["--db", db, "freeze", "status"])
    assert "days_left" in capsys.readouterr().out
