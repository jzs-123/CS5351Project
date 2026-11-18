"""R3 P4: config resolution, per-run preparation, and the completion hook.

This module is the small, cohesive glue between the pure core
(``tradingagents.position``), the historical-input adapter
(``tradingagents.position_inputs``), and the graph. It has three jobs:

1. **Resolve and validate** the R3 config (flag, explicit context, policy) into a
   normalized, JSON-serializable, detached form at construction time — before any
   directory or model client exists — so malformed input fails cheaply.

2. **Prepare** one run (``prepare_run``): revalidate the strict flag and policy,
   reread a path-loaded context exactly once before model work, normalize it into
   ``self.config`` so the checkpoint signature reflects it, and validate that it
   matches the run's ticker/date. The result is frozen for the run, so a file
   mutated during model work cannot change that run's signal; a later run over the
   same graph rereads and picks up the change.

3. **Complete** a finished run (``complete_position_signal``): compute the
   position via the core, fetch historical volatility only when the core says it is
   genuinely needed, and attach a plain JSON dict to ``final_state['position_signal']``
   from the frozen context/policy — never rereading a file. The original rating and
   decision are never touched, and no model is called.

Numeric policy lives only in ``tradingagents.position.PositionPolicy``; this module
constructs one from approved defaults plus overrides and lets it validate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from tradingagents.agents.rating import run_rating
from tradingagents.position import (
    CONFIDENCE_SOURCE_NOT_PROVIDED,
    STATUS_NEEDS_DATA,
    PositionPolicy,
    PositionSignal,
    process_position,
)

# Policy field names a caller may override, derived from the frozen dataclass.
_POLICY_FIELDS = frozenset(f.name for f in fields(PositionPolicy))

FLAG_KEY = "position_enabled"
CONTEXT_KEY = "position_context"
POLICY_KEY = "position_policy"
# Source path of a file-loaded context, kept so each run can reread it.
SOURCE_PATH_KEY = "position_context_path"
# Private marker: the normalized context dict last written into config, so a later
# run can tell our normalization apart from a caller-supplied replacement.
_NORMALIZED_KEY = "position_context_normalized"


def _require_strict_bool(value: Any, name: str) -> bool:
    """A real boolean, or ValueError. Ints, strings and None are rejected."""
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean, got {value!r}")
    return value


def build_policy(policy: Any) -> PositionPolicy:
    """A ``PositionPolicy`` from approved defaults plus caller overrides.

    Accepts a ``PositionPolicy`` (returned as-is), ``None`` (absent -> defaults),
    or a mapping whose keys are a subset of the policy's fields (``{}`` = defaults).
    An explicitly supplied non-mapping, non-``PositionPolicy`` value (including a
    string or list) raises ``ValueError``. Unknown keys and malformed values raise
    via the policy's own construction, so the numeric rules live in one place. Keys
    are compared after a string-only check, so mixed key types cannot raise a
    ``TypeError`` from ``sorted``.
    """
    if policy is None:
        return PositionPolicy()
    if isinstance(policy, PositionPolicy):
        return policy
    if not isinstance(policy, Mapping):
        raise ValueError(f"{POLICY_KEY} must be a mapping or PositionPolicy, got {policy!r}")
    bad_keys = [k for k in policy if not isinstance(k, str)]
    if bad_keys:
        raise ValueError(f"{POLICY_KEY} keys must be strings, got {bad_keys!r}")
    unknown = set(policy) - _POLICY_FIELDS
    if unknown:
        raise ValueError(f"{POLICY_KEY} has unknown field(s): {sorted(unknown)}")
    return PositionPolicy(**dict(policy))


def policy_to_json(policy: PositionPolicy) -> dict[str, Any]:
    """The actual policy values as a JSON-safe dict (its dataclass fields)."""
    return asdict(policy)


def _context_from_value(value: Any) -> tuple[Any, str | None]:
    """Interpret ``position_context``: a context, a mapping, a JSON path, or None.

    Returns ``(context_or_None, path_or_None)``. A ``str``/``Path`` is read and
    fully validated by the existing P3 loader and its path remembered. A mapping or
    ``PositionContext`` is validated by the shared parser. A path that is the wrong
    type (e.g. an int) or an empty mapping is rejected.
    """
    from tradingagents.position_inputs import (
        PositionContext,
        load_position_context,
        parse_position_context,
    )

    if value is None:
        return None, None
    if isinstance(value, PositionContext):
        return value, None
    if isinstance(value, (str, Path)):
        context = load_position_context(value)
        return context, str(value)
    if isinstance(value, Mapping):
        # An explicit empty mapping is invalid (not "absent"); the parser rejects it
        # via its required-field check.
        return parse_position_context(value), None
    raise ValueError(
        f"{CONTEXT_KEY} must be a PositionContext, mapping, JSON path, or null, "
        f"got {type(value).__name__}"
    )


# Accepted types for the persisted source path.
def _validate_source_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{SOURCE_PATH_KEY} must be a string path, got {type(value).__name__}")
    return str(value)


def _policy_from_config(config: Mapping[str, Any]) -> PositionPolicy:
    """Build the policy from config, rejecting an explicitly supplied ``None``.

    ``position_policy`` absent -> approved defaults; present (including ``{}`` or a
    ``PositionPolicy``) -> validated. An explicit ``None`` is rejected rather than
    silently defaulted, so a caller cannot mean "defaults" by writing null.
    """
    if POLICY_KEY in config:
        value = config[POLICY_KEY]
        if value is None:
            raise ValueError(f"{POLICY_KEY} must not be null; omit it or pass a mapping")
        return build_policy(value)
    return build_policy(None)


def resolve_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the R3 config and return normalized, detached R3 values.

    Reads the strict bool flag, the context (context/mapping/path/null), the policy
    (mapping/PositionPolicy/absent) and any persisted source path. Supplying a
    non-null context enables R3 even when the flag is False; otherwise the flag
    alone decides. Explicit bad policy/context is validated even when disabled. The
    input mapping is never mutated.
    """
    flag = _require_strict_bool(config.get(FLAG_KEY, False), FLAG_KEY)
    policy = _policy_from_config(config)
    context_obj, source_path = _context_from_value(config.get(CONTEXT_KEY))
    # A caller may also have persisted the source path directly; keep it if present.
    persisted_path = _validate_source_path(config.get(SOURCE_PATH_KEY))
    return {
        "position_enabled": flag,
        "position_context": context_obj.to_dict() if context_obj is not None else None,
        "position_context_path": source_path or persisted_path,
        "position_policy": policy_to_json(policy),
    }


def with_resolved_config(config: Mapping[str, Any] | None, base: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the caller's config (or ``base``) with R3 keys resolved and normalized.

    The caller's dict and ``base`` are never mutated. Non-R3 keys pass through.
    """
    source = dict(config) if config is not None else dict(base)
    resolved = resolve_config(source)
    merged = dict(source)
    merged[FLAG_KEY] = resolved["position_enabled"]
    merged[CONTEXT_KEY] = resolved["position_context"]
    merged[POLICY_KEY] = resolved["position_policy"]
    if resolved["position_context_path"] is not None:
        merged[SOURCE_PATH_KEY] = resolved["position_context_path"]
        # Mark the normalized dict we wrote so a later path refresh can tell it from
        # a caller-supplied replacement.
        merged[_NORMALIZED_KEY] = _deep_copy(resolved["position_context"])
    else:
        merged.pop(SOURCE_PATH_KEY, None)
        merged.pop(_NORMALIZED_KEY, None)
    return merged


def is_r3_enabled(config: Mapping[str, Any]) -> bool:
    """Whether R3 is active: an explicit context, or the enabled flag."""
    if config.get(CONTEXT_KEY) is not None:
        return True
    return bool(config.get(FLAG_KEY, False))


@dataclass(frozen=True)
class PreparedRun:
    """One run's frozen R3 inputs: its identity, enabled flag, context and policy.

    ``enabled`` is frozen here so completion uses the flag the run started with, not
    a live config flag mutated mid-run. ``ticker``/``trade_date`` identify the run so
    a prepared object is never applied to a different run. ``context`` and
    ``policy`` are the run's own detached copies (see ``prepare_run``); mutating the
    graph's config dicts in place afterwards cannot change them, and completion hands
    out further copies so callers cannot reach these internals either.
    """

    ticker: str
    trade_date: str
    enabled: bool
    context: dict[str, Any] | None
    policy: dict[str, Any]


def _deep_copy(value: Any) -> Any:
    """An independent JSON-shaped copy (context/policy are plain JSON data)."""
    return json.loads(json.dumps(value, sort_keys=True))


def prepare_run(config: Mapping[str, Any], ticker: str, trade_date: str) -> tuple[dict[str, Any], PreparedRun]:
    """Validate one run's R3 config, rereading its source context once.

    Returns ``(new_config, prepared)``. ``prepared`` holds detached deep copies of
    the frozen context and policy, so mutating ``new_config``'s dicts (or the graph's
    live config) after this call cannot change what completion uses. When R3 is
    disabled, ``new_config`` is returned with R3 keys left exactly as the caller set
    them (no inert defaults injected), so the checkpoint signature is unchanged for
    legacy callers; only an enabled run stores normalized context/policy values.
    A path-loaded context is reread here, before model work; a mapping/
    ``PositionContext`` is revalidated. A mismatch raises ``ValueError``.
    """
    # Always validate the strict flag and policy, even when disabled; an explicit
    # None policy is rejected the same way whether at construction or on a rerun.
    flag = _require_strict_bool(config.get(FLAG_KEY, False), FLAG_KEY)
    policy = _policy_from_config(config)
    policy_values = policy_to_json(policy)
    enabled = flag or config.get(CONTEXT_KEY) is not None

    if not enabled:
        # Legacy/disabled: leave the config untouched (validated above only) so the
        # checkpoint signature does not drift from what the caller built.
        return dict(config), PreparedRun(
            ticker=str(ticker), trade_date=str(trade_date), enabled=False,
            context=None, policy=_deep_copy(policy_values),
        )

    context = _fresh_context(config, ticker, trade_date)
    new_config = dict(config)
    new_config[FLAG_KEY] = flag
    new_config[POLICY_KEY] = policy_values
    new_config[CONTEXT_KEY] = context
    # Remember the exact normalized dict we wrote and the source path (if any), so a
    # later run can (a) refresh a path-based context and (b) notice when the caller
    # replaced the context in memory (then the replaced value is honored, not the
    # stale path).
    new_config[_NORMALIZED_KEY] = _deep_copy(context) if context is not None else None
    source = _current_source_path(config)
    if source is not None:
        new_config[SOURCE_PATH_KEY] = source
    else:
        new_config.pop(SOURCE_PATH_KEY, None)
    # Freeze from independent deep copies, not the dicts now held in new_config.
    return new_config, PreparedRun(
        ticker=str(ticker), trade_date=str(trade_date), enabled=True,
        context=_deep_copy(context) if context is not None else None,
        policy=_deep_copy(policy_values),
    )


def _strict_same(left: Any, right: Any) -> bool:
    """JSON-type-aware deep equality: ``False`` never equals ``0`` / ``True`` ``1``.

    ``==`` would treat a bool mutated in place into a stored number as unchanged and
    let a file reload mask the explicit invalid value (T07). Compare JSON encodings
    so ``true``/``false`` and ``0``/``0.0`` differ or match exactly as written.
    """
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return False


def _current_source_path(config: Mapping[str, Any]) -> str | None:
    """The source path this run's context came from, if it is a path-based context.

    A context is path-based when ``position_context`` is a path (str/``Path``), or
    when it is a mapping that still strictly equals the dict we normalized from a
    path last run and a source path is recorded. The stored mapping is validated
    first, so an in-place change (e.g. ``current_weight`` set to a bool) is rejected
    as an explicit invalid value rather than discarded by a source reload. A
    caller-replaced mapping/``PositionContext``/``None`` is not path-based and clears
    any prior source path so it cannot override the change.
    """
    stored = config.get(CONTEXT_KEY)
    # Validate any recorded source path metadata before use, so a bad type is a
    # clear error rather than silently ignored.
    recorded_path = config.get(SOURCE_PATH_KEY)
    if recorded_path is not None:
        recorded_path = _validate_source_path(recorded_path)
    if isinstance(stored, (str, Path)):
        return _validate_source_path(stored)
    if isinstance(stored, Mapping):
        # Validate the actual value the caller now holds (raises for a bool weight,
        # NaN, unknown/missing fields, ...) before considering a file refresh.
        from tradingagents.position_inputs import parse_position_context

        parse_position_context(stored)
        if recorded_path is not None and _strict_same(stored, config.get(_NORMALIZED_KEY)):
            return recorded_path
    return None


def _fresh_context(config: Mapping[str, Any], ticker: str, trade_date: str) -> dict[str, Any] | None:
    """Reread/validate the configured context for this run and match it.

    Honors every accepted context form even when assigned to ``graph.config`` later:
    a path-based context (``position_context`` is a path, or unchanged since we
    normalized it from one) is reread; a caller-replaced ``PositionContext`` or
    mapping is validated and used as-is; ``None`` means no context. Mismatch raises.
    """
    from tradingagents.position_inputs import (
        PositionContext,
        load_position_context,
        parse_position_context,
    )

    def _match(context: PositionContext, origin: str) -> dict[str, Any]:
        if not context.matches_run(ticker, trade_date):
            raise ValueError(
                f"{origin} is for {context.ticker} on {context.as_of}, not "
                f"{ticker} on {trade_date}"
            )
        return context.to_dict()

    source = _current_source_path(config)
    if source is not None:
        return _match(load_position_context(source), f"position context file {source}")

    stored = config.get(CONTEXT_KEY)
    if stored is None:
        return None
    if isinstance(stored, PositionContext):
        return _match(stored, "position context")
    return _match(parse_position_context(stored), "position context")


def _market_digest(market: Any) -> str | None:
    """The market content digest, or None when no market data was fetched."""
    return getattr(market, "content_digest", None) if market is not None else None


def _input_digest(
    ticker: str,
    trade_date: str,
    context: dict[str, Any] | None,
    policy: dict[str, Any],
    market: Any,
) -> str:
    """Digest over run identity, context, the actual policy values, and market data.

    Includes the ticker/date so a Sell without context still identifies its run, and
    uses the market *content* digest (never a path, version or retrieval time) when
    data was fetched. A no-fetch run passes ``None`` and never invents volatility
    provenance, so identical numeric inputs always hash the same.
    """
    payload = {
        "ticker": ticker,
        "trade_date": trade_date,
        "context": context,
        "policy": policy,
        "market": _market_digest(market),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def complete_position_signal(
    config: Mapping[str, Any],
    ticker: str,
    trade_date: str,
    final_state: dict,
    prepared: PreparedRun | None = None,
) -> None:
    """Attach (or clear) ``final_state['position_signal']`` from frozen run inputs.

    Uses the frozen ``prepared`` (identity, enabled flag, context, policy) when given
    — the normal graph path — so a live config flag mutated mid-run cannot change the
    outcome, and a prepared object for another ticker/date is ignored. Without a
    prepared run it falls back to the already-normalized config values. It never
    rereads a context file. Disabled: any stale signal is removed. Enabled: the
    rating is read with the existing ``run_rating`` (never modified) and the core
    decides whether market data is needed; only a positive target needing volatility
    fetches. Runtime market failures become a truthful ``needs_data`` keeping the
    report/rating, except ``BudgetExceeded``/model errors, which propagate.
    """
    if prepared is not None and (
        prepared.ticker != str(ticker) or prepared.trade_date != str(trade_date)):
        prepared = None  # never apply another run's frozen inputs
    if prepared is None:
        enabled = is_r3_enabled(config)
        context = config.get(CONTEXT_KEY) if enabled else None
        policy = build_policy(config.get(POLICY_KEY))
        policy_values = policy_to_json(policy)
    else:
        enabled = prepared.enabled
        context = prepared.context
        policy_values = prepared.policy
        policy = PositionPolicy(**policy_values)

    if not enabled:
        final_state.pop("position_signal", None)
        return

    rating = run_rating(final_state)
    weight = context["current_weight"] if context is not None else None

    initial = process_position(rating, weight, None, policy)
    market = None
    signal = initial
    if initial.status == STATUS_NEEDS_DATA:
        signal, market = _fetch_and_size(config, ticker, trade_date, rating, weight, policy, initial)

    payload = signal.to_dict()
    payload["input_as_of"] = trade_date
    # Emit detached copies so a caller cannot mutate the frozen prepared internals
    # (or the live config dict) through the emitted JSON.
    payload["context"] = _deep_copy(context) if context is not None else None
    payload["policy"] = _deep_copy(policy_values)
    if market is not None:
        # Top-level provenance from the fetched MarketInputs, plus the full record.
        payload["data_source"] = market.data_source
        payload["last_price_date"] = market.last_observed_date
        payload["sample_size"] = market.sample_size
        payload["market"] = market.to_dict()
    else:
        # No fetch: no invented source/volatility provenance.
        payload["market"] = None
    payload["input_digest"] = _input_digest(ticker, trade_date, context, policy_values, market)
    final_state["position_signal"] = payload


def _fetch_and_size(
    config: Mapping[str, Any],
    ticker: str,
    trade_date: str,
    rating: str | None,
    weight: float | None,
    policy: PositionPolicy,
    initial: PositionSignal,
) -> tuple[PositionSignal, Any]:
    """Fetch historical volatility and re-run the core; degrade safely on failure."""
    from tradingagents.llm_clients.usage_tracker import BudgetExceeded
    from tradingagents.position_inputs import load_market_inputs

    try:
        market = load_market_inputs(ticker, trade_date, config=config)
    except BudgetExceeded:
        raise  # a budget stop must abort the run
    except Exception as exc:  # noqa: BLE001 — degrade to needs_data, keep the report
        reason = (
            f"{rating} has a positive target but market data could not be loaded this "
            f"run ({type(exc).__name__}); no volatility is assumed. The original "
            "analysis and rating are unchanged."
        )
        return _needs_data_failure(rating, weight, policy, reason), None

    signal = process_position(rating, weight, market.daily_volatility, policy)
    return signal, market


def _needs_data_failure(
    rating: str | None, weight: float | None, policy: PositionPolicy, reason: str
) -> PositionSignal:
    """A needs_data signal with no executable weight (core schema)."""
    return PositionSignal(
        rating=rating,
        status=STATUS_NEEDS_DATA,
        weight=None,
        stop_loss_pct=None,
        confidence=None,
        confidence_source=CONFIDENCE_SOURCE_NOT_PROVIDED,
        current_weight=weight,
        raw_target_weight=None,
        effective_cap=None,
        daily_volatility=None,
        reason=reason,
        policy_version=policy.policy_version,
        cap_override=False,
    )


__all__ = [
    "resolve_config",
    "with_resolved_config",
    "prepare_run",
    "complete_position_signal",
    "build_policy",
    "policy_to_json",
    "is_r3_enabled",
    "PreparedRun",
    "FLAG_KEY",
    "CONTEXT_KEY",
    "POLICY_KEY",
    "SOURCE_PATH_KEY",
]
