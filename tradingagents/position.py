"""Rating-to-position mapping (R3): a pure, deterministic core.

Turns the Portfolio Manager's 5-tier rating plus an explicitly supplied current
weight into an executable target position, with a fixed single-name cap and an
optional volatility-scaled cap. Nothing here calls a model, reads the network or
touches the filesystem: given the same inputs it always returns the same signal,
so it can be unit-tested in isolation and reused by the graph, the CLI and the
report writers alike.

The rating itself is never computed or overridden here. It is read elsewhere
(``tradingagents.agents.rating.run_rating``) and passed in, so the existing
propagate contract ``(final_state, rating_string)`` is preserved.

Policy (fractions in [0, 1], never absolute prices):

- Buy            -> the configured fixed target weight.
- Overweight     -> current weight + increase step.
- Hold           -> current weight.
- Underweight    -> max(0, current weight - decrease step); never adds exposure.
- Sell           -> zero.
- Anything else  -> needs_review (no executable weight).

The raw target is then clamped by an effective cap, the fixed single-name cap
reduced by volatility:

    scale = 1 if daily volatility is 0 else min(1, threshold / daily volatility)
    effective_cap = max_position_weight * scale
    final weight = min(raw_target, effective_cap)

A positive target requires a reliable, explicitly supplied, non-negative daily
volatility when scaling is enabled; a missing one is ``needs_data`` rather than a
fabricated low-volatility assumption. Zero targets need no volatility and carry
no stop loss. Confidence is never a model-produced probability: it is always
``None`` with source ``"not_provided"``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

# Reason/status vocabulary. ``status`` is ``"ok"`` on a computed weight, else a
# short machine-readable code naming why no executable weight was produced.
STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_NEEDS_CONTEXT = "needs_context"
STATUS_NEEDS_DATA = "needs_data"

# The canonical, ordered 5-tier scale (most bullish to most bearish). Kept local
# so this module has no dependency on the agents package and stays importable on
# its own; the values match ``tradingagents.agents.rating.RATINGS_5_TIER``.
RATINGS_5_TIER: tuple[str, ...] = ("Buy", "Overweight", "Hold", "Underweight", "Sell")

CONFIDENCE_SOURCE_NOT_PROVIDED = "not_provided"

# The canonical non-tradeable sentinel, matching
# ``tradingagents.agents.rating.RATING_REVIEW``; kept local so the signal's
# rating stays JSON-safe without importing the agents package.
RATING_REVIEW = "REVIEW"

# Ratings whose target is the fixed Buy target rather than current-relative.
_FIXED_TARGET = "Buy"
_KNOWN_RATINGS = frozenset(RATINGS_5_TIER)


def _is_real_number(value: Any) -> bool:
    """A finite real number, excluding booleans.

    ``bool`` is a subclass of ``int`` in Python, so ``True`` would otherwise pass
    as ``1``; and JSON permits ``NaN``/``Infinity``. Neither is a usable weight.
    An integer too large to convert to a float (e.g. ``10**400``) is not finite
    as a weight, so it is rejected here rather than letting ``math.isfinite``
    raise ``OverflowError`` further down.
    """
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _fraction(name: str, value: Any) -> float:
    """A number in [0, 1], or ValueError. Used for weights and the cap."""
    if not _is_real_number(value):
        raise ValueError(f"{name} must be a finite real number, got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be within [0, 1], got {value!r}")
    return number


def _step(name: str, value: Any) -> float:
    """A change size in [0, 1], or ValueError."""
    if not _is_real_number(value):
        raise ValueError(f"{name} must be a finite real number, got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be within [0, 1], got {value!r}")
    return number


def _positive(name: str, value: Any) -> float:
    """A strictly positive finite number, or ValueError."""
    if not _is_real_number(value):
        raise ValueError(f"{name} must be a finite real number, got {value!r}")
    number = float(value)
    if number <= 0.0:
        raise ValueError(f"{name} must be strictly positive, got {value!r}")
    return number


@dataclass(frozen=True)
class PositionPolicy:
    """The configurable mapping parameters. Frozen and validated on construction.

    All weights are fractions of the portfolio (0.20 is 20%), never absolute
    prices or share counts. Defaults match the approved R3 policy v1.
    """

    buy_target_weight: float = 0.20
    step: float = 0.05
    max_position_weight: float = 0.20
    volatility_threshold: float = 0.02
    volatility_scaling: bool = True
    stop_loss_pct: float = 0.08
    policy_version: str = "r3-v1"

    def __post_init__(self) -> None:
        # Validate explicit values; a caller passing a bad policy learns now,
        # before any run, rather than producing a silently wrong weight.
        object.__setattr__(
            self, "buy_target_weight", _fraction("buy_target_weight", self.buy_target_weight)
        )
        object.__setattr__(self, "step", _step("step", self.step))
        object.__setattr__(
            self, "max_position_weight",
            _fraction("max_position_weight", self.max_position_weight),
        )
        object.__setattr__(
            self, "volatility_threshold",
            _positive("volatility_threshold", self.volatility_threshold),
        )
        object.__setattr__(
            self, "stop_loss_pct", _stop_loss("stop_loss_pct", self.stop_loss_pct)
        )
        if not isinstance(self.volatility_scaling, bool):
            raise ValueError(
                f"volatility_scaling must be a boolean, got {self.volatility_scaling!r}"
            )
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError(
                f"policy_version must be a non-empty string, got {self.policy_version!r}"
            )


def _stop_loss(name: str, value: Any) -> float:
    """A stop-loss fraction in [0, 1), or ValueError. 1.0 would be a total loss."""
    if not _is_real_number(value):
        raise ValueError(f"{name} must be a finite real number, got {value!r}")
    number = float(value)
    if not 0.0 <= number < 1.0:
        raise ValueError(f"{name} must be within [0, 1), got {value!r}")
    return number


@dataclass(frozen=True)
class PositionSignal:
    """The result of mapping one rating to a position. JSON-safe via ``to_dict``.

    ``status`` is ``"ok"`` only when ``weight`` is a computed number. On a
    non-ok status ``weight`` (and every executable-weight field) is ``None``:
    a failure must never look like a tradeable position. The provenance fields
    after ``cap_override`` are optional so a later OHLCV adapter can fill them
    without changing this core's callers.
    """

    rating: str | None
    status: str
    weight: float | None
    stop_loss_pct: float | None
    confidence: float | None
    confidence_source: str
    current_weight: float | None
    raw_target_weight: float | None
    effective_cap: float | None
    daily_volatility: float | None
    reason: str
    policy_version: str
    cap_override: bool
    # Provenance, filled by a later adapter; safe defaults keep this core standalone.
    input_as_of: str | None = None
    input_digest: str | None = None
    data_source: str | None = None
    last_price_date: str | None = None
    sample_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict (floats already finite or ``None``)."""
        return asdict(self)


def _failure(
    rating: str | None,
    status: str,
    reason: str,
    policy: PositionPolicy,
    *,
    current_weight: float | None = None,
) -> PositionSignal:
    """A non-executable result: every weight field is None, confidence is None."""
    return PositionSignal(
        rating=rating,
        status=status,
        weight=None,
        stop_loss_pct=None,
        confidence=None,
        confidence_source=CONFIDENCE_SOURCE_NOT_PROVIDED,
        current_weight=current_weight,
        raw_target_weight=None,
        effective_cap=None,
        daily_volatility=None,
        reason=reason,
        policy_version=policy.policy_version,
        cap_override=False,
    )


def _validate_explicit(current_weight: Any, daily_volatility: Any) -> None:
    """Reject a supplied-but-invalid number, before any mapping happens.

    Missing (``None``) is not invalid: it is handled per rating later. But a
    value that is present and not a usable number (bool, NaN, inf, out-of-range
    weight, negative volatility) fails now, even for Sell, since the caller
    clearly meant to supply it.
    """
    if current_weight is not None:
        _fraction("current_weight", current_weight)
    if daily_volatility is not None:
        # Volatility is an unbounded, non-negative fraction; 0 is the valid flat case.
        if not _is_real_number(daily_volatility):
            raise ValueError(
                f"daily_volatility must be a finite real number, got {daily_volatility!r}"
            )
        if float(daily_volatility) < 0.0:
            raise ValueError(
                f"daily_volatility must be non-negative, got {daily_volatility!r}"
            )


def _raw_target(rating: str, current_weight: float, policy: PositionPolicy) -> float:
    """The pre-cap target weight for a recognized, non-Sell rating."""
    if rating == _FIXED_TARGET:
        return policy.buy_target_weight
    if rating == "Overweight":
        return current_weight + policy.step
    if rating == "Hold":
        return current_weight
    if rating == "Underweight":
        return max(0.0, current_weight - policy.step)
    raise AssertionError(f"unhandled recognized rating {rating!r}")


def _normalize_rating(rating: Any) -> str | None:
    """A JSON-safe rating token, or ``None`` for anything that is not a string.

    A string is trimmed and capitalized; the non-tradeable sentinel (any casing of
    "review") is returned as the canonical ``REVIEW``. Anything that is not a
    string (``None``, a number, or an unhashable list or dict) has no rating to
    recognize; returning ``None`` keeps ``PositionSignal`` serializable instead of
    letting an unhashable value reach a set lookup.
    """
    if not isinstance(rating, str):
        return None
    token = rating.strip()
    if token.lower() == RATING_REVIEW.lower():
        return RATING_REVIEW
    return token.capitalize()


def process_position(
    rating: str | None,
    current_weight: float | None = None,
    daily_volatility: float | None = None,
    policy: PositionPolicy | None = None,
) -> PositionSignal:
    """Map ``rating`` (+ explicit context) to a target position signal.

    Pure and deterministic: same inputs give the same ``PositionSignal``, and the
    inputs are never mutated. Raises ``ValueError`` for an explicitly supplied
    value that is not a valid number (before any mapping), but treats a missing
    value as a per-rating condition (``needs_context``/``needs_data``), never as
    zero.
    """
    if policy is None:
        policy = PositionPolicy()
    elif not isinstance(policy, PositionPolicy):
        raise ValueError(
            f"policy must be a PositionPolicy, got {type(policy).__name__}"
        )
    _validate_explicit(current_weight, daily_volatility)

    normalized = _normalize_rating(rating)

    # REVIEW / unrecognized / missing rating: no executable position at all.
    if normalized not in _KNOWN_RATINGS:
        return _failure(
            normalized,
            STATUS_NEEDS_REVIEW,
            "No executable rating was recognized (missing, REVIEW, or unknown); "
            "this needs a human or a re-run, not a position.",
            policy,
            current_weight=float(current_weight) if current_weight is not None else None,
        )

    # Sell clears a position; it needs neither the current weight nor prices.
    if normalized == "Sell":
        signal = PositionSignal(
            rating=normalized,
            status=STATUS_OK,
            weight=0.0,
            stop_loss_pct=None,
            confidence=None,
            confidence_source=CONFIDENCE_SOURCE_NOT_PROVIDED,
            current_weight=float(current_weight) if current_weight is not None else None,
            raw_target_weight=0.0,
            effective_cap=None,
            daily_volatility=float(daily_volatility) if daily_volatility is not None else None,
            reason="Sell maps to a zero target; no volatility or current holding is required.",
            policy_version=policy.policy_version,
            cap_override=False,
        )
        return signal

    # Every other recognized rating sizes against the current weight, which must
    # be supplied explicitly: an unknown weight is not a flat book.
    if current_weight is None:
        return _failure(
            normalized,
            STATUS_NEEDS_CONTEXT,
            f"{normalized} needs an explicit current weight; without one the target "
            "cannot be computed, and an unknown weight is not treated as zero.",
            policy,
        )

    weight = float(current_weight)
    raw_target = _raw_target(normalized, weight, policy)

    # A zero raw target (e.g. Underweight on a small holding, or a zero Buy target)
    # is a decision of zero: no volatility fetch and no stop loss needed.
    if raw_target == 0.0:
        return PositionSignal(
            rating=normalized,
            status=STATUS_OK,
            weight=0.0,
            stop_loss_pct=None,
            confidence=None,
            confidence_source=CONFIDENCE_SOURCE_NOT_PROVIDED,
            current_weight=weight,
            raw_target_weight=0.0,
            effective_cap=None,
            daily_volatility=float(daily_volatility) if daily_volatility is not None else None,
            reason=f"{normalized} resolves to a zero target; no volatility or stop loss is required.",
            policy_version=policy.policy_version,
            cap_override=False,
        )

    # A fixed zero cap means no positive position can be held: zero without any
    # volatility requirement, regardless of scaling.
    if policy.max_position_weight == 0.0:
        return _sized(
            normalized, weight, raw_target, policy,
            effective_cap=0.0, daily_volatility=daily_volatility,
            note="The configured maximum position weight is zero, so the target is capped at zero.",
        )

    # From here the target is positive and a positive cap may apply, so volatility
    # is needed when scaling is enabled.
    scale, vol_used = _volatility_scale(daily_volatility, policy)
    if scale is None:
        return _failure(
            normalized,
            STATUS_NEEDS_DATA,
            f"{normalized} has a positive target but no reliable daily volatility "
            "was supplied; a fabricated low-volatility assumption is not used.",
            policy,
            current_weight=weight,
        )

    effective_cap = policy.max_position_weight * scale
    return _sized(
        normalized, weight, raw_target, policy,
        effective_cap=effective_cap, daily_volatility=vol_used,
        note=_cap_note(normalized, raw_target, effective_cap, policy),
    )


def _volatility_scale(
    daily_volatility: float | None, policy: PositionPolicy
) -> tuple[float | None, float | None]:
    """The cap scale from volatility, or (None, None) when it cannot be computed.

    With scaling disabled the fixed cap applies unchanged (scale 1). With scaling
    enabled a missing volatility is uncomputable; zero volatility is the valid
    flat case (scale 1); otherwise ``min(1, threshold / vol)``.
    """
    if not policy.volatility_scaling:
        return 1.0, (float(daily_volatility) if daily_volatility is not None else None)
    if daily_volatility is None:
        return None, None
    vol = float(daily_volatility)
    if vol == 0.0:
        return 1.0, vol
    return min(1.0, policy.volatility_threshold / vol), vol


def _sized(
    rating: str,
    current_weight: float,
    raw_target: float,
    policy: PositionPolicy,
    *,
    effective_cap: float,
    daily_volatility: float | None,
    note: str,
) -> PositionSignal:
    """Assemble an ``ok`` signal from a positive target and a computed cap."""
    weight = min(raw_target, effective_cap)
    cap_override = weight < raw_target
    if cap_override:
        # Be explicit whenever the cap, not the rating, decided the weight.
        note = (f"Risk cap overrides the {rating} target: raw target "
                f"{raw_target:.4g} reduced to {weight:.4g} by the effective cap "
                f"{effective_cap:.4g}. " + note)
    # Any actual zero result (e.g. a fixed zero cap) holds nothing, so it carries
    # no stop-loss policy: a stop on a zero target would be meaningless.
    stop_loss = None if weight == 0.0 else policy.stop_loss_pct
    return PositionSignal(
        rating=rating,
        status=STATUS_OK,
        weight=weight,
        stop_loss_pct=stop_loss,
        confidence=None,
        confidence_source=CONFIDENCE_SOURCE_NOT_PROVIDED,
        current_weight=current_weight,
        raw_target_weight=raw_target,
        effective_cap=effective_cap,
        daily_volatility=float(daily_volatility) if daily_volatility is not None else None,
        reason=note,
        policy_version=policy.policy_version,
        cap_override=cap_override,
    )


def _cap_note(
    rating: str, raw_target: float, effective_cap: float, policy: PositionPolicy
) -> str:
    """A plain-English note on how the effective cap was derived."""
    if policy.volatility_scaling:
        return (
            f"{rating} target {raw_target:.4g} bounded by the volatility-scaled cap "
            f"{effective_cap:.4g} (base cap {policy.max_position_weight:.4g}, "
            f"threshold {policy.volatility_threshold:.4g})."
        )
    return (
        f"{rating} target {raw_target:.4g} bounded by the fixed cap "
        f"{effective_cap:.4g} (volatility scaling disabled)."
    )


__all__ = [
    "PositionPolicy",
    "PositionSignal",
    "process_position",
    "RATINGS_5_TIER",
    "STATUS_OK",
    "STATUS_NEEDS_REVIEW",
    "STATUS_NEEDS_CONTEXT",
    "STATUS_NEEDS_DATA",
    "CONFIDENCE_SOURCE_NOT_PROVIDED",
    "RATING_REVIEW",
]
