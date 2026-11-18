# R3 runtime policy catalog

| Trigger | Action | Retained evidence |
|---|---|---|
| R3 off/no signal | omit guidance; remove owned old guidance file | original rating/report |
| Bad explicit JSON/number/policy/date match | reject before costly model execution | clear ValueError, no normal position |
| REVIEW/unrecognized rating | needs_review | original AI decision, null target |
| Non-Sell without current weight | needs_context | unknown remains unknown |
| Positive target lacks usable history | needs_data | original AI analysis and safe failure reason |
| Sell/zero target/zero cap | target0 without history | no stop-loss policy |
| Scaling off | apply fixed cap without volatility fetch | policy and reason |
| Volatility/risk cap binds | reduce target and explain override | raw/effective/final fields |
| BudgetExceeded/model error | propagate/abort; keep finally costs | no successful R3 completion log |
| Snapshot replay | validate/recompute and label replay | provenance/content digest |
| Malformed/old result in presentation | show unavailable or omit, never old executable values | legacy reports continue |

P6 fault injection is explicitly labeled controlled testing, not a claimed real market outage. School deployment price remains unknown; a numeric0 estimate is not proof of free service.
