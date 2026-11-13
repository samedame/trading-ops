"""Load the IBS callout bot's closed trades into the ledger, so the scorecard can judge the one bot with a tested edge.

The bot's state.json records each closed trade as a signal date and an exit-signal date, with no return. Its own rule
fills both at the NEXT session's open, so a trade is valued here exactly as its kill-switch monitor values it:

    return = open(first session after exit_signal_date) / open(first session after entry_date) - 1

minus the cost the verdict charged (1.5 bp a side). The unit is bp of the entry price, which is what
config/bots.json expects for `ibs`. These are alert-derived estimates in `paper` mode, not your fills: where you have
real fills, log them with `open` / `fill` / `close` instead and they will count the same way.

Re-running is safe: a trade already in the ledger (same symbol, signal date and exit-signal date) is skipped, and a
trade whose exit open has not printed yet is left for the next run.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import ledger

BOT = "ibs"
DEFAULT_STATE = "https://raw.githubusercontent.com/samedame/ibs-callout-bot/main/state.json"
COST_BP_PER_SIDE = 1.5          # claude/callout-bot-verdict.md: 1.5 bp a side on index ETFs
_UA = {"User-Agent": "Mozilla/5.0 (compatible; trading-ops-ibs-import/1.0)"}
_NY = ZoneInfo("America/New_York")

OpenFetcher = Callable[[str], dict]


def load_history(src: Optional[str] = None) -> list[dict]:
    """Closed trades from the bot's state.json. `src` is a file path or an http(s) URL."""
    src = src or DEFAULT_STATE
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(urllib.request.Request(src, headers=_UA), timeout=30) as r:
            state = json.load(r)
    else:
        with open(src, encoding="utf-8") as fh:
            state = json.load(fh)
    return list(state.get("history", []))


def fetch_opens_yahoo(symbol: str, days: int = 400) -> dict:
    """{YYYY-MM-DD (New York date): opening price} for the last `days` calendar days."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={days}d&interval=1d"
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    opens = res["indicators"]["quote"][0]["open"]
    out = {}
    for ts, o in zip(res["timestamp"], opens):
        if o is not None:
            out[datetime.fromtimestamp(ts, timezone.utc).astimezone(_NY).date().isoformat()] = float(o)
    return out


def next_open(opens: dict, after: str) -> Optional[tuple]:
    """(date, open) of the first session strictly after `after`, or None if it has not printed yet."""
    later = sorted(d for d in opens if d > after)
    return (later[0], opens[later[0]]) if later else None


def value_trades(history: list[dict], opens_by_symbol: dict, cost_bp_per_side: float = COST_BP_PER_SIDE) -> tuple:
    """Return (valued, skipped). Each valued trade carries its fill dates, prices and net return in bp."""
    valued, skipped = [], []
    for h in history:
        sym, sig, exit_sig = h.get("sym"), h.get("entry_date"), h.get("exit_signal_date")
        if not (sym and sig and exit_sig):
            skipped.append({"trade": h, "why": "missing sym, entry_date or exit_signal_date"})
            continue
        opens = opens_by_symbol.get(sym) or {}
        entry, exit_ = next_open(opens, sig), next_open(opens, exit_sig)
        if entry is None or exit_ is None or entry[1] <= 0:
            skipped.append({"trade": h, "why": "no opening price yet for the entry or the exit session"})
            continue
        gross_bp = (exit_[1] / entry[1] - 1.0) * 1e4
        valued.append({"sym": sym, "signal_date": sig, "exit_signal_date": exit_sig, "signal_ibs": h.get("signal_ibs"),
                       "entry_date": entry[0], "entry_px": entry[1], "exit_date": exit_[0], "exit_px": exit_[1],
                       "gross_bp": gross_bp, "ret_bp": gross_bp - 2 * cost_bp_per_side, "reason": h.get("reason")})
    valued.sort(key=lambda t: (t["exit_date"], t["sym"]))
    return valued, skipped


def _tag(t: dict) -> str:
    return f"ibs-import:{t['sym']}:{t['signal_date']}:{t['exit_signal_date']}"


def sync(conn, valued: list[dict]) -> dict:
    """Insert each valued trade once. Returns {'added': n, 'already': n}."""
    have = {r["notes"] for r in conn.execute("SELECT notes FROM trades WHERE bot=? AND notes LIKE 'ibs-import:%'", (BOT,))}
    added = 0
    for t in valued:
        tag = _tag(t)
        if tag in have:
            continue
        sid = ledger.log_signal(conn, BOT, t["sym"], "buy", t.get("signal_ibs"),
                                {"signal_date": t["signal_date"], "exit_signal_date": t["exit_signal_date"],
                                 "exit_reason": t.get("reason")}, ts=f"{t['signal_date']}T21:00:00Z")
        tid = ledger.open_trade(conn, BOT, "paper", t["sym"], 1, structure="etf_long", signal_id=sid,
                                entry_ref=t["entry_px"], opened_at=f"{t['entry_date']}T00:00:00Z", notes=tag)
        ledger.close_trade(conn, tid, pnl=round(t["ret_bp"], 2), pnl_unit="bp", closed_at=f"{t['exit_date']}T00:00:00Z")
        added += 1
    return {"added": added, "already": len(valued) - added}


def run(conn, src: Optional[str] = None, fetch: Optional[OpenFetcher] = None,
        cost_bp_per_side: float = COST_BP_PER_SIDE, dry_run: bool = False) -> dict:
    fetch = fetch or fetch_opens_yahoo
    history = load_history(src)
    opens, failed = {}, {}
    for sym in sorted({h.get("sym") for h in history if h.get("sym")}):
        try:
            opens[sym] = fetch(sym)
        except Exception as exc:                      # noqa: BLE001 - one dead symbol must not hide the others
            failed[sym] = repr(exc)
    valued, skipped = value_trades(history, opens, cost_bp_per_side)
    res = {"closed_in_state": len(history), "valued": len(valued), "skipped": len(skipped), "price_errors": failed,
           "mean_bp": (sum(t["ret_bp"] for t in valued) / len(valued)) if valued else None,
           "win_rate": (sum(1 for t in valued if t["ret_bp"] > 0) / len(valued)) if valued else None}
    if not dry_run:
        res.update(sync(conn, valued))
    res["trades"] = valued
    return res
