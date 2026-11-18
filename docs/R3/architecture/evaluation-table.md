# R3 candidate evaluation

This is a qualitative decision table; only the selected implementation was executed. No timing/accuracy measurements are invented for rejected candidates.

| Candidate | Safety authority | Input validity | Scope/cost | Decision/evidence |
|---|---|---|---|---|
| LLM-generated weights | requires deterministic checking anyway | may guess holdings | extra/variable output | rejected as sole authority |
| Infer account weight from existing portfolio | unreliable without complete valuation | missing total marked value | expands account semantics | rejected for v1 |
| Fixed cap only | deterministic | explicit holding possible | simplest | useful switch, insufficient as default adopted requirement |
| Full portfolio optimizer/execution | can enforce constraints | needs full live book | substantial new scope | deferred |
| Explicit context + pure mapping + historical cap | code decides final target | validated ticker/date/history | zero additional LLM | selected;1774 offline tests and real P6 evidence |

Known limits: conservative completed-day rule, adjustable experimental parameters, no execution guarantees, no numerical confidence, unknown deployment pricing and uncompleted browser visual inspection. These are not silently converted into success claims.
