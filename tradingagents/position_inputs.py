"""R3 P3: input context and historical-price adaptation.

This module turns two optional external inputs into validated, deterministic
market inputs for the rating-to-position mapping in ``tradingagents.position``:

1. An explicit ``PositionContext`` (the caller's current weight in the analyzed
   instrument), supplied as a mapping or a JSON file.
2. A historical close series, adapted by ``analyze_prices`` into the 21 most
   recent real closes, their 20 simple daily returns and the sample (ddof=1)
   daily standard deviation.

It also provides ``load_market_inputs`` (an adapter over the existing, cached
``load_ohlcv`` vendor function) and explicit ``write_snapshot``/``read_snapshot``
functions for replaying those selected closes offline.

Design boundaries:

- This is *input* preparation only. It never calls ``process_position`` and never
  decides whether to fetch: a Sell, zero target or missing context is
  short-circuited by the later wiring stage (P4).
- The first version targets historical analysis dates. A run dated today or in
  the future reports unavailable rather than consuming a partial daily candle.
- Prices are read as reported (``fill_gaps=False``); a missing close is a data
  problem, not something to forward-fill into a fabricated low-volatility reading.
- No direct vendor-library import lives here; the layering test forbids
  ``yfinance`` outside ``dataflows``, and this module calls ``load_ohlcv`` instead.
- No model calls, no network of its own, no mutable global state.

The content digests here are unkeyed integrity checks over the saved values. They
let a caller notice a changed file or a tampered snapshot; they are not a
signature and do not prove authenticity.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.ohlcv import MAX_OHLCV_STALE_DAYS, load_ohlcv

# The approved window: 21 completed closes -> 20 simple returns -> sample stdev.
SAMPLE_SIZE = 21
RETURN_COUNT = 20
WINDOW_DDOF = 1
RETURN_TYPE = "simple"
FREQUENCY = "1d"
ADJUSTMENT = "auto_adjust"
SNAPSHOT_VERSION = 1

# Acquisition provenance labels, so a replay is never presented as a fresh fetch.
ACQUISITION_ADAPTER = "load_ohlcv"
ACQUISITION_SNAPSHOT_REPLAY = "snapshot_replay"

_CONTEXT_FIELDS = frozenset({"ticker", "as_of", "current_weight", "weight_source"})


def _is_real_number(value: Any) -> bool:
    """A finite real number, excluding booleans.

    ``bool`` is an ``int`` subclass and JSON permits ``NaN``/``Infinity``; an
    integer too large to convert to a float (``10**400``) is rejected rather than
    letting ``math.isfinite`` raise ``OverflowError``.
    """
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _canonical_date(value: Any, name: str) -> str:
    """A strict ``YYYY-MM-DD`` string, or ValueError.

    Only a real calendar date in canonical form is accepted; ``"2026-1-3"`` and
    timestamps are rejected so two spellings never compare as different runs.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a YYYY-MM-DD string, got {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid YYYY-MM-DD date, got {value!r}") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"{name} must be in canonical YYYY-MM-DD form, got {value!r}")
    return value


def _utc_today() -> str:
    """Today's calendar date in UTC as ``YYYY-MM-DD``.

    The project's ``get_current_date()`` uses the *local* calendar date. Near local
    midnight (e.g. Asia/Shanghai) a date that is "yesterday" locally can still be
    the current, unsettled US session, so a completed-day check needs the UTC day
    too. Kept as its own function so tests can monkeypatch the clock.
    """
    return datetime.now(UTC).date().strftime("%Y-%m-%d")


def _require_completed_historical(as_of: str) -> None:
    """Reject a run date that is not strictly before today, in both local and UTC days.

    A present or future date (by either calendar) has no settled daily close yet,
    so it is reported unavailable rather than read from a partial candle. This is
    the approved completed-historical-day restriction, not a trading-calendar rule.
    """
    if as_of >= get_current_date():
        raise ValueError(
            f"analysis date {as_of} is not strictly before the local date "
            f"{get_current_date()}; a current or future date cannot use a settled "
            "daily close"
        )
    if as_of >= _utc_today():
        raise ValueError(
            f"analysis date {as_of} is not strictly before the UTC date {_utc_today()}; "
            "a current or future date cannot use a settled daily close"
        )


@dataclass(frozen=True)
class PositionContext:
    """Explicit current weight in the analyzed instrument, for one run.

    ``current_weight`` is a fraction in [0, 1], or ``None`` for unknown. Unknown
    is not zero: an unstated holding cannot be read as a flat book. ``weight_source``
    records where the number came from (a human, a broker export) and is kept as a
    free-text label, never parsed into a position. This is caller-supplied context,
    not a claim that an account was verified.
    """

    ticker: str
    as_of: str
    current_weight: float | None
    weight_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError(f"ticker must be a non-empty string, got {self.ticker!r}")
        # Canonical, path-safe symbol: the same spelling every other path uses.
        canonical = safe_ticker_component(normalize_symbol(self.ticker))
        object.__setattr__(self, "ticker", canonical)
        _canonical_date(self.as_of, "as_of")
        if not isinstance(self.weight_source, str) or not self.weight_source.strip():
            raise ValueError(
                f"weight_source must be a non-empty string, got {self.weight_source!r}"
            )
        if self.current_weight is not None:
            if not _is_real_number(self.current_weight):
                raise ValueError(
                    f"current_weight must be a finite real number or null, "
                    f"got {self.current_weight!r}"
                )
            number = float(self.current_weight)
            if not 0.0 <= number <= 1.0:
                raise ValueError(
                    f"current_weight must be within [0, 1], got {self.current_weight!r}"
                )
            object.__setattr__(self, "current_weight", number)

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict."""
        return asdict(self)

    def digest(self) -> str:
        """Stable digest of the context's content (not of a file's bytes)."""
        return _digest_json(self.to_dict())

    def matches_run(self, ticker: str, as_of: str) -> bool:
        """Whether this context is for the given run's instrument and date.

        Compares the normalized ticker and canonical date, so a broker spelling of
        the same symbol matches, but no other instrument or date does.
        """
        try:
            other_ticker = safe_ticker_component(normalize_symbol(ticker))
            other_date = _canonical_date(as_of, "as_of")
        except ValueError:
            return False
        return other_ticker == self.ticker and other_date == self.as_of


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json`` object hook that rejects a repeated key rather than keeping the last."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in JSON object: {key!r}")
        seen[key] = value
    return seen


def parse_position_context(data: Mapping[str, Any]) -> PositionContext:
    """Build a ``PositionContext`` from an already-parsed mapping.

    Unknown fields are rejected so a typo (``current_weigth``) cannot silently drop
    the weight; ``current_weight`` may be omitted or ``null`` for unknown.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"position context must be a JSON object, got {type(data).__name__}")
    keys = set(data)
    unknown = keys - _CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"unknown position-context field(s): {sorted(unknown)}")
    missing = {"ticker", "as_of", "weight_source"} - keys
    if missing:
        raise ValueError(f"missing required position-context field(s): {sorted(missing)}")
    return PositionContext(
        ticker=data["ticker"],
        as_of=data["as_of"],
        current_weight=data.get("current_weight"),
        weight_source=data["weight_source"],
    )


def load_position_context(path: str | Path) -> PositionContext:
    """Read and validate a position-context JSON file, failing before a run.

    The file's content is re-read on every call, so a changed file yields a new
    context and a new digest. Malformed JSON, a non-object document, a duplicate
    key or an unknown field all raise ``ValueError``.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"position-context file {path} could not be read: {exc}") from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise ValueError(f"position-context file {path} is not valid JSON: {exc}") from exc
    return parse_position_context(data)


# ---------------------------------------------------------------------------
# Historical price adaptation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketInputs:
    """Validated inputs for daily-volatility scaling, plus provenance.

    ``last_21_closes`` holds ``(YYYY-MM-DD, close)`` pairs for the selected window,
    oldest first, so a reader can recompute the volatility by hand. The numeric
    content digest excludes the runtime retrieval timestamp, so identical inputs
    always produce the same digest.
    """

    ticker: str
    as_of: str
    last_observed_date: str
    sample_size: int
    return_count: int
    window_ddof: int
    return_type: str
    frequency: str
    adjustment: str
    daily_volatility: float
    last_21_closes: tuple[tuple[str, float], ...]
    data_source: str
    acquisition: str
    retrieval_utc: str
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-safe dict (closes as lists, no NaN/Infinity)."""
        payload = asdict(self)
        payload["last_21_closes"] = [[d, c] for d, c in self.last_21_closes]
        return payload


def _frame_columns(frame: Any) -> tuple[pd.Series, pd.Series] | None:
    """``(dates, closes)`` from a DataFrame with Date/Close, or (Date index, Close).

    Returns ``None`` when the frame lacks the required columns so the caller can
    report a clear error. A DatetimeIndex is accepted only when there is no Date
    column; either way both series are validated identically.
    """
    if not isinstance(frame, pd.DataFrame):
        return None
    if "Close" not in frame.columns:
        return None
    if "Date" in frame.columns:
        return frame["Date"], frame["Close"]
    if isinstance(frame.index, pd.DatetimeIndex):
        return pd.Series(frame.index, index=frame.index), frame["Close"]
    return None


def _local_date(value: Any) -> pd.Timestamp | None:
    """A timestamp as its naive, local wall-clock date (or None if unparseable).

    Mirrors the vendor adapter's rule: a tz-aware value keeps its own calendar
    date rather than shifting to a UTC day, so a Tokyo midnight bar stays on its day.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def _validate_close(value: Any) -> float:
    """A strictly positive finite close, or ValueError. Never forwards a bad value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Close must be a real number, got {value!r}")
    try:
        if not math.isfinite(value):
            raise ValueError(f"Close must be finite, got {value!r}")
    except OverflowError as exc:
        raise ValueError(f"Close is too large to be finite: {value!r}") from exc
    number = float(value)
    if number <= 0.0:
        raise ValueError(f"Close must be strictly positive, got {value!r}")
    return number


def _require_provenance_text(value: Any, name: str) -> str:
    """A non-empty, non-blank provenance string, or ValueError. No silent coercion."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _parse_utc_timestamp(value: Any, name: str) -> str:
    """A timezone-aware UTC ISO-8601 timestamp, returned unchanged, or ValueError.

    Requires an explicit UTC offset so a naive or local timestamp cannot be
    mistaken for a retrieval time in another zone.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty ISO-8601 timestamp, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid ISO-8601 timestamp: {value!r}") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset != timedelta(0):
        raise ValueError(f"{name} must be a UTC timestamp with a zero offset, got {value!r}")
    return value


def _validate_provenance(
    data_source: Any, acquisition: Any, retrieval_utc: Any
) -> tuple[str, str, str]:
    """Validate the provenance fields ``analyze_prices`` records.

    Checked here so a valid ``analyze_prices`` call never produces a
    ``MarketInputs`` whose snapshot fields a reader would reject. A missing
    retrieval time defaults to the current UTC instant; a supplied one must be a
    UTC-offset ISO-8601 string and is preserved verbatim.
    """
    source = _require_provenance_text(data_source, "data_source")
    acquisition_label = _require_provenance_text(acquisition, "acquisition")
    if retrieval_utc is None:
        retrieval = datetime.now(UTC).isoformat(timespec="seconds")
    else:
        retrieval = _parse_utc_timestamp(retrieval_utc, "retrieval_utc")
    return source, acquisition_label, retrieval


def analyze_prices(
    frame: Any,
    ticker: str,
    as_of: str,
    *,
    data_source: str = "load_ohlcv",
    acquisition: str = ACQUISITION_ADAPTER,
    retrieval_utc: str | None = None,
) -> MarketInputs:
    """Validate a close series and compute its daily volatility as of ``as_of``.

    The analysis date must be strictly before today in both the local and UTC
    calendars: a present or future run cannot have a settled daily close and is
    reported unavailable instead. Every supplied row is validated (dates parse to
    local wall-clock dates, are unique and ascending, are not in the future and not
    stale beyond ``MAX_OHLCV_STALE_DAYS``; closes are real, finite and positive).
    The latest 21 valid observations are then used to compute 20 simple returns
    and their sample (ddof=1) standard deviation, never annualized. The provenance
    fields are validated too, so a valid call can never build an unwritable snapshot.
    """
    _canonical_date(as_of, "as_of")
    _require_completed_historical(as_of)
    source, acquisition_label, retrieval = _validate_provenance(
        data_source, acquisition, retrieval_utc
    )

    columns = _frame_columns(frame)
    if columns is None:
        raise ValueError("price frame must be a DataFrame with Date and Close columns")
    dates_raw, closes_raw = columns
    if len(dates_raw) != len(closes_raw):
        raise ValueError("price frame Date and Close columns differ in length")
    if dates_raw.empty:
        raise ValueError("price frame has no rows")

    dates: list[pd.Timestamp] = []
    closes: list[float] = []
    for raw_date, raw_close in zip(dates_raw, closes_raw, strict=True):
        parsed = _local_date(raw_date)
        if parsed is None:
            raise ValueError(f"price row has an invalid date: {raw_date!r}")
        dates.append(parsed)
        closes.append(_validate_close(raw_close))

    # Order and uniqueness are checked on the supplied rows; nothing is sorted or
    # deduplicated, so a malformed series is reported rather than cleaned into
    # a success.
    for earlier, later in zip(dates, dates[1:], strict=False):
        if later == earlier:
            raise ValueError(f"duplicate price date: {earlier.date()}")
        if later < earlier:
            raise ValueError(
                f"price dates are not ascending at {later.date()} (after {earlier.date()})"
            )

    as_of_ts = pd.Timestamp(as_of).normalize()
    future = dates[-1]
    if future > as_of_ts:
        raise ValueError(f"price row {future.date()} is after the analysis date {as_of}")

    stale_days = (as_of_ts - dates[-1]).days
    if stale_days > MAX_OHLCV_STALE_DAYS:
        raise ValueError(
            f"latest price {dates[-1].date()} is {stale_days} days before the "
            f"analysis date {as_of} (stale beyond {MAX_OHLCV_STALE_DAYS} days)"
        )

    if len(closes) < SAMPLE_SIZE:
        raise ValueError(
            f"need {SAMPLE_SIZE} completed closes to compute {RETURN_COUNT} returns, "
            f"got {len(closes)}"
        )

    selected = list(zip(dates, closes, strict=True))[-SAMPLE_SIZE:]
    window_closes = [c for _d, c in selected]

    returns: list[float] = []
    for previous, current in zip(window_closes, window_closes[1:], strict=False):
        ret = (current - previous) / previous
        if not math.isfinite(ret):
            raise ValueError(f"computed return is not finite: {ret!r}")
        returns.append(ret)
    volatility = statistics.stdev(returns)  # sample stdev, ddof=1
    if not math.isfinite(volatility):
        raise ValueError(f"computed volatility is not finite: {volatility!r}")

    window_pairs = tuple((d.strftime("%Y-%m-%d"), c) for d, c in selected)
    return MarketInputs(
        ticker=safe_ticker_component(normalize_symbol(ticker)),
        as_of=as_of,
        last_observed_date=selected[-1][0].strftime("%Y-%m-%d"),
        sample_size=SAMPLE_SIZE,
        return_count=RETURN_COUNT,
        window_ddof=WINDOW_DDOF,
        return_type=RETURN_TYPE,
        frequency=FREQUENCY,
        adjustment=ADJUSTMENT,
        daily_volatility=float(volatility),
        last_21_closes=window_pairs,
        data_source=source,
        acquisition=acquisition_label,
        retrieval_utc=retrieval,
        content_digest=_market_content_digest(
            ticker=safe_ticker_component(normalize_symbol(ticker)),
            as_of=as_of,
            window_pairs=window_pairs,
            volatility=float(volatility),
        ),
    )


def _market_content_digest(
    *, ticker: str, as_of: str, window_pairs: tuple[tuple[str, float], ...], volatility: float
) -> str:
    """Digest over the numeric content and parameters, excluding retrieval time."""
    payload = {
        "ticker": ticker,
        "as_of": as_of,
        "closes": [[d, c] for d, c in window_pairs],
        "daily_volatility": volatility,
        "sample_size": SAMPLE_SIZE,
        "return_count": RETURN_COUNT,
        "window_ddof": WINDOW_DDOF,
        "return_type": RETURN_TYPE,
        "frequency": FREQUENCY,
        "adjustment": ADJUSTMENT,
    }
    return _digest_json(payload)


def _digest_json(payload: Any) -> str:
    """A stable sha256 digest of JSON-serializable content."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cached-vendor adapter
# ---------------------------------------------------------------------------


def load_market_inputs(
    ticker: str, as_of: str, config: Mapping[str, Any] | None = None
) -> MarketInputs:
    """Fetch historical closes through the cached vendor adapter, then validate.

    Calls ``load_ohlcv(canonical, as_of, fill_gaps=False)`` — the existing, cached
    Yahoo adapter — and runs ``analyze_prices`` on the result. When ``config`` is
    given, it is scoped with ``run_config`` so the adapter reads that run's cache
    directory and vendors, and the caller's prior config is restored afterwards even
    on failure. The completed-historical-day guard is applied *before* the fetch, so
    a current or future date never reaches the network. Acquisition errors
    (``NoMarketDataError`` and friends) and validation errors propagate unchanged;
    nothing is broadly swallowed.
    """
    canonical = safe_ticker_component(normalize_symbol(ticker))
    _canonical_date(as_of, "as_of")
    # Reject a present/future date before calling the vendor at all.
    _require_completed_historical(as_of)
    if config is not None:
        with run_config(config):
            frame = load_ohlcv(canonical, as_of, fill_gaps=False)
    else:
        frame = load_ohlcv(canonical, as_of, fill_gaps=False)
    return analyze_prices(
        frame, canonical, as_of,
        data_source="Yahoo/yfinance/load_ohlcv (possibly cached)",
        acquisition=ACQUISITION_ADAPTER,
    )


# ---------------------------------------------------------------------------
# Snapshot write / replay
# ---------------------------------------------------------------------------

_SNAPSHOT_FIELDS = frozenset({
    "version", "ticker", "as_of", "last_observed_date", "sample_size", "return_count",
    "window_ddof", "return_type", "frequency", "adjustment", "daily_volatility",
    "last_21_closes", "data_source", "retrieval_utc", "content_digest",
})


def write_snapshot(inputs: MarketInputs, path: str | Path) -> Path:
    """Write the validated selected closes to a versioned JSON snapshot atomically.

    Creates only the destination's parent directory. The file records the actual
    dated closes and parameters, so a reader can recompute the volatility without
    trusting the stored number. It is an integrity-checked record, not proof that
    the prices are authentic.

    ``replace_file`` is a cache helper that quietly keeps the old file when another
    reader holds it open (Windows). For a snapshot that silence would look like a
    successful write, so after the replacement this reads the file back and raises
    ``OSError`` unless it plainly holds the intended content.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SNAPSHOT_VERSION,
        "ticker": inputs.ticker,
        "as_of": inputs.as_of,
        "last_observed_date": inputs.last_observed_date,
        "sample_size": inputs.sample_size,
        "return_count": inputs.return_count,
        "window_ddof": inputs.window_ddof,
        "return_type": inputs.return_type,
        "frequency": inputs.frequency,
        "adjustment": inputs.adjustment,
        "daily_volatility": inputs.daily_volatility,
        "last_21_closes": [[d, c] for d, c in inputs.last_21_closes],
        "data_source": inputs.data_source,
        "retrieval_utc": inputs.retrieval_utc,
        "content_digest": inputs.content_digest,
    }
    # allow_nan=False makes a non-finite value a hard error, never a NaN token.
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    replace_file(path, lambda temp: Path(temp).write_text(text, encoding="utf-8"))
    # replace_file may silently skip the move (PermissionError -> keeps the old
    # file). A snapshot must not report success without the intended content.
    try:
        written = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"snapshot {path} was not written: {exc}") from exc
    if written != text:
        raise OSError(
            f"snapshot {path} does not hold the intended content after writing "
            "(a concurrent reader may have blocked the replacement)"
        )
    return path


def read_snapshot(
    path: str | Path, *, ticker: str, as_of: str
) -> MarketInputs:
    """Read a snapshot and independently re-validate it for the requested run.

    Rejects a malformed or tampered file, a version/parameter mismatch, a run
    mismatch (different ticker or date) and an inconsistent stored volatility. The
    stored closes are re-run through ``analyze_prices`` — so date order, positivity,
    futures and staleness are re-checked and the volatility recomputed — and the
    result is labelled ``snapshot_replay``, preserving the original source and
    retrieval time. The digest is an unkeyed numeric integrity check, not a
    signature, so it does not authenticate the provenance. A replay is never
    presented as a fresh fetch.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"snapshot {path} could not be read: {exc}") from exc
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise ValueError(f"snapshot {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"snapshot {path} is not a JSON object")
    unknown = set(data) - _SNAPSHOT_FIELDS
    if unknown:
        raise ValueError(f"snapshot has unknown field(s): {sorted(unknown)}")
    missing = _SNAPSHOT_FIELDS - set(data)
    if missing:
        raise ValueError(f"snapshot missing field(s): {sorted(missing)}")

    # Integer parameters must be real ints, never booleans (True == 1 must not pass).
    if isinstance(data["version"], bool) or data["version"] != SNAPSHOT_VERSION:
        raise ValueError(f"snapshot version {data['version']!r} is not {SNAPSHOT_VERSION}")
    for field, expected in (
        ("sample_size", SAMPLE_SIZE), ("return_count", RETURN_COUNT),
        ("window_ddof", WINDOW_DDOF), ("return_type", RETURN_TYPE),
        ("frequency", FREQUENCY), ("adjustment", ADJUSTMENT),
    ):
        if isinstance(data[field], bool) or data[field] != expected:
            raise ValueError(f"snapshot {field} {data[field]!r} is not {expected!r}")

    requested_ticker = safe_ticker_component(normalize_symbol(ticker))
    requested_date = _canonical_date(as_of, "as_of")
    if data["ticker"] != requested_ticker or data["as_of"] != requested_date:
        raise ValueError(
            f"snapshot is for {data['ticker']} on {data['as_of']}, not "
            f"{requested_ticker} on {requested_date}"
        )

    # Provenance must be well-formed: a string source, a UTC retrieval timestamp,
    # and a last observed date that is a real canonical date.
    source = _require_provenance_text(data["data_source"], "snapshot data_source")
    retrieval = _parse_utc_timestamp(data["retrieval_utc"], "snapshot retrieval_utc")
    stored_last = _canonical_date(data["last_observed_date"], "snapshot last_observed_date")

    stored_closes = data["last_21_closes"]
    if not isinstance(stored_closes, list) or len(stored_closes) != SAMPLE_SIZE:
        raise ValueError(f"snapshot closes must be a list of {SAMPLE_SIZE} entries")
    stored_pairs_list: list[tuple[Any, Any]] = []
    for entry in stored_closes:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError(f"snapshot close entry must be a [date, close] pair, got {entry!r}")
        # Each stored close date must be a canonical YYYY-MM-DD string.
        _canonical_date(entry[0], "snapshot close date")
        stored_pairs_list.append((entry[0], entry[1]))
    stored_pairs = tuple(stored_pairs_list)
    if stored_pairs[-1][0] != stored_last:
        raise ValueError(
            f"snapshot last_observed_date {stored_last} does not match its final "
            f"close date {stored_pairs[-1][0]}"
        )

    stored_volatility = data["daily_volatility"]
    if not _is_real_number(stored_volatility) or float(stored_volatility) < 0.0:
        raise ValueError("snapshot daily_volatility is not a non-negative finite number")

    # Recompute the digest from the stored content and compare, then independently
    # re-validate order/positivity/freshness and recompute the volatility.
    recomputed_digest = _market_content_digest(
        ticker=requested_ticker, as_of=requested_date,
        window_pairs=stored_pairs, volatility=float(stored_volatility),
    )
    if recomputed_digest != data["content_digest"]:
        raise ValueError("snapshot content digest does not match its stored values")

    frame = pd.DataFrame(
        {"Date": [d for d, _c in stored_pairs], "Close": [c for _d, c in stored_pairs]}
    )
    replayed = analyze_prices(
        frame, requested_ticker, requested_date,
        data_source=source,
        acquisition=ACQUISITION_SNAPSHOT_REPLAY,
        retrieval_utc=retrieval,
    )
    if not math.isclose(
        replayed.daily_volatility, float(stored_volatility), rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ValueError("snapshot stored volatility is inconsistent with its closes")
    return replayed


__all__ = [
    "PositionContext",
    "MarketInputs",
    "parse_position_context",
    "load_position_context",
    "analyze_prices",
    "load_market_inputs",
    "write_snapshot",
    "read_snapshot",
    "SAMPLE_SIZE",
    "RETURN_COUNT",
    "WINDOW_DDOF",
    "RETURN_TYPE",
    "FREQUENCY",
    "ADJUSTMENT",
    "SNAPSHOT_VERSION",
    "ACQUISITION_ADAPTER",
    "ACQUISITION_SNAPSHOT_REPLAY",
]
