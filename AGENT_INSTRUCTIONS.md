# Local AI Agent Collaboration Instructions — Version 0.3

These are **human-to-agent prompt commands**, not required shell subcommands. When the human types `SYNC`, `PUB`, `INT C-17`, `REV C-17`, `TEST`, `STATUS`, `SYNTHESIZE <topic>`, or `FINAL`, follow the procedure below. Use Git and `groupctl` for repeatable mechanics; use reasoning for semantic compatibility and conflict decisions. Ask the human when the intended behavior is materially uncertain.

The low-level `groupctl` CLI includes `init`, `validate`, `events`, `status`, `event-create`, `remote-events`, `agent-context`, `agent-submit`, `final-candidate`, and `finalize-confirmed`. The last two are finalization helpers, **not replacements for the human prompt command `FINAL`**.

## Global rules

1. Start in the repository root. Read `.group/group.json`; its `canonical_branch` is authoritative. Do not assume the canonical branch is `main` if configuration says otherwise.
2. Use `.group/members/<person_id>.json` for member identity. Personal branches follow `people/<person_id>/<topic>`.
3. Use `python groupctl.py validate` when relevant. Invalid records are not valid protocol evidence.
4. A published event's summary is context, not proof. Verify referenced commits and inspect real diffs and tests.
5. Event files under `.group/events/<event_id>.json` are immutable after publication. Correct a mistake with a new related event, not by editing the old event.
6. Never force-push, discard uncommitted work, reset canonical state, overwrite tests merely to make them pass, or resolve a conflict by arbitrarily selecting one side.
7. Keep private chat and experimental reasoning private. Publish only useful engineering information.
8. `.group/local/` is for local-only state and must not be committed except `.group/local/.gitkeep`.
9. Do not report a Git operation or test as successful unless its output/history confirms it.
10. `SYNC` does not merge every feature branch; `PUB` does not merge into canonical state; a positive `REV` does not merge; successful Git merge does not prove semantic compatibility.

## v0.3 external-agent bridge

A provider-specific AI coding tool can use the JSON bridge without changing the collaboration event protocol. Before asking an external agent to work, export a validated context bundle:

```powershell
python groupctl.py agent-context --person-id <PERSON_ID> --output .group/local/agent_context.json
```

After `git fetch`, add `--remote origin` to include validated events from fetched remote-tracking branches. Pass the JSON context file and relevant source code to the coding agent. Ask it to return one JSON submission containing `event_type`, `payload`, and optional `related_events`; use `examples/agent_finding_submission.json` as a minimal example. Validate and record the response with:

```powershell
python groupctl.py agent-submit --person-id <PERSON_ID> --input-file .group/local/agent_submission.json
python groupctl.py validate
python groupctl.py status
```

The bridge refuses agent-originated finalization, self-review, unknown review/answer targets, unknown synthesis sources, and Git evidence that cannot be verified. It records a local event only. Inspect the event before deciding whether to commit and push it. The member ID is attribution, not authentication. The bridge does not call an LLM or automatically execute, commit, push, or merge generated code. See `specifications/08_ai_agent_bridge.md`.

## `SYNC` — synchronize working context with the group

1. Record current branch (`git branch --show-current`), commit (`git rev-parse HEAD`), and `git status --short`. Inspect staged, unstaged, and untracked files; preserve all unfinished work.
2. Fetch refs and tags without changing the checkout: `git fetch --all --tags --prune`. If fetch fails, report the failure and do not claim synchronization succeeded.
3. Discover events on fetched remote-tracking branches, not just the current checkout: `python groupctl.py remote-events --remote origin --new-only`. Replace `origin` if the repository uses a different remote. Read each new event and investigate its referenced code/commit.
4. Compare the current branch with `origin/<canonical_branch>` as configured in `.group/group.json`. Check whether the canonical remote ref exists and inspect ahead/behind information.
5. Incorporate canonical updates into the current work branch only when safe. Protect dirty work first. Prefer a non-destructive merge if no repository policy says otherwise. Do not blindly run `git pull`, discard changes, or merge unrelated feature branches automatically.
6. Inspect relevant `change_published`, `review`, `integration_result`, `test_result`, `finding`, `question`, `answer`, `blocker`, and `synthesis_candidate` events. Check important claims against real diffs and test output.
7. If code changed, run relevant quick validation. Summarize current branch/commit, canonical commit, new events, what was incorporated, what remains separate, blockers, and any assumptions that need revision.
8. Only after all events are inspected and summarized, checkpoint them with `python groupctl.py remote-events --remote origin --mark-seen`. A checkpoint means “inspected,” not “accepted” or “integrated.” Do not checkpoint if validation or immutability errors occurred.

A clean `git pull` by itself is not a complete SYNC.

## `PUB` — publish a coherent contribution

1. Inspect branch, staged/unstaged diff, untracked files, and recent commits. If on the configured canonical branch, stop before publishing source changes and move to a personal branch with human agreement.
2. Confirm branch ownership follows `people/<person_id>/<topic>`.
3. Select a coherent contribution. Stage only relevant files. Check for secrets, credentials, generated files, conflict markers, unrelated edits, and accidental content. Run appropriate tests/build checks.
4. Review `git diff --cached`, run `git diff --cached --check`, and commit source changes with a meaningful message. Do not force-push.
5. Push the personal work branch. If rejected, fetch and reconcile safely or ask the human.
6. Use the code commit's full SHA and a unique `change_id` not already used by a `change_published` event. The default convention is `C-<short-commit-sha>`.
7. Create a payload with the documented fields: `change_id`, `branch`, `commit`, `summary`, `files_changed` (string array), `interface_changes` (string array), `tests_run` (string array), `known_issues` (string array), and `proposed_for_integration` (boolean). `groupctl` supplies the common envelope.
8. Save the payload under `.group/local/`, then create the event, for example:

   ```powershell
   python groupctl.py event-create --event-type change_published --payload-file .group/local/change_published_payload.json --person-id P17
   python groupctl.py validate
   ```

9. Stage only the new `.group/events/<event_id>.json`, commit the event separately, and push the personal branch again. The event's `commit` identifies the source-code commit, not the later event-only commit.
10. Report the change ID, code commit, branch, event ID, tests, limitations, and whether both code and event were pushed. Never claim PUB succeeded unless those operations succeeded.

## `REV <change_id>` — review without merging

1. Fetch as needed and locate the exact `change_published` event across fetched remote branches.
2. Verify its referenced branch and commit; inspect the actual diff and surrounding code, interface changes, assumptions, risks, tests, and compatibility with canonical work.
3. Run relevant tests when practical. Separate definite defects, plausible risks, missing evidence, and subjective preferences.
4. Select exactly one verdict: `accept`, `accept_with_changes`, `reject`, or `needs_more_evidence`.
5. Create a `review` event with `change_id`, `verdict`, `summary`, `issues` (string array), and `tests_run` (string array). Validate, then commit/push the event. A positive review does not merge the change. A self-review is not independent approval.

## `INT <change_id>` — integrate a specific published change

1. Perform the discovery parts of SYNC and locate the matching `change_published` event. Confirm its branch and commit exist.
2. Inspect the actual diff, interface changes, assumptions, surrounding code, and target branch. Default target is the current working branch unless the human or repository policy specifies otherwise; do not silently target canonical state.
3. Preserve uncommitted work. Integrate according to repository policy. If conflicts occur, identify files, inspect both sides and their intent, and resolve only if the intended combined behavior is clear. Otherwise preserve the conflict state, stop, and publish a `blocker` requesting a human decision.
4. Verify the commit is incorporated, check for leftover conflict markers, build if applicable, run relevant tests, and perform semantic compatibility review. A conflict-free merge alone is insufficient.
5. Publish an `integration_result` containing `change_id`, `target_branch`, `result` (`success`, `failed`, or `needs_human`), `merge_commit` for successful integration, `conflicts` (string array), `conflict_resolution_summary` when relevant, `verification` (mapping check names to string results), and `summary`.
6. Commit/push the event and explain the outcome. Integration into a personal branch does not mean the change is canonical.

## `TEST [scope]` — produce test evidence

1. Resolve scope: current branch by default, configured canonical branch when requested, or a named published change when a change ID is supplied. Record the exact commit before running tests.
2. Discover available tests/build/lint/static-analysis/UI checks from project configuration and instructions. Run applicable checks; do not rewrite expected results just to make tests pass.
3. Investigate failures enough to distinguish product failure, broken/stale test, missing environment, or inconclusive result. Give concrete instructions for human-required checks.
4. Publish a `test_result` event with `scope`, `commit`, `commands` (string array), `automated_result` (`pass`, `fail`, `partial`, `not_available`), non-negative integer counts `passed`, `failed`, and `skipped`, `human_checks_required` (string array), and `summary`.
5. Report what is proven, what failed, and what remains unknown. A result applies only to its exact commit.

## `STATUS` — report current group position

1. Inspect branch, commit, working tree, configured canonical branch/commit, and ahead/behind relationship. Fetch metadata first when useful and safe.
2. Validate local events and inspect relevant remote events. After fetching, run `python groupctl.py status --remote origin` (replace `origin` if needed) for the structured event summary. This command does not fetch or mutate state.
3. Report dirty/uncommitted work, published but unintegrated changes, latest reviews/integrations, synthesis candidates, blockers, and last tested commit when known.
4. Recommend one concise next action. STATUS itself should not perform a large merge or rewrite code.

## `SYNTHESIZE <topic>` — compare ideas without losing their origins

1. Synchronize first. Identify the relevant `change_published` events and inspect their actual diffs, interfaces, assumptions, reviews, tests, and known issues.
2. Compare what each contribution solves. Separate reusable ideas from incompatible implementation details; do not choose a proposal based only on author, agent confidence, or lines of code.
3. Prepare one `synthesis_candidate` payload containing `synthesis_id`, `title`, `problem_statement`, `source_change_ids`, `proposed_approach`, `alternatives_considered`, `tradeoffs`, `validation_plan`, `decision` (`draft`, `accepted`, `needs_revision`, or `rejected`), and `unresolved_concerns`.
4. Publish it with `python groupctl.py event-create --event-type synthesis_candidate --payload-file .group/local/synthesis_candidate_payload.json --person-id <PERSON_ID>`, validate, then commit/push the event.
5. Explain that the synthesis record is a proposal. It does not integrate code or replace the required independent review, tests, or human/group decision. If updated, publish a new immutable event with the same `synthesis_id` and relate it to the earlier synthesis event.

## `FINAL` — finalize one group result

1. Synchronize first. Enumerate published `change_published` events and check which referenced commits are incorporated in configured canonical state. Investigate every unincorporated change: intentionally rejected/obsolete, still important, or unknown.
2. Review latest review, test, integration, and blocker events; run final automated checks on the proposed canonical code commit; identify concrete human checks and honest known limitations. If important work/failures remain unresolved, do not claim successful completion.
3. Check out the configured canonical branch, ensure its working tree is clean, and identify the exact full commit SHA to propose. The `final-candidate` helper deliberately refuses to run on a personal branch or a stale branch tip.
4. Create `.group/local/final_candidate_payload.json` with this shape (replace example values with inspected facts):

   ```json
   {
     "canonical_branch": "main",
     "canonical_commit": "FULL_40_CHARACTER_COMMIT_SHA",
     "summary": "What the group produced.",
     "completed_work": ["Implemented and integrated feature X."],
     "contributors": ["P17 (Student A)", "P22 (Student B)"],
     "test_evidence": ["python -m unittest discover -s tests -v: pass on the stated commit"],
     "known_limitations": [],
     "unresolved_concerns": []
   }
   ```

5. Run `python groupctl.py final-candidate --payload-file .group/local/final_candidate_payload.json --person-id P17`. Inspect `.group/final/candidate.md` and the related `final_candidate` event. Commit and push only the candidate report and event to the canonical branch. If revising a candidate, use `--replace-candidate` deliberately; the new event remains immutable.
6. Show `candidate.md` to the initiating human and obtain explicit confirmation. Do not infer confirmation from silence or from a test pass.
7. After confirmation, create `.group/local/final_payload.json` with `canonical_branch`, the full `canonical_commit` named in the candidate, `summary`, `completed_work`, `contributors`, `test_evidence`, `known_limitations`, and `status`. Use `completed_with_known_limitations` when known limitations are non-empty.
8. Run `python groupctl.py finalize-confirmed --payload-file .group/local/final_payload.json --person-id P17 --human-confirmed --related-event <FINAL_CANDIDATE_EVENT_ID>`. The explicit flag records that human confirmation happened; it is not a substitute for actually showing the candidate first. The helper verifies the canonical checkout, candidate event, absence of unresolved concerns, and that commits after the proposed canonical code commit change only `.group/` metadata.
9. Validate, then commit/push `.group/final/final_report.md`, `.group/final/final.json`, and the `final_result` event. Report the exact canonical code commit and report path.

`FINAL` is group finalization, not submission of the initiator's personal branch. The final code commit is intentionally distinct from later commits that publish finalization records.
