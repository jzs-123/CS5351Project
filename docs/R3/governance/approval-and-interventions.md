# Approval and intervention provenance

## Actual human decisions

The user requested staged work and stop-time handoffs, explicitly adopted the policy and T01-T18 test matrix("采用草案和测试计划，继续实施"), and explicitly authorized sending public project source, adopted rules and necessary development instructions to the existing school model service. These are actual user decisions; their preserved record is approved-policy-and-tests.md. Subsequent requests authorized continuing the planned stages.

## Actors must not be conflated

OpenCode1.18.34, session ses_e1a88030affeTF5v4YbjJalaxd, generated and corrected product source/tests. Codex is a separate supervising AI that prepared prompts/scripts, inspected results, identified defects and ran verification. Titles in some historical prompts say "human review"; those corrective prompts were drafted/sent by Codex and are NOT evidence that the student personally performed line-by-line human review. The student/peer must still review the concrete result before formal approval.

## Observed corrective interventions

| Stage | Codex-observed issue and direction | Real generation evidence |
|---|---|---|
| P2 | zero target retained stop policy; huge integer/non-string safety | P2fix raw session/prompt |
| P3 | completed-day/UTC checks, snapshot metadata/write verification | P3fix/P3qa |
| P4 | checkpoint ordering, reload timing, shallow freezing, bool equality, legacy doubles | P4fix/P4qa/P4final/P4guard |
| P5 | invalid percentage display, zero stop, unavailable-data wording, status escaping, brittle new tests | P5fix/P5qa/P5tests |
| P6 | actual output-limit parameter renamed by dependency | P6fix and64-token service result |

Raw session JSON is retained unmodified in the local course bundle, together with the actual prompts/events and SHA256 manifest. Public technical docs contain its identity/hash and this index, not credentials or a claim that a summary replaces the raw session. No retrospective timestamps, fabricated approval or historical commits are introduced.
