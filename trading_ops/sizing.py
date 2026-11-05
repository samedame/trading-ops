"""Position-sizing and account-feasibility math.

Never guesses your account size: ACCOUNT_CAPITAL_USD must be set by you. Every function is pure so it can be
imported by any bot. Sizing is fixed-fractional on worst-case loss, and it rounds DOWN (zero is a valid answer).
"""
from __future__ import annotations

import math
import os

ENV_VAR = "ACCOUNT_CAPITAL_USD"


class CapitalNotSet(RuntimeError):
    pass


def capital_from_env(env: dict | None = None) -> float:
    env = os.environ if env is None else env
    raw = env.get(ENV_VAR)
    if raw is None or str(raw).strip() == "":
        raise CapitalNotSet(f"{ENV_VAR} is not set. Set it to the capital you are allocating to this bot. "
                            "It is deliberately never guessed.")
    try:
        value = float(str(raw).replace(",", "").replace("$", ""))
    except ValueError as exc:
        raise CapitalNotSet(f"{ENV_VAR}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise CapitalNotSet(f"{ENV_VAR} must be a positive number, got {raw!r}")
    return value


def spread_max_loss(width: float, credit: float, mult: float = 100) -> float:
    """Worst-case loss per credit spread contract, USD. Credit must be below width."""
    if width <= 0 or credit < 0 or credit >= width:
        raise ValueError("need 0 <= credit < width and width > 0")
    return (width - credit) * mult


def csp_collateral(strike: float, credit: float = 0.0, mult: float = 100) -> float:
    """Cash needed to fully secure one short put (strike minus credit received)."""
    if strike <= 0 or credit < 0 or credit >= strike:
        raise ValueError("invalid strike/credit")
    return (strike - credit) * mult


def contracts_for_risk(capital: float, risk_pct: float, max_loss_per_contract: float) -> int:
    """Whole contracts whose combined worst-case loss stays within risk_pct of capital. Rounds down."""
    if capital <= 0 or not (0 < risk_pct <= 1) or max_loss_per_contract <= 0:
        raise ValueError("capital > 0, 0 < risk_pct <= 1, max_loss > 0 required")
    return int(math.floor(capital * risk_pct / max_loss_per_contract + 1e-9))


def feasibility(capital: float, risk_pct: float, max_loss_per_contract: float) -> dict:
    n = contracts_for_risk(capital, risk_pct, max_loss_per_contract)
    return {
        "capital": capital,
        "risk_budget_usd": capital * risk_pct,
        "max_loss_per_contract_usd": max_loss_per_contract,
        "contracts": n,
        "tradable": n >= 1,
        "one_contract_risks_pct_of_capital": 100 * max_loss_per_contract / capital,
        "min_capital_for_one_contract": max_loss_per_contract / risk_pct,
        "realised_risk_pct": 100 * n * max_loss_per_contract / capital,
    }


def share_plan(capital: float, target_weight: float, price: float, fractional: bool = False) -> dict:
    """How far whole-share rounding pushes you from the target weight."""
    if capital <= 0 or not (0 < target_weight <= 1) or price <= 0:
        raise ValueError("capital > 0, 0 < target_weight <= 1, price > 0 required")
    target_usd = capital * target_weight
    shares = target_usd / price if fractional else math.floor(target_usd / price + 1e-9)
    actual_usd = shares * price
    return {
        "target_usd": target_usd,
        "shares": shares,
        "actual_usd": actual_usd,
        "actual_weight": actual_usd / capital,
        "weight_error_pct_points": 100 * (actual_usd / capital - target_weight),
        "tradable": shares > 0,
        "min_capital_for_one_share_at_target": price / target_weight,
    }
