"""Offline tests for the R3 pure core: rating-to-position mapping.

These cover the approved policy matrix T01-T08 and the pure invariants in T12.
They exercise only ``tradingagents.position`` (no graph, adapter, report, CLI or
network). Expected numbers are computed by hand from the policy formulas, not by
mirroring the implementation, and floats are compared with a tolerance.
"""

from __future__ import annotations

import copy
import math
from dataclasses import FrozenInstanceError

import pytest

from tradingagents.position import (
    CONFIDENCE_SOURCE_NOT_PROVIDED,
    RATING_REVIEW,
    RATINGS_5_TIER,
    STATUS_NEEDS_CONTEXT,
    STATUS_NEEDS_DATA,
    STATUS_NEEDS_REVIEW,
    STATUS_OK,
    PositionPolicy,
    PositionSignal,
    process_position,
)

TOL = 1e-9


def _ok(signal: PositionSignal) -> PositionSignal:
    """Assert an ok, executable signal and return it."""
    assert signal.status == STATUS_OK, signal.reason
    assert signal.weight is not None
    return signal


# ---------------------------------------------------------------------------
# T01 / T02: Buy target and its volatility-scaled cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_buy_low_vol_keeps_full_target_with_stop_loss():
    # vol 1% < threshold 2%, so scale = min(1, 0.02/0.01) = 1; cap stays 0.20.
    signal = _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.01))
    assert signal.weight == pytest.approx(0.20)
    assert signal.raw_target_weight == pytest.approx(0.20)
    assert signal.effective_cap == pytest.approx(0.20)
    assert signal.stop_loss_pct == pytest.approx(0.08)
    assert signal.cap_override is False


@pytest.mark.unit
def test_buy_high_vol_is_capped_by_scaled_cap():
    # vol 4% > threshold 2%, so scale = 0.02/0.04 = 0.5; effective cap = 0.20*0.5 = 0.10.
    signal = _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.04))
    assert signal.effective_cap == pytest.approx(0.10)
    assert signal.weight == pytest.approx(0.10)
    assert signal.cap_override is True
    assert "cap" in signal.reason.lower()


# ---------------------------------------------------------------------------
# T03: Overweight / Hold / Underweight under low volatility
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rating, current, expected", [
    ("Overweight", 0.05, 0.10),   # 0.05 + 0.05, under the 0.20 cap
    ("Hold", 0.08, 0.08),         # unchanged
    ("Underweight", 0.15, 0.10),  # 0.15 - 0.05
])
def test_relative_ratings_low_vol(rating, current, expected):
    signal = _ok(process_position(rating, current_weight=current, daily_volatility=0.01))
    assert signal.weight == pytest.approx(expected)
    assert signal.cap_override is False


# ---------------------------------------------------------------------------
# T04 / T05 / Sell: zeros and the explicit Hold cap override
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_underweight_on_a_small_holding_yields_zero_without_increasing():
    # 0.03 - 0.05 would be negative; it clamps to 0, never a negative (short) add.
    signal = _ok(process_position("Underweight", current_weight=0.03, daily_volatility=0.01))
    assert signal.weight == 0.0
    assert signal.stop_loss_pct is None


@pytest.mark.unit
def test_sell_yields_zero_without_current_weight_or_volatility():
    signal = _ok(process_position("Sell"))
    assert signal.weight == 0.0
    assert signal.stop_loss_pct is None
    assert signal.current_weight is None
    assert signal.daily_volatility is None


@pytest.mark.unit
def test_hold_over_the_cap_is_reduced_with_explicit_cap_override():
    # Hold 25% exceeds the 20% cap at low vol; the cap, not the rating, decides.
    signal = _ok(process_position("Hold", current_weight=0.25, daily_volatility=0.01))
    assert signal.raw_target_weight == pytest.approx(0.25)
    assert signal.weight == pytest.approx(0.20)
    assert signal.cap_override is True
    assert "cap" in signal.reason.lower()


# ---------------------------------------------------------------------------
# T06: needs_review / needs_context, and unknown-vs-explicit-zero
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["REVIEW", "review", "", "Strong Buy", None])
def test_unrecognized_or_missing_rating_needs_review(rating):
    signal = process_position(rating, current_weight=0.1, daily_volatility=0.01)
    assert signal.status == STATUS_NEEDS_REVIEW
    assert signal.weight is None
    assert signal.stop_loss_pct is None


@pytest.mark.unit
def test_review_rating_keeps_the_canonical_sentinel():
    # The non-tradeable sentinel stays the canonical REVIEW string in the signal.
    assert process_position("REVIEW").to_dict()["rating"] == RATING_REVIEW
    assert process_position("review").to_dict()["rating"] == RATING_REVIEW


@pytest.mark.unit
@pytest.mark.parametrize("rating", [123, 1.5, ["Buy"], {"rating": "Buy"}, object()])
def test_non_string_rating_yields_needs_review_without_typeerror(rating):
    # A non-string (including an unhashable list/dict) has no rating to recognize;
    # it must map to needs_review with a JSON-safe rating (None), never a TypeError.
    signal = process_position(rating, current_weight=0.1, daily_volatility=0.01)
    assert signal.status == STATUS_NEEDS_REVIEW
    assert signal.weight is None
    assert signal.rating is None


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["Buy", "Overweight", "Hold", "Underweight"])
def test_non_sell_without_current_weight_needs_context_not_zero(rating):
    signal = process_position(rating, current_weight=None, daily_volatility=0.01)
    assert signal.status == STATUS_NEEDS_CONTEXT
    assert signal.weight is None


@pytest.mark.unit
def test_unknown_current_weight_is_not_treated_as_zero():
    """Omitted context is a distinct state from an explicit flat book."""
    unknown = process_position("Hold", current_weight=None, daily_volatility=0.01)
    flat = _ok(process_position("Hold", current_weight=0.0, daily_volatility=0.01))
    assert unknown.status == STATUS_NEEDS_CONTEXT
    assert unknown.weight is None
    assert flat.weight == 0.0


# ---------------------------------------------------------------------------
# T07: explicit invalid values fail before any computation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bad_weight", [True, False, float("nan"), float("inf"), -0.01, 1.01, "0.1"])
def test_invalid_explicit_current_weight_is_rejected(bad_weight):
    # Invalid numbers fail even though some are truthy. A missing value (None) is
    # a separate, valid case positively tested elsewhere, not an invalid number.
    with pytest.raises(ValueError):
        process_position("Hold", current_weight=bad_weight, daily_volatility=0.01)


@pytest.mark.unit
@pytest.mark.parametrize("bad_vol", [True, float("nan"), float("inf"), -0.0001, "0.02"])
def test_invalid_explicit_daily_volatility_is_rejected(bad_vol):
    with pytest.raises(ValueError):
        process_position("Buy", current_weight=0.05, daily_volatility=bad_vol)


@pytest.mark.unit
def test_explicit_bad_values_still_fail_for_sell():
    # Sell needs no context, but a supplied-and-invalid number is still rejected.
    with pytest.raises(ValueError):
        process_position("Sell", current_weight=float("nan"))
    with pytest.raises(ValueError):
        process_position("Sell", daily_volatility=-1.0)


@pytest.mark.unit
def test_huge_json_integer_is_rejected_not_overflowerror():
    # 10**400 is a valid Python int but not a finite weight; it must be a
    # ValueError, never an OverflowError escaping from the float conversion.
    huge = 10**400
    with pytest.raises(ValueError):
        process_position("Hold", current_weight=huge, daily_volatility=0.01)
    with pytest.raises(ValueError):
        process_position("Buy", current_weight=0.05, daily_volatility=huge)


@pytest.mark.unit
def test_non_position_policy_type_is_rejected():
    # A plausible-but-wrong object (e.g. a dict) must fail clearly, not from an
    # AttributeError deep inside the computation.
    with pytest.raises(ValueError, match="PositionPolicy"):
        process_position("Buy", current_weight=0.05, daily_volatility=0.01, policy={"step": 0.05})


@pytest.mark.unit
@pytest.mark.parametrize("field, bad", [
    ("buy_target_weight", 1.2),
    ("buy_target_weight", -0.1),
    ("step", 1.5),
    ("step", float("nan")),
    ("max_position_weight", 1.01),
    ("volatility_threshold", 0.0),
    ("volatility_threshold", -0.02),
    ("stop_loss_pct", 1.0),
    ("stop_loss_pct", -0.01),
    ("buy_target_weight", True),
])
def test_policy_rejects_invalid_numeric_config(field, bad):
    with pytest.raises(ValueError):
        PositionPolicy(**{field: bad})


@pytest.mark.unit
def test_policy_validates_on_construction():
    # Valid boundary values construct fine; an invalid one is caught at build time.
    PositionPolicy(buy_target_weight=0.0, step=0.0, stop_loss_pct=0.0, volatility_threshold=1e-6)
    with pytest.raises(ValueError):
        PositionPolicy(volatility_threshold=-1.0)


# ---------------------------------------------------------------------------
# T08: zero volatility, disabled scaling, customized policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_flat_zero_volatility_is_valid_and_uses_scale_one():
    # Zero vol is a real flat series, not missing data: scale = 1, full cap.
    signal = _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.0))
    assert signal.effective_cap == pytest.approx(0.20)
    assert signal.weight == pytest.approx(0.20)
    assert signal.stop_loss_pct == pytest.approx(0.08)


@pytest.mark.unit
def test_disabled_scaling_still_enforces_the_fixed_cap():
    policy = PositionPolicy(volatility_scaling=False)
    # High vol would shrink the cap when scaling were on; here it is ignored.
    signal = _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.10, policy=policy))
    assert signal.effective_cap == pytest.approx(0.20)
    assert signal.weight == pytest.approx(0.20)


@pytest.mark.unit
def test_disabled_scaling_still_caps_a_hold_over_the_limit():
    policy = PositionPolicy(volatility_scaling=False)
    signal = _ok(process_position("Hold", current_weight=0.30, daily_volatility=None, policy=policy))
    assert signal.weight == pytest.approx(0.20)
    assert signal.cap_override is True


@pytest.mark.unit
def test_customized_policy_is_used_not_hardcoded():
    # cap 0.5, threshold 0.10, step 0.02, sell target for Buy = 0.3, stop 0.05.
    policy = PositionPolicy(
        buy_target_weight=0.30, step=0.02, max_position_weight=0.50,
        volatility_threshold=0.10, stop_loss_pct=0.05,
    )
    buy = _ok(process_position("Buy", current_weight=0.0, daily_volatility=0.05, policy=policy))
    # vol 0.05 < threshold 0.10 -> scale 1 -> cap 0.50; raw 0.30 -> weight 0.30.
    assert buy.weight == pytest.approx(0.30)
    assert buy.stop_loss_pct == pytest.approx(0.05)
    assert buy.policy_version == "r3-v1"
    over = _ok(process_position("Overweight", current_weight=0.10, daily_volatility=0.05, policy=policy))
    assert over.raw_target_weight == pytest.approx(0.12)  # 0.10 + step 0.02


# ---------------------------------------------------------------------------
# needs_data: a positive target with no reliable volatility
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rating, current", [("Buy", 0.0), ("Overweight", 0.10), ("Hold", 0.10)])
def test_positive_target_without_volatility_needs_data(rating, current):
    signal = process_position(rating, current_weight=current, daily_volatility=None)
    assert signal.status == STATUS_NEEDS_DATA
    assert signal.weight is None


@pytest.mark.unit
def test_zero_targets_need_no_volatility():
    # Underweight on a tiny holding resolves to zero; no prices required.
    under = _ok(process_position("Underweight", current_weight=0.02, daily_volatility=None))
    assert under.weight == 0.0 and under.stop_loss_pct is None
    flat_hold = _ok(process_position("Hold", current_weight=0.0, daily_volatility=None))
    assert flat_hold.weight == 0.0


@pytest.mark.unit
def test_zero_cap_yields_zero_without_volatility():
    policy = PositionPolicy(max_position_weight=0.0)
    signal = _ok(process_position("Buy", current_weight=0.0, daily_volatility=None, policy=policy))
    assert signal.weight == 0.0
    assert signal.effective_cap == 0.0
    # Regression: a fixed-zero-cap target is an actual zero result, so it must
    # carry no stop-loss policy (approved: all effective zero targets -> None).
    assert signal.stop_loss_pct is None


@pytest.mark.unit
def test_every_actual_zero_target_has_no_stop_loss():
    # Any path that produces a 0.0 weight must leave the stop loss unset.
    zeros = [
        process_position("Sell"),
        process_position("Underweight", current_weight=0.02, daily_volatility=0.01),
        process_position("Hold", current_weight=0.0, daily_volatility=0.01),
        process_position("Buy", current_weight=0.0, daily_volatility=0.0,
                         policy=PositionPolicy(max_position_weight=0.0)),
    ]
    for signal in zeros:
        assert signal.status == STATUS_OK
        assert signal.weight == 0.0
        assert signal.stop_loss_pct is None


# ---------------------------------------------------------------------------
# Canonical scale / T12 monotonicity and no-reverse invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("rating", RATINGS_5_TIER)
def test_every_canonical_rating_is_recognized(rating):
    # Each rating produces either an ok weight or a documented status, never REVIEW
    # for a canonical tier.
    signal = process_position(rating, current_weight=0.10, daily_volatility=0.01)
    assert signal.status in {STATUS_OK, STATUS_NEEDS_DATA}
    assert signal.status != STATUS_NEEDS_REVIEW


@pytest.mark.unit
def test_weight_never_increases_as_volatility_rises():
    """T12: for a fixed rating and current weight, more volatility never adds size."""
    weights = [
        _ok(process_position("Buy", current_weight=0.05, daily_volatility=v)).weight
        for v in (0.0, 0.01, 0.02, 0.03, 0.05, 0.10, 0.25)
    ]
    assert all(a >= b - TOL for a, b in zip(weights, weights[1:], strict=False)), weights


@pytest.mark.unit
@pytest.mark.parametrize("current", [0.0, 0.01, 0.04, 0.05, 0.10, 0.30])
@pytest.mark.parametrize("vol", [0.0, 0.01, 0.04, 0.10])
def test_underweight_never_adds_exposure(current, vol):
    signal = _ok(process_position("Underweight", current_weight=current, daily_volatility=vol))
    assert signal.weight <= current + TOL


@pytest.mark.unit
@pytest.mark.parametrize("rating, current, vol", [
    ("Buy", 0.05, 0.01), ("Overweight", 0.10, 0.01), ("Hold", 0.08, 0.04),
    ("Underweight", 0.15, 0.0), ("Sell", 0.30, 0.10),
])
def test_ok_weight_always_within_unit_interval(rating, current, vol):
    signal = _ok(process_position(rating, current_weight=current, daily_volatility=vol))
    assert 0.0 <= signal.weight <= 1.0


@pytest.mark.unit
def test_larger_buy_target_only_shrinks_with_volatility_not_grows():
    high = _ok(process_position("Buy", current_weight=0.0, daily_volatility=0.005)).weight
    low = _ok(process_position("Buy", current_weight=0.0, daily_volatility=0.20)).weight
    assert high >= low - TOL


# ---------------------------------------------------------------------------
# Confidence, serialization, determinism, immutability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_confidence_is_always_none_and_not_provided_on_every_status():
    signals = [
        process_position("Buy", current_weight=0.05, daily_volatility=0.01),
        process_position("REVIEW"),
        process_position("Hold", current_weight=None),
        process_position("Buy", current_weight=0.05, daily_volatility=None),
    ]
    for signal in signals:
        assert signal.confidence is None
        assert signal.confidence_source == CONFIDENCE_SOURCE_NOT_PROVIDED


@pytest.mark.unit
def test_to_dict_is_plain_and_json_safe():
    import json

    signal = _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.04))
    payload = signal.to_dict()
    assert isinstance(payload, dict)
    # Round-trips through JSON with no special encoder needed (all floats finite).
    assert json.loads(json.dumps(payload))["weight"] == pytest.approx(payload["weight"])
    for key in (
        "rating", "status", "weight", "stop_loss_pct", "confidence", "confidence_source",
        "current_weight", "raw_target_weight", "effective_cap", "daily_volatility",
        "reason", "policy_version", "cap_override",
    ):
        assert key in payload


@pytest.mark.unit
def test_failure_signals_have_null_executable_fields_and_serialize():
    for signal in (
        process_position("REVIEW"),
        process_position("Hold", current_weight=None),
        process_position("Buy", current_weight=0.05, daily_volatility=None),
    ):
        payload = signal.to_dict()
        assert payload["weight"] is None
        assert payload["stop_loss_pct"] is None
        assert payload["confidence"] is None


@pytest.mark.unit
def test_process_position_is_deterministic():
    args = {"rating": "Overweight", "current_weight": 0.10, "daily_volatility": 0.037}
    first = process_position(**args)
    second = process_position(**args)
    assert first == second
    assert first.to_dict() == second.to_dict()


@pytest.mark.unit
def test_inputs_and_policy_are_not_mutated():
    policy = PositionPolicy()
    snapshot = copy.deepcopy(policy)
    process_position("Buy", current_weight=0.05, daily_volatility=0.04, policy=policy)
    assert policy == snapshot


@pytest.mark.unit
def test_policy_and_signal_are_frozen():
    policy = PositionPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.step = 0.9  # type: ignore[misc]
    signal = _ok(process_position("Buy", current_weight=0.0, daily_volatility=0.01))
    with pytest.raises(FrozenInstanceError):
        signal.weight = 0.9  # type: ignore[misc]


@pytest.mark.unit
def test_reason_is_present_for_success_and_failure():
    for signal in (
        _ok(process_position("Buy", current_weight=0.05, daily_volatility=0.01)),
        process_position("REVIEW"),
        process_position("Hold", current_weight=None),
        process_position("Buy", current_weight=0.05, daily_volatility=None),
    ):
        assert isinstance(signal.reason, str) and signal.reason.strip()


@pytest.mark.unit
def test_no_nan_or_inf_leaks_into_any_ok_field():
    signal = _ok(process_position("Buy", current_weight=0.0, daily_volatility=1e-12))
    for value in (signal.weight, signal.raw_target_weight, signal.effective_cap,
                  signal.daily_volatility, signal.stop_loss_pct):
        assert value is None or math.isfinite(value)
