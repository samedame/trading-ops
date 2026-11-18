"""Download the price files the benchmark ledger needs: VOO total-return closes and the 13-week bill yield.

    python scripts/fetch_benchmark_prices.py [--out data/benchmark] [--allow-proxy]

VOO comes from Yahoo's chart API (adjusted close = total return). Yahoo often refuses cloud IPs; run this on your own
computer or in a GitHub Action. If Yahoo fails and you pass --allow-proxy, SPY's adjusted close from the
Grand-Tournament repo snapshot is used instead, with a steady uplift of 6.45 bp a year added so the proxy is not
handicapped by SPY's higher expense ratio (about 0.0945% against VOO's 0.03%; check both before relying on the gap).
The choice is written to SOURCE.json and printed in every report, so a proxy can never pass for VOO.

The bill yield comes from FRED (DTB3). Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

UA = {"User-Agent": "Mozilla/5.0 (compatible; trading-ops-benchmark/1.0)"}      # Yahoo wants a browser-like agent
PLAIN_UA: dict = {}                                                              # FRED drops browser-like and unknown agents; urllib's default works
PROXY_UPLIFT_BP = 6.45
# explicit period1/period2: range=max can silently return monthly bars
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1=0&period2={now}&interval=1d&events=div%2Csplit"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
GT_SPY = "https://raw.githubusercontent.com/samedame/Grand-Tournament/main/data/raw/SPY.csv"


def _get(url: str, tries: int = 3, headers: dict | None = None) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA if headers is None else headers), timeout=60) as r:
                return r.read()
        except Exception as exc:                                # noqa: BLE001  flaky networks and proxies
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def fetch_yahoo(sym: str) -> dict[str, float]:
    res = json.loads(_get(YAHOO.format(sym=sym, now=int(time.time()))))["chart"]["result"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    out: dict[str, float] = {}
    for ts, v in zip(res["timestamp"], adj):
        if v is not None:
            out[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = float(v)
    if len(out) < 1000:
        raise RuntimeError(f"Yahoo returned only {len(out)} rows for {sym}")
    return out


def fetch_proxy() -> dict[str, float]:
    rows = csv.DictReader(io.StringIO(_get(GT_SPY).decode()))
    out = {r["date"]: float(r["adjclose"]) for r in rows if r.get("adjclose")}
    if len(out) < 1000:
        raise RuntimeError("SPY proxy file looks truncated")
    return out


def fetch_rates() -> dict[str, float]:
    rows = csv.reader(io.StringIO(_get(FRED, headers=PLAIN_UA).decode()))
    next(rows)
    return {d: float(v) for d, v in rows if v not in ("", ".")}


def build(out_dir: Path, allow_proxy: bool, fetch_voo: Callable | None = None,
          fetch_spy: Callable = fetch_proxy, fetch_bills: Callable = fetch_rates, now: datetime | None = None) -> dict:
    fetch_voo = fetch_voo or (lambda: fetch_yahoo("VOO"))
    out_dir.mkdir(parents=True, exist_ok=True)
    note, uplift = "Yahoo VOO adjusted close", 0.0
    try:
        prices = fetch_voo()
    except Exception as exc:                                    # noqa: BLE001  network, parse, anything
        if not allow_proxy:
            raise SystemExit(f"error: VOO download failed ({exc}). Run this on your own computer, or pass "
                             "--allow-proxy to use the SPY proxy.")
        prices, uplift = fetch_spy(), PROXY_UPLIFT_BP
        note = f"SPY adjusted close (Grand-Tournament snapshot) as a VOO proxy, +{PROXY_UPLIFT_BP} bp/yr; VOO failed: {exc}"
    with (out_dir / "voo.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "adjclose"])
        for d in sorted(prices):
            w.writerow([d, f"{prices[d]:.6f}"])
    rates_note = "FRED DTB3"
    try:
        rates = fetch_bills()
        with (out_dir / "dtb3.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "dtb3"])
            for d in sorted(rates):
                w.writerow([d, rates[d]])
    except Exception as exc:                                    # noqa: BLE001
        rates_note = f"bill yield not fetched: {exc}"
    meta = {"source": note, "uplift_bp": uplift, "rates": rates_note, "first": min(prices), "last": max(prices),
            "fetched_at": (now or datetime.now(timezone.utc)).isoformat()}
    (out_dir / "SOURCE.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "benchmark"))
    ap.add_argument("--allow-proxy", action="store_true", help="use SPY (+6.45 bp/yr) if the VOO download fails")
    args = ap.parse_args(argv)
    meta = build(Path(args.out), args.allow_proxy)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
