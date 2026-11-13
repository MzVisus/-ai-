# `groupctl` — Group Collaboration Helper, Version 0.3

This project implements the first collaboration layer for several **human + local AI agent pairs working as one programming group through a shared Git repository**. Git manages commits, branches, and merges; `groupctl` validates and records standardized engineering events; `AGENT_INSTRUCTIONS.md` tells the local AI agent how to execute the collaboration prompt commands consistently.

This is not the Dispatcher, not an autonomous task-planning brain, and not a complete self-directed multi-agent system. Version 0.3 provides a provider-neutral JSON bridge, but does not include a built-in model/API connector or separate server.

## Included

- `groupctl.py`: deterministic local helper.
- `AGENT_INSTRUCTIONS.md`: portable agent procedures for `SYNC`, `PUB`, `INT`, `REV`, `TEST`, `STATUS`, `SYNTHESIZE`, and `FINAL`.
- `.group/` repository contract is created at initialization and follows the supplied specifications.
- JSON event validation for all eleven event types, including traceable synthesis candidates.
- Provider-neutral AI-agent bridge: `agent-context` exports a validated JSON context bundle and `agent-submit` validates structured responses before creating local events.
- Agent bridge safeguards: no agent finalization, no self-review, known-source checks for reviews/answers/synthesis, and Git-reference checks for publication/test/integration evidence.
- `scripts/mock_agent.py` exercises the bridge without paid APIs or an external model.
- `examples/agent_prompt_template.md` provides a copy/paste prompt for an external AI coding assistant.
- `UPGRADE_V0.2_TO_V0.3.md` explains how to apply the upgrade patch while preserving `.git/` and existing `.group/` history.
- Immutable event creation, validation, and local listing.
- Read-only event discovery on already-fetched remote branches, with raw-blob byte hashes for immutability checks and a local-only “inspected” checkpoint.
- `status` summary for published changes, latest reviews/integrations, synthesis candidates, blockers, and finalization state. It can include fetched remote events without fetching or mutating the repository.
- Final candidate generation and confirmed finalization helpers. They refuse finalization without explicit confirmation, require the configured canonical branch, check unresolved candidate concerns, and protect against source-code changes after the candidate commit.
- Unit tests including local-Git-remote integration tests.
- A three-member local demo that exercises independent branches, publication, remote event discovery, a clean merge, a deliberate merge conflict, post-merge tests, a synthesis-candidate event, group status, and finalization against a temporary bare Git remote.
- The provided specifications copied into `specifications/` for reference.

## Requirements

- Python 3.10 or newer is recommended.
- Git must be installed and available on `PATH` for remote-event inspection, finalization helpers, and integration tests.
- No third-party Python dependencies are required.

## Windows quick start

For a copy/paste setup and run-through, see [`QUICKSTART_WINDOWS.md`](QUICKSTART_WINDOWS.md).

Open PowerShell in the folder containing `groupctl.py`:

```powershell
python --version
python -m unittest discover -s tests -v
python groupctl.py validate
python groupctl.py events
```

This packaged repository is already initialized as group `G-12`, so do not run `init` in this existing copy. For a brand-new repository that does not yet contain `.group/group.json`, run the following command exactly once:

```powershell
python groupctl.py init --group-id G-12 --person-id P17 --display-name "Student A"
```

Initialization creates an assignment template. Replace that template with the group's actual assignment before collaborative work begins. In this package, `.group/assignment/assignment.md` is already populated from the project brief. In a multi-member repository, supply `--person-id` explicitly when creating events.

## Low-level CLI reference

The human-facing commands from the specifications are **prompt commands for the local AI agent**, not a requirement to implement matching shell subcommands. Their complete procedure lives in `AGENT_INSTRUCTIONS.md`.

| `groupctl` helper | Purpose |
|---|---|
| `init` | Create `.group/group.json`, the initial member descriptor, an assignment template, events/local/final folders, and local-state ignore rules. |
| `validate` | Validate group configuration, all member descriptors, local events, and any finalization records. |
| `events` | List local events; exits nonzero when invalid event files are found. |
| `status` | Summarize contribution/review/integration/synthesis/finalization state. `--remote origin` also inspects already-fetched remote-tracking events without fetching or changing the working tree. |
| `event-create` | Create one immutable-format local JSON event from event-specific payload fields. Common envelope fields are added by `groupctl`. |
| `remote-events` | Inspect validated event blobs on fetched remote-tracking branches without changing branches or the working tree. |
| `agent-context` | Export group assignment, member identity, repository state, instructions, and recent validated events as JSON for an external AI agent. |
| `agent-submit` | Validate an agent response and create an immutable local event; does not commit or push it. |
| `final-candidate` | Generate `.group/final/candidate.md` and a `final_candidate` event from a JSON payload, while requiring the checked-out configured canonical branch and exact tip commit. |
| `finalize-confirmed` | After a human has reviewed and confirmed the candidate, generate `final_report.md`, `final.json`, and a `final_result` event. Requires `--human-confirmed`. |

### Example: create a local finding event

```powershell
python groupctl.py event-create --event-type finding --payload-file examples/finding_payload.json --person-id P17
python groupctl.py validate
python groupctl.py events
```

An event written locally is **not published** until it is committed and pushed. Published event files are immutable: correct errors by creating a new event related to the earlier one.

### Example: discover remote events

As part of `SYNC`, record branch/commit/status first, then fetch:

```powershell
git fetch --all --tags --prune
python groupctl.py remote-events --remote origin --new-only
```

After the agent has inspected and summarized all events successfully, it may checkpoint them locally:

```powershell
python groupctl.py remote-events --remote origin --mark-seen
```

The checkpoint is stored under `.group/local/` and is not committed. “Seen” means inspected, not accepted or integrated. Do not mark events seen if validation or immutability errors are reported.

## AI agent bridge (v0.3)

Export context for an external coding agent:

```powershell
python groupctl.py agent-context --person-id P17 --output .group/local/agent_context.json
```

After fetching, include remote event summaries with `--remote origin`. Have the agent return JSON with `event_type`, `payload`, and optional `related_events`. Validate and record it locally:

```powershell
python groupctl.py agent-submit --person-id P17 --input-file .group/local/agent_submission.json
python groupctl.py validate
python groupctl.py status
```

Try the deterministic mock without an AI API:

```powershell
python scripts/mock_agent.py --person-id P17
python scripts/mock_agent.py --person-id P17 --submit
```

The first command only writes local drafts. The second records a sample finding event. Review it before committing/pushing. The bridge is a provider-neutral interface, not a built-in LLM or authenticated multi-user service. See [`specifications/08_ai_agent_bridge.md`](specifications/08_ai_agent_bridge.md).

## Synthesis workflow

When several contributors solve related problems, use a synthesis candidate to preserve the origin of each idea and explain how they might fit together. A synthesis candidate is a structured proposal, not a merge. Start with `examples/synthesis_candidate_payload.json`, replace its sample change IDs with real published change IDs, then create the event:

```powershell
python groupctl.py event-create --event-type synthesis_candidate --payload-file examples/synthesis_candidate_payload.json --person-id P17
python groupctl.py validate
python groupctl.py status
```

The candidate records its source changes, problem, proposed approach, alternatives, tradeoffs, validation plan, decision, and unresolved concerns. Later revisions use a new immutable event with the same `synthesis_id`. Code still requires normal review, testing, and integration.

## Group status

```powershell
python groupctl.py status
git fetch --all --tags --prune
python groupctl.py status --remote origin
```

The status command never fetches, merges, marks events as seen, or changes files. `--remote` reads only the remote-tracking refs already present locally.

## Finalization helpers

`FINAL` remains an AI-agent procedure. The helpers enforce mechanical checks, but the agent must first synchronize, find unintegrated published changes, inspect review/test/blocker evidence, and prepare a truthful candidate.

`final-candidate` expects a JSON payload with this shape (replace the sample commit with the current full 40-character canonical commit):

```json
{
  "canonical_branch": "main",
  "canonical_commit": "FULL_40_CHARACTER_COMMIT_SHA",
  "summary": "What the group produced.",
  "completed_work": ["Implemented and integrated feature X."],
  "contributors": ["P17 (Student A)", "P22 (Student B)"],
  "test_evidence": ["python -m unittest discover -s tests -v: pass on the specified commit"],
  "known_limitations": [],
  "unresolved_concerns": []
}
```

Save this in `.group/local/final_candidate_payload.json` and run:

```powershell
python groupctl.py final-candidate --payload-file .group/local/final_candidate_payload.json --person-id P17
```

Show `.group/final/candidate.md` to the human. Commit and push the candidate report and its `final_candidate` event. **Only after explicit human confirmation**, prepare `.group/local/final_payload.json` with `canonical_branch`, the candidate's full `canonical_commit`, `summary`, `completed_work`, `contributors`, `test_evidence`, `known_limitations`, and `status` (`completed` or `completed_with_known_limitations`). Then run:

```powershell
python groupctl.py finalize-confirmed --payload-file .group/local/final_payload.json --person-id P17 --human-confirmed --related-event <FINAL_CANDIDATE_EVENT_ID>
python groupctl.py validate
```

Commit and push `.group/final/final_report.md`, `.group/final/final.json`, and the created `final_result` event. The final code commit may be an ancestor of the branch tip when commits after it contain only `.group/` metadata records; the helper refuses to finalize if source/test files changed after the proposed code commit.

## Three-member demonstration

The demo creates a disposable local bare remote and three separate clones. It does not use your actual GitHub repository or modify this project folder.

Interactive run (the script asks you to type `FINALIZE` after showing the candidate):

```powershell
python scripts/run_three_member_demo.py
```

Fully scripted demonstration with an explicit simulated human confirmation:

```powershell
python scripts/run_three_member_demo.py --auto-confirm-final
```

The script demonstrates a clean integration, then deliberately introduces a conflict in `src/shared.py`, resolves it by combining both stated intents, runs tests, records a synthesis proposal, shows the group status, integrates the accepted branch into the simulated `main`, and exercises finalization. The temporary demo repository is removed automatically when the script exits.

## Verification performed for this package

The package's test suite covers configuration/member validation, all documented event payloads including synthesis candidates, event creation and immutability, group status aggregation, remote event discovery, invalid and conflicting remote events, final candidate creation, explicit confirmation, final report/JSON generation, and refusal when a candidate has unresolved concerns. The local three-member demo is also run end-to-end as a separate integration demonstration.

A local simulation is useful evidence but does **not** prove live GitHub authentication/permissions, simultaneous use by real students, or the quality of an independent AI agent's semantic judgement. Those still require the group to perform the assignment's live demonstration.
