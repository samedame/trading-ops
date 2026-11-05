#!/usr/bin/env python3
"""Find which of your GitHub repos use a Tradier secret and set it in each one.

Dry run by default: it only reads workflow files and prints a plan. Nothing is written until you add --apply.

Requirements: the GitHub CLI (`gh`), logged in as the repo owner with permission to manage Actions secrets
(`gh auth login`, then `gh auth status` to check). The Tradier token is read from a hidden prompt, or from the
TRADIER_TOKEN environment variable. It is never accepted as a command-line argument (those leak into shell
history and process lists), never printed, and never written to disk. It is sent to GitHub over stdin.

How "needs it" is decided: a repo needs the secret if one of its workflow files references
`secrets.<NAME>` where NAME contains TRADIER (for example TRADIER_TOKEN or TRADIER_API_KEY). The secret is set
under whatever name each repo's workflows actually use. A repo whose code reads Tradier credentials some other
way (a committed config, a Cloudflare Worker secret) is not detected; pass it with --repos and --secret-name.

Examples
    python scripts/set_tradier_secret.py                       # dry run over every non-fork, non-archived repo
    python scripts/set_tradier_secret.py --apply               # set it (prompts for the token, verifies it first)
    python scripts/set_tradier_secret.py --apply --overwrite   # also replace secrets that already exist
    python scripts/set_tradier_secret.py --repos daytrading_bot,trading-ops --apply
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]*TRADIER[A-Za-z0-9_]*)", re.IGNORECASE)
URL_NAME_RE = re.compile(r"(URL|HOST|ENDPOINT|BASE)", re.IGNORECASE)
Runner = Callable[..., tuple]


def is_url_name(name: str) -> bool:
    """TRADIER_BASE_URL and friends hold an address, not a credential. They must never receive the token."""
    return bool(URL_NAME_RE.search(name))


def default_run(cmd: list, input_text: Optional[str] = None) -> tuple:
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("error: the GitHub CLI (gh) is not installed. Install it from https://cli.github.com "
                         "and run `gh auth login`.")
    return p.returncode, p.stdout, p.stderr


def list_repos(owner: str, run: Runner, include_forks: bool = False) -> list:
    rc, out, err = run(["gh", "repo", "list", owner, "--limit", "200", "--json", "name,isArchived,isFork"])
    if rc:
        raise RuntimeError(err.strip() or "gh repo list failed (is `gh auth status` OK?)")
    return sorted(r["name"] for r in json.loads(out) if not r["isArchived"] and (include_forks or not r["isFork"]))


def workflow_paths(owner: str, repo: str, run: Runner) -> list:
    rc, out, _ = run(["gh", "api", f"repos/{owner}/{repo}/contents/.github/workflows"])
    if rc:
        return []  # no workflows directory
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [i["path"] for i in items if isinstance(i, dict) and i.get("name", "").endswith((".yml", ".yaml"))]


def read_text(owner: str, repo: str, path: str, run: Runner) -> str:
    rc, out, _ = run(["gh", "api", "-H", "Accept: application/vnd.github.raw+json",
                      f"repos/{owner}/{repo}/contents/{path}"])
    return out if rc == 0 else ""


def secrets_used(owner: str, repo: str, run: Runner) -> set:
    names: set = set()
    for path in workflow_paths(owner, repo, run):
        names.update(m.group(1) for m in SECRET_RE.finditer(read_text(owner, repo, path, run)))
    return names


def existing_secrets(owner: str, repo: str, run: Runner) -> set:
    rc, out, _ = run(["gh", "secret", "list", "--repo", f"{owner}/{repo}", "--json", "name"])
    if rc:
        return set()
    return {s["name"] for s in json.loads(out)}


def build_plan(owner: str, repos: list, run: Runner, overwrite: bool = False,
               forced_name: Optional[str] = None, explicit: bool = False, base_url: Optional[str] = None) -> list:
    plan = []
    for repo in repos:
        names = secrets_used(owner, repo, run)
        if explicit and forced_name:
            names.add(forced_name)
        if not names:
            if explicit:
                plan.append({"repo": repo, "name": None, "action": "skip: no workflow references a Tradier secret "
                                                                    "(pass --secret-name to force)"})
            continue
        have = existing_secrets(owner, repo, run)
        for name in sorted(names):
            if is_url_name(name) and not base_url:
                plan.append({"repo": repo, "name": name, "kind": "url",
                             "action": "skip: URL setting, not a token (pass --base-url to set it)"})
            elif name in have and not overwrite:
                plan.append({"repo": repo, "name": name, "kind": "url" if is_url_name(name) else "token",
                             "action": "skip: already set (use --overwrite)"})
            else:
                plan.append({"repo": repo, "name": name, "kind": "url" if is_url_name(name) else "token",
                             "action": "replace" if name in have else "set"})
    return plan


def verify_token(token: str, opener=urllib.request.urlopen) -> str:
    """Return 'valid', 'invalid' or 'unreachable'. Calls Tradier's profile endpoint; token is only sent there."""
    req = urllib.request.Request("https://api.tradier.com/v1/user/profile",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with opener(req, timeout=20):
            return "valid"
    except urllib.error.HTTPError as exc:
        return "invalid" if exc.code in (401, 403) else "unreachable"
    except Exception:
        return "unreachable"


def get_token() -> str:
    token = os.environ.get("TRADIER_TOKEN") or getpass.getpass("Tradier token (input hidden): ")
    token = token.strip()
    if not token or re.search(r"\s", token):
        raise SystemExit("error: the token is empty or contains whitespace.")
    return token


def main(argv=None, run: Runner = default_run, verify: Callable = verify_token,
         token_source: Callable = get_token) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", default="samedame")
    ap.add_argument("--repos", help="comma-separated repo names (default: every non-fork, non-archived repo)")
    ap.add_argument("--secret-name", help="also set this secret name in each repo given with --repos")
    ap.add_argument("--apply", action="store_true", help="actually set secrets (default is a dry run)")
    ap.add_argument("--overwrite", action="store_true", help="replace secrets that already exist")
    ap.add_argument("--include-forks", action="store_true")
    ap.add_argument("--base-url", help="value for URL-type secrets such as TRADIER_BASE_URL. Without this they are "
                                       "skipped, never filled with the token")
    ap.add_argument("--no-verify", action="store_true", help="skip the check that the token works against Tradier")
    args = ap.parse_args(argv)

    explicit = bool(args.repos)
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] if explicit else \
        list_repos(args.owner, run, args.include_forks)
    plan = build_plan(args.owner, repos, run, args.overwrite, args.secret_name, explicit, args.base_url)

    print(f"Scanned {len(repos)} repo(s) under {args.owner}.")
    if not plan:
        print("No repo references a Tradier secret in its workflows. Nothing to do.")
        return 0
    width = max(len(p["repo"]) for p in plan)
    for p in plan:
        print(f"  {p['repo']:<{width}}  {p['name'] or '-':<24} {p['action']}")
    todo = [p for p in plan if p["action"] in ("set", "replace")]
    if not args.apply:
        print(f"\nDry run: {len(todo)} secret(s) would be written. Re-run with --apply to do it.")
        return 0
    if not todo:
        print("\nNothing to write.")
        return 0

    token = token_source() if any(p.get("kind") != "url" for p in todo) else ""
    if token and not args.no_verify:
        state = verify(token)
        if state == "invalid":
            print("error: Tradier rejected this token (401/403). Nothing was written.", file=sys.stderr)
            return 1
        print("Token check:", "accepted by Tradier." if state == "valid" else
              "could not reach Tradier to verify; continuing.")
    failures = 0
    for p in todo:
        value = args.base_url if p.get("kind") == "url" else token
        rc, _, err = run(["gh", "secret", "set", p["name"], "--repo", f"{args.owner}/{p['repo']}"], value)
        ok = rc == 0
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {args.owner}/{p['repo']} {p['name']}"
              + ("" if ok else f"  ({err.strip().splitlines()[-1] if err.strip() else 'unknown error'})"))
    print(f"\n{len(todo) - failures} written, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
