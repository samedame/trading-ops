"""90-day build freeze tracker. Exceptions are logged to the ledger so the freeze is auditable."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

from . import ledger


def window(cfg: dict) -> tuple[date, date]:
    start = date.fromisoformat(cfg["start"])
    return start, start + timedelta(days=cfg.get("days", 90))


def status(cfg: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    start, end = window(cfg)
    if today < start:
        state, left = "NOT STARTED", (end - start).days
    elif today < end:
        state, left = "ACTIVE", (end - today).days
    else:
        state, left = "ENDED", 0
    return {"state": state, "start": start.isoformat(), "end": end.isoformat(), "days_left": left,
            "day_number": max(0, (today - start).days + 1) if today >= start else 0}


def log_exception(conn, reason: str, reverse_if: str) -> int:
    if not reason.strip() or not reverse_if.strip():
        raise ValueError("an exception needs both a reason and the result that would make you reverse it")
    return ledger.log_event(conn, "freeze_exception", json.dumps({"reason": reason, "reverse_if": reverse_if}))


def exceptions(conn) -> list[dict]:
    return [{"ts": r["ts"], **json.loads(r["detail"])} for r in
            conn.execute("SELECT ts, detail FROM events WHERE kind='freeze_exception' ORDER BY id")]
