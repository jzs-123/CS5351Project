"""Offline tests for R3 P3: input context and historical-price adaptation.

Covers the approved T07 input portion, T09-T11 adapter/snapshot contracts and the
relevant T16 state/digest cases. No test here reaches the network or a model; the
vendor adapter is monkeypatched. Expected volatilities are computed independently
from the formulas, not by calling the implementation's own helper.
"""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

import tradingagents.position_inputs as pi
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.position_inputs import (
    ACQUISITION_ADAPTER,
    ACQUISITION_SNAPSHOT_REPLAY,
    SAMPLE_SIZE,
    analyze_prices,
    load_market_inputs,
    load_position_context,
    parse_position_context,
    read_snapshot,
    write_snapshot,
)

FIXED_NOW = "2026-06-01"
AS_OF = "2026-05-29"  # strictly before FIXED_NOW


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Pin both the local and UTC day helpers so fixtures are deterministic."""
    monkeypatch.setattr(pi, "get_current_date", lambda: FIXED_NOW)
    monkeypatch.setattr(pi, "_utc_today", lambda: FIXED_NOW)


def _frame(closes):
    """A Date/Close frame with business days ending at AS_OF."""
    dates = pd.bdate_range(end=AS_OF, periods=len(closes))
    return pd.DataFrame({"Date": dates, "Close": list(closes)})


def _flat_frame(n=21, price=100.0):
    return _frame([price] * n)


# ---------------------------------------------------------------------------
# PositionContext: explicit zero vs unknown, validation, digest, matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_context_accepts_explicit_zero_and_unknown_is_not_zero():
    zero = parse_position_context(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": 0, "weight_source": "user"}
    )
    unknown = parse_position_context(
        {"ticker": "AAPL", "as_of": AS_OF, "weight_source": "user"}
    )
    assert zero.current_weight == 0.0
    assert unknown.current_weight is None
    assert zero.digest() != unknown.digest()


@pytest.mark.unit
def test_context_null_weight_is_unknown():
    context = parse_position_context(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": None, "weight_source": "user"}
    )
    assert context.current_weight is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), -0.01, 1.01,
                                 "0.1", 10**400, [0.1]])
def test_context_rejects_bad_current_weight(bad):
    with pytest.raises(ValueError):
        parse_position_context(
            {"ticker": "AAPL", "as_of": AS_OF, "current_weight": bad, "weight_source": "user"}
        )


@pytest.mark.unit
def test_context_requires_nonempty_strings_and_canonical_date():
    for bad in (
        {"ticker": "", "as_of": AS_OF, "weight_source": "user"},
        {"ticker": "AAPL", "as_of": "2026-5-29", "weight_source": "user"},
        {"ticker": "AAPL", "as_of": "2026-02-30", "weight_source": "user"},
        {"ticker": "AAPL", "as_of": AS_OF, "weight_source": ""},
    ):
        with pytest.raises(ValueError):
            parse_position_context(bad)


@pytest.mark.unit
def test_context_rejects_unknown_and_missing_fields():
    with pytest.raises(ValueError, match="unknown"):
        parse_position_context(
            {"ticker": "AAPL", "as_of": AS_OF, "weight_source": "user", "current_weigth": 0.1}
        )
    with pytest.raises(ValueError, match="missing"):
        parse_position_context({"ticker": "AAPL", "as_of": AS_OF})


@pytest.mark.unit
def test_context_normalizes_ticker_aliases():
    context = parse_position_context(
        {"ticker": "700.HK", "as_of": AS_OF, "weight_source": "user"}
    )
    assert context.ticker == "0700.HK"


@pytest.mark.unit
def test_context_matches_run_after_normalization():
    context = parse_position_context(
        {"ticker": "aapl", "as_of": AS_OF, "weight_source": "user"}
    )
    assert context.matches_run("AAPL", AS_OF)
    assert not context.matches_run("MSFT", AS_OF)
    assert not context.matches_run("AAPL", "2026-05-28")
    assert not context.matches_run("AAPL", "not-a-date")


@pytest.mark.unit
def test_context_to_dict_is_json_safe():
    context = parse_position_context(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": 0.05, "weight_source": "user"}
    )
    payload = context.to_dict()
    assert json.loads(json.dumps(payload, allow_nan=False))["current_weight"] == 0.05


# ---------------------------------------------------------------------------
# File loading: JSON syntax, duplicate/nonobject/unknown keys, content change
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_context_reads_valid_file(tmp_path):
    path = tmp_path / "pos.json"
    path.write_text(json.dumps(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": 0.1, "weight_source": "user"}
    ), encoding="utf-8")
    assert load_position_context(path).current_weight == 0.1


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "{not json}",
    "[]",
    f'{{"ticker": "AAPL", "ticker": "MSFT", "as_of": "{AS_OF}", "weight_source": "u"}}',
    f'{{"ticker": "AAPL", "as_of": "{AS_OF}", "weight_source": "u", "extra": 1}}',
])
def test_load_context_rejects_bad_documents(tmp_path, text):
    path = tmp_path / "pos.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_position_context(path)


@pytest.mark.unit
def test_load_context_rereads_and_digest_changes_with_content(tmp_path):
    path = tmp_path / "pos.json"
    path.write_text(json.dumps(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": 0.1, "weight_source": "user"}
    ), encoding="utf-8")
    first = load_position_context(path)
    path.write_text(json.dumps(
        {"ticker": "AAPL", "as_of": AS_OF, "current_weight": 0.2, "weight_source": "user"}
    ), encoding="utf-8")
    second = load_position_context(path)
    assert first.digest() != second.digest()


# ---------------------------------------------------------------------------
# analyze_prices: independent analytic volatility example (T09)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_alternating_returns_give_analytic_stdev():
    # 20 returns alternating -0.01, +0.01 (mean 0) -> sample stdev = 0.01*sqrt(20/19).
    closes = [100.0]
    for i in range(20):
        step = -0.01 if i % 2 == 0 else 0.01
        closes.append(closes[-1] * (1.0 + step))
    inputs = analyze_prices(_frame(closes), "AAPL", AS_OF)
    expected = 0.01 * math.sqrt(20 / 19)
    assert inputs.daily_volatility == pytest.approx(expected, rel=1e-9)
    assert inputs.sample_size == 21 and inputs.return_count == 20


@pytest.mark.unit
def test_flat_prices_are_valid_zero_volatility():
    inputs = analyze_prices(_flat_frame(), "AAPL", AS_OF)
    assert inputs.daily_volatility == 0.0


@pytest.mark.unit
def test_selects_last_21_from_longer_clean_data():
    # A clean 40-row series: the window is the last 21 closes only.
    closes = [100.0 + 0.5 * i for i in range(40)]
    inputs = analyze_prices(_frame(closes), "AAPL", AS_OF)
    assert len(inputs.last_21_closes) == SAMPLE_SIZE
    assert inputs.last_21_closes[-1][1] == pytest.approx(closes[-1])
    assert inputs.last_21_closes[0][1] == pytest.approx(closes[-21])


# ---------------------------------------------------------------------------
# analyze_prices: date and price validation (T10)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_insufficient_observations_rejected():
    with pytest.raises(ValueError, match="21"):
        analyze_prices(_flat_frame(n=20), "AAPL", AS_OF)


@pytest.mark.unit
def test_duplicate_dates_rejected():
    frame = _flat_frame()
    frame.loc[frame.index[-1], "Date"] = frame.loc[frame.index[-2], "Date"]
    with pytest.raises(ValueError, match="duplicate"):
        analyze_prices(frame, "AAPL", AS_OF)


@pytest.mark.unit
def test_unordered_dates_rejected():
    frame = _flat_frame()
    frame = frame.iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="ascending"):
        analyze_prices(frame, "AAPL", AS_OF)


@pytest.mark.unit
def test_future_row_rejected():
    frame = _flat_frame()
    frame.loc[frame.index[-1], "Date"] = pd.Timestamp("2026-05-30")  # after AS_OF
    with pytest.raises(ValueError, match="after the analysis date"):
        analyze_prices(frame, "AAPL", AS_OF)


@pytest.mark.unit
def test_nat_date_rejected():
    frame = _flat_frame()
    frame.loc[frame.index[0], "Date"] = pd.NaT
    with pytest.raises(ValueError, match="invalid date"):
        analyze_prices(frame, "AAPL", AS_OF)


@pytest.mark.unit
@pytest.mark.parametrize("bad_close", [0.0, -1.0, float("nan"), float("inf"), True, "100"])
def test_bad_close_rejected(bad_close):
    frame = _flat_frame()
    # Cast to object first: pandas 3 assigns into a float64 column by dtype, which
    # would raise a TypeError here before analyze_prices can reject the value.
    frame["Close"] = frame["Close"].astype(object)
    frame.loc[frame.index[-1], "Close"] = bad_close
    with pytest.raises(ValueError):
        analyze_prices(frame, "AAPL", AS_OF)


@pytest.mark.unit
def test_today_and_future_analysis_dates_unavailable():
    # A present/future date cannot have a settled daily close.
    with pytest.raises(ValueError, match="strictly before"):
        analyze_prices(_flat_frame(), "AAPL", FIXED_NOW)
    with pytest.raises(ValueError, match="strictly before"):
        analyze_prices(_flat_frame(), "AAPL", "2026-06-30")


@pytest.mark.unit
def test_load_market_inputs_checks_utc_day_before_fetching(monkeypatch):
    # Local day is May 30 but the UTC day is still May 29 morning; a May 29 run is
    # the still-current US session and must be rejected BEFORE load_ohlcv is called.
    monkeypatch.setattr(pi, "get_current_date", lambda: "2026-05-30")
    monkeypatch.setattr(pi, "_utc_today", lambda: "2026-05-29")
    called = []
    monkeypatch.setattr(pi, "load_ohlcv", lambda *a, **k: called.append(a) or _flat_frame())
    with pytest.raises(ValueError, match="strictly before"):
        load_market_inputs("AAPL", "2026-05-29")
    assert called == []  # no vendor call happened


@pytest.mark.unit
def test_load_market_inputs_allows_a_date_before_both_days(monkeypatch):
    monkeypatch.setattr(pi, "get_current_date", lambda: "2026-05-30")
    monkeypatch.setattr(pi, "_utc_today", lambda: "2026-05-29")
    # The vendor frame ends on the requested date (2026-05-28), not AS_OF.
    frame = pd.DataFrame(
        {"Date": pd.bdate_range(end="2026-05-28", periods=21), "Close": [100.0] * 21}
    )
    monkeypatch.setattr(pi, "load_ohlcv", lambda *a, **k: frame)
    inputs = load_market_inputs("AAPL", "2026-05-28")
    assert inputs.as_of == "2026-05-28"


@pytest.mark.unit
def test_provenance_requires_clear_source_and_utc_timestamp():
    with pytest.raises(ValueError, match="data_source"):
        analyze_prices(_flat_frame(), "AAPL", AS_OF, data_source="")
    with pytest.raises(ValueError, match="data_source"):
        analyze_prices(_flat_frame(), "AAPL", AS_OF, data_source=123)
    with pytest.raises(ValueError, match="acquisition"):
        analyze_prices(_flat_frame(), "AAPL", AS_OF, acquisition="  ")
    # A non-UTC or naive timestamp is rejected rather than silently coerced.
    with pytest.raises(ValueError, match="retrieval_utc"):
        analyze_prices(_flat_frame(), "AAPL", AS_OF, retrieval_utc="2026-06-01T00:00:00")
    with pytest.raises(ValueError, match="retrieval_utc"):
        analyze_prices(
            _flat_frame(), "AAPL", AS_OF, retrieval_utc="2026-06-01T00:00:00+08:00"
        )


@pytest.mark.unit
def test_stale_boundary_10_allowed_11_rejected():
    # A 21-row window whose latest close is exactly 10 calendar days before as_of.
    def frame_ending(days_before):
        as_of_ts = pd.Timestamp(AS_OF)
        last = as_of_ts - pd.Timedelta(days=days_before)
        dates = pd.bdate_range(end=last, periods=21)
        return pd.DataFrame({"Date": dates, "Close": [100.0] * 21})

    analyze_prices(frame_ending(10), "AAPL", AS_OF)  # exactly 10 -> allowed
    with pytest.raises(ValueError, match="stale"):
        analyze_prices(frame_ending(11), "AAPL", AS_OF)


@pytest.mark.unit
def test_timezone_local_dates_are_preserved():
    # A tz-aware bar at +09:00 keeps its own calendar date, not a shifted UTC day.
    dates = pd.bdate_range(end=AS_OF, periods=21).tz_localize("Asia/Tokyo")
    frame = pd.DataFrame({"Date": dates, "Close": [100.0] * 21})
    inputs = analyze_prices(frame, "AAPL", AS_OF)
    assert inputs.last_observed_date == AS_OF


@pytest.mark.unit
def test_datetimeindex_supported_and_validated_identically():
    dates = pd.bdate_range(end=AS_OF, periods=21)
    frame = pd.DataFrame({"Close": [100.0] * 21}, index=dates)
    inputs = analyze_prices(frame, "AAPL", AS_OF)
    assert len(inputs.last_21_closes) == 21
    # Duplicate index dates rejected the same way as a Date column.
    dup = pd.DataFrame({"Close": [100.0] * 21}, index=[dates[0]] * 21)
    with pytest.raises(ValueError, match="duplicate"):
        analyze_prices(dup, "AAPL", AS_OF)


@pytest.mark.unit
def test_frame_is_not_mutated():
    frame = _flat_frame()
    before = frame.copy(deep=True)
    analyze_prices(frame, "AAPL", AS_OF)
    pd.testing.assert_frame_equal(frame, before)


@pytest.mark.unit
def test_missing_columns_rejected():
    with pytest.raises(ValueError, match="Date and Close"):
        analyze_prices(pd.DataFrame({"Close": [100.0] * 21}), "AAPL", AS_OF)


# ---------------------------------------------------------------------------
# load_market_inputs: adapter scoping and error propagation (T11)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_adapter_calls_load_ohlcv_with_fill_gaps_false(monkeypatch):
    calls = {}

    def fake_load(symbol, as_of, fill_gaps=True):
        calls["symbol"], calls["as_of"], calls["fill_gaps"] = symbol, as_of, fill_gaps
        return _flat_frame()

    monkeypatch.setattr(pi, "load_ohlcv", fake_load)
    inputs = load_market_inputs("700.HK", AS_OF)
    assert calls == {"symbol": "0700.HK", "as_of": AS_OF, "fill_gaps": False}
    assert inputs.ticker == "0700.HK"
    assert inputs.acquisition == ACQUISITION_ADAPTER


@pytest.mark.unit
def test_adapter_scopes_config_and_restores_prior(monkeypatch):
    from tradingagents.dataflows.config import get_config

    seen = {}

    def fake_load(symbol, as_of, fill_gaps=True):
        seen["cache"] = get_config()["data_cache_dir"]
        return _flat_frame()

    monkeypatch.setattr(pi, "load_ohlcv", fake_load)
    load_market_inputs("AAPL", AS_OF, config={"data_cache_dir": "/tmp/r3-cache"})
    assert seen["cache"] == "/tmp/r3-cache"
    # After the call, the process-wide config is unaffected by the scoped one.
    assert get_config()["data_cache_dir"] != "/tmp/r3-cache"


@pytest.mark.unit
def test_adapter_restores_config_on_failure(monkeypatch):
    from tradingagents.dataflows.config import get_config

    def fake_load(symbol, as_of, fill_gaps=True):
        raise NoMarketDataError(symbol)

    monkeypatch.setattr(pi, "load_ohlcv", fake_load)
    with pytest.raises(NoMarketDataError):
        load_market_inputs("AAPL", AS_OF, config={"data_cache_dir": "/tmp/r3-fail"})
    assert get_config()["data_cache_dir"] != "/tmp/r3-fail"


@pytest.mark.unit
def test_adapter_does_not_swallow_acquisition_errors(monkeypatch):
    def fake_load(symbol, as_of, fill_gaps=True):
        raise NoMarketDataError(symbol, detail="empty")

    monkeypatch.setattr(pi, "load_ohlcv", fake_load)
    with pytest.raises(NoMarketDataError):
        load_market_inputs("AAPL", AS_OF)


# ---------------------------------------------------------------------------
# Digests and provenance: retrieval time does not change content digest
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_digest_ignores_retrieval_time_but_tracks_content():
    a = analyze_prices(_flat_frame(), "AAPL", AS_OF, retrieval_utc="2026-06-01T00:00:00+00:00")
    b = analyze_prices(_flat_frame(), "AAPL", AS_OF, retrieval_utc="2026-06-02T12:00:00+00:00")
    assert a.content_digest == b.content_digest
    changed = _frame([100.0] * 20 + [101.0])
    c = analyze_prices(changed, "AAPL", AS_OF)
    assert c.content_digest != a.content_digest


@pytest.mark.unit
def test_provenance_fields_record_parameters_and_source():
    inputs = analyze_prices(_flat_frame(), "AAPL", AS_OF)
    assert inputs.last_observed_date == AS_OF
    assert (inputs.sample_size, inputs.return_count) == (21, 20)
    assert inputs.return_type == "simple" and inputs.frequency == "1d"
    assert "load_ohlcv" in inputs.data_source


# ---------------------------------------------------------------------------
# Snapshots: round-trip, replay recomputation, tampering (T11 / T16)
# ---------------------------------------------------------------------------


def _inputs():
    closes = [100.0 + 0.5 * i for i in range(21)]
    return analyze_prices(_frame(closes), "AAPL", AS_OF, retrieval_utc="2026-06-01T00:00:00+00:00")


@pytest.mark.unit
def test_snapshot_round_trip_recomputes_and_flags_replay(tmp_path):
    original = _inputs()
    path = write_snapshot(original, tmp_path / "snaps" / "AAPL.json")
    replayed = read_snapshot(path, ticker="AAPL", as_of=AS_OF)
    assert replayed.daily_volatility == pytest.approx(original.daily_volatility)
    assert replayed.last_21_closes == original.last_21_closes
    assert replayed.acquisition == ACQUISITION_SNAPSHOT_REPLAY
    assert replayed.data_source == original.data_source
    assert replayed.retrieval_utc == original.retrieval_utc
    assert replayed.content_digest == original.content_digest


@pytest.mark.unit
def test_snapshot_is_json_without_nan(tmp_path):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)


@pytest.mark.unit
def test_snapshot_rejects_different_run(tmp_path):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    with pytest.raises(ValueError, match="not"):
        read_snapshot(path, ticker="MSFT", as_of=AS_OF)
    with pytest.raises(ValueError, match="not"):
        read_snapshot(path, ticker="AAPL", as_of="2026-05-28")


@pytest.mark.unit
def test_snapshot_rejects_tampered_close(tmp_path):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["last_21_closes"][-1][1] = 9999.0
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_rejects_inconsistent_stored_volatility(tmp_path):
    original = _inputs()
    path = write_snapshot(original, tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Keep closes and digest consistent but corrupt the stored volatility so the
    # independent recomputation disagrees.
    data["daily_volatility"] = float(data["daily_volatility"]) + 1.0
    # Recompute a matching digest with the corrupted value to bypass the digest check.
    from tradingagents.position_inputs import _market_content_digest
    data["content_digest"] = _market_content_digest(
        ticker=data["ticker"], as_of=data["as_of"],
        window_pairs=tuple((d, c) for d, c in data["last_21_closes"]),
        volatility=data["daily_volatility"],
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="inconsistent"):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", [
    {"version": 999},
    {"sample_size": 20},
    {"frequency": "1h"},
    {"return_count": 19},
])
def test_snapshot_rejects_bad_params(tmp_path, mutation):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(mutation)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_rejects_malformed_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json}", encoding="utf-8")
    with pytest.raises(ValueError):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_replay_revalidates_dates(tmp_path):
    # A stored file whose closes are out of order must fail re-validation, not
    # replay a silently-accepted bad series.
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["last_21_closes"][0], data["last_21_closes"][1] = (
        data["last_21_closes"][1], data["last_21_closes"][0])
    from tradingagents.position_inputs import _market_content_digest
    data["content_digest"] = _market_content_digest(
        ticker=data["ticker"], as_of=data["as_of"],
        window_pairs=tuple((d, c) for d, c in data["last_21_closes"]),
        volatility=data["daily_volatility"],
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="ascending"):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


def _rewrite_with_matching_digest(path, data):
    """Store ``data`` back with a digest recomputed over its current content."""
    from tradingagents.position_inputs import _market_content_digest
    data = dict(data)
    data["content_digest"] = _market_content_digest(
        ticker=data["ticker"], as_of=data["as_of"],
        window_pairs=tuple((d, c) for d, c in data["last_21_closes"]),
        volatility=data["daily_volatility"],
    )
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["sample_size", "version", "window_ddof"])
def test_snapshot_rejects_boolean_integer_params(tmp_path, field):
    # True == 1 in Python. version==1 and window_ddof==1 in particular would pass
    # an int-equality check, so each case must start from a fresh valid snapshot
    # and be rejected on its own.
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_rejects_inconsistent_last_observed_date(tmp_path):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Set it to a canonical date that is not the final stored close date.
    last_close_date = data["last_21_closes"][-1][0]
    data["last_observed_date"] = data["last_21_closes"][0][0]
    assert data["last_observed_date"] != last_close_date
    _rewrite_with_matching_digest(path, data)
    with pytest.raises(ValueError, match="last_observed_date"):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_rejects_noncanonical_close_date(tmp_path):
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["last_21_closes"][0][0] = "2026-4-1"
    _rewrite_with_matching_digest(path, data)
    with pytest.raises(ValueError):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
@pytest.mark.parametrize("field, bad", [
    ("data_source", ""),
    ("data_source", 5),
    ("retrieval_utc", "2026-06-01T00:00:00"),
    ("retrieval_utc", "2026-06-01T00:00:00+08:00"),
])
def test_snapshot_rejects_blank_source_and_nonutc_retrieval(tmp_path, field, bad):
    # Each case starts from a fresh valid snapshot so only the mutated field can
    # be the cause of the failure.
    path = write_snapshot(_inputs(), tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = bad
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_snapshot(path, ticker="AAPL", as_of=AS_OF)


@pytest.mark.unit
def test_snapshot_preserves_source_and_retrieval_on_replay(tmp_path):
    # A valid write/read round-trip keeps the original provenance verbatim.
    original = _inputs()
    path = write_snapshot(original, tmp_path / "s.json")
    replayed = read_snapshot(path, ticker="AAPL", as_of=AS_OF)
    assert replayed.data_source == original.data_source
    assert replayed.retrieval_utc == original.retrieval_utc == "2026-06-01T00:00:00+00:00"
    assert replayed.acquisition == ACQUISITION_SNAPSHOT_REPLAY


@pytest.mark.unit
def test_write_snapshot_raises_when_replacement_is_swallowed(tmp_path, monkeypatch):
    # Simulate replace_file's Windows behaviour: it keeps the old file and does not
    # write the new content. write_snapshot must not report success.
    path = tmp_path / "s.json"

    def swallowed(p, write):
        # Pretend the target is in use: leave any existing file untouched.
        return None

    monkeypatch.setattr(pi, "replace_file", swallowed)
    with pytest.raises(OSError):
        write_snapshot(_inputs(), path)


@pytest.mark.unit
def test_write_snapshot_raises_when_old_file_remains(tmp_path, monkeypatch):
    # The target already holds different content and the replacement is skipped.
    path = tmp_path / "s.json"
    path.write_text("stale content", encoding="utf-8")
    monkeypatch.setattr(pi, "replace_file", lambda p, write: None)
    with pytest.raises(OSError):
        write_snapshot(_inputs(), path)
    assert path.read_text(encoding="utf-8") == "stale content"  # untouched, not claimed
