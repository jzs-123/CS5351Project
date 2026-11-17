# R3 invariant register

| Invariant | Requirement | Enforcer | Violation/fallback | Evidence |
|---|---|---|---|---|
| No executable target beyond effective cap | R3-I01 | process_position | clamp and cap_override explanation | pure core cap tests |
| Underweight never adds exposure | R3-I01 | pure mapping then min cap | zero floor / cap | Underweight grid tests |
| More volatility never increases target | R3-I01 | scaling formula | lower/equal cap | monotonic volatility grid |
| Unknown is not empty holding | R3-I02 | explicit context and pure core | needs_context, null target | unknown-vs-zero tests |
| No fabricated price/confidence | R3-I02/F02 | data validation / signal contract | needs_data or null confidence | bad history/status tests |
| No look-ahead or fabricated gap fill | R3-F02 | input adapter | reject invalid series/date, no normal target | future/duplicate/stale tests |
| One run uses one validated context/policy | R3-I03 | PreparedRun / signature | mismatch rejection, later refresh | in-place mutation/samepath tests |
| Original rating/API and budget exception survive | R3-Q01/C01 | shared hook / finally | preserve original exception and costs | API/CLI and R5 tests |
| Reports cannot resurrect stale advice | R3-Q02 | shared formatter/writer | hide invalid target; remove only owned file | saved report transition tests |

Concrete test node IDs and live artifacts are in trace-matrix.md; tests enforce the contract, not the English rendering of this table.
