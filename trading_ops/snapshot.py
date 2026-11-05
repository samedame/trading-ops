"""Daily option-chain snapshotter.

Why it exists: every real-chain research track in your project was blocked by missing historical chains.
Snapshotting a few underlyings a day builds that dataset for free. It also records the H8 put ladder at
3.5 / 4.0 / 4.5 % OTM that the H8 verdict named as the most valuable thing to log.

Providers: YFinance (free, delayed, quotes can be zero off-hours) and Tradier (needs TRADIER_TOKEN).
The Tradier parser is tested against a canned payload only; it has NOT been run against the live API.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Protocol


@dataclass
class Quote:
    expiry: str
    strike: float
    right: str  # 'P' or 'C'
    bid: Optional[float]
    ask: Optional[float]
    iv: Optional[float] = None
    delta: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None


class Provider(Protocol):
    name: str

    def spot(self, symbol: str) -> float: ...
    def vix(self) -> Optional[float]: ...
    def expirations(self, symbol: str) -> list[str]: ...
    def chain(self, symbol: str, expiry: str) -> list[Quote]: ...


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot: float, strike: float, iv: float, dte: int, right: str, r: float = 0.04) -> Optional[float]:
    """Black-Scholes delta from the quote's own IV. None when inputs cannot support it."""
    t = max(dte, 0) / 365.0
    if not iv or iv <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0


def _clean(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


class YFinanceProvider:
    name = "yfinance"

    def __init__(self):
        import yfinance as yf  # lazy: only needed on the machine that runs the snapshotter

        self._yf = yf

    def spot(self, symbol: str) -> float:
        hist = self._yf.Ticker(symbol).history(period="1d")
        return float(hist["Close"].iloc[-1])

    def vix(self) -> Optional[float]:
        try:
            return self.spot("^VIX")
        except Exception:
            return None

    def expirations(self, symbol: str) -> list[str]:
        return list(self._yf.Ticker(symbol).options)

    def chain(self, symbol: str, expiry: str) -> list[Quote]:
        oc = self._yf.Ticker(symbol).option_chain(expiry)
        out: list[Quote] = []
        for right, frame in (("P", oc.puts), ("C", oc.calls)):
            for row in frame.to_dict("records"):
                out.append(Quote(expiry, float(row["strike"]), right, _clean(row.get("bid")), _clean(row.get("ask")),
                                 _clean(row.get("impliedVolatility")), None, _clean(row.get("volume")),
                                 _clean(row.get("openInterest"))))
        return out


class TradierProvider:
    name = "tradier"

    def __init__(self, token: Optional[str] = None, base: str = "https://api.tradier.com/v1"):
        self.token = token or os.environ.get("TRADIER_TOKEN")
        if not self.token:
            raise RuntimeError("TRADIER_TOKEN is not set")
        self.base = base

    def _get(self, path: str, params: dict) -> dict:
        req = urllib.request.Request(f"{self.base}{path}?{urllib.parse.urlencode(params)}",
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _quote(self, symbol: str) -> Optional[float]:
        q = self._get("/markets/quotes", {"symbols": symbol}).get("quotes", {}).get("quote")
        if isinstance(q, list):
            q = q[0] if q else None
        return _clean(q.get("last")) if q else None

    def spot(self, symbol: str) -> float:
        v = self._quote(symbol)
        if v is None:
            raise RuntimeError(f"no quote for {symbol}")
        return v

    def vix(self) -> Optional[float]:
        for sym in ("VIX", "$VIX.X"):  # symbol varies by account; the condor bot tries both
            try:
                v = self._quote(sym)
                if v:
                    return v
            except Exception:
                continue
        return None

    def expirations(self, symbol: str) -> list[str]:
        d = self._get("/markets/options/expirations", {"symbol": symbol}).get("expirations") or {}
        dates = d.get("date") or []
        return [dates] if isinstance(dates, str) else list(dates)

    def chain(self, symbol: str, expiry: str) -> list[Quote]:
        return parse_tradier_chain(self._get("/markets/options/chains",
                                             {"symbol": symbol, "expiration": expiry, "greeks": "true"}), expiry)


def parse_tradier_chain(payload: dict, expiry: str) -> list[Quote]:
    opts = (payload.get("options") or {}).get("option") or []
    if isinstance(opts, dict):
        opts = [opts]
    out = []
    for o in opts:
        g = o.get("greeks") or {}
        out.append(Quote(expiry, float(o["strike"]), "P" if o.get("option_type") == "put" else "C",
                         _clean(o.get("bid")), _clean(o.get("ask")), _clean(g.get("mid_iv")), _clean(g.get("delta")),
                         _clean(o.get("volume")), _clean(o.get("open_interest"))))
    return out


def snapshot(conn: sqlite3.Connection, provider: Provider, symbols: list[str], slot: str = "close",
             max_dte: int = 45, band: float = 0.08, today: Optional[date] = None,
             now: Optional[datetime] = None) -> int:
    """Store puts and calls within +/-band of spot for expiries up to max_dte. Re-running a slot replaces it."""
    today = today or date.today()
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    vix = provider.vix()
    stored = 0
    for sym in symbols:
        spot = provider.spot(sym)
        for exp in provider.expirations(sym):
            dte = (date.fromisoformat(exp) - today).days
            if dte < 0 or dte > max_dte:
                continue
            for q in provider.chain(sym, exp):
                if abs(q.strike / spot - 1.0) > band:
                    continue
                mid = (q.bid + q.ask) / 2 if q.bid is not None and q.ask is not None and q.ask > 0 else None
                otm = (1 - q.strike / spot) * 100 if q.right == "P" else (q.strike / spot - 1) * 100
                delta = q.delta if q.delta is not None else bs_delta(spot, q.strike, q.iv or 0, dte, q.right)
                conn.execute(
                    "INSERT OR REPLACE INTO chain_snapshots(snap_date, slot, ts, underlying, spot, vix, expiry, dte, "
                    "strike, right, otm_pct, bid, ask, mid, iv, delta, volume, open_interest, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (today.isoformat(), slot, ts, sym, spot, vix, exp, dte, q.strike, q.right, otm, q.bid, q.ask, mid,
                     q.iv, delta, q.volume, q.open_interest, provider.name))
                stored += 1
    conn.commit()
    return stored


EXPORT_COLUMNS = ["snap_date", "slot", "ts", "underlying", "spot", "vix", "expiry", "dte", "strike", "right",
                  "otm_pct", "bid", "ask", "mid", "iv", "delta", "volume", "open_interest", "source"]


def export_chains(conn: sqlite3.Connection, snap_date: str, slot: str, out_path: str) -> int:
    """Write one day/slot of snapshots to a gzip CSV (git-friendly: small, diffable, no binary DB in history)."""
    import csv
    import gzip

    rows = conn.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM chain_snapshots WHERE snap_date=? AND slot=? "
                        "ORDER BY underlying, expiry, right, strike", (snap_date, slot)).fetchall()
    with gzip.open(out_path, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for r in rows:
            w.writerow([r[c] for c in EXPORT_COLUMNS])
    return len(rows)


def put_ladder_bp(conn: sqlite3.Connection, snap_date: str, underlying: str = "SPY", slot: str = "close",
                  targets: tuple = (3.5, 4.0, 4.5), min_dte: int = 1) -> list[dict]:
    """Credit of the nearest-expiry OTM put at each target distance, in basis points of notional (never fixed $)."""
    row = conn.execute("SELECT MIN(dte) AS d FROM chain_snapshots WHERE snap_date=? AND slot=? AND underlying=? "
                       "AND right='P' AND dte>=?", (snap_date, slot, underlying, min_dte)).fetchone()
    if row is None or row["d"] is None:
        return []
    out = []
    for target in targets:
        r = conn.execute(
            "SELECT * FROM chain_snapshots WHERE snap_date=? AND slot=? AND underlying=? AND right='P' AND dte=? "
            "ORDER BY ABS(otm_pct - ?) LIMIT 1", (snap_date, slot, underlying, row["d"], target)).fetchone()
        if r is None:
            continue
        bp = lambda p: None if p is None else p / r["spot"] * 1e4  # noqa: E731
        out.append({"target_otm_pct": target, "actual_otm_pct": round(r["otm_pct"], 2), "strike": r["strike"],
                    "expiry": r["expiry"], "dte": r["dte"], "bid_bp": bp(r["bid"]), "mid_bp": bp(r["mid"]),
                    "ask_bp": bp(r["ask"]), "delta": r["delta"]})
    return out
