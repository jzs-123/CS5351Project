# ADR R3-001: deterministic postprocessing with explicit holdings

Status: adopted policy and test matrix, user decision on 2026-11-17; formal ADR consolidated retrospectively on that date. Source: 2026-11-11 feasible plan and preserved approval record.

## Decision

Preserve the old rating/API. Require explicit current weight for non-Sell sizing, reuse completed historical closes, and add a deterministic PositionSignal after the existing AI graph. Apply validation, rating intent, then risk cap. Use21 closes/20 simple returns/sample standard deviation(ddof1), daily not annualized. The cap is C*min(1,T/vol), with scale1 at vol0; result is min(raw target,effective cap). Hold over the cap is explicitly overridden. Zero targets/Sell require no prices or stop policy.

## Alternatives

Allowing the LLM to invent weights offers flexible prose but cannot itself enforce the safety boundary and adds output variability. Inferring weights from quantity/cost/cash lacks reliable whole-account valuation. A full portfolio engine adds scope and account-state obligations. A fixed cap without volatility is simpler but misses the adopted volatility requirement. These are qualitative design comparisons, not invented benchmark experiments.

## Consequences and reconsideration

Confidence remains null rather than a manufactured probability. Explicit input can become stale, so ticker/date are checked and source/digest are recorded. Historical-only close validation is conservative and does not implement an exchange closing calendar. Snapshot hashes prove integrity consistency, not source authenticity. Parameters are experimental and configurable. Revisit when real valuation, validated confidence or intraday execution is explicitly required.

## AI context and constraints

R3 adds zero LLM calls. The existing R1 layered debate context policy is unchanged. Operational P6 selected one analyst, two tool rounds, one debate/risk round, a4096-token configured cap after a DeepSeek wire-parameter fix, and bounded request/token callbacks. No new LLM compression strategy is claimed here. Full model context/compression design remains the R1/team responsibility.
