"""Hard spending limits. Agent may only deploy up to MAX_SPEND_USD."""

from __future__ import annotations

# HARD CAP — do not raise without explicit human approval.
MAX_SPEND_USD = 5.0


class SpendCapError(RuntimeError):
    """Raised when a BUY would exceed the hard USDC spend cap."""


def assert_buy_spend(usd: float, *, label: str = "order") -> None:
    """Abort if planned BUY notional exceeds the hard cap."""
    amount = float(usd)
    if amount < 0:
        raise SpendCapError(f"ABORT: {label} spend ${amount:.4f} is negative")
    if amount > MAX_SPEND_USD + 1e-9:
        raise SpendCapError(
            f"ABORT: {label} spend ${amount:.4f} exceeds hard cap ${MAX_SPEND_USD:.2f}"
        )


def clamp_buy_spend(usd: float) -> float:
    """Clamp a BUY amount down to the hard cap (never raise it)."""
    return min(max(float(usd), 0.0), MAX_SPEND_USD)


def max_shares_for_budget(cost_per_share: float, budget: float = MAX_SPEND_USD) -> float:
    """Max equal-size shares affordable under budget for a complete-set buy."""
    if cost_per_share <= 0:
        return 0.0
    return float(budget) / float(cost_per_share)
