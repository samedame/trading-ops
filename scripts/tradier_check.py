#!/usr/bin/env python3
"""Manual live-data check for trading-ops' Tradier provider. Posts nothing, writes nothing, never prints the token.

Run it from the "Tradier check (manual)" workflow (uses this repo's TRADIER_TOKEN secret) or locally:
    TRADIER_TOKEN=... python scripts/tradier_check.py

It runs the SAME code path the daily snapshot uses (TradierProvider + parse_tradier_chain) against the live API, which
nothing had done before, and reports how old the quotes are.
Exit 0 = production, parser works, and (market open) quotes under 2 minutes old. Exit 1 = anything else.
"""
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from trading_ops.snapshot import TradierProvider, parse_tradier_chain  # noqa: E402

FRESH_MAX_MIN = 2.0


def _lst(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def _age_min(rec, now):
    stamps = [rec.get(k) for k in ("trade_date", "bid_date", "ask_date") if rec.get(k)]
    return None if not stamps else max(0.0, now - max(stamps) / 1000.0) / 60.0


def run(provider=None, symbol="SPY", now=None, say=print):
    now = time.time() if now is None else now
    try:
        p = provider or TradierProvider(token=os.environ.get("TRADIER_TOKEN") or os.environ.get("TRADIER_API_KEY"))
    except RuntimeError:
        say("FAIL: the TRADIER_TOKEN secret is empty or missing in this repo.")
        return 1
    say(f"Host: {p.base} (this provider is production-only)")
    try:
        clock = p._get("/markets/clock", {}).get("clock", {})
        q = _lst((p._get("/markets/quotes", {"symbols": symbol}).get("quotes") or {}).get("quote"))
        q = q[0] if q else {}
        if not q.get("last"):
            say(f"FAIL: Tradier returned no quote for {symbol}.")
            return 1
        exps = p.expirations(symbol)
        if not exps:
            say("FAIL: no option expirations returned.")
            return 1
        raw = p._get("/markets/options/chains", {"symbol": symbol, "expiration": exps[0], "greeks": "true"})
        parsed = parse_tradier_chain(raw, exps[0])
    except urllib.error.HTTPError as e:
        say(f"FAIL: HTTP {e.code} from Tradier (401/403 = token rejected).")
        return 1
    except Exception as e:  # noqa: BLE001
        say(f"FAIL: {type(e).__name__} while reading Tradier.")
        return 1
    spot = float(q["last"])
    near = sorted(parsed, key=lambda r: abs(r.strike - spot))[:6]
    with_px = [r for r in near if r.bid and r.ask]
    with_delta = [r for r in near if r.delta is not None]
    say(f"Parser: {len(parsed)} contracts parsed for {symbol} {exps[0]}; {len(with_px)}/{len(near)} ATM rows have bid+ask, "
        f"{len(with_delta)}/{len(near)} have delta")
    qage = _age_min(q, now)
    raw_near = sorted(_lst((raw.get("options") or {}).get("option")), key=lambda o: abs(float(o["strike"]) - spot))[:6]
    oages = [a for a in (_age_min(o, now) for o in raw_near) if a is not None]
    oage = min(oages) if oages else None
    say(f"Market: {clock.get('state')}. {symbol} last={q.get('last')}, newest stamp "
        + ("none" if qage is None else f"{qage:.1f} min old")
        + "; freshest ATM option quote " + ("has no timestamp" if oage is None else f"{oage:.1f} min old"))
    if not parsed or (clock.get("state") == "open" and not with_px):
        say("FAIL: the chain parsed to nothing usable (no bid/ask near the money).")
        return 1
    if clock.get("state") != "open":
        say("PASS (token works and the parser reads the real chain). Freshness can't be judged while the market is closed.")
        return 0
    worst = max([a for a in (qage, oage) if a is not None], default=None)
    if worst is None:
        say("FAIL: market is open but no timestamps came back, so freshness can't be confirmed.")
        return 1
    if worst > FRESH_MAX_MIN:
        say(f"FAIL: quotes are {worst:.0f} min old while the market is open (delayed feed).")
        return 1
    say("PASS: LIVE. Market open, parser works, quotes under 2 minutes old.")
    return 0


if __name__ == "__main__":
    sys.exit(run(symbol=(sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()))
