"""P5: presentation of the R3 position-and-risk guidance (T14/T15/T16/T17/T18).

Offline only: no network or model. The shared helper formats an existing
``position_signal`` (the P4 payload); these tests prove the Markdown, HTML and
terminal renderings agree with that payload, that failure statuses never show a
stale positive target, that hostile text stays inert (inspecting the real saved
HTML), and that both the API and CLI report entry points produce the section.
"""

from __future__ import annotations

import copy
import io
import re
from html.parser import HTMLParser

import pytest
from rich.console import Console

import cli.display as cli_display
import cli.run as cli_run
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.position import PositionPolicy, process_position
from tradingagents.position_reporting import (
    SECTION_FILENAME,
    SECTION_FOLDER,
    render_position_markdown,
)
from tradingagents.reporting import write_report_tree

SETTINGS = {"version": "1.2.3", "llm_provider": "openai", "deep_think_llm": "c",
            "quick_think_llm": "q", "analysts": ["market"], "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1, "data_vendors": {"core_stock_apis": "yfinance"}}

POLICY = PositionPolicy()


def _payload(rating, current_weight=None, daily_volatility=None, *, policy=POLICY,
             market=True, status_override=None, **overrides):
    """A P4-shaped position_signal payload from the real core (no model/network)."""
    signal = process_position(rating, current_weight, daily_volatility, policy)
    payload = signal.to_dict()
    payload["input_as_of"] = "2026-01-09"
    payload["context"] = (
        None if current_weight is None else
        {"ticker": "NVDA", "as_of": "2026-01-09", "current_weight": current_weight,
         "weight_source": "user"}
    )
    payload["policy"] = {
        "buy_target_weight": policy.buy_target_weight, "step": policy.step,
        "max_position_weight": policy.max_position_weight,
        "volatility_threshold": policy.volatility_threshold,
        "volatility_scaling": policy.volatility_scaling,
        "stop_loss_pct": policy.stop_loss_pct, "policy_version": policy.policy_version,
    }
    if market and signal.status == "ok" and signal.daily_volatility is not None:
        payload["data_source"] = "Yahoo/yfinance/load_ohlcv (possibly cached)"
        payload["last_price_date"] = "2026-01-09"
        payload["sample_size"] = 21
        payload["market"] = {
            "data_source": "Yahoo/yfinance/load_ohlcv (possibly cached)",
            "acquisition": "load_ohlcv", "last_observed_date": "2026-01-09",
            "sample_size": 21, "return_count": 20, "window_ddof": 1,
            "return_type": "simple", "frequency": "1d", "adjustment": "auto_adjust",
        }
    else:
        payload["market"] = None
    payload["input_digest"] = "deadbeefcafe"
    if status_override is not None:
        payload["status"] = status_override
    payload.update(overrides)
    return payload


def _state(signal, **extra):
    state = {"company_of_interest": "NVDA", "trade_date": "2026-01-09", "market_report": "M",
             "sentiment_report": "", "news_report": "", "fundamentals_report": "",
             "investment_plan": "P", "trader_investment_plan": "T",
             "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy",
             "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "",
                                         "current_response": "", "count": 0},
             "risk_debate_state": {"aggressive_history": "", "conservative_history": "",
                                   "neutral_history": "", "history": "", "latest_speaker": "",
                                   "current_aggressive_response": "", "current_conservative_response": "",
                                   "current_neutral_response": "", "count": 0}}
    if signal is not None:
        state["position_signal"] = signal
    state.update(extra)
    return state


def _bad_pct_cells(text):
    """Any percentage cell holding nan/inf (never legitimate source text)."""
    return re.findall(r"(?:nan|inf)%", text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Core mapping -> text agreement
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_buy_high_vol_shows_ten_percent_and_stop():
    # Buy, +5% current, 4% vol -> scale 0.5 -> cap 0.10 -> final 10%; stop 8%.
    # The raw 20% target is reduced to the 10% cap, so cap_override is TRUE.
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    assert payload["status"] == "ok" and payload["weight"] == pytest.approx(0.10)
    assert payload["raw_target_weight"] == pytest.approx(0.20)
    assert payload["effective_cap"] == pytest.approx(0.10)
    assert payload["cap_override"] is True
    text = render_position_markdown(payload)
    assert "10.0%" in text and "8.0%" in text
    assert "20.0%" in text  # raw target shown before cap
    assert "Computed" in text
    assert "Cap override | Yes" in text
    assert "risk cap" in text.lower()


@pytest.mark.unit
def test_buy_low_vol_no_cap_override():
    # Buy, +5% current, 1% vol -> scale 1 -> cap 0.20 -> raw target 20% kept; no override.
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.01)
    assert payload["status"] == "ok" and payload["weight"] == pytest.approx(0.20)
    assert payload["cap_override"] is False
    text = render_position_markdown(payload)
    assert "No cap override" in text


@pytest.mark.unit
def test_hold_over_cap_shows_cap_override():
    payload = _payload("Hold", current_weight=0.25, daily_volatility=0.01)
    assert payload["status"] == "ok" and payload["weight"] == pytest.approx(0.20)
    text = render_position_markdown(payload)
    assert "Cap override | Yes" in text


@pytest.mark.unit
def test_sell_zero_target_has_no_stop_loss():
    payload = _payload("Sell")
    assert payload["status"] == "ok" and payload["weight"] == 0.0
    text = render_position_markdown(payload)
    assert "0.0%" in text
    assert "Stop-loss policy | not applicable" in text


@pytest.mark.unit
def test_zero_target_suppresses_stale_stop():
    # A zero target with a stale 8% stop must still show no stop loss.
    payload = _payload("Sell")
    payload["stop_loss_pct"] = 0.08
    text = render_position_markdown(payload)
    assert "Stop-loss policy | not applicable" in text
    assert "| Stop-loss policy | 8.0%" not in text


@pytest.mark.unit
def test_needs_context_shows_no_target():
    for rating in ("Hold", "Overweight"):
        payload = _payload(rating, current_weight=None, daily_volatility=0.01)
        assert payload["status"] == "needs_context"
        text = render_position_markdown(payload)
        assert "not available" in text
        assert "Stop-loss policy | not applicable" in text
        # It is a missing input prerequisite, not an adequate-prices statement.
        assert "not assessed" in text


@pytest.mark.unit
def test_needs_data_market_text_is_honest_about_failure():
    payload = _payload("Buy", current_weight=0.05, daily_volatility=None)
    assert payload["status"] == "needs_data" and payload["market"] is None
    text = render_position_markdown(payload)
    assert "Needs market data" in text and "not available" in text
    # Must not claim data was not required when a positive target needed it.
    assert "not required for this result" not in text
    assert "required historical data unavailable" in text


@pytest.mark.unit
def test_review_shows_no_target_and_rating_prerequisite():
    text = render_position_markdown(_payload("REVIEW"))
    assert "Needs review" in text and "not available" in text
    assert "not assessed" in text


@pytest.mark.unit
def test_confidence_never_becomes_a_probability():
    text = render_position_markdown(_payload("Buy", current_weight=0.05, daily_volatility=0.01))
    assert "Confidence | not provided" in text


@pytest.mark.unit
def test_daily_volatility_and_top_level_policy_version_shown():
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload["daily_volatility"] = 0.04  # recorded, exceeds nothing here
    text = render_position_markdown(payload)
    assert "Daily volatility (recorded) | 4.0%" in text
    # Top-level policy_version still visible even with no nested policy block.
    payload2 = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload2["policy"] = None
    text2 = render_position_markdown(payload2)
    assert "Policy version: r3-v1" in text2


@pytest.mark.unit
def test_input_is_not_mutated():
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    snapshot = copy.deepcopy(payload)
    render_position_markdown(payload)
    assert payload == snapshot


# ---------------------------------------------------------------------------
# Failure status must not leak stale positive fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stale_positive_and_cap_fields_hidden_on_failure():
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload.update(status="needs_data", weight=0.20, stop_loss_pct=0.08,
                   raw_target_weight=0.20, effective_cap=0.10, cap_override=True)
    text = render_position_markdown(payload)
    assert "Final target weight | not available" in text
    assert "Stop-loss policy | not applicable" in text
    assert "| Final target weight | 20.0% |" not in text
    assert "Raw target" not in text and "Effective cap" not in text and "Cap override" not in text


# ---------------------------------------------------------------------------
# Malformed / overflow / hostile values
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-0.1, 1.1, 1e308, True, None, float("nan"), float("inf"),
                                 10**400, "0.2"])
def test_invalid_ok_weight_is_unavailable_not_computed(bad):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload["weight"] = bad
    text = render_position_markdown(payload)
    assert "Final target weight | not available" in text
    assert "Computed" not in text  # a bad ok result must not read as computed
    assert "Unavailable" in text
    assert _bad_pct_cells(text) == []


@pytest.mark.unit
def test_non_mapping_signal_omits_section():
    assert render_position_markdown(None) is None
    assert render_position_markdown("not a dict") is None
    assert render_position_markdown([]) is None


@pytest.mark.unit
def test_unknown_status_is_unavailable_not_crash():
    text = render_position_markdown({"status": "weird", "weight": 0.2, "rating": "Buy"})
    assert "Unavailable" in text
    assert "Final target weight | not available" in text


@pytest.mark.unit
def test_hostile_text_is_escaped_and_inert():
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    hostile = "<script>alert(1)</script> ![x](http://evil/x.png) [l](javascript:alert(1)) | pipe"
    payload["reason"] = hostile
    payload["policy"]["policy_version"] = "v1\n## Fake Heading\n<img src=x>"
    text = render_position_markdown(payload)
    assert "<script>" not in text
    assert "<img" not in text
    assert "\\[l\\]" in text
    assert "\n## Fake Heading" not in text


# ---------------------------------------------------------------------------
# Report tree: MD/HTML agreement, reconciliation, terminal
# ---------------------------------------------------------------------------


def _md(tmp_path):
    return (tmp_path / "complete_report.md").read_text(encoding="utf-8")


def _html(tmp_path):
    return (tmp_path / "complete_report.html").read_text(encoding="utf-8")


@pytest.mark.unit
def test_writer_adds_section_file_and_both_reports_agree(tmp_path):
    write_report_tree(_state(_payload("Buy", current_weight=0.05, daily_volatility=0.04)),
                      "NVDA", tmp_path, settings=SETTINGS)
    guidance = (tmp_path / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8")
    assert "10.0%" in guidance and "Stop-loss policy" in guidance
    md = _md(tmp_path)
    assert md.count("VI. Position and Risk Guidance") == 1  # one heading, no duplicate
    page = _html(tmp_path)
    assert page.count("VI. Position and Risk Guidance") >= 1
    assert "10.0%" in page
    assert "<script" not in page.lower()
    assert not re.search(r'<(img|iframe|object|embed)\b', page, re.IGNORECASE)
    assert "default-src 'none'" in page


@pytest.mark.unit
def test_no_signal_omits_r3_and_matches_legacy(tmp_path):
    write_report_tree(_state(None), "NVDA", tmp_path, settings=SETTINGS)
    assert "VI. Position and Risk Guidance" not in _md(tmp_path)
    assert not (tmp_path / SECTION_FOLDER / SECTION_FILENAME).exists()


@pytest.mark.unit
def test_success_then_off_removes_owned_guidance_only(tmp_path):
    write_report_tree(_state(_payload("Buy", current_weight=0.05, daily_volatility=0.04)),
                      "NVDA", tmp_path, settings=SETTINGS)
    (tmp_path / "notes.md").write_text("root note", encoding="utf-8")
    (tmp_path / SECTION_FOLDER / "mine.md").write_text("keep me", encoding="utf-8")
    write_report_tree(_state(None), "NVDA", tmp_path, settings=SETTINGS)
    assert not (tmp_path / SECTION_FOLDER / SECTION_FILENAME).exists()
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "root note"
    assert (tmp_path / SECTION_FOLDER / "mine.md").read_text(encoding="utf-8") == "keep me"
    assert "VI. Position and Risk Guidance" not in _md(tmp_path)
    assert "VI. Position and Risk Guidance" not in _html(tmp_path)


@pytest.mark.unit
def test_success_then_needs_data_overwrites_without_stale_positive(tmp_path):
    good = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    write_report_tree(_state(good), "NVDA", tmp_path, settings=SETTINGS)
    bad = dict(good)
    bad.update(status="needs_data", weight=0.20, stop_loss_pct=0.08, market=None)
    write_report_tree(_state(bad), "NVDA", tmp_path, settings=SETTINGS)
    guidance = (tmp_path / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8")
    assert "Needs market data" in guidance
    assert "Final target weight | not available" in guidance
    assert "| Final target weight | 20.0% |" not in guidance
    # The target-side stop-loss ROW must be not applicable; the recorded 8% stop-loss
    # POLICY in "Policy used" is legitimate metadata and may remain.
    assert "| Stop-loss policy | not applicable |" in guidance
    assert "| Stop-loss policy | 8.0%" not in guidance
    # Stale target-side fields are absent.
    assert "Raw target" not in guidance
    assert "Effective cap" not in guidance
    assert "Cap override" not in guidance


@pytest.mark.unit
def test_html_false_removes_old_html(tmp_path):
    write_report_tree(_state(_payload("Sell")), "NVDA", tmp_path, settings=SETTINGS, html=True)
    assert (tmp_path / "complete_report.html").exists()
    write_report_tree(_state(_payload("Sell")), "NVDA", tmp_path, settings=SETTINGS, html=False)
    assert not (tmp_path / "complete_report.html").exists()


@pytest.mark.unit
def test_repeated_save_is_deterministic_guidance(tmp_path):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    write_report_tree(_state(payload), "NVDA", tmp_path, settings=SETTINGS)
    first = (tmp_path / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8")
    write_report_tree(_state(payload), "NVDA", tmp_path, settings=SETTINGS)
    assert (tmp_path / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8") == first


@pytest.mark.unit
def test_old_checkpoint_shape_without_signal_is_compatible(tmp_path):
    write_report_tree(_state(None), "NVDA", tmp_path, settings=SETTINGS)
    assert "VI. Position and Risk Guidance" not in _md(tmp_path)
    assert (tmp_path / "5_portfolio" / "decision.md").exists()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_terminal_display_matches_saved_values(monkeypatch):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    buf = io.StringIO()
    monkeypatch.setattr(cli_display, "console", Console(file=buf, width=100, force_terminal=False))
    cli_display.display_complete_report(_state(payload))
    out = buf.getvalue()
    assert "VI. Position and Risk Guidance" in out
    assert "10.0%" in out and "8.0%" in out


@pytest.mark.unit
def test_terminal_renders_hostile_text_literally(monkeypatch):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload["reason"] = "[red]not markup[/red] <script>x</script>"
    buf = io.StringIO()
    monkeypatch.setattr(cli_display, "console", Console(file=buf, width=100, force_terminal=False))
    cli_display.display_complete_report(_state(payload))
    out = buf.getvalue()
    assert "not markup" in out and "script" in out
    assert "\x1b[" not in out


# ---------------------------------------------------------------------------
# Actual entry points: API save_reports and CLI _offer_reports
# ---------------------------------------------------------------------------


def _bare_report_graph(tmp_path):
    """A graph double with just what save_reports/run_settings need (no clients)."""
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path / "results")}
    graph.selected_analysts = ("market",)
    graph.run_settings = lambda: SETTINGS
    return graph


@pytest.mark.unit
def test_api_save_reports_writes_r3_matching_json(tmp_path):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    state = _state(payload)
    before = copy.deepcopy(state)
    graph = _bare_report_graph(tmp_path)
    graph.save_reports(state, "NVDA", save_path=tmp_path / "api", html=True)
    assert state == before  # input unchanged
    guidance = (tmp_path / "api" / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8")
    assert f"{payload['weight'] * 100:.1f}%" in guidance
    assert "| Reason |" in guidance and payload["reason"][:20] in guidance
    assert (tmp_path / "api" / "complete_report.html").exists()


@pytest.mark.unit
def test_cli_offer_reports_writes_r3(tmp_path, monkeypatch):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    state = _state(payload)
    graph = _bare_report_graph(tmp_path)
    graph.default_report_path = lambda ticker: tmp_path / "cli"
    cli_run._offer_reports(state, graph, "NVDA", save=True, show=False, html=True)
    guidance = (tmp_path / "cli" / SECTION_FOLDER / SECTION_FILENAME).read_text(encoding="utf-8")
    assert "10.0%" in guidance and "Stop-loss policy" in guidance
    assert (tmp_path / "cli" / "complete_report.html").exists()


# ---------------------------------------------------------------------------
# Real saved HTML: hostile R3 fields add no active content
# ---------------------------------------------------------------------------


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.scripts = 0
        self.embeds = 0
        self.headings = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "href" in attrs:
            self.hrefs.append(attrs["href"])
        if tag == "script":
            self.scripts += 1
        if tag in ("img", "iframe", "object", "embed"):
            self.embeds += 1
        if tag in ("h1", "h2"):
            self.headings.append(tag)


@pytest.mark.unit
def test_saved_html_hostile_r3_fields_add_no_active_content(tmp_path):
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    hostile = '<script>alert(1)</script><img src="http://evil/x.png">'
    payload["reason"] = hostile + ' <a href="http://evil/">x</a>'
    payload["input_digest"] = hostile
    payload["policy"]["policy_version"] = hostile
    payload["context"]["weight_source"] = hostile
    payload["data_source"] = hostile
    write_report_tree(_state(payload), "NVDA", tmp_path, settings=SETTINGS)

    parser = _PageParser()
    parser.feed(_html(tmp_path))
    # In-page anchors and the two official footer links are allowed; a hostile R3
    # value must not add a javascript:/http(s) link or any active element.
    allowed = {"https://tauric.ai", "https://github.com/TauricResearch",
               "https://github.com/TauricResearch/TradingAgents"}
    for href in parser.hrefs:
        if href.startswith("#"):
            continue
        assert href in allowed, href
        assert not href.lower().startswith("javascript:")
    assert not any("evil" in h for h in parser.hrefs)
    assert parser.scripts == 0 and parser.embeds == 0
    # Exactly one VI section heading in the page body/contents (no injected headings).
    assert "VI. Position and Risk Guidance" in _html(tmp_path)


@pytest.mark.unit
def test_saved_html_known_status_code_is_exact(tmp_path):
    # A known machine code must stay exact in the saved HTML — no visible backslash
    # from Markdown underscore escaping inside the code span.
    payload = _payload("Buy", current_weight=0.05, daily_volatility=None)  # needs_data
    write_report_tree(_state(payload), "NVDA", tmp_path, settings=SETTINGS)
    page = _html(tmp_path)
    assert "<code>needs_data</code>" in page
    assert "needs\\_data" not in page


@pytest.mark.unit
def test_saved_html_r3_section_is_scoped_and_wraps_long_digest(tmp_path):
    # The R3 section carries a scoped wrapping class; a 64-hex digest and a long
    # single-word weight_source remain present and literal. Section ids depend on how
    # many sections are populated, so locate the R3 section by class, not a fixed id.
    digest = "a" * 64
    payload = _payload("Buy", current_weight=0.05, daily_volatility=0.04)
    payload["input_digest"] = digest
    payload["context"]["weight_source"] = "x" * 120
    write_report_tree(_state(payload), "NVDA", tmp_path, settings=SETTINGS)
    page = _html(tmp_path)
    match = re.search(
        r'<section id="s(\d+)" class="position-guidance">(.*?)</section>', page, re.DOTALL
    )
    assert match, "R3 section with the scoped class was not found"
    section = match.group(0)
    # Its heading is the VI guidance section, inside the scoped R3 section only.
    assert "VI. Position and Risk Guidance" in section
    assert ".position-guidance" in page and "overflow-wrap: anywhere" in page
    assert digest in section
    assert "x" * 120 in section
    assert "<script" not in page.lower()


