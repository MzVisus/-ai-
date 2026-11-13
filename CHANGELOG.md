# Changelog


## Version 0.3

- Added `agent-context`, a read-only JSON context export for external AI coding agents, including assignment, identity, Git state, validated local/recent remote events, portable instructions, and safety rules.
- Added `agent-submit`, a provider-neutral structured-response boundary that validates an agent payload before creating an immutable local collaboration event.
- Added additional bridge checks for self-review, known question/review targets, known synthesis sources, and Git-backed commit/branch evidence for published changes, test results, and successful integrations.
- Kept final-candidate/final-result events outside the agent bridge; finalization still requires the existing explicit human-confirmation workflow.
- Added `scripts/mock_agent.py`, a no-API bridge smoke test, JSON examples, and the v0.3 agent bridge specification.
- Added automated tests for context export, submission validation, event relationships, and bridge safety.

## Version 0.2

- Added a `status` CLI command that summarizes published changes, latest reviews and integrations, test evidence, synthesis candidates, blockers, and finalization state.
- Added optional `status --remote <name>` support for validated events on already-fetched remote-tracking branches. The command does not fetch, merge, or mark events seen.
- Added Git ancestry checks so a source commit can be recognized as present on the configured canonical branch even when the integration event originally targeted a contributor branch.
- Added `synthesis_candidate` events to record source change IDs, the problem, proposed approach, alternatives, tradeoffs, validation plan, decision, and unresolved concerns.
- Added the `SYNTHESIZE` AI-agent procedure and a sample synthesis payload.
- Updated the three-member demo to publish a draft synthesis candidate and display group status.
- Made local/remote event comparison robust to Windows `core.autocrlf` checkout normalization by preferring the committed Git blob.
- Expanded unit tests to 31 passing tests and retained the end-to-end three-clone demonstration.

## Version 0.1

- Initial group configuration and member descriptors.
- Structured immutable collaboration events.
- Local event creation, validation, and listing.
- Read-only remote event discovery and local inspected-event checkpoints.
- Final candidate generation and explicit-confirmation finalization.
- Initial three-member local Git collaboration demo.
