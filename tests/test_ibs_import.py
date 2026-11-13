import json
from datetime import date
from pathlib import Path

import pytest

from trading_ops import ibs_import, ledger, scorecard
from trading_ops.cli import main

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config" / "bots.json").read_text())

OPENS = {
    "SPY": {"2026-09-30": 700.0, "2026-10-01": 702.0, "2026-10-02": 705.0, "2026-10-05": 706.0, "2026-10-06": 708.0,
            "2026-10-07": 712.53},
    "IWM": {"2026-09-11": 240.0, "2026-09-14": 250.0, "2026-09-22": 251.0, "2026-09-23": 247.5},
}
HISTORY = [
    {"sym": "IWM", "entry_date": "2026-09-11", "exit_signal_date": "2026-09-22", "signal_ibs": 0.045,
     "reason": "close above yesterday's high"},
    {"sym": "SPY", "entry_date": "2026-09-30", "exit_signal_date": "2026-10-06", "signal_ibs": 0.0622,
     "reason": "close above yesterday's high"},
]


@pytest.fixture
def conn(tmp_path):
    return ledger.connect(str(tmp_path / "t.db"))


def test_next_open_is_strictly_after_the_signal_day():
    assert ibs_import.next_open(OPENS["SPY"], "2026-09-30") == ("2026-10-01", 702.0)
    assert ibs_import.next_open(OPENS["SPY"], "2026-10-02") == ("2026-10-05", 706.0)   # over a weekend
    assert ibs_import.next_open(OPENS["SPY"], "2026-10-07") is None                     # not printed yet


def test_valuation_is_next_open_to_next_open_net_of_cost():
    valued, skipped = ibs_import.value_trades(HISTORY, OPENS)
    assert skipped == []
    iwm, spy = valued                                         # sorted by exit date
    assert (iwm["entry_date"], iwm["exit_date"]) == ("2026-09-14", "2026-09-23")
    assert iwm["gross_bp"] == pytest.approx((247.5 / 250.0 - 1) * 1e4)            # -100 bp
    assert iwm["ret_bp"] == pytest.approx(-100.0 - 3.0)
    assert spy["ret_bp"] == pytest.approx((712.53 / 702.0 - 1) * 1e4 - 3.0)       # +150 bp gross


def test_trade_without_an_exit_open_waits_for_the_next_run():
    pending = [{"sym": "SPY", "entry_date": "2026-10-06", "exit_signal_date": "2026-10-07"}]
    valued, skipped = ibs_import.value_trades(pending, OPENS)
    assert valued == [] and len(skipped) == 1 and "no opening price" in skipped[0]["why"]
    valued, skipped = ibs_import.value_trades([{"sym": "QQQ", "entry_date": "2026-10-06"}], OPENS)
    assert valued == [] and "missing" in skipped[0]["why"]


def test_sync_is_idempotent_and_logs_bp_paper_trades(conn):
    valued, _ = ibs_import.value_trades(HISTORY, OPENS)
    assert ibs_import.sync(conn, valued) == {"added": 2, "already": 0}
    assert ibs_import.sync(conn, valued) == {"added": 0, "already": 2}
    rows = ledger.closed_trades(conn, "ibs")
    assert len(rows) == 2
    assert {r["pnl_unit"] for r in rows} == {"bp"} and {r["mode"] for r in rows} == {"paper"}
    assert rows[0]["symbol"] == "IWM" and rows[0]["pnl"] == pytest.approx(-103.0)
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE bot='ibs' AND taken=1").fetchone()[0] == 2


def test_run_survives_one_dead_price_source(conn, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"history": HISTORY}))

    def fetch(sym):
        if sym == "IWM":
            raise OSError("HTTP 429")
        return OPENS[sym]

    res = ibs_import.run(conn, str(state), fetch=fetch)
    assert res["closed_in_state"] == 2 and res["valued"] == 1 and res["added"] == 1 and "IWM" in res["price_errors"]
    res = ibs_import.run(conn, str(state), fetch=lambda s: OPENS[s])     # the next run picks up the one it missed
    assert res["added"] == 1 and res["already"] == 1


def test_dry_run_writes_nothing(conn, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"history": HISTORY}))
    res = ibs_import.run(conn, str(state), fetch=lambda s: OPENS[s], dry_run=True)
    assert res["valued"] == 2 and "added" not in res
    assert ledger.closed_trades(conn, "ibs") == []


def test_ibs_entry_has_the_verdict_benchmark_and_is_judged(conn):
    cfg = CONFIG["bots"]["ibs"]
    assert cfg["unit"] == "bp" and cfg["backtest_mean"] == 68.0 and cfg["retire_at_n"] == 100 and cfg["retire_if_mean_le"] == 20.0
    # 16 trades (what the bot has logged) is still short of the 30 the scorecard needs before any verdict.
    for i in range(16):
        tid = ledger.open_trade(conn, "ibs", "paper", "SPY", 1, opened_at=f"2026-10-{i + 1:02d}T00:00:00Z")
        ledger.close_trade(conn, tid, pnl=50.0, pnl_unit="bp", closed_at=f"2026-10-{i + 2:02d}T00:00:00Z")
    r = scorecard.evaluate("ibs", cfg, ledger.closed_trades(conn, "ibs"), CONFIG["global"], today=date(2026, 12, 31))
    assert r["status"] == "INSUFFICIENT DATA" and r["expected_mean_after_haircut"] == 68.0 and r["unit"] == "bp"
    # 100 trades averaging +10 bp trips the verdict's kill rule (below +0.2% over 100 alerts).
    for i in range(84):
        tid = ledger.open_trade(conn, "ibs", "paper", "SPY", 1, opened_at=f"2026-11-01T00:00:{i % 60:02d}Z")
        ledger.close_trade(conn, tid, pnl=2.38, pnl_unit="bp", closed_at=f"2026-11-02T00:00:{i % 60:02d}Z")
    r = scorecard.evaluate("ibs", cfg, ledger.closed_trades(conn, "ibs"), CONFIG["global"], today=date(2026, 12, 31))
    assert r["stats"]["n"] == 100 and r["stats"]["mean"] < 20 and r["status"] == "RETIRE"


def test_cli_import_ibs_dry_run_from_a_file(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"history": HISTORY}))
    monkeypatch.setattr(ibs_import, "fetch_opens_yahoo", lambda s, days=400: OPENS[s])
    assert main(["--db", str(tmp_path / "c.db"), "import-ibs", "--state", str(state), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valued"] == 2 and out["mean_bp"] == pytest.approx((-103.0 + 147.0) / 2)
