#!/usr/bin/env python3
"""Decide which quote provider the daily chain snapshot uses, and refuse to silently fall back.

The workflow used to say `PROVIDER: ${{ secrets.TRADIER_TOKEN && 'tradier' || 'yfinance' }}`: if the secret was missing or
named differently, the snapshot quietly came from yfinance (delayed, no greeks) and nobody was told.

Now:
  * TRADIER_TOKEN present                      -> provider=tradier
  * TRADIER_TOKEN missing, PROVIDER_ALLOW_FALLBACK=1 -> provider=yfinance, and a visible warning is printed
  * TRADIER_TOKEN missing, fallback NOT allowed -> post a Discord notice (best effort) and exit 1

Writes `provider=<name>` to $GITHUB_OUTPUT when set. Never prints a token or webhook. Standard library only. The Discord
post is fail-soft: a Discord outage cannot change the exit code (it is already 1 in the failing case).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def decide(env: dict) -> tuple:
    """Return (provider | None, message). provider None means: fail loudly."""
    if (env.get("TRADIER_TOKEN") or "").strip():
        return "tradier", "Provider: tradier (TRADIER_TOKEN is set)."
    if (env.get("PROVIDER_ALLOW_FALLBACK") or "").strip() == "1":
        return "yfinance", ("WARNING: TRADIER_TOKEN is not set; PROVIDER_ALLOW_FALLBACK=1, so this snapshot uses "
                            "yfinance (delayed, no greeks).")
    return None, ("trading-ops chain snapshot has no TRADIER_TOKEN secret, so it would silently use yfinance data. "
                  "Set the secret, or set the repo Variable PROVIDER_ALLOW_FALLBACK=1 to accept yfinance on purpose.")


def main(env=None, notify=None) -> int:
    env = os.environ if env is None else env
    provider, msg = decide(env)
    if provider:
        print(msg)
        out = env.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(f"provider={provider}\n")
        return 0
    print(f"ERROR: {msg}", file=sys.stderr)
    try:  # fail-soft Discord notice; never raises
        if notify is None:
            from notify_failure import post
            hook = env.get("DISCORD_WEBHOOK") or env.get("DISCORD_WEBHOOK_URL") or ""
            notify = (lambda text: post(hook, text)) if hook else (lambda text: False)
        link = ""
        if env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID"):
            link = f" {env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
        if notify(f"⚠️ **{msg}**{link}"):  # tell notify_failure.py this run already reported itself (no double message)
            open(os.path.join(env.get("MARKER_DIR") or os.path.join(HERE, ".."), ".notified"), "w").close()
    except Exception as exc:  # noqa: BLE001
        print(f"[resolve_provider] Discord notice failed: {type(exc).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
