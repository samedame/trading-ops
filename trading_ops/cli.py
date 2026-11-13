"""Command line: python -m trading_ops <command>. Database path: --db or TRADING_OPS_DB (default ledger.db)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import freeze, ibs_import, ledger, scorecard, sizing, snapshot

ROOT = Path(__file__).resolve().parent.parent


def _load(path: str | None, default: str) -> dict:
    return json.loads(Path(path or ROOT / "config" / default).read_text())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading_ops", description=__doc__)
    p.add_argument("--db", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    s = sub.add_parser("signal")
    s.add_argument("--bot", required=True); s.add_argument("--symbol", required=True)
    s.add_argument("--side"); s.add_argument("--grade", type=float); s.add_argument("--params")
    s.add_argument("--skip-reason")

    o = sub.add_parser("open")
    o.add_argument("--bot", required=True); o.add_argument("--mode", required=True, choices=ledger.MODES)
    o.add_argument("--symbol", required=True); o.add_argument("--qty", type=float, required=True)
    o.add_argument("--structure"); o.add_argument("--signal-id", type=int); o.add_argument("--entry-ref", type=float)

    f = sub.add_parser("fill")
    f.add_argument("--trade-id", type=int, required=True)
    f.add_argument("--action", required=True, choices=("open", "close"))
    f.add_argument("--side", required=True, choices=ledger.SIDES)
    f.add_argument("--qty", type=float, required=True)
    f.add_argument("--ref", type=float, required=True, help="the price the alert/mid said you would get")
    f.add_argument("--price", type=float, required=True, help="the price you actually got")
    f.add_argument("--mult", type=float, default=100); f.add_argument("--fees", type=float, default=0.0)
    f.add_argument("--leg")
    f.add_argument("--expected-cost-per-lot", type=float,
                   help="prior bid/ask crossing cost per one-lot for this action; default: looked up from config/cost_priors.json by the trade's structure")

    c = sub.add_parser("close")
    c.add_argument("--trade-id", type=int, required=True)
    c.add_argument("--pnl", type=float, help="omit to compute USD P&L from fills")
    c.add_argument("--unit", default="usd")

    sl = sub.add_parser("slippage"); sl.add_argument("--bot")

    z = sub.add_parser("size")
    zs = z.add_subparsers(dest="kind", required=True)
    a = zs.add_parser("spread"); a.add_argument("--width", type=float, required=True)
    a.add_argument("--credit", type=float, required=True); a.add_argument("--risk-pct", type=float, default=0.02)
    a.add_argument("--mult", type=float, default=100)
    b = zs.add_parser("csp"); b.add_argument("--strike", type=float, required=True)
    b.add_argument("--credit", type=float, default=0.0)
    d = zs.add_parser("shares"); d.add_argument("--price", type=float, required=True)
    d.add_argument("--weight", type=float, required=True); d.add_argument("--fractional", action="store_true")

    sn = sub.add_parser("snapshot")
    sn.add_argument("--symbols", default="SPY,QQQ,IWM"); sn.add_argument("--slot", default="close")
    sn.add_argument("--provider", choices=("yfinance", "tradier"), default="yfinance")
    sn.add_argument("--max-dte", type=int, default=45)

    ex = sub.add_parser("export-chains")
    ex.add_argument("--date", required=True); ex.add_argument("--slot", default="close")
    ex.add_argument("--out", required=True)

    ld = sub.add_parser("ladder")
    ld.add_argument("--date", required=True); ld.add_argument("--underlying", default="SPY")
    ld.add_argument("--slot", default="close")

    sc = sub.add_parser("scorecard")
    sc.add_argument("--config"); sc.add_argument("--json", action="store_true")

    ib = sub.add_parser("import-ibs", help="load the IBS callout bot's closed trades (valued at the next open, in bp)")
    ib.add_argument("--state", help="path or URL of the bot's state.json (default: the public repo's main branch)")
    ib.add_argument("--cost-bp-per-side", type=float, default=ibs_import.COST_BP_PER_SIDE)
    ib.add_argument("--dry-run", action="store_true", help="value the trades and print them; write nothing")

    fr = sub.add_parser("freeze")
    fs = fr.add_subparsers(dest="kind", required=True)
    fs.add_parser("status")
    fe = fs.add_parser("exception"); fe.add_argument("--reason", required=True)
    fe.add_argument("--reverse-if", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = ledger.connect(args.db)
    out = lambda obj: print(json.dumps(obj, indent=2, default=str))  # noqa: E731

    if args.cmd == "init":
        print(f"ledger ready at {args.db or os.environ.get('TRADING_OPS_DB', 'ledger.db')}")
    elif args.cmd == "signal":
        print(ledger.log_signal(conn, args.bot, args.symbol, args.side, args.grade,
                                json.loads(args.params) if args.params else None, skip_reason=args.skip_reason))
    elif args.cmd == "open":
        print(ledger.open_trade(conn, args.bot, args.mode, args.symbol, args.qty, args.structure,
                                args.signal_id, args.entry_ref))
    elif args.cmd == "fill":
        ledger.add_fill(conn, args.trade_id, args.action, args.side, args.qty, args.ref, args.price,
                        args.mult, args.fees, args.leg, expected_cost_per_lot=args.expected_cost_per_lot)
        print("fill logged")
    elif args.cmd == "close":
        print(f"closed with pnl {ledger.close_trade(conn, args.trade_id, args.pnl, args.unit):.4f} {args.unit}")
    elif args.cmd == "slippage":
        out(ledger.slippage_summary(conn, args.bot))
    elif args.cmd == "size":
        if args.kind == "shares":
            cap = sizing.capital_from_env()
            out(sizing.share_plan(cap, args.weight, args.price, args.fractional))
        else:
            cap = sizing.capital_from_env()
            if args.kind == "spread":
                loss = sizing.spread_max_loss(args.width, args.credit, args.mult)
                print(f"(risk per trade = {args.risk_pct:.1%} of capital; this default is illustrative, set your own)")
                out(sizing.feasibility(cap, args.risk_pct, loss))
            else:
                need = sizing.csp_collateral(args.strike, args.credit)
                out({"collateral_per_contract": need, "capital": cap, "contracts_fully_secured": int(cap // need),
                     "tradable": cap >= need})
    elif args.cmd == "snapshot":
        prov = snapshot.YFinanceProvider() if args.provider == "yfinance" else snapshot.TradierProvider()
        n = snapshot.snapshot(conn, prov, [s.strip() for s in args.symbols.split(",")], args.slot, args.max_dte)
        print(f"stored {n} quotes")
    elif args.cmd == "export-chains":
        try:
            print(f"exported {snapshot.export_chains(conn, args.date, args.slot, args.out)} rows")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif args.cmd == "ladder":
        out(snapshot.put_ladder_bp(conn, args.date, args.underlying, args.slot))
    elif args.cmd == "scorecard":
        report = scorecard.run(conn, _load(args.config, "bots.json"))
        print(json.dumps(report, indent=2, default=str) if args.json else scorecard.render_markdown(report))
    elif args.cmd == "import-ibs":
        res = ibs_import.run(conn, args.state, cost_bp_per_side=args.cost_bp_per_side, dry_run=args.dry_run)
        out(res)
        if res["price_errors"] and not res["valued"]:
            print("error: no opening prices could be fetched, so nothing was valued", file=sys.stderr)
            return 1
    elif args.cmd == "freeze":
        cfg = _load(None, "freeze.json")
        if args.kind == "status":
            out({**freeze.status(cfg), "exceptions_logged": len(freeze.exceptions(conn)),
                 "allowed": cfg["allowed"], "forbidden": cfg["forbidden"]})
        else:
            print(f"exception logged (event {freeze.log_exception(conn, args.reason, args.reverse_if)})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except sizing.CapitalNotSet as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
