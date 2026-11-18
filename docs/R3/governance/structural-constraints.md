# R3 structural and verification constraints

- Keep rating interpretation in the existing rating module; process_position accepts it as input and never calls a model.
- Keep mapping/invariants, input validation/history, completion orchestration and presentation in their respective modules. Do not import a vendor SDK into pure position.py.
- Shared completion runs before JSON logging in API/CLI; display consumes recorded state rather than refetching.
- Explicit invalid config fails early; runtime history failures preserve analysis; budget/model exceptions retain finally costs.
- Default-off and old report/checkpoint shapes remain compatible. Report reconciliation owns only named generated files.
- The current GitHub CI already runs the default pytest suite and strict ruff, so published R3 tests join those existing checks. Local results do not prove a remote CI run occurred.
- R3 requirements/schema are validated locally with Draft2020-12 jsonschema. A negative mutation missing priority is checked as a failure. No claim is made that this schema gate has already been added to team CI or branch protection.
- This component does not replace team-owned global requirements, Report.md, protected-branch settings, semantic evaluation gates or other members' transcripts. Track remaining integration explicitly.
