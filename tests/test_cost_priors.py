"""Tests for the cost-prior journal fields and the break-even / expectancy scorecard columns (branch feat/cost-priors)."""
import json
import sqlite3
from datetime import date

import pytest

from trading_ops import cli, ledger, scorecard

PRIORS = {"structures": {"spy_bps_p0_40d_14dte_5pct": {"expected_cost_per_lot": {"open": 2.5, "close": 1.5}}}}


@pytest.fixture
def conn(tmp_path):
    return ledger.connect(str(tmp_path / "t.db"))


def test_summarize_breakeven_and_expectancy_known_numbers():
    # 3 wins of +10, 1 loss of -40: avg_win 10, avg_loss 40, breakeven 40/50 = 80%, mean = -2.5, expectancy = -2.5/40 = -0.0625 R
    s = scorecard.summarize([10, 10, 10, -40])
    assert s["avg_win"] == pytest.approx(10) and s["avg_loss"] == pytest.approx(40)
    assert s["breakeven_win_rate"] == pytest.approx(0.8)
    assert s["win_rate"] == pytest.approx(0.75)                      # 75% < 80% breakeven -> negative expectancy
    assert s["expectancy_R"] == pytest.approx(-2.5 / 40)


def test_summarize_edge_cases():
    assert scorecard.summarize([])["breakeven_win_rate"] is None
    only_wins = scorecard.summarize([5, 6])
    assert only_wins["breakeven_win_rate"] is None and only_wins["expectancy_R"] is None
    only_losses = scorecard.summarize([-5, -6])
    assert only_losses["breakeven_win_rate"] is None and only_losses["expectancy_R"] == pytest.approx(-1.0)


def test_existing_summary_keys_unchanged():
    s = scorecard.summarize([10, -5, 3])
    for k in ("n", "mean", "median", "win_rate", "profit_factor", "cum", "max_drawdown"):
        assert k in s
    assert s["n"] == 3 and s["cum"] == pytest.approx(8)


def test_fill_stores_explicit_expected_cost(conn):
    t = ledger.open_trade(conn, "spy_bps", "paper", "SPY", 2, "something_without_prior")
    ledger.add_fill(conn, t, "open", "sell", 2, 1.00, 0.97, expected_cost_per_lot=4.0)
    ledger.add_fill(conn, t, "open", "buy", 2, 0.40, 0.42)       # no expected value given, structure has no prior
    rows = conn.execute("SELECT expected_cost_per_lot FROM fills ORDER BY id").fetchall()
    assert rows[0][0] == pytest.approx(4.0) and rows[1][0] is None


def test_fill_looks_up_prior_by_structure(conn, monkeypatch, tmp_path):
    p = tmp_path / "priors.json"
    p.write_text(json.dumps(PRIORS))
    monkeypatch.setenv("TRADING_OPS_COST_PRIORS", str(p))
    t = ledger.open_trade(conn, "spy_bps", "paper", "SPY", 1, "spy_bps_p0_40d_14dte_5pct")
    ledger.add_fill(conn, t, "open", "sell", 1, 1.00, 0.98)
    ledger.add_fill(conn, t, "close", "buy", 1, 0.50, 0.51)
    got = {r["action"]: r["expected_cost_per_lot"] for r in conn.execute("SELECT action, expected_cost_per_lot FROM fills")}
    assert got == {"open": 2.5, "close": 1.5}


def test_shipped_priors_file_has_the_bots_structures():
    pri = ledger.load_cost_priors()
    for k in ("spy_bps_p0_40d_14dte_5pct", "premium_sleeve_30d_10d_30to45dte"):
        assert ledger.expected_cost_per_lot(k, "open", pri) > 0
    assert ledger.expected_cost_per_lot("unknown", "open", pri) is None
    assert ledger.expected_cost_per_lot(None, "open", pri) is None


def test_cost_vs_prior_compares_per_lot(conn):
    # 2 lots, two legs on the open: slippage 0.03*2*100 = 6 and 0.02*2*100 = 4 -> $10 / 2 lots = $5 per lot vs prior $2.5
    t = ledger.open_trade(conn, "spy_bps", "paper", "SPY", 2, "s")
    ledger.add_fill(conn, t, "open", "sell", 2, 1.00, 0.97, leg="short", expected_cost_per_lot=2.5)
    ledger.add_fill(conn, t, "open", "buy", 2, 0.40, 0.42, leg="long", expected_cost_per_lot=2.5)
    out = ledger.cost_vs_prior(conn, "spy_bps")
    assert len(out) == 1
    o = out[0]
    assert o["n"] == 1 and o["action"] == "open"
    assert o["avg_actual_cost_per_lot"] == pytest.approx(5.0) and o["avg_expected_cost_per_lot"] == pytest.approx(2.5)
    assert o["avg_excess_vs_prior_per_lot"] == pytest.approx(2.5) and o["share_worse_than_prior"] == 1.0


def test_cost_vs_prior_ignores_fills_without_prior(conn):
    t = ledger.open_trade(conn, "b", "paper", "SPY", 1, "s")
    ledger.add_fill(conn, t, "open", "sell", 1, 1.0, 0.9)
    assert ledger.cost_vs_prior(conn) == []


def test_old_database_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY, bot TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
        structure TEXT, qty REAL NOT NULL, signal_id INTEGER, opened_at TEXT NOT NULL, closed_at TEXT, entry_ref REAL, pnl REAL, pnl_unit TEXT, notes TEXT);
        CREATE TABLE fills (id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, ts TEXT NOT NULL, leg TEXT, action TEXT NOT NULL, side TEXT NOT NULL,
        qty REAL NOT NULL, mult REAL NOT NULL DEFAULT 100, ref_price REAL NOT NULL, fill_price REAL NOT NULL, fees REAL NOT NULL DEFAULT 0,
        slippage_per_unit REAL NOT NULL, slippage_usd REAL NOT NULL);
        INSERT INTO trades VALUES (1,'b','paper','SPY','s',1,NULL,'2026-01-01',NULL,NULL,NULL,NULL,NULL);
        INSERT INTO fills VALUES (1,1,'2026-01-01','l','open','sell',1,100,1.0,0.9,0,0.1,10);""")
    old.commit(); old.close()
    c = ledger.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(fills)")}
    assert "expected_cost_per_lot" in cols
    assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1          # data kept
    ledger.connect(path)                                                        # second connect is a no-op


def test_scorecard_markdown_has_new_columns_and_cost_line(conn, monkeypatch, tmp_path):
    p = tmp_path / "priors.json"
    p.write_text(json.dumps(PRIORS))
    monkeypatch.setenv("TRADING_OPS_COST_PRIORS", str(p))
    t = ledger.open_trade(conn, "condor", "paper", "SPY", 1, "spy_bps_p0_40d_14dte_5pct")
    ledger.add_fill(conn, t, "open", "sell", 1, 1.00, 0.97, leg="short")
    ledger.close_trade(conn, t, pnl=12.0)
    cfg = {"global": {"min_n": 30}, "bots": {"condor": {"unit": "usd"}}}
    rep = scorecard.run(conn, cfg, today=date(2026, 12, 31))
    md = scorecard.render_markdown(rep)
    assert "Break-even win" in md and "Expectancy (R)" in md and "cost vs prior" in md
    assert rep["results"][0]["cost_vs_prior"][0]["avg_actual_cost_per_lot"] == pytest.approx(3.0)


def test_cli_fill_flag(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    cli.main(["--db", db, "open", "--bot", "b", "--mode", "paper", "--symbol", "SPY", "--qty", "1", "--structure", "x"])
    cli.main(["--db", db, "fill", "--trade-id", "1", "--action", "open", "--side", "sell", "--qty", "1", "--ref", "1.0", "--price", "0.97",
              "--expected-cost-per-lot", "3.5"])
    c = ledger.connect(db)
    assert c.execute("SELECT expected_cost_per_lot FROM fills").fetchone()[0] == pytest.approx(3.5)
