"""R3 P4 integration tests: config, per-run preparation, the completion hook, CLI.

These exercise the actual paths — the graph constructor, ``propagate``/
``record_decision``, the CLI command and ``run_analysis`` — with scripted offline
models and vendors (reusing the harness from ``test_usage_tracker``); no test
reaches the network or a model. The pure core and adapter are covered elsewhere.
"""

from __future__ import annotations

import copy
import json

import pandas as pd
import pytest
from typer.testing import CliRunner

import cli.main as cli_main
import cli.run as cli_run
import tradingagents.graph.trading_graph as trading_graph
import tradingagents.position_inputs as position_inputs
from tests.test_usage_tracker import (
    FAKE_PRICING,
    TRADE_DATE,
    _e2e_graph,
    _E2EClient,
    _E2EModel,
    _read_cost,
    _wire_cli,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.position_processing import complete_position_signal

AS_OF = TRADE_DATE  # a historical date the adapter accepts


@pytest.fixture
def e2e_offline(monkeypatch):
    """Every data vendor answers offline so a full graph/CLI run needs no network.

    Mirrors the shared fixture in test_usage_tracker (imported there as a fixture is
    not re-exportable cleanly); kept local to avoid module-level fixture coupling.
    """
    from tradingagents.agents import context as agent_context
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot

    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor, lambda *a, _m=method, **k: f"{_m} data")
    prices = pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000,
    })
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker",
                        lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})())
    agent_context._identity.cache_clear()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"))
    cfg.update(overrides)
    return cfg


def _market_frame(closes):
    return pd.DataFrame({
        "Date": pd.bdate_range(end=AS_OF, periods=len(closes)),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": list(closes), "Volume": 1_000_000,
    })


def _patch_market(monkeypatch, frame=None, raises=None):
    """Control R3 market data independently of the analysts' snapshot path."""
    calls = {"n": 0}

    def fake_load(symbol, as_of, fill_gaps=True):
        calls["n"] += 1
        if raises is not None:
            raise raises
        return (frame if frame is not None else _market_frame([100.0] * 21)).copy()

    monkeypatch.setattr(position_inputs, "load_ohlcv", fake_load)
    return calls


def _context(ticker="NVDA", as_of=AS_OF, weight=0.05, source="user"):
    return {"ticker": ticker, "as_of": as_of, "current_weight": weight, "weight_source": source}


def _write_context(tmp_path, name="pos.json", weight=0.05, ticker="NVDA", as_of=AS_OF):
    path = tmp_path / name
    path.write_text(json.dumps(_context(ticker, as_of, weight)), encoding="utf-8")
    return path


def _full_state(rating, ticker="NVDA", date=AS_OF):
    """A minimal final_state with every field _log_state requires."""
    return {
        "company_of_interest": ticker, "trade_date": date, "asset_type": "stock",
        "market_report": "M", "sentiment_report": "S", "news_report": "N",
        "fundamentals_report": "F", "investment_plan": "P",
        "trader_investment_plan": "T", "final_trade_decision": f"Rating: {rating}",
        "final_rating": rating,
        "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "",
                                    "current_response": "", "count": 0},
        "risk_debate_state": {"aggressive_history": "", "conservative_history": "",
                              "neutral_history": "", "history": "", "latest_speaker": "",
                              "current_aggressive_response": "", "current_conservative_response": "",
                              "current_neutral_response": "", "count": 0},
    }


def _hook(config, rating, ticker="NVDA", date=AS_OF):
    """Call the completion hook directly with a tiny state (no _log_state involved)."""
    state = {"final_trade_decision": f"Rating: {rating}", "final_rating": rating}
    complete_position_signal(config, ticker, date, state)
    return state["position_signal"]


def _resolved_cfg(tmp_path, **overrides):
    """A graph-style normalized R3 config dict for direct hook tests."""
    from tradingagents.position_processing import with_resolved_config

    return with_resolved_config(_cfg(tmp_path, **overrides), DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# 1. Constructor: config validation happens before client creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"position_enabled": 1},
    {"position_enabled": "yes"},
    {"position_enabled": None},
    {"position_policy": None},               # explicit null is rejected
    {"position_policy": {"step": 2.0}},
    {"position_policy": {"nope": 0.1}},
    {"position_context": {}},                # explicit empty dict is invalid
    {"position_context": {"ticker": "NVDA", "as_of": AS_OF, "weight_source": "u",
                          "current_weight": True}},
])
def test_bad_r3_config_fails_before_client_creation(tmp_path, monkeypatch, bad):
    made = []
    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda *a, **k: made.append(1) or _E2EClient(_E2EModel(), "p", "m"))
    with pytest.raises(ValueError):
        trading_graph.TradingAgentsGraph(config=_cfg(tmp_path, **bad), debug=False)
    assert made == []


@pytest.mark.unit
def test_mixed_type_policy_keys_raise_valueerror_not_typeerror(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda *a, **k: _E2EClient(_E2EModel(), "p", "m"))
    with pytest.raises(ValueError):
        trading_graph.TradingAgentsGraph(
            config=_cfg(tmp_path, position_policy={"step": 0.05, 1: 0.2}))


@pytest.mark.unit
def test_valid_config_is_detached_and_preserves_source_path(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda *a, **k: _E2EClient(_E2EModel(), "p", "m"))
    path = _write_context(tmp_path)
    caller = _cfg(tmp_path, position_context=str(path), position_policy={"step": 0.03})
    original = copy.deepcopy(caller)
    graph = trading_graph.TradingAgentsGraph(config=caller, debug=False)
    assert caller == original                          # caller untouched
    assert isinstance(graph.config["position_context"], dict)
    assert graph.config["position_context_path"] == str(path)  # source preserved
    assert graph.config["position_policy"]["step"] == 0.03
    assert DEFAULT_CONFIG["position_context"] is None
    assert DEFAULT_CONFIG["position_policy"] == {}


# ---------------------------------------------------------------------------
# 2. Per-run match fails before graph invocation; frozen across the run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_python_context_mismatch_fails_before_invoke(tmp_path, monkeypatch):
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(),
                       position_context=_context(ticker="MSFT"))
    invoked = []
    monkeypatch.setattr(graph.graph, "invoke", lambda *a, **k: invoked.append(1) or {})
    with pytest.raises(ValueError, match="not NVDA"):
        graph.propagate("NVDA", AS_OF)
    assert invoked == []


@pytest.mark.unit
def test_file_changed_during_run_does_not_change_signal(tmp_path, monkeypatch, e2e_offline):
    # Freeze at prepare time: a file mutated inside the model run must not change
    # this run's signal (it is read once, before model work).
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    model = _E2EModel()
    graph = _e2e_graph(tmp_path, monkeypatch, model, position_context=str(path))
    real_invoke = graph.graph.invoke

    def invoke_then_mutate(*a, **k):
        _write_context(tmp_path, weight=0.90)  # changed mid-run
        return real_invoke(*a, **k)

    monkeypatch.setattr(graph.graph, "invoke", invoke_then_mutate)
    state, _ = graph.propagate("NVDA", AS_OF)
    assert state["position_signal"]["context"]["current_weight"] == 0.05


# ---------------------------------------------------------------------------
# 3. Hook: no-fetch branches, enabled/off, provenance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_disabled_pops_stale_signal(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    cfg = _resolved_cfg(tmp_path, position_enabled=False)
    state = {"final_trade_decision": "Rating: Buy", "final_rating": "Buy",
             "position_signal": {"stale": True}}
    complete_position_signal(cfg, "NVDA", AS_OF, state)
    assert "position_signal" not in state
    assert calls["n"] == 0


@pytest.mark.unit
def test_enabled_without_context_reports_needs_context_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(_resolved_cfg(tmp_path, position_enabled=True), "Hold")
    assert signal["status"] == "needs_context" and signal["weight"] is None
    assert calls["n"] == 0


@pytest.mark.unit
def test_enabled_without_context_sell_is_zero_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(_resolved_cfg(tmp_path, position_enabled=True), "Sell")
    assert signal["status"] == "ok" and signal["weight"] == 0.0
    assert signal["market"] is None
    assert calls["n"] == 0


@pytest.mark.unit
def test_review_needs_review_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(_resolved_cfg(tmp_path, position_context=_context()), "REVIEW")
    assert signal["status"] == "needs_review" and signal["weight"] is None
    assert calls["n"] == 0


@pytest.mark.unit
def test_sell_needs_no_market_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.3)), "Sell")
    assert signal["status"] == "ok" and signal["weight"] == 0.0
    assert signal["market"] is None and calls["n"] == 0


@pytest.mark.unit
def test_zero_cap_needs_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(
        _resolved_cfg(tmp_path, position_context=_context(weight=0.05),
                      position_policy={"max_position_weight": 0.0}), "Buy")
    assert signal["weight"] == 0.0
    assert calls["n"] == 0


@pytest.mark.unit
def test_underweight_zero_target_needs_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.02)), "Underweight")
    assert signal["weight"] == 0.0 and calls["n"] == 0


@pytest.mark.unit
def test_scaling_disabled_positive_target_needs_no_fetch(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch)
    signal = _hook(
        _resolved_cfg(tmp_path, position_context=_context(weight=0.05),
                      position_policy={"volatility_scaling": False}), "Buy")
    assert signal["weight"] is not None and calls["n"] == 0


@pytest.mark.unit
def test_positive_target_fetches_and_top_level_provenance_is_populated(tmp_path, monkeypatch):
    calls = _patch_market(monkeypatch, frame=_market_frame([100.0 + i for i in range(21)]))
    signal = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.05)), "Buy")
    assert calls["n"] == 1
    # Top-level provenance filled from MarketInputs (not left None).
    assert signal["data_source"]
    assert signal["last_price_date"] == AS_OF
    assert signal["sample_size"] == 21
    assert signal["market"]["last_observed_date"] == AS_OF
    assert signal["input_digest"]

    # A no-fetch run invents no source/volatility provenance.
    nofetch = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.05)), "Sell")
    assert nofetch["market"] is None
    assert nofetch["data_source"] is None and nofetch["sample_size"] is None


@pytest.mark.unit
def test_market_failure_degrades_to_needs_data_keeping_rating(tmp_path, monkeypatch):
    _patch_market(monkeypatch, raises=RuntimeError("provider secret /internal/path"))
    signal = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.05)), "Buy")
    assert signal["status"] == "needs_data"
    assert signal["weight"] is None and signal["stop_loss_pct"] is None
    assert signal["rating"] == "Buy"
    assert "RuntimeError" in signal["reason"]
    assert "secret" not in signal["reason"] and "/internal/path" not in signal["reason"]


# ---------------------------------------------------------------------------
# 4. record_decision persists; state log allowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_record_decision_persists_signal_with_full_state(tmp_path, monkeypatch):
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_context=_context(weight=0.05))
    state = _full_state("Buy")
    graph.record_decision("NVDA", AS_OF, state)
    log = json.loads(next((tmp_path / "results").rglob("full_states_log_*.json")).read_text())
    assert log["position_signal"]["status"] == "ok"
    assert log["final_rating"] == "Buy"


@pytest.mark.unit
def test_log_state_omits_signal_when_disabled(tmp_path, monkeypatch):
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_enabled=False)
    graph.record_decision("NVDA", AS_OF, _full_state("Hold"))
    log = json.loads(next((tmp_path / "results").rglob("full_states_log_*.json")).read_text())
    assert "position_signal" not in log


# ---------------------------------------------------------------------------
# 5. Real propagate(): API normal/debug parity via the actual graph path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
def test_propagate_attaches_signal_and_preserves_rating(tmp_path, monkeypatch, e2e_offline, capsys, debug):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0 + 0.1 * i for i in range(21)]))
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), debug=debug,
                       position_context=_context(ticker="NVDA", weight=0.05))
    state, signal = graph.propagate("NVDA", AS_OF)
    assert signal == state["final_rating"] == "Overweight"
    assert state["position_signal"]["rating"] == "Overweight"
    assert state["position_signal"]["status"] == "ok"
    assert [e["rating"] for e in graph.memory_log.load_entries()] == ["Overweight"]


# ---------------------------------------------------------------------------
# 6. R5 budget / model failure: cost persisted, no hook on aborted graph
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_api_budget_stop_persists_cost_and_runs_no_hook(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    model = _E2EModel()
    graph = _e2e_graph(tmp_path, monkeypatch, model, selected_analysts=("market",),
                       position_context=_context(ticker="NVDA"), max_run_budget_usd=0.0)
    hook_calls = []
    orig = trading_graph.complete_position_signal
    monkeypatch.setattr(trading_graph, "complete_position_signal",
                        lambda *a, **k: hook_calls.append(1) or orig(*a, **k))
    with pytest.raises(ut.BudgetExceeded):
        graph.propagate("NVDA", AS_OF)
    data = _read_cost(tmp_path)
    assert data["stopped"] is True and data["stop_reason"] == "BUDGET_STOPPED"
    assert hook_calls == []                                    # no completion on abort
    assert list((tmp_path / "results").rglob("full_states_log_*.json")) == []


@pytest.mark.unit
def test_api_model_error_persists_cost_and_runs_no_hook(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    model = _E2EModel(fail_after=2)
    graph = _e2e_graph(tmp_path, monkeypatch, model, selected_analysts=("market",),
                       position_context=_context(ticker="NVDA"))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        graph.propagate("NVDA", AS_OF)
    assert _read_cost(tmp_path)["tokens_in"] > 0
    assert list((tmp_path / "results").rglob("full_states_log_*.json")) == []


# ---------------------------------------------------------------------------
# 7. Actual CLI run_analysis success path and parity with the API
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_run_analysis_attaches_signal_and_writes_state_log(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    cli_run = _wire_cli(tmp_path, monkeypatch, _E2EModel())
    cli_run.run_analysis(flags={"ticker": "NVDA", "date": AS_OF, "analysts": "market",
                                "save": False, "show": False}, position_context=str(path))
    log = json.loads(next((tmp_path / "results").rglob("full_states_log_*.json")).read_text())
    assert log["position_signal"]["status"] == "ok"
    assert log["final_rating"] == "Overweight"


@pytest.mark.unit
def test_api_and_cli_produce_identical_signal_for_same_input(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)

    api_graph = _e2e_graph(tmp_path / "api", monkeypatch, _E2EModel(), position_context=str(path))
    api_state, _ = api_graph.propagate("NVDA", AS_OF)
    api_signal = api_state["position_signal"]

    cli_run = _wire_cli(tmp_path / "cli", monkeypatch, _E2EModel())
    cli_run.run_analysis(flags={"ticker": "NVDA", "date": AS_OF, "analysts": "market",
                                "save": False, "show": False}, position_context=str(path))
    cli_log = json.loads(next((tmp_path / "cli" / "results").rglob("full_states_log_*.json")).read_text())
    cli_signal = cli_log["position_signal"]

    assert api_signal["rating"] == cli_signal["rating"] == "Overweight"
    assert api_signal["weight"] == pytest.approx(cli_signal["weight"])
    assert api_signal["input_digest"] == cli_signal["input_digest"]


# ---------------------------------------------------------------------------
# 8. CLI failures before graph construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_malformed_context_json_exits_before_run(tmp_path, monkeypatch):
    # Exercise the ACTUAL command: cli.main.run_analysis is real, so the malformed
    # JSON must be rejected inside run_analysis before prompts or the graph. Sentinel
    # stubs prove get_user_selections/TradingAgentsGraph were never reached.
    bad = tmp_path / "bad.json"
    bad.write_text("{not json}", encoding="utf-8")
    reached = []
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: reached.append("prompts"))
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: reached.append("graph"))
    result = CliRunner().invoke(cli_main.app, ["--position-context", str(bad)])
    assert result.exit_code == 1
    assert reached == []  # no prompt, no graph construction


def _stub_selection(monkeypatch, ticker="NVDA", date=AS_OF):
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": ticker, "analysis_date": date, "asset_type": "stock",
        "analysts": [], "research_depth": 1, "quick_think_llm": "q",
        "deep_think_llm": "d", "backend_url": None, "llm_provider": "openai",
        "output_language": "English",
    })


@pytest.mark.unit
def test_cli_context_mismatch_exits_before_graph_constructor(tmp_path, monkeypatch):
    _stub_selection(monkeypatch, ticker="NVDA", date=AS_OF)
    path = _write_context(tmp_path, ticker="MSFT")
    built = []
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: built.append(1))
    with pytest.raises(cli_run.typer.Exit):
        cli_run.run_analysis(flags={"ticker": "NVDA"}, position_context=str(path))
    assert built == []


@pytest.mark.unit
def test_cli_direct_run_analysis_malformed_json_exits_before_constructor(tmp_path, monkeypatch):
    _stub_selection(monkeypatch)
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")  # non-object JSON
    built = []
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: built.append(1))
    with pytest.raises(cli_run.typer.Exit):
        cli_run.run_analysis(flags={"ticker": "NVDA"}, position_context=str(bad))
    assert built == []


# ---------------------------------------------------------------------------
# 9. Config scope/restoration, no cross-run leakage, signature invalidation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_scope_used_during_fetch_and_restored(tmp_path, monkeypatch, e2e_offline, capsys):
    from tradingagents.dataflows import config as config_module

    seen = {}

    def fake_load(symbol, as_of, fill_gaps=True):
        seen["cache_during"] = config_module.get_config()["data_cache_dir"]
        return _market_frame([100.0] * 21).copy()

    monkeypatch.setattr(position_inputs, "load_ohlcv", fake_load)
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(),
                       position_context=_context(ticker="NVDA"))
    graph.propagate("NVDA", AS_OF)
    assert seen["cache_during"] == str(tmp_path / "cache")
    assert config_module._run_config.get() is None  # scoped config restored


@pytest.mark.unit
def test_no_cross_run_context_leakage(tmp_path, monkeypatch):
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(),
                       position_context=_context(ticker="NVDA", weight=0.05))
    with pytest.raises(ValueError, match="not MSFT"):
        graph.create_run_state("MSFT", AS_OF)


@pytest.mark.unit
def test_reused_graph_rereads_same_path_on_each_run(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_context=str(path),
                       checkpoint_enabled=True)

    # Record every _run_signature the graph computes; the first after each run start
    # is the one begin_checkpoint keyed on, i.e. before that run executed.
    sigs = []
    real_sig = graph._run_signature
    monkeypatch.setattr(graph, "_run_signature",
                        lambda *a, **k: sigs.append(real_sig(*a, **k)) or sigs[-1])

    graph.propagate("NVDA", AS_OF)
    first_run_sig = sigs[0]
    _write_context(tmp_path, weight=0.20)  # same path, changed content
    sigs.clear()
    graph.propagate("NVDA", AS_OF)
    second_run_sig = sigs[0]
    # The second run reread the same path and keyed its checkpoint on new content.
    assert first_run_sig != second_run_sig


# ---------------------------------------------------------------------------
# 10. Digest tracks actual policy values; rerun/disable replace the signal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_policy_values_change_digest_with_same_version(tmp_path, monkeypatch):
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    a = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.05),
                            position_policy={"step": 0.05}), "Buy")
    b = _hook(_resolved_cfg(tmp_path, position_context=_context(weight=0.05),
                            position_policy={"step": 0.03}), "Buy")
    assert a["policy_version"] == b["policy_version"]
    assert a["input_digest"] != b["input_digest"]


@pytest.mark.unit
def test_success_then_data_failure_clears_stale_provenance(tmp_path, monkeypatch):
    cfg = _resolved_cfg(tmp_path, position_context=_context(weight=0.05))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    state = {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}
    complete_position_signal(cfg, "NVDA", AS_OF, state)
    assert state["position_signal"]["market"] is not None
    # Now the market source fails: the fresh signal must not retain the old source/vol.
    _patch_market(monkeypatch, raises=RuntimeError("down"))
    complete_position_signal(cfg, "NVDA", AS_OF, state)
    assert state["position_signal"]["status"] == "needs_data"
    assert state["position_signal"]["market"] is None
    assert state["position_signal"]["data_source"] is None
    assert state["position_signal"]["daily_volatility"] is None


@pytest.mark.unit
def test_invalid_flag_change_before_run_rejects_before_invoke(tmp_path, monkeypatch):
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_context=_context())
    graph.config["position_enabled"] = "not-a-bool"  # in-memory change
    invoked = []
    monkeypatch.setattr(graph.graph, "invoke", lambda *a, **k: invoked.append(1) or {})
    with pytest.raises(ValueError):
        graph.propagate("NVDA", AS_OF)
    assert invoked == []


# ---------------------------------------------------------------------------
# 11. Exactly-once hook, frozen-during-run inputs, strict None rerun
# ---------------------------------------------------------------------------


def _count_hook(monkeypatch):
    calls = []
    orig = trading_graph.complete_position_signal
    monkeypatch.setattr(trading_graph, "complete_position_signal",
                        lambda *a, **k: calls.append(1) or orig(*a, **k))
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("debug", [False, True], ids=["normal", "debug"])
def test_hook_runs_exactly_once_api(tmp_path, monkeypatch, e2e_offline, capsys, debug):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    calls = _count_hook(monkeypatch)
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), debug=debug,
                       position_context=_context(ticker="NVDA"))
    graph.propagate("NVDA", AS_OF)
    assert len(calls) == 1


@pytest.mark.unit
def test_hook_runs_exactly_once_cli(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    calls = _count_hook(monkeypatch)
    cli_run = _wire_cli(tmp_path, monkeypatch, _E2EModel())
    cli_run.run_analysis(flags={"ticker": "NVDA", "date": AS_OF, "analysts": "market",
                                "save": False, "show": False}, position_context=str(path))
    assert len(calls) == 1


@pytest.mark.unit
def test_context_and_policy_frozen_during_fake_invoke(tmp_path, monkeypatch, e2e_offline):
    # Mutating the context/policy inside the model run must not affect this run's
    # signal; the next run sees the new file.
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_context=str(path))
    real_invoke = graph.graph.invoke

    def invoke_mutate(*a, **k):
        _write_context(tmp_path, weight=0.90)          # context file changed mid-run
        graph.config["position_policy"] = {"step": 0.5}  # policy mutated mid-run
        return real_invoke(*a, **k)

    monkeypatch.setattr(graph.graph, "invoke", invoke_mutate)
    state, _ = graph.propagate("NVDA", AS_OF)
    sig = state["position_signal"]
    assert sig["context"]["current_weight"] == 0.05      # frozen at start
    assert sig["policy"]["step"] == 0.05                 # frozen at start

    # A fresh run picks up the changed file (and its own frozen policy).
    state2, _ = graph.propagate("NVDA", AS_OF)
    assert state2["position_signal"]["context"]["current_weight"] == 0.90


@pytest.mark.unit
def test_prepare_run_frozen_despite_in_place_dict_mutation(tmp_path, monkeypatch):
    # Finding: PreparedRun aliased the normalized config dicts. Mutating those dicts
    # IN PLACE after prepare_run must not change completion's context/policy/weight/
    # digest, and the emitted JSON must not alias the prepared internals either.
    from tradingagents.position_processing import complete_position_signal, prepare_run

    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    cfg = _resolved_cfg(tmp_path, position_context=_context(weight=0.05),
                        position_policy={"step": 0.05, "volatility_scaling": False})
    new_config, prepared = prepare_run(cfg, "NVDA", AS_OF)

    # Mutate the normalized dicts in place (not by replacing graph.config references).
    new_config["position_context"]["current_weight"] = 0.9
    new_config["position_policy"]["step"] = 0.5

    state = {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}
    complete_position_signal(new_config, "NVDA", AS_OF, state, prepared)
    sig = state["position_signal"]
    assert sig["context"]["current_weight"] == 0.05   # start value, not 0.9
    assert sig["policy"]["step"] == 0.05              # start value, not 0.5
    # Emitted JSON must be a copy: mutating it must not touch the prepared internals.
    sig["context"]["current_weight"] = 0.0
    sig["policy"]["step"] = 0.0
    assert prepared.context["current_weight"] == 0.05
    assert prepared.policy["step"] == 0.05
    # A subsequent run validates the actual new (mutated) values from config.
    state2 = {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}
    complete_position_signal(new_config, "NVDA", AS_OF, state2, None)
    assert state2["position_signal"]["context"]["current_weight"] == 0.9
    assert state2["position_signal"]["policy"]["step"] == 0.5


@pytest.mark.unit
def test_path_context_assigned_after_construction_refreshes_each_run(tmp_path, monkeypatch, e2e_offline):
    # A path assigned to graph.config AFTER construction must refresh every run (and
    # keep its source path), and replacing it with an explicit mapping must stop the
    # old path from overriding the replacement.
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel())  # R3 off at construction
    path = _write_context(tmp_path, weight=0.05)
    graph.config["position_context"] = str(path)            # path assigned later

    state, _ = graph.propagate("NVDA", AS_OF)
    assert state["position_signal"]["context"]["current_weight"] == 0.05

    # Same path, changed file content: the next run rereads it.
    _write_context(tmp_path, weight=0.30)
    state, _ = graph.propagate("NVDA", AS_OF)
    assert state["position_signal"]["context"]["current_weight"] == 0.30

    # Replace the file context with an explicit mapping; the old path must not win.
    graph.config["position_context"] = _context(weight=0.12)
    state, _ = graph.propagate("NVDA", AS_OF)
    assert state["position_signal"]["context"]["current_weight"] == 0.12


@pytest.mark.unit
@pytest.mark.parametrize("base, mutated", [(0, False), (1, True)])
def test_bool_mutated_path_context_rejected_not_reloaded(tmp_path, monkeypatch, e2e_offline, base, mutated):
    # A path-based context normalizes to a mapping + source marker. Changing its
    # current_weight in place to a bool must be rejected (T07), not hidden by a file
    # reload: False==0 / True==1 must not read as "unchanged since normalization".
    _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel())
    path = _write_context(tmp_path, weight=base)
    graph.config["position_context"] = str(path)
    graph.propagate("NVDA", AS_OF)  # establishes the normalized mapping + source marker

    graph.config["position_context"]["current_weight"] = mutated  # in-place bool
    invoked = []
    monkeypatch.setattr(graph.graph, "invoke", lambda *a, **k: invoked.append(1) or {})
    with pytest.raises(ValueError):
        graph.propagate("NVDA", AS_OF)
    assert invoked == []  # rejected before graph invocation


@pytest.mark.unit
def test_explicit_none_policy_rejected_on_rerun(tmp_path, monkeypatch, e2e_offline):
    # A policy set to None on the live config before a rerun is rejected, matching
    # the constructor's strict handling (no silent default on a rerun).
    graph = _e2e_graph(tmp_path, monkeypatch, _E2EModel(), position_context=_context())
    graph.config["position_policy"] = None
    invoked = []
    monkeypatch.setattr(graph.graph, "invoke", lambda *a, **k: invoked.append(1) or {})
    with pytest.raises(ValueError):
        graph.propagate("NVDA", AS_OF)
    assert invoked == []


# ---------------------------------------------------------------------------
# 12. CLI budget stop / provider error with R3 enabled: no hook, cost persisted
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_budget_stop_with_r3_no_hook_no_fetch_cost_persisted(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    market_calls = _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    hook_calls = _count_hook(monkeypatch)
    ended = []
    orig_end = TradingAgentsGraph.end_checkpoint
    monkeypatch.setattr(TradingAgentsGraph, "end_checkpoint",
                        lambda self: ended.append(1) or orig_end(self))
    cli_run = _wire_cli(tmp_path, monkeypatch, _E2EModel(),
                        config_extra={"max_run_budget_usd": 0.0, "checkpoint_enabled": True})
    cli_run.run_analysis(flags={"ticker": "NVDA", "date": AS_OF, "analysts": "market",
                                "save": False, "show": False}, position_context=str(path))
    assert hook_calls == []            # no completion hook on the aborted graph
    assert market_calls["n"] == 0      # no R3 price fetch
    assert ended, "checkpoint must be released on budget stop"
    data = _read_cost(tmp_path)
    assert data["stopped"] is True and data["stop_reason"] == "BUDGET_STOPPED"
    assert list((tmp_path / "results").rglob("full_states_log_*.json")) == []


@pytest.mark.unit
def test_cli_provider_error_with_r3_no_hook_cost_persisted(tmp_path, monkeypatch, e2e_offline):
    from tradingagents.llm_clients import usage_tracker as ut

    monkeypatch.setattr(ut, "get_price", lambda p, m: FAKE_PRICING.get((p, m)))
    market_calls = _patch_market(monkeypatch, frame=_market_frame([100.0] * 21))
    path = _write_context(tmp_path, weight=0.05)
    hook_calls = _count_hook(monkeypatch)
    cli_run = _wire_cli(tmp_path, monkeypatch, _E2EModel(fail_after=3))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        cli_run.run_analysis(flags={"ticker": "NVDA", "date": AS_OF, "analysts": "market",
                                    "save": False, "show": False}, position_context=str(path))
    assert hook_calls == []
    assert market_calls["n"] == 0
    assert _read_cost(tmp_path)["tokens_in"] > 0
    assert list((tmp_path / "results").rglob("full_states_log_*.json")) == []
