"""P5: pure, shared presentation for the R3 position-and-risk guidance.

The only job here is to *format* an already-computed ``final_state['position_signal']``
(the P4 payload) as readable Markdown. It never computes a position, never refetches
market data, never reinterprets the rating, never changes state, and never calls a
model. The CLI terminal, the Markdown report tree and the HTML page (through the
shared report parts) all render exactly the text this module returns. The function
returns the section *body* only; the report writer and terminal own the VI heading.

Security posture: every value taken from the signal/context/policy/market dicts is
free text and is rendered as literal text — Markdown/table delimiters, angle
brackets and newlines are escaped so a value cannot open a link, an image, a script,
a heading or a new report section. Only an allowlist of display fields is read; whole
dicts, raw config or file paths are never stringified onto the page. The HTML page
reuses the existing renderer (raw HTML and images off, CSP ``default-src 'none'``), so
no new active content is introduced.
"""

from __future__ import annotations

import math
import re
from typing import Any

# The one section this module owns, kept together so the writer and the terminal
# display agree on where the guidance lives.
SECTION_HEADING = "VI. Position and Risk Guidance"
SECTION_FOLDER = "6_position"
SECTION_FILENAME = "guidance.md"
SECTION_AGENT = "Position Guidance"

STATUS_OK = "ok"
STATUS_NEEDS_CONTEXT = "needs_context"
STATUS_NEEDS_DATA = "needs_data"
STATUS_NEEDS_REVIEW = "needs_review"

# Status -> (short label, plain-English explanation). "ok" is computed, not success.
_STATUS_LABELS = {
    STATUS_OK: ("Computed", "A target position was computed from the rating and inputs."),
    STATUS_NEEDS_CONTEXT: (
        "Needs current position",
        "The current weight for this instrument was not provided, so no target "
        "could be computed. An unknown holding is not treated as zero.",
    ),
    STATUS_NEEDS_DATA: (
        "Needs market data",
        "A positive target needs a reliable recent daily volatility, which was not "
        "available this run, so no target could be computed.",
    ),
    STATUS_NEEDS_REVIEW: (
        "Needs review",
        "The final decision had no readable 5-tier rating, so no position was "
        "computed. Re-run or read the decision and judge it yourself.",
    ),
}

# Policy fields we will display, in reading order, with a human label.
_POLICY_FIELDS = (
    ("buy_target_weight", "Buy target"),
    ("step", "Increase/decrease step"),
    ("max_position_weight", "Fixed single-name cap"),
    ("volatility_threshold", "Volatility threshold"),
    ("stop_loss_pct", "Stop-loss policy"),
)


def _is_real_number(value: Any) -> bool:
    """A finite real number, excluding booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _fraction(value: Any) -> float | None:
    """A finite value already within [0, 1], else None (weights/caps/confidence)."""
    if not _is_real_number(value):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


def _stop_fraction(value: Any) -> float | None:
    """A finite value within [0, 1), else None (a stop-loss fraction)."""
    if not _is_real_number(value):
        return None
    number = float(value)
    return number if 0.0 <= number < 1.0 else None


def _pct(number: float | None) -> str | None:
    """A validated fraction as a percentage, or None. Never yields inf%/nan%."""
    if number is None:
        return None
    scaled = number * 100.0
    if not math.isfinite(scaled):
        return None
    return f"{scaled:.1f}%"


def _pct_value(value: Any) -> str | None:
    """A [0, 1] fraction value as a percentage, else None."""
    return _pct(_fraction(value))


def _text(value: Any) -> str:
    """An allowlisted scalar as escaped, single-line literal text.

    Numeric fields never reach here; this is for free text (reason/source/version)
    and must stay inert in Markdown.
    """
    if value is None:
        return ""
    text = str(value)
    # Collapse CR/LF/tab to spaces so a value cannot start a new line, list item or
    # heading; then escape the characters that could form Markdown/table markup.
    text = re.sub(r"[\r\n\t]+", " ", text)
    for char, safe in (
        ("\\", "\\\\"), ("|", "\\|"), ("`", "\\`"), ("*", "\\*"), ("_", "\\_"),
        ("<", "&lt;"), (">", "&gt;"), ("[", "\\["), ("]", "\\]"),
        ("#", "\\#"), ("!", "\\!"),
    ):
        text = text.replace(char, safe)
    return text


def _status_label(status: Any) -> tuple[str, str]:
    if isinstance(status, str) and status in _STATUS_LABELS:
        return _STATUS_LABELS[status]
    return (
        "Unavailable",
        "The stored position result was not in a form this report can read, so no "
        "target is shown.",
    )


def _status_code(status: Any) -> str:
    """The machine status code, rendered exactly for allowlisted codes.

    Known codes (``needs_data`` etc.) contain underscores that Markdown would treat
    as emphasis inside a code span, leaving a visible backslash; they are emitted
    verbatim so the machine code stays exact. Any other value is untrusted and is
    escaped as ordinary text (never placed in an active Markdown construct).
    """
    if isinstance(status, str) and status in _STATUS_LABELS:
        return f"`{status}`"
    if isinstance(status, str):
        return _text(status)
    return "unknown"


def _mapping(value: Any) -> dict[str, Any]:
    """A dict-like value, else {} — so malformed shapes never crash rendering."""
    return value if isinstance(value, dict) else {}


def _is_ok(status: Any) -> bool:
    return status == STATUS_OK


def _market_line(status: Any, market: dict[str, Any], signal: dict[str, Any]) -> str:
    """The Market-data provenance line, honest about what happened.

    A present ``market`` block is always described from its recorded fields. When it
    is absent the wording depends on why: a positive-target ``needs_data`` run
    required a fetch that did not yield reliable volatility (never "not required");
    a missing-context/review run has an unmet input/rating prerequisite; an ``ok``
    result that needed no volatility (Sell / zero target / scaling off) may say not
    required. A malformed/unknown status is described as unrecorded.
    """
    if market:
        acquisition = market.get("acquisition")
        src = market.get("data_source")
        last = market.get("last_observed_date") or signal.get("last_price_date")
        parts = []
        if acquisition == "snapshot_replay":
            parts.append("replayed from a saved snapshot (not a fresh fetch)")
        elif isinstance(acquisition, str) and acquisition:
            parts.append(f"acquisition {_text(acquisition)}")
        if isinstance(src, str) and src:
            parts.append(f"source {_text(src)}")
        if isinstance(last, str) and last:
            parts.append(f"last price date {_text(last)}")
        head = "- Market data: recorded historical data"
        return head + (" (" + "; ".join(parts) + ")." if parts else ".")

    if status == STATUS_NEEDS_DATA:
        return ("- Market data: required historical data unavailable this run; no "
                "reliable volatility was recorded, so no target is shown.")
    if status in (STATUS_NEEDS_CONTEXT, STATUS_NEEDS_REVIEW):
        return ("- Market data: not assessed; this result stopped before a target "
                "needing prices was computed.")
    if status == STATUS_OK:
        return "- Market data: not required for this result (no volatility was fetched)."
    return "- Market data: provenance not recorded for this result."


def _markdown(source: Any) -> str:
    """The guidance section body (Markdown), assembled from allowlisted fields."""
    signal = _mapping(source)
    status = signal.get("status")
    ok = _is_ok(status)

    weight = signal.get("weight")
    weight_frac = _fraction(weight) if ok else None
    # An "ok" row is executable only with a valid [0,1] target; a positive target on a
    # failure status, or an invalid/overflowing/non-finite weight, is never shown.
    executable = weight_frac is not None
    # A malformed "ok" (bad weight) must not read as a successfully computed target.
    if ok and not executable:
        label, explanation = (
            "Unavailable",
            "This result is marked computed but its target weight is not a usable "
            "fraction, so no target is shown.",
        )
    else:
        label, explanation = _status_label(status)

    lines: list[str] = [
        f"- Status: **{label}** ({_status_code(status)}). "
        f"{_text(explanation)}",
        "- This is position *guidance*, not an executed order. A stop-loss is a "
        "percentage policy, not a guaranteed fill. A computed target is arithmetic, "
        "not investment success.",
        "",
        "| Item | Value |",
        "| --- | --- |",
    ]

    rating = signal.get("rating")
    lines.append(f"| Rating (unchanged) | {_text(rating) if isinstance(rating, str) and rating else 'not readable'} |")

    current = _fraction(signal.get("current_weight"))
    lines.append(f"| Current weight | {_pct(current) or 'unknown'} |")

    # On a failure/invalid result, suppress stale target-side fields entirely.
    if executable:
        lines.append(f"| Final target weight | {_pct(weight_frac)} |")
        raw = _fraction_beyond_one(signal.get("raw_target_weight"))
        if raw is not None:
            lines.append(f"| Raw target (before cap) | {_pct(raw)} |")
        cap = _fraction(signal.get("effective_cap"))
        if cap is not None:
            lines.append(f"| Effective cap | {_pct(cap)} |")
        cap_override = signal.get("cap_override")
        if cap_override is True:
            lines.append(
                "| Cap override | Yes — the risk cap, not the rating, decided the "
                "size: the raw target was reduced to the effective cap. |"
            )
        elif cap_override is False:
            lines.append("| Cap override | No cap override — the rating's raw target was within the cap. |")
        # A zero target carries no stop, even if an old dict still holds one.
        stop = _stop_fraction(signal.get("stop_loss_pct")) if weight_frac > 0.0 else None
        if stop is not None:
            lines.append(f"| Stop-loss policy | {_pct(stop)} (percentage policy, not a guaranteed fill) |")
        else:
            lines.append("| Stop-loss policy | not applicable |")
    else:
        lines.append("| Final target weight | not available |")
        lines.append("| Stop-loss policy | not applicable |")

    confidence = _fraction(signal.get("confidence"))
    source = signal.get("confidence_source")
    conf = _pct(confidence) if confidence is not None else "not provided"
    src_text = _text(source) if isinstance(source, str) and source else "not provided"
    lines.append(f"| Confidence | {_text(conf)} ({src_text}) |")

    # Recorded daily volatility (a fraction that may exceed 1). Never invents a value.
    daily_vol = signal.get("daily_volatility")
    if _is_real_number(daily_vol) and float(daily_vol) >= 0.0:
        lines.append(f"| Daily volatility (recorded) | {_pct(float(daily_vol)) or 'unavailable'} |")

    reason = signal.get("reason")
    if isinstance(reason, str) and reason.strip():
        lines.append(f"| Reason | {_text(reason)} |")
    lines.append("")

    # --- Policy actually used ---
    policy = _mapping(signal.get("policy"))
    top_version = signal.get("policy_version")
    if policy or (isinstance(top_version, str) and top_version):
        lines.append("### Policy used")
        lines.append("")
        for key, human in _POLICY_FIELDS:
            if key not in policy:
                continue
            shown = _pct_value(policy[key])
            if shown is not None:
                lines.append(f"- {human}: {shown}")
        scaling = policy.get("volatility_scaling")
        if scaling is True:
            lines.append("- Volatility scaling: on")
        elif scaling is False:
            lines.append("- Volatility scaling: off")
        version = policy.get("policy_version")
        if not (isinstance(version, str) and version):
            version = top_version
        if isinstance(version, str) and version:
            lines.append(f"- Policy version: {_text(version)}")
        lines.append("")
    else:
        lines.append("- Policy details were not recorded for this result.")
        lines.append("")

    # --- Inputs and provenance ---
    lines.append("### Inputs and data")
    lines.append("")
    context = _mapping(signal.get("context"))
    if context:
        ws = context.get("weight_source")
        if isinstance(ws, str) and ws:
            lines.append(f"- Current weight source: {_text(ws)}")
        else:
            lines.append("- Current weight source: not provided")
    elif status == STATUS_NEEDS_CONTEXT:
        lines.append("- Current weight source: not provided (this is why no target was computed).")
    else:
        lines.append("- Current weight source: not recorded for this result.")
    input_as_of = signal.get("input_as_of")
    if isinstance(input_as_of, str) and input_as_of:
        lines.append(f"- Analysis date: {_text(input_as_of)}")

    lines.append(_market_line(status, _mapping(signal.get("market")), signal))

    market = _mapping(signal.get("market"))
    if market:
        bits = []
        for key, human in (
            ("return_count", "returns"), ("window_ddof", "std-dev ddof"),
            ("return_type", "return type"), ("frequency", "frequency"),
            ("adjustment", "adjustment"),
        ):
            val = market.get(key)
            if _is_real_number(val):
                bits.append(f"{human} {int(val)}" if isinstance(val, int) else f"{human} {_text(val)}")
            elif isinstance(val, str) and val:
                bits.append(f"{human} {_text(val)}")
        if bits:
            lines.append("- Market data basis: " + "; ".join(bits) + ".")

    # Old results may lack a nested market block but still carry top-level provenance.
    if isinstance(signal.get("data_source"), str) and signal["data_source"]:
        lines.append(f"- Data source: {_text(signal['data_source'])}")
    if isinstance(signal.get("last_price_date"), str) and signal["last_price_date"]:
        lines.append(f"- Last price date: {_text(signal['last_price_date'])}")
    sample = signal.get("sample_size")
    if _is_real_number(sample) and float(sample) >= 0.0:
        lines.append(f"- Sample size: {int(sample)} closes")

    lines.append("")
    digest = signal.get("input_digest")
    if isinstance(digest, str) and digest:
        lines.append(f"Input digest: `{_text(digest)}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fraction_beyond_one(value: Any) -> float | None:
    """A non-negative finite number (raw target may exceed 1), or None."""
    if not _is_real_number(value):
        return None
    number = float(value)
    return number if number >= 0.0 else None


def render_position_markdown(signal: Any) -> str | None:
    """The shared Markdown guidance section *body*, or None to omit R3 entirely.

    Returns the body only (no VI heading — the writer and terminal own that) and
    ``None`` when there is no position signal (``None``) or it is not an object, so
    old states/checkpoints and disabled runs are unchanged. Never mutates input.
    """
    if not isinstance(signal, dict):
        return None
    return _markdown(signal)


__all__ = [
    "render_position_markdown",
    "SECTION_HEADING",
    "SECTION_FOLDER",
    "SECTION_FILENAME",
    "SECTION_AGENT",
]
