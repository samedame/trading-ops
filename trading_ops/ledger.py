"""Shared SQLite ledger for every bot: signals, trades, fills, chain snapshots, events.

One file (ledger.db) is the single source of truth for live-vs-backtest measurement.
Nothing here places orders. It only records what happened.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

MODES = ("paper", "shadow", "live")
SIDES = ("buy", "sell")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    bot TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    grade REAL,
    params_json TEXT,
    taken INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    bot TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper','shadow','live')),
    symbol TEXT NOT NULL,
    structure TEXT,
    qty REAL NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry_ref REAL,
    pnl REAL,
    pnl_unit TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    ts TEXT NOT NULL,
    leg TEXT,
    action TEXT NOT NULL CHECK (action IN ('open','close')),
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    qty REAL NOT NULL,
    mult REAL NOT NULL DEFAULT 100,
    ref_price REAL NOT NULL,
    fill_price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    slippage_per_unit REAL NOT NULL,
    slippage_usd REAL NOT NULL,
    expected_cost_per_lot REAL
);
CREATE TABLE IF NOT EXISTS chain_snapshots (
    snap_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    ts TEXT NOT NULL,
    underlying TEXT NOT NULL,
    spot REAL NOT NULL,
    vix REAL,
    expiry TEXT NOT NULL,
    dte INTEGER NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('P','C')),
    otm_pct REAL NOT NULL,
    bid REAL, ask REAL, mid REAL,
    iv REAL, delta REAL, volume REAL, open_interest REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (snap_date, slot, underlying, expiry, strike, right)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_bot ON trades(bot, closed_at);
CREATE INDEX IF NOT EXISTS idx_fills_trade ON fills(trade_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or os.environ.get("TRADING_OPS_DB", "ledger.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a ledger.db was first created (idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)")}
    if "expected_cost_per_lot" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN expected_cost_per_lot REAL")
        conn.commit()


def load_cost_priors(path: Optional[str] = None) -> dict:
    """config/cost_priors.json (or $TRADING_OPS_COST_PRIORS): expected bid/ask crossing cost per one-lot, by structure."""
    path = path or os.environ.get("TRADING_OPS_COST_PRIORS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "cost_priors.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def expected_cost_per_lot(structure: Optional[str], action: str, priors: Optional[dict] = None) -> Optional[float]:
    """Prior cost (USD per one-lot, all legs, one crossing) for `structure` ('open' or 'close'); None if the structure has no prior."""
    if not structure:
        return None
    priors = load_cost_priors() if priors is None else priors
    entry = (priors.get("structures") or {}).get(structure)
    if not entry:
        return None
    v = (entry.get("expected_cost_per_lot") or {}).get(action)
    return None if v is None else float(v)


def log_event(conn, kind: str, detail: str = "", ts: Optional[str] = None) -> int:
    cur = conn.execute("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)", (ts or utcnow(), kind, detail))
    conn.commit()
    return cur.lastrowid


def log_signal(conn, bot: str, symbol: str, side: Optional[str] = None, grade: Optional[float] = None,
               params: Optional[dict] = None, taken: bool = False, skip_reason: Optional[str] = None,
               ts: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO signals(ts, bot, symbol, side, grade, params_json, taken, skip_reason) VALUES (?,?,?,?,?,?,?,?)",
        (ts or utcnow(), bot, symbol, side, grade, json.dumps(params) if params else None, int(taken), skip_reason),
    )
    conn.commit()
    return cur.lastrowid


def open_trade(conn, bot: str, mode: str, symbol: str, qty: float, structure: Optional[str] = None,
               signal_id: Optional[int] = None, entry_ref: Optional[float] = None,
               opened_at: Optional[str] = None, notes: Optional[str] = None) -> int:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if qty <= 0:
        raise ValueError("qty must be positive")
    cur = conn.execute(
        "INSERT INTO trades(bot, mode, symbol, structure, qty, signal_id, opened_at, entry_ref, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (bot, mode, symbol, structure, qty, signal_id, opened_at or utcnow(), entry_ref, notes),
    )
    if signal_id:
        conn.execute("UPDATE signals SET taken=1 WHERE id=?", (signal_id,))
    conn.commit()
    return cur.lastrowid


def slippage_per_unit(side: str, ref_price: float, fill_price: float) -> float:
    """Positive = cost to you. Buying above ref or selling below ref is a cost."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    return (fill_price - ref_price) if side == "buy" else (ref_price - fill_price)


def add_fill(conn, trade_id: int, action: str, side: str, qty: float, ref_price: float, fill_price: float,
             mult: float = 100, fees: float = 0.0, leg: Optional[str] = None, ts: Optional[str] = None,
             expected_cost_per_lot: Optional[float] = None) -> int:
    spu = slippage_per_unit(side, ref_price, fill_price)
    if expected_cost_per_lot is None:   # default: the prior for this trade's structure, if one exists
        row = conn.execute("SELECT structure FROM trades WHERE id=?", (trade_id,)).fetchone()
        expected_cost_per_lot = expected_cost_per_lot_for(row["structure"] if row else None, action)
    cur = conn.execute(
        "INSERT INTO fills(trade_id, ts, leg, action, side, qty, mult, ref_price, fill_price, fees, "
        "slippage_per_unit, slippage_usd, expected_cost_per_lot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, ts or utcnow(), leg, action, side, qty, mult, ref_price, fill_price, fees, spu, spu * qty * mult,
         expected_cost_per_lot),
    )
    conn.commit()
    return cur.lastrowid


expected_cost_per_lot_for = expected_cost_per_lot   # alias: add_fill has a parameter with the same name


def cost_vs_prior(conn, bot: Optional[str] = None) -> list[dict]:
    """Per (bot, structure, action): actual crossing cost per lot vs the prior, for fills that carry a prior.

    actual per lot = sum of the leg slippages of one action on one trade / the trade's lot count. Positive excess = worse than the prior."""
    q = ("SELECT t.id AS trade_id, t.bot AS bot, t.structure AS structure, f.action AS action, t.qty AS lots, "
         "SUM(f.slippage_usd) AS actual_usd, MAX(f.expected_cost_per_lot) AS expected FROM fills f "
         "JOIN trades t ON t.id=f.trade_id WHERE f.expected_cost_per_lot IS NOT NULL")
    args: list[Any] = []
    if bot:
        q += " AND t.bot=?"
        args.append(bot)
    q += " GROUP BY t.id, f.action"
    agg: dict = {}
    for r in conn.execute(q, args).fetchall():
        actual = r["actual_usd"] / r["lots"] if r["lots"] else 0.0
        a = agg.setdefault((r["bot"], r["structure"], r["action"]), {"actual": [], "expected": []})
        a["actual"].append(actual)
        a["expected"].append(r["expected"])
    out = []
    for (b, st, act), a in sorted(agg.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        n = len(a["actual"])
        worse = sum(1 for x, e in zip(a["actual"], a["expected"]) if x > e)
        out.append({"bot": b, "structure": st, "action": act, "n": n,
                    "avg_actual_cost_per_lot": sum(a["actual"]) / n, "avg_expected_cost_per_lot": sum(a["expected"]) / n,
                    "avg_excess_vs_prior_per_lot": (sum(a["actual"]) - sum(a["expected"])) / n, "share_worse_than_prior": worse / n})
    return out


def pnl_from_fills(conn, trade_id: int) -> float:
    """Net cash P&L in USD. Log an expiry-worthless short as a buy at 0 so the trade nets out."""
    rows = conn.execute("SELECT side, qty, mult, fill_price, fees FROM fills WHERE trade_id=?", (trade_id,)).fetchall()
    if not rows:
        raise ValueError("no fills logged for this trade")
    total = 0.0
    for r in rows:
        cash = r["fill_price"] * r["qty"] * r["mult"]
        total += cash if r["side"] == "sell" else -cash
        total -= r["fees"]
    return total


def close_trade(conn, trade_id: int, pnl: Optional[float] = None, pnl_unit: str = "usd",
                closed_at: Optional[str] = None, notes: Optional[str] = None) -> float:
    if pnl is None:
        pnl, pnl_unit = pnl_from_fills(conn, trade_id), "usd"
    conn.execute("UPDATE trades SET pnl=?, pnl_unit=?, closed_at=?, notes=COALESCE(?, notes) WHERE id=?",
                 (pnl, pnl_unit, closed_at or utcnow(), notes, trade_id))
    conn.commit()
    return pnl


def closed_trades(conn, bot: Optional[str] = None, modes: Optional[tuple] = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM trades WHERE closed_at IS NOT NULL AND pnl IS NOT NULL"
    args: list[Any] = []
    if bot:
        q += " AND bot=?"
        args.append(bot)
    if modes:
        q += f" AND mode IN ({','.join('?' * len(modes))})"
        args.extend(modes)
    return conn.execute(q + " ORDER BY closed_at, id", args).fetchall()


def slippage_summary(conn, bot: Optional[str] = None) -> list[dict]:
    q = ("SELECT t.bot AS bot, f.action AS action, COUNT(*) AS n, AVG(f.slippage_per_unit) AS avg_per_unit, "
         "SUM(f.slippage_usd) AS total_usd, AVG(CASE WHEN f.ref_price != 0 THEN f.slippage_per_unit / f.ref_price END) "
         "AS avg_pct_of_ref FROM fills f JOIN trades t ON t.id=f.trade_id")
    args: list[Any] = []
    if bot:
        q += " WHERE t.bot=?"
        args.append(bot)
    q += " GROUP BY t.bot, f.action ORDER BY t.bot, f.action"
    return [dict(r) for r in conn.execute(q, args).fetchall()]
