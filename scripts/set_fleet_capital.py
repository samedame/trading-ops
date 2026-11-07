#!/usr/bin/env python3
"""Set every bot's capital Repo Variable from ONE table (config/capital.json).

Today the same account size lives under five different Variable names in ten repos. This reads the table, checks it,
prints how much of the account is allocated, and (only with --apply) writes each Variable with the GitHub CLI.

Dry run by default. It refuses to run if `total_account_usd` or any bot's `usd` is null: it never invents a number.
It sets Repo *Variables* only (never secrets), and prints the plan without any token or webhook value.

    python scripts/set_fleet_capital.py            # dry run: validate + print the plan
    python scripts/set_fleet_capital.py --apply    # write the Variables
    python scripts/set_fleet_capital.py --config path/to/capital.json --only Apex_Chronos,Tier1-Bot

Overcommit ratio = sum of per-bot usd / total_account_usd. Above 1.0 the bots together are sized for more money than
the account holds; that prints a warning but is not blocked (a deliberate choice is yours to make).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from typing import Callable, Optional

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "capital.json")
Runner = Callable[..., tuple]


def default_run(cmd: list, input_text: Optional[str] = None) -> tuple:
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("error: the GitHub CLI (gh) is not installed. Install it from https://cli.github.com "
                         "and run `gh auth login`.")
    return p.returncode, p.stdout, p.stderr


def _valid_usd(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def validate(cfg: dict) -> list:
    """Return a list of problems (empty = OK). Nulls are problems: nothing is guessed."""
    problems = []
    if not _valid_usd(cfg.get("total_account_usd")):
        problems.append("total_account_usd is null or not a positive number")
    bots = cfg.get("bots")
    if not isinstance(bots, list) or not bots:
        return problems + ["no bots listed"]
    seen = set()
    for b in bots:
        tag = b.get("bot") or b.get("repo") or "?"
        if not _valid_usd(b.get("usd")):
            problems.append(f"{tag}: usd is null or not a positive number")
        if not str(b.get("repo", "")).count("/") == 1:
            problems.append(f"{tag}: repo must be 'owner/name'")
        if not b.get("variables"):
            problems.append(f"{tag}: no variable names")
        for name in b.get("variables") or []:
            key = (b.get("repo"), name)
            if key in seen:
                problems.append(f"{tag}: duplicate {b.get('repo')} {name}")
            seen.add(key)
    return problems


def fmt_usd(v) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def overcommit(cfg: dict) -> tuple:
    """(sum of per-bot usd, ratio to total). Only call after validate() passes."""
    s = sum(float(b["usd"]) for b in cfg["bots"])
    return s, s / float(cfg["total_account_usd"])


def plan(cfg: dict, only: Optional[set] = None) -> list:
    rows = []
    for b in cfg["bots"]:
        repo_name = b["repo"].split("/", 1)[1]
        if only and repo_name not in only and b["repo"] not in only:
            continue
        for name in b["variables"]:
            rows.append({"bot": b["bot"], "repo": b["repo"], "name": name, "value": fmt_usd(b["usd"]),
                         "note": b.get("note", "")})
    return rows


def main(argv=None, run: Runner = default_run) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--apply", action="store_true", help="actually set the Variables (default is a dry run)")
    ap.add_argument("--only", help="comma-separated repo names to limit the run to")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    problems = validate(cfg)
    if problems:
        print("Refusing to run: config/capital.json is not filled in. Nothing was written.", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    total, ratio = overcommit(cfg)
    print(f"Account total: ${fmt_usd(cfg['total_account_usd'])}   allocated across bots: ${fmt_usd(total)}   "
          f"overcommit ratio: {ratio:.2f}")
    if ratio > 1.0:
        print(f"WARNING: overcommitted. The bots are sized against {ratio:.2f}x the account "
              f"(${fmt_usd(total - cfg['total_account_usd'])} more than it holds).")

    only = {r.strip() for r in args.only.split(",") if r.strip()} if args.only else None
    rows = plan(cfg, only)
    if not rows:
        print("Nothing matches --only; nothing to do.")
        return 0
    for r in rows:
        print(f"  {r['repo']:<42} {r['name']:<28} = {r['value']:>10}" + (f"   ({r['note']})" if r["note"] else ""))
    if not args.apply:
        print(f"\nDry run: {len(rows)} Variable(s) would be written. Re-run with --apply to do it.")
        return 0

    failures = 0
    for r in rows:
        rc, _, err = run(["gh", "variable", "set", r["name"], "--body", r["value"], "--repo", r["repo"]])
        ok = rc == 0
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {r['repo']} {r['name']}"
              + ("" if ok else f"  ({err.strip().splitlines()[-1] if err.strip() else 'unknown error'})"))
    print(f"\n{len(rows) - failures} written, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
