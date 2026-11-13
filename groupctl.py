#!/usr/bin/env python3
"""Version 0.3 group collaboration helper.

Implemented milestones:
- repository initialization;
- group and event validation;
- local creation and listing of immutable-format collaboration event files;
- read-only discovery of events on already-fetched remote branches with a local-only checkpoint;
- group status aggregation and synthesis-candidate records;
- final candidate and confirmed finalization bookkeeping.

The human-to-agent collaboration prompt commands are defined in AGENT_INSTRUCTIONS.md. Git
handles branch/commit/merge mechanics, this CLI handles deterministic validation and
records, and the local AI agent supplies semantic judgment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_VERSION = "0.3.0"
PROTOCOL_VERSION = "0.1"
GROUP_DIR = Path(".group")
CONFIG_PATH = GROUP_DIR / "group.json"
DEFAULT_ASSIGNMENT_PATH = GROUP_DIR / "assignment" / "assignment.md"
EVENTS_DIR = GROUP_DIR / "events"
MEMBERS_DIR = GROUP_DIR / "members"
LOCAL_DIR = GROUP_DIR / "local"

SUPPORTED_EVENT_TYPES = {
    "change_published",
    "finding",
    "question",
    "answer",
    "review",
    "test_result",
    "integration_result",
    "blocker",
    "synthesis_candidate",
    "final_candidate",
    "final_result",
}

# AI agents may submit collaboration evidence and proposals, but may not use the
# bridge to finalize a group or bypass the human-confirmation workflow.
AGENT_SUBMITTABLE_EVENT_TYPES = {
    "change_published",
    "finding",
    "question",
    "answer",
    "review",
    "test_result",
    "integration_result",
    "blocker",
    "synthesis_candidate",
}

COMMON_EVENT_FIELDS = {
    "protocol_version",
    "event_id",
    "event_type",
    "group_id",
    "person_id",
    "created_at",
    "related_events",
}

REVIEW_VERDICTS = {"accept", "accept_with_changes", "reject", "needs_more_evidence"}
TEST_RESULTS = {"pass", "fail", "partial", "not_available"}
INTEGRATION_RESULTS = {"success", "failed", "needs_human"}
FINAL_STATUSES = {"completed", "completed_with_known_limitations"}
SYNTHESIS_DECISIONS = {"draft", "accepted", "needs_revision", "rejected"}


def read_json(path: Path) -> Any:
    """Read JSON and return the decoded value."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path}: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    except OSError as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write readable UTF-8 JSON, refusing to overwrite an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive mode is deliberate: published-format events are immutable and
    # unique names should not overwrite an earlier event.
    try:
        with path.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except FileExistsError as exc:
        raise ValueError(f"Refusing to overwrite existing file: {path}") from exc


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_required_string_fields(data: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for field in fields:
        if field not in data:
            errors.append(f"{data.get('event_type')} event is missing required field: {field}")
        elif not _is_nonempty_string(data[field]):
            errors.append(f"{field} must be a non-empty string.")
    return errors


def _validate_string_list_field(data: dict[str, Any], field: str, *, required: bool = True) -> list[str]:
    errors: list[str] = []
    if field not in data:
        if required:
            errors.append(f"{data.get('event_type')} event is missing required field: {field}")
        return errors
    value = data[field]
    if not isinstance(value, list):
        errors.append(f"{field} must be a JSON array of strings.")
    elif any(not isinstance(item, str) for item in value):
        errors.append(f"Every {field} item must be a string.")
    return errors


def validate_event_payload(data: dict[str, Any]) -> list[str]:
    """Validate the documented payload shape for event types with explicit examples.

    The final_candidate/final_result event concepts are documented in the contract,
    but their event payload key names are not fully enumerated there. Their complete
    validation will be added alongside the FINAL workflow rather than inventing a
    possibly incompatible schema here.
    """
    errors: list[str] = []
    event_type = data.get("event_type")

    if event_type == "change_published":
        errors.extend(_validate_required_string_fields(
            data, ("change_id", "branch", "commit", "summary")
        ))
        for field in ("files_changed", "interface_changes", "tests_run", "known_issues"):
            errors.extend(_validate_string_list_field(data, field))
        if "proposed_for_integration" not in data:
            errors.append("change_published event is missing required field: proposed_for_integration")
        elif not isinstance(data["proposed_for_integration"], bool):
            errors.append("proposed_for_integration must be a JSON boolean.")

    elif event_type == "finding":
        errors.extend(_validate_required_string_fields(data, ("summary",)))
        errors.extend(_validate_string_list_field(data, "evidence"))

    elif event_type == "question":
        errors.extend(_validate_required_string_fields(data, ("question_id", "text")))
        if "blocking" not in data:
            errors.append("question event is missing required field: blocking")
        elif not isinstance(data["blocking"], bool):
            errors.append("blocking must be a JSON boolean.")

    elif event_type == "answer":
        errors.extend(_validate_required_string_fields(data, ("question_id", "text")))

    elif event_type == "review":
        errors.extend(_validate_required_string_fields(data, ("change_id", "verdict", "summary")))
        if "verdict" in data and (not isinstance(data["verdict"], str) or data["verdict"] not in REVIEW_VERDICTS):
            errors.append("verdict must be one of: " + ", ".join(sorted(REVIEW_VERDICTS)) + ".")
        errors.extend(_validate_string_list_field(data, "issues"))
        errors.extend(_validate_string_list_field(data, "tests_run"))

    elif event_type == "test_result":
        errors.extend(_validate_required_string_fields(data, ("scope", "commit", "automated_result", "summary")))
        errors.extend(_validate_string_list_field(data, "commands"))
        errors.extend(_validate_string_list_field(data, "human_checks_required"))
        if "automated_result" in data and (not isinstance(data["automated_result"], str) or data["automated_result"] not in TEST_RESULTS):
            errors.append("automated_result must be one of: " + ", ".join(sorted(TEST_RESULTS)) + ".")
        for field in ("passed", "failed", "skipped"):
            if field not in data:
                errors.append(f"test_result event is missing required field: {field}")
            elif isinstance(data[field], bool) or not isinstance(data[field], int) or data[field] < 0:
                errors.append(f"{field} must be a non-negative integer.")

    elif event_type == "integration_result":
        errors.extend(_validate_required_string_fields(data, ("change_id", "target_branch", "result", "summary")))
        if "result" in data and (not isinstance(data["result"], str) or data["result"] not in INTEGRATION_RESULTS):
            errors.append("result must be one of: " + ", ".join(sorted(INTEGRATION_RESULTS)) + ".")
        errors.extend(_validate_string_list_field(data, "conflicts"))
        if "verification" not in data:
            errors.append("integration_result event is missing required field: verification")
        elif not isinstance(data["verification"], dict):
            errors.append("verification must be a JSON object.")
        else:
            for name, value in data["verification"].items():
                if not isinstance(name, str) or not isinstance(value, str):
                    errors.append("verification must map check names to string results.")
                    break
        if data.get("result") == "success":
            if not _is_nonempty_string(data.get("merge_commit")):
                errors.append("A successful integration_result must include a non-empty merge_commit.")
        if "conflict_resolution_summary" in data and not isinstance(data["conflict_resolution_summary"], str):
            errors.append("conflict_resolution_summary must be a string when provided.")

    elif event_type == "blocker":
        errors.extend(_validate_required_string_fields(data, ("summary", "requested_action")))
        if "blocking_change_id" in data and not _is_nonempty_string(data["blocking_change_id"]):
            errors.append("blocking_change_id must be a non-empty string when provided.")

    elif event_type == "synthesis_candidate":
        errors.extend(_validate_required_string_fields(
            data, ("synthesis_id", "title", "problem_statement", "proposed_approach", "decision")
        ))
        for field in (
            "source_change_ids", "alternatives_considered", "tradeoffs",
            "validation_plan", "unresolved_concerns",
        ):
            errors.extend(_validate_string_list_field(data, field))
        source_ids = data.get("source_change_ids")
        if isinstance(source_ids, list):
            if not source_ids:
                errors.append("source_change_ids must contain at least one source change ID.")
            if any(not _is_nonempty_string(item) for item in source_ids):
                errors.append("Every source_change_ids item must be a non-empty string.")
            if len(source_ids) != len(set(item for item in source_ids if isinstance(item, str))):
                errors.append("source_change_ids must not contain duplicates.")
        if "decision" in data and (
            not isinstance(data["decision"], str) or data["decision"] not in SYNTHESIS_DECISIONS
        ):
            errors.append("decision must be one of: " + ", ".join(sorted(SYNTHESIS_DECISIONS)) + ".")

    elif event_type == "final_candidate":
        # The contract describes the meaning of these fields; the helper uses the
        # same names in both the candidate event and its local candidate report.
        errors.extend(_validate_required_string_fields(
            data, ("canonical_branch", "canonical_commit", "report_file", "summary")
        ))
        for field in ("test_evidence", "known_limitations", "unresolved_concerns"):
            errors.extend(_validate_string_list_field(data, field))
        for field in ("completed_work", "contributors"):
            errors.extend(_validate_string_list_field(data, field, required=False))
        if _is_nonempty_string(data.get("report_file")) and (Path(data["report_file"]).is_absolute() or ".." in Path(data["report_file"]).parts):
            errors.append("report_file must be a repository-relative path without '..'.")

    elif event_type == "final_result":
        errors.extend(_validate_required_string_fields(
            data, ("canonical_branch", "canonical_commit", "report_file", "initiated_by", "status", "summary")
        ))
        for field in ("test_evidence", "known_limitations"):
            errors.extend(_validate_string_list_field(data, field))
        if "status" in data and (not isinstance(data["status"], str) or data["status"] not in FINAL_STATUSES):
            errors.append("status must be one of: " + ", ".join(sorted(FINAL_STATUSES)) + ".")
        if _is_nonempty_string(data.get("report_file")) and (Path(data["report_file"]).is_absolute() or ".." in Path(data["report_file"]).parts):
            errors.append("report_file must be a repository-relative path without '..'.")

    return errors


def validate_group_config(data: Any, root: Path) -> list[str]:
    """Return validation errors for .group/group.json and its assignment path."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["group.json must contain a JSON object."]

    required_fields = ("protocol_version", "group_id", "canonical_branch", "assignment_file")
    for field in required_fields:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if data.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION!r}.")

    for field in ("group_id", "canonical_branch", "assignment_file"):
        value = data.get(field)
        if field in data and not _is_nonempty_string(value):
            errors.append(f"{field} must be a non-empty string.")

    assignment_value = data.get("assignment_file")
    if isinstance(assignment_value, str) and assignment_value.strip():
        assignment_path = Path(assignment_value)
        if assignment_path.is_absolute() or ".." in assignment_path.parts:
            errors.append("assignment_file must be a repository-relative path without '..'.")
        elif not (root / assignment_path).is_file():
            errors.append(f"Assignment file does not exist: {assignment_value}")

    return errors


def validate_event(data: Any, expected_path: Path | None = None) -> list[str]:
    """Validate an event's common envelope and its documented payload fields."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Event must contain a JSON object."]

    for field in sorted(COMMON_EVENT_FIELDS):
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if data.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION!r}.")

    for field in ("event_id", "group_id", "person_id"):
        value = data.get(field)
        if field in data and not _is_nonempty_string(value):
            errors.append(f"{field} must be a non-empty string.")

    event_type = data.get("event_type")
    if not isinstance(event_type, str) or event_type not in SUPPORTED_EVENT_TYPES:
        errors.append("event_type must be one of: " + ", ".join(sorted(SUPPORTED_EVENT_TYPES)) + ".")
    else:
        errors.extend(validate_event_payload(data))

    related_events = data.get("related_events")
    if "related_events" in data:
        if not isinstance(related_events, list):
            errors.append("related_events must be a JSON array of event ID strings.")
        elif any(not isinstance(item, str) or not item.strip() for item in related_events):
            errors.append("Every related_events item must be a non-empty string.")

    created_at = data.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        parse_value = created_at[:-1] + "+00:00" if created_at.endswith("Z") else created_at
        try:
            parsed = datetime.fromisoformat(parse_value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                errors.append("created_at must include a timezone, such as Z or +08:00.")
        except ValueError:
            errors.append("created_at must be an ISO 8601 timestamp with a timezone.")
    elif "created_at" in data:
        errors.append("created_at must be a non-empty ISO 8601 timestamp string.")

    if expected_path is not None:
        event_id = data.get("event_id")
        if isinstance(event_id, str) and event_id and expected_path.name != f"{event_id}.json":
            errors.append(
                f"Event filename must match event_id: expected {event_id}.json, got {expected_path.name}."
            )

    return errors


def initialize_repository(root: Path, group_id: str, person_id: str, display_name: str,
                          canonical_branch: str, assignment_file: str) -> None:
    """Create the required collaboration directory scaffold without overwriting files."""
    root = root.resolve()
    normalized_assignment = assignment_file.replace("\\", "/")
    assignment_relative = Path(normalized_assignment)
    if assignment_relative.is_absolute() or ".." in assignment_relative.parts:
        raise ValueError("assignment_file must be a repository-relative path without '..'.")
    if not group_id.strip() or not canonical_branch.strip() or not person_id.strip() or not display_name.strip():
        raise ValueError("group_id, canonical_branch, person_id, and display_name must not be empty.")
    if "/" in person_id or "\\" in person_id or person_id in {".", ".."}:
        raise ValueError("person_id must be a single path-safe identifier, such as P17.")

    config_path = root / CONFIG_PATH
    member_path = root / MEMBERS_DIR / f"{person_id}.json"
    assignment_path = root / assignment_relative
    if config_path.exists():
        raise ValueError(f"{config_path} already exists; refusing to overwrite group configuration.")
    if member_path.exists():
        raise ValueError(f"{member_path} already exists; refusing to overwrite member descriptor.")

    for directory in (root / EVENTS_DIR, root / MEMBERS_DIR, root / LOCAL_DIR, assignment_path.parent):
        directory.mkdir(parents=True, exist_ok=True)

    config = {
        "protocol_version": PROTOCOL_VERSION,
        "group_id": group_id,
        "canonical_branch": canonical_branch,
        "assignment_file": normalized_assignment,
    }
    member = {
        "protocol_version": PROTOCOL_VERSION,
        "person_id": person_id,
        "display_name": display_name,
        "joined_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    write_json(config_path, config)
    write_json(member_path, member)
    if not assignment_path.exists():
        assignment_path.write_text(
            "# Group Assignment\n\nReplace this placeholder with the group's assignment description.\n",
            encoding="utf-8",
        )

    (root / LOCAL_DIR / ".gitkeep").touch(exist_ok=True)

    gitignore = root / ".gitignore"
    ignore_lines = [".group/local/*", "!.group/local/.gitkeep"]
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [line for line in ignore_lines if line not in existing]
    if missing:
        with gitignore.open("a", encoding="utf-8", newline="\n") as f:
            if existing and existing[-1] != "":
                f.write("\n")
            f.write("\n".join(missing) + "\n")


def validate_member_descriptors(root: Path) -> list[str]:
    """Validate committed member descriptors against the repository contract."""
    errors: list[str] = []
    members_dir = root / MEMBERS_DIR
    if not members_dir.is_dir():
        return [f"Members directory does not exist: {members_dir}"]
    paths = sorted(members_dir.glob("*.json"))
    if not paths:
        errors.append("At least one member descriptor is required under .group/members/.")
    for path in paths:
        try:
            member = read_json(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(member, dict):
            errors.append(f"{path.relative_to(root)} must contain a JSON object.")
            continue
        for field in ("protocol_version", "person_id", "display_name", "joined_at"):
            if field not in member:
                errors.append(f"{path.relative_to(root)} is missing required field: {field}")
        if member.get("protocol_version") != PROTOCOL_VERSION:
            errors.append(f"{path.relative_to(root)} protocol_version must be {PROTOCOL_VERSION!r}.")
        for field in ("person_id", "display_name", "joined_at"):
            if field in member and not _is_nonempty_string(member[field]):
                errors.append(f"{path.relative_to(root)} field {field} must be a non-empty string.")
        person_id = member.get("person_id")
        if _is_nonempty_string(person_id):
            if "/" in person_id or "\\" in person_id or person_id in {".", ".."}:
                errors.append(f"{path.relative_to(root)} person_id must be a single path-safe identifier.")
            if path.name != f"{person_id}.json":
                errors.append(f"Member filename must match person_id: {path.name} (expected {person_id}.json).")
        joined_at = member.get("joined_at")
        if _is_nonempty_string(joined_at):
            timestamp = joined_at[:-1] + "+00:00" if joined_at.endswith("Z") else joined_at
            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    errors.append(f"{path.relative_to(root)} joined_at must include a timezone.")
            except ValueError:
                errors.append(f"{path.relative_to(root)} joined_at must be an ISO 8601 timestamp with timezone.")
    return errors


def validate_final_records(root: Path, config: dict[str, Any]) -> list[str]:
    """Validate finalization files if finalization has begun or completed."""
    errors: list[str] = []
    final_dir = root / GROUP_DIR / "final"
    report_path = final_dir / "final_report.md"
    envelope_path = final_dir / "final.json"
    if report_path.exists() != envelope_path.exists():
        errors.append(".group/final/final_report.md and .group/final/final.json must either both exist or both be absent.")
    if envelope_path.is_file():
        try:
            data = read_json(envelope_path)
        except ValueError as exc:
            return errors + [str(exc)]
        if not isinstance(data, dict):
            return errors + [".group/final/final.json must contain a JSON object."]
        required = ("protocol_version", "group_id", "canonical_branch", "canonical_commit", "report_file", "initiated_by", "created_at", "status", "known_limitations")
        for field in required:
            if field not in data:
                errors.append(f".group/final/final.json is missing required field: {field}")
        if data.get("protocol_version") != PROTOCOL_VERSION:
            errors.append(f".group/final/final.json protocol_version must be {PROTOCOL_VERSION!r}.")
        if data.get("group_id") != config.get("group_id"):
            errors.append(".group/final/final.json group_id does not match .group/group.json.")
        if data.get("canonical_branch") != config.get("canonical_branch"):
            errors.append(".group/final/final.json canonical_branch does not match .group/group.json.")
        if not _is_nonempty_string(data.get("canonical_commit")):
            errors.append(".group/final/final.json canonical_commit must be a non-empty string.")
        if data.get("report_file") != ".group/final/final_report.md":
            errors.append(".group/final/final.json report_file must be '.group/final/final_report.md'.")
        if not isinstance(data.get("status"), str) or data.get("status") not in FINAL_STATUSES:
            errors.append(".group/final/final.json status must be one of: " + ", ".join(sorted(FINAL_STATUSES)) + ".")
        if not isinstance(data.get("known_limitations"), list) or any(not isinstance(x, str) for x in data.get("known_limitations", [])):
            errors.append(".group/final/final.json known_limitations must be an array of strings.")
        if data.get("status") == "completed" and data.get("known_limitations"):
            errors.append("Use completed_with_known_limitations when known_limitations is non-empty.")
        if not report_path.is_file():
            errors.append(".group/final/final_report.md is missing.")
        else:
            report_text = report_path.read_text(encoding="utf-8")
            if _is_nonempty_string(data.get("canonical_commit")) and data["canonical_commit"] not in report_text:
                errors.append("final_report.md does not mention the canonical_commit recorded in final.json.")
        events_dir = root / EVENTS_DIR
        matching_final_events: list[dict[str, Any]] = []
        if events_dir.is_dir():
            for event_path in events_dir.glob("*.json"):
                try:
                    event = read_json(event_path)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("event_type") != "final_result":
                    continue
                if event.get("canonical_commit") != data.get("canonical_commit"):
                    continue
                if event.get("report_file") != data.get("report_file"):
                    continue
                if event.get("status") != data.get("status"):
                    continue
                if not validate_event(event, event_path):
                    matching_final_events.append(event)
        if not matching_final_events:
            errors.append("No valid final_result event matches final.json canonical_commit, report_file, and status.")
    return errors


def _load_valid_group(root: Path) -> dict[str, Any]:
    config_path = root / CONFIG_PATH
    if not config_path.is_file():
        raise ValueError(f"Missing required configuration: {config_path}")
    config = read_json(config_path)
    config_errors = validate_group_config(config, root)
    if config_errors:
        raise ValueError("Invalid group configuration: " + "; ".join(config_errors))
    return config


def _resolve_person_id(root: Path, requested_person_id: str | None) -> str:
    members_dir = root / MEMBERS_DIR
    if not members_dir.is_dir():
        raise ValueError(f"Members directory does not exist: {members_dir}")

    if requested_person_id:
        if "/" in requested_person_id or "\\" in requested_person_id or requested_person_id in {".", ".."}:
            raise ValueError("person_id must be a single path-safe identifier, such as P17.")
        member_path = members_dir / f"{requested_person_id}.json"
        if not member_path.is_file():
            raise ValueError(f"No member descriptor found for person_id {requested_person_id!r}.")
        member = read_json(member_path)
        if not isinstance(member, dict) or member.get("person_id") != requested_person_id:
            raise ValueError(f"Member descriptor does not match person_id {requested_person_id!r}.")
        return requested_person_id

    member_paths = sorted(members_dir.glob("*.json"))
    if len(member_paths) != 1:
        raise ValueError(
            "Cannot infer the current person because the repository has "
            f"{len(member_paths)} member descriptor(s). Supply --person-id explicitly."
        )
    member = read_json(member_paths[0])
    if not isinstance(member, dict) or not _is_nonempty_string(member.get("person_id")):
        raise ValueError(f"Invalid member descriptor: {member_paths[0]}")
    if member_paths[0].name != f"{member['person_id']}.json":
        raise ValueError(f"Member filename must match person_id: {member_paths[0].name}")
    return member["person_id"]


def create_event(root: Path, event_type: str, payload: Any, person_id: str | None,
                 related_events: list[str]) -> Path:
    """Create a local event file from a payload, adding the common envelope."""
    root = root.resolve()
    config = _load_valid_group(root)
    if event_type not in SUPPORTED_EVENT_TYPES:
        raise ValueError("Unsupported event type: " + event_type)
    if not isinstance(payload, dict):
        raise ValueError("Payload file must contain a JSON object.")

    reserved = sorted(COMMON_EVENT_FIELDS.intersection(payload.keys()))
    if reserved:
        raise ValueError(
            "Payload must not supply common envelope fields; groupctl creates these: "
            + ", ".join(reserved)
        )

    chosen_person_id = _resolve_person_id(root, person_id)
    if not isinstance(related_events, list) or any(not _is_nonempty_string(item) for item in related_events):
        raise ValueError("Each --related-event value must be a non-empty event ID.")

    now = datetime.now(timezone.utc)
    timestamp_id = now.strftime("%Y%m%dT%H%M%SZ")
    events_dir = root / EVENTS_DIR
    events_dir.mkdir(parents=True, exist_ok=True)

    # A short UUID suffix keeps simultaneous events from getting the same name.
    while True:
        event_id = f"E-{timestamp_id}-{chosen_person_id}-{uuid.uuid4().hex[:4]}"
        event_path = events_dir / f"{event_id}.json"
        if not event_path.exists():
            break

    event: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "group_id": config["group_id"],
        "person_id": chosen_person_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "related_events": related_events,
        **payload,
    }
    errors = validate_event(event, event_path)
    if errors:
        raise ValueError("Event payload is invalid: " + "; ".join(errors))

    write_json(event_path, event)
    return event_path



def _agent_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, predictable event representation for an external agent."""
    fields = (
        "event_id", "event_type", "group_id", "person_id", "created_at",
        "change_id", "summary", "branch", "commit", "verdict", "result",
        "decision", "synthesis_id", "title", "source_change_ids", "scope",
    )
    return {key: event[key] for key in fields if key in event}


def _load_agent_events(root: Path, remote: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load validated local events and optionally validated fetched remote events."""
    config = _load_valid_group(root)
    events_by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    events_dir = root / EVENTS_DIR
    for path in sorted(events_dir.glob("*.json")):
        try:
            event = read_json(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        event_errors = validate_event(event, path)
        if isinstance(event, dict) and event.get("group_id") != config["group_id"]:
            event_errors.append(f"{path.name}: group_id does not match this repository.")
        if event_errors:
            errors.extend(f"{path.name}: {item}" for item in event_errors)
            continue
        events_by_id[event["event_id"]] = event

    if remote:
        remote_events, remote_errors = collect_remote_events(root, remote)
        errors.extend(remote_errors)
        for event_id, item in remote_events.items():
            remote_event = item["data"]
            if event_id in events_by_id and events_by_id[event_id] != remote_event:
                errors.append(f"Event {event_id} differs between local and remote copies.")
            else:
                events_by_id[event_id] = remote_event

    events = sorted(events_by_id.values(), key=_event_time_key)
    return events, errors


def _git_commit_exists(root: Path, commit: str) -> bool:
    if not _is_nonempty_string(commit):
        return False
    try:
        result = _run_git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    except OSError:
        return False
    return result.returncode == 0


def _resolve_branch_ref(root: Path, branch: str) -> str | None:
    """Resolve a local or fetched remote-tracking ref for a branch name."""
    if not _is_nonempty_string(branch):
        return None
    check = _run_git(root, "check-ref-format", "--branch", branch)
    if check.returncode != 0:
        return None
    result = _run_git(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    if result.returncode != 0:
        return None
    refs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    preferred = f"refs/heads/{branch}"
    if preferred in refs:
        return preferred
    suffix = f"/{branch}"
    matches = [ref for ref in refs if ref.startswith("refs/remotes/") and ref.endswith(suffix)]
    return sorted(matches)[0] if matches else None


def build_agent_context(
    root: Path,
    person_id: str,
    *,
    remote: str | None = None,
    limit: int = 30,
    include_instructions: bool = True,
) -> dict[str, Any]:
    """Build a read-only, provider-neutral context package for a local AI agent."""
    root = root.resolve()
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500.")
    config = _load_valid_group(root)
    person_id = _resolve_person_id(root, person_id)
    member = read_json(root / MEMBERS_DIR / f"{person_id}.json")
    assignment_relative = Path(config["assignment_file"])
    assignment_path = root / assignment_relative
    assignment_text = assignment_path.read_text(encoding="utf-8")

    member_errors = validate_member_descriptors(root)
    if member_errors:
        raise ValueError("Invalid member descriptors: " + "; ".join(member_errors))
    final_errors = validate_final_records(root, config)
    if final_errors:
        raise ValueError("Invalid finalization records: " + "; ".join(final_errors))
    events, event_errors = _load_agent_events(root, remote)
    if event_errors:
        raise ValueError("Cannot provide agent context from invalid collaboration events: " + "; ".join(event_errors))

    git_info: dict[str, Any] = {
        "available": False,
        "branch": None,
        "head": None,
        "working_tree_clean": None,
        "changed_paths": [],
        "canonical_branch": config["canonical_branch"],
        "canonical_head": None,
        "is_canonical_branch": False,
        "head_is_canonical_tip": False,
    }
    try:
        top = _run_git(root, "rev-parse", "--show-toplevel")
        if top.returncode == 0:
            git_info["available"] = True
            branch_result = _run_git(root, "branch", "--show-current")
            head_result = _run_git(root, "rev-parse", "HEAD")
            status_result = _run_git(root, "status", "--porcelain", "--untracked-files=all")
            canonical_result = _run_git(root, "rev-parse", f"refs/heads/{config['canonical_branch']}")
            git_info["branch"] = branch_result.stdout.strip() if branch_result.returncode == 0 else None
            git_info["head"] = head_result.stdout.strip() if head_result.returncode == 0 else None
            git_info["changed_paths"] = [
                line[3:].strip() for line in status_result.stdout.splitlines() if len(line) >= 4
            ] if status_result.returncode == 0 else []
            git_info["working_tree_clean"] = status_result.returncode == 0 and not status_result.stdout.strip()
            git_info["canonical_head"] = canonical_result.stdout.strip() if canonical_result.returncode == 0 else None
            git_info["is_canonical_branch"] = git_info["branch"] == config["canonical_branch"]
            git_info["head_is_canonical_tip"] = bool(
                git_info["is_canonical_branch"] and git_info["head"]
                and git_info["head"] == git_info["canonical_head"]
            )
    except OSError:
        # Context generation remains useful for a source archive without Git.
        pass

    instructions_text = None
    instructions_path = root / "AGENT_INSTRUCTIONS.md"
    if include_instructions and instructions_path.is_file():
        instructions_text = instructions_path.read_text(encoding="utf-8")

    compact_events = [_agent_event_summary(event) for event in events[-limit:]]
    return {
        "schema_version": 1,
        "tool_version": APP_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group": {
            "group_id": config["group_id"],
            "canonical_branch": config["canonical_branch"],
            "assignment_file": config["assignment_file"],
        },
        "member": member,
        "assignment": {"path": config["assignment_file"], "text": assignment_text},
        "repository": git_info,
        "collaboration": {
            "event_count": len(events),
            "events_included": len(compact_events),
            "events": compact_events,
            "available_submission_types": sorted(AGENT_SUBMITTABLE_EVENT_TYPES),
        },
        "agent_instructions": instructions_text,
        "safety_rules": [
            "Do not commit, push, merge, or delete branches unless the human explicitly authorizes the exact action.",
            "A published change is not automatically reviewed or integrated.",
            "Do not review your own change; independent review must come from another member.",
            "Tie test evidence to the exact commit that was tested.",
            "A synthesis candidate is a proposal, not an implementation or merge.",
            "Finalization requires a human to inspect the candidate and explicitly confirm it.",
        ],
    }


def submit_agent_event(
    root: Path,
    person_id: str,
    submission: Any,
    *,
    remote: str | None = None,
) -> Path:
    """Validate an agent response and record it as one immutable collaboration event."""
    root = root.resolve()
    config = _load_valid_group(root)
    person_id = _resolve_person_id(root, person_id)
    if not isinstance(submission, dict):
        raise ValueError("Agent submission must be a JSON object.")
    unknown = sorted(set(submission) - {"event_type", "payload", "related_events"})
    if unknown:
        raise ValueError("Agent submission contains unsupported top-level fields: " + ", ".join(unknown))
    event_type = submission.get("event_type")
    if not isinstance(event_type, str) or event_type not in AGENT_SUBMITTABLE_EVENT_TYPES:
        raise ValueError(
            "Agent bridge does not permit this event type. Allowed types: "
            + ", ".join(sorted(AGENT_SUBMITTABLE_EVENT_TYPES))
        )
    payload = submission.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Agent submission 'payload' must be a JSON object.")
    related_events = submission.get("related_events", [])
    if not isinstance(related_events, list) or any(not _is_nonempty_string(item) for item in related_events):
        raise ValueError("related_events must be an array of non-empty event IDs.")

    events, event_errors = _load_agent_events(root, remote)
    if event_errors:
        raise ValueError("Cannot accept an agent submission while collaboration events are invalid: " + "; ".join(event_errors))
    by_id = {event["event_id"]: event for event in events}

    if event_type == "answer":
        question_id = payload.get("question_id")
        source = next((event for event in events if event.get("event_type") == "question" and event.get("question_id") == question_id), None)
        if source is None:
            raise ValueError(f"Cannot answer unknown question_id {question_id!r}.")
        if source["event_id"] not in related_events:
            related_events = [*related_events, source["event_id"]]

    elif event_type == "blocker":
        blocking_change_id = payload.get("blocking_change_id")
        if blocking_change_id:
            source = next((event for event in events if event.get("event_type") == "change_published" and event.get("change_id") == blocking_change_id), None)
            if source is None:
                raise ValueError(f"Blocker references unknown change_id {blocking_change_id!r}.")
            if source["event_id"] not in related_events:
                related_events = [*related_events, source["event_id"]]

    elif event_type == "review":
        change_id = payload.get("change_id")
        source = next((event for event in events if event.get("event_type") == "change_published" and event.get("change_id") == change_id), None)
        if source is None:
            raise ValueError(f"Cannot review unknown change_id {change_id!r}.")
        if source.get("person_id") == person_id:
            raise ValueError("Self-review is not allowed by the agent bridge; use a different member for independent review.")
        if source["event_id"] not in related_events:
            related_events = [*related_events, source["event_id"]]

    elif event_type == "change_published":
        branch = payload.get("branch")
        commit = payload.get("commit")
        if not _git_commit_exists(root, commit):
            raise ValueError("change_published commit must resolve to a Git commit available in this repository.")
        if _resolve_branch_ref(root, branch) is None:
            raise ValueError(f"change_published branch {branch!r} must exist as a local or fetched remote branch.")

    elif event_type == "test_result":
        if not _git_commit_exists(root, payload.get("commit")):
            raise ValueError("test_result commit must resolve to a Git commit available in this repository.")

    elif event_type == "integration_result":
        change_id = payload.get("change_id")
        source = next((event for event in events if event.get("event_type") == "change_published" and event.get("change_id") == change_id), None)
        if source is None:
            raise ValueError(f"Cannot record integration for unknown change_id {change_id!r}.")
        if source["event_id"] not in related_events:
            related_events = [*related_events, source["event_id"]]
        if payload.get("result") == "success":
            merge_commit = payload.get("merge_commit")
            branch_ref = _resolve_branch_ref(root, payload.get("target_branch"))
            if not _git_commit_exists(root, merge_commit):
                raise ValueError("Successful integration requires a merge_commit available in this repository.")
            if branch_ref is None:
                raise ValueError("Successful integration target_branch must exist as a local or fetched remote branch.")
            ancestry = _run_git(root, "merge-base", "--is-ancestor", merge_commit, branch_ref)
            if ancestry.returncode != 0:
                raise ValueError("Successful integration cannot be recorded: merge_commit is not an ancestor of target_branch.")

    elif event_type == "synthesis_candidate":
        source_ids = payload.get("source_change_ids")
        if isinstance(source_ids, list):
            for change_id in source_ids:
                source = next((event for event in events if event.get("event_type") == "change_published" and event.get("change_id") == change_id), None)
                if source is None:
                    raise ValueError(f"Synthesis source change_id {change_id!r} is unknown in the supplied group context.")
                if source["event_id"] not in related_events:
                    related_events = [*related_events, source["event_id"]]

    # Ensure every caller-supplied related event is part of the validated group context.
    unknown_related = [event_id for event_id in related_events if event_id not in by_id]
    if unknown_related:
        raise ValueError("related_events contains unknown event IDs: " + ", ".join(unknown_related))

    return create_event(root, event_type, payload, person_id, related_events)


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Git command without changing the user's working tree."""
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _run_git_blob(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run a Git command returning bytes, preserving exact blob contents."""
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _remote_checkpoint_path(root: Path, remote: str) -> Path:
    """Return a safe local-only checkpoint path for one named Git remote."""
    if not remote or remote.startswith("-") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in remote):
        raise ValueError("Remote name may contain only letters, numbers, underscore, dot, and hyphen.")
    return root / LOCAL_DIR / f"known_remote_events_{remote}.json"


def collect_remote_events(root: Path, remote: str = "origin") -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read and validate event files from already-fetched remote-tracking branches.

    This deliberately does not fetch or check out branches. SYNC records the local
    state first, fetches remote refs, and then uses this read-only view to discover
    events without replacing the current working tree.
    """
    root = root.resolve()
    config = _load_valid_group(root)
    errors: list[str] = []

    remote_check = _run_git(root, "remote", "get-url", remote)
    if remote_check.returncode != 0:
        message = remote_check.stderr.strip() or f"Git remote {remote!r} is not configured."
        return {}, [message]

    refs_result = _run_git(root, "for-each-ref", "--format=%(refname)", f"refs/remotes/{remote}/")
    if refs_result.returncode != 0:
        return {}, [refs_result.stderr.strip() or "Could not list remote-tracking branches."]
    refs = [ref.strip() for ref in refs_result.stdout.splitlines()
            if ref.strip() and not ref.strip().endswith("/HEAD")]
    if not refs:
        return {}, [f"No fetched remote-tracking branches found for {remote!r}; run git fetch first."]

    # event_id -> validated event plus immutable content digest and all refs carrying it
    events: dict[str, dict[str, Any]] = {}
    for ref in refs:
        tree = _run_git(root, "ls-tree", "-r", "--name-only", ref, "--", str(EVENTS_DIR).replace("\\", "/"))
        if tree.returncode != 0:
            errors.append(f"Could not inspect {ref}: {tree.stderr.strip()}")
            continue
        event_paths = [name.strip() for name in tree.stdout.splitlines()
                       if name.strip().endswith(".json")
                       and Path(name.strip()).parent.as_posix() == EVENTS_DIR.as_posix()]
        for event_path in event_paths:
            shown = _run_git_blob(root, "cat-file", "blob", f"{ref}:{event_path}")
            if shown.returncode != 0:
                message = shown.stderr.decode("utf-8", errors="replace").strip()
                errors.append(f"Could not read {ref}:{event_path}: {message}")
                continue
            raw_bytes = shown.stdout
            try:
                raw_text = raw_bytes.decode("utf-8")
                data = json.loads(raw_text)
            except UnicodeDecodeError as exc:
                errors.append(
                    f"INVALID {ref}:{event_path}: event is not valid UTF-8 (byte offset {exc.start})"
                )
                continue
            except json.JSONDecodeError as exc:
                errors.append(f"INVALID {ref}:{event_path}: invalid JSON ({exc.msg}, line {exc.lineno})")
                continue
            validation_errors = validate_event(data, Path(event_path))
            if isinstance(data, dict) and data.get("group_id") != config.get("group_id"):
                validation_errors.append(
                    f"group_id {data.get('group_id')!r} does not match local group {config.get('group_id')!r}."
                )
            if validation_errors:
                errors.append(f"INVALID {ref}:{event_path}: " + "; ".join(validation_errors))
                continue

            event_id = data["event_id"]
            # Hash the Git blob bytes exactly. Text mode can normalize CRLF/LF on
            # Windows, which would hide a change to an immutable published event.
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if event_id not in events:
                events[event_id] = {
                    "data": data,
                    "sha256": digest,
                    "refs": [ref],
                    "path": event_path,
                }
            else:
                previous = events[event_id]
                if previous["sha256"] != digest:
                    errors.append(
                        f"IMMUTABILITY CONFLICT: event {event_id} has different contents on "
                        f"{', '.join(previous['refs'])} and {ref}."
                    )
                else:
                    previous["refs"].append(ref)

    return events, errors


def command_remote_events(args: argparse.Namespace) -> int:
    """List events in fetched remote branches, optionally checkpointing inspected IDs."""
    root = Path(args.root).resolve()
    try:
        checkpoint_path = _remote_checkpoint_path(root, args.remote)
        events, errors = collect_remote_events(root, args.remote)
        known: dict[str, str] = {}
        if checkpoint_path.is_file():
            checkpoint = read_json(checkpoint_path)
            if not isinstance(checkpoint, dict) or checkpoint.get("protocol_version") != PROTOCOL_VERSION:
                raise ValueError(f"Invalid remote event checkpoint: {checkpoint_path}")
            raw_known = checkpoint.get("events", {})
            if not isinstance(raw_known, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in raw_known.items()
            ):
                raise ValueError(f"Invalid events mapping in remote event checkpoint: {checkpoint_path}")
            known = raw_known
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for error in errors:
        print(f"ERROR: {error}")

    if not events and not errors:
        print(f"No collaboration events found on fetched branches for remote {args.remote!r}.")

    new_or_changed: list[str] = []
    for event_id, item in sorted(events.items(), key=lambda pair: (pair[1]["data"].get("created_at", ""), pair[0])):
        digest = item["sha256"]
        if event_id not in known:
            state = "NEW"
            new_or_changed.append(event_id)
        elif known[event_id] != digest:
            state = "CHANGED"
            new_or_changed.append(event_id)
            errors.append(f"IMMUTABILITY CONFLICT: previously seen event {event_id} has changed content.")
        else:
            state = "KNOWN"
        if args.new_only and state == "KNOWN":
            continue
        event = item["data"]
        refs = ", ".join(ref.removeprefix(f"refs/remotes/{args.remote}/") for ref in item["refs"])
        print(f"{state:<7} {event['event_type']:<20} {event_id} (person {event['person_id']}; refs: {refs})")

    if args.new_only and events and not new_or_changed:
        print("No new remote events since the last local checkpoint.")

    # A checkpoint is local-only and is updated only when explicitly requested.
    # Refuse to checkpoint an invalid or conflicting view of the event history.
    if args.mark_seen:
        if errors:
            print("Checkpoint not updated because remote events contain validation or immutability errors.", file=sys.stderr)
            return 1
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        combined = dict(known)
        combined.update({event_id: item["sha256"] for event_id, item in events.items()})
        checkpoint = {
            "protocol_version": PROTOCOL_VERSION,
            "remote": args.remote,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "events": combined,
        }
        checkpoint_path.write_text(json.dumps(checkpoint, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Checkpoint updated: {checkpoint_path.relative_to(root)}")

    return 1 if errors else 0

def command_init(args: argparse.Namespace) -> int:
    root = Path(args.root)
    try:
        initialize_repository(
            root=root,
            group_id=args.group_id,
            person_id=args.person_id,
            display_name=args.display_name,
            canonical_branch=args.canonical_branch,
            assignment_file=args.assignment_file,
        )
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Initialized group collaboration metadata in: {root.resolve() / GROUP_DIR}")
    print(f"Group ID: {args.group_id}")
    print(f"Canonical branch: {args.canonical_branch}")
    print(f"Member: {args.person_id} ({args.display_name})")
    print("Next: run `python groupctl.py validate --root .` from the repository root.")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config_path = root / CONFIG_PATH
    if not config_path.is_file():
        print(f"ERROR: Missing required configuration: {config_path}", file=sys.stderr)
        return 1

    try:
        config = read_json(config_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors = validate_group_config(config, root)
    if errors:
        print("Group configuration: INVALID")
        for error in errors:
            print(f"  - {error}")
    else:
        print("Group configuration: OK")

    events_dir = root / EVENTS_DIR
    event_count = 0
    event_errors: list[str] = []
    if events_dir.exists():
        for event_path in sorted(events_dir.glob("*.json")):
            event_count += 1
            try:
                event_data = read_json(event_path)
                for error in validate_event(event_data, event_path):
                    event_errors.append(f"{event_path.relative_to(root)}: {error}")
                if isinstance(event_data, dict) and isinstance(config, dict) and event_data.get("group_id") != config.get("group_id"):
                    event_errors.append(
                        f"{event_path.relative_to(root)}: group_id {event_data.get('group_id')!r} "
                        f"does not match local group {config.get('group_id')!r}."
                    )
            except ValueError as exc:
                event_errors.append(str(exc))

    if event_errors:
        print(f"Events: INVALID ({event_count} file(s) checked)")
        for error in event_errors:
            print(f"  - {error}")
    else:
        print(f"Events: OK ({event_count} file(s) checked)")

    member_errors = validate_member_descriptors(root)
    if member_errors:
        print("Members: INVALID")
        for error in member_errors:
            print(f"  - {error}")
    else:
        print("Members: OK")

    final_errors = validate_final_records(root, config if isinstance(config, dict) else {})
    if final_errors:
        print("Final records: INVALID")
        for error in final_errors:
            print(f"  - {error}")
    elif (root / GROUP_DIR / "final" / "final.json").exists():
        print("Final records: OK")
    else:
        print("Final records: not finalized")

    return 1 if errors or event_errors or member_errors or final_errors else 0


def command_list_events(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    events_dir = root / EVENTS_DIR
    if not events_dir.is_dir():
        print(f"ERROR: Events directory does not exist: {events_dir}", file=sys.stderr)
        return 1

    try:
        config = _load_valid_group(root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    files = sorted(events_dir.glob("*.json"))
    if not files:
        print("No collaboration events found.")
        return 0

    invalid_count = 0
    for path in files:
        try:
            data = read_json(path)
        except ValueError as exc:
            print(f"INVALID  {path.name}: {exc}")
            invalid_count += 1
            continue
        errors = validate_event(data, path)
        if isinstance(data, dict) and data.get("group_id") != config.get("group_id"):
            errors.append(f"group_id {data.get('group_id')!r} does not match local group {config.get('group_id')!r}.")
        if errors:
            print(f"INVALID  {path.name}: {'; '.join(errors)}")
            invalid_count += 1
        else:
            print(f"OK       {data['event_type']:<20} {data['event_id']} (person {data['person_id']})")
    return 1 if invalid_count else 0


def _event_time_key(event: dict[str, Any]) -> float:
    """Return a sortable UTC timestamp for a validated event."""
    raw = event.get("created_at", "")
    parsed_value = raw[:-1] + "+00:00" if isinstance(raw, str) and raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(parsed_value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return 0.0
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def command_status(args: argparse.Namespace) -> int:
    """Summarize published changes, reviews, integrations, syntheses, and blockers."""
    root = Path(args.root).resolve()
    if command_validate(argparse.Namespace(root=str(root))) != 0:
        print("ERROR: Refusing to summarize an invalid collaboration state.", file=sys.stderr)
        return 1

    try:
        config = _load_valid_group(root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    events_dir = root / EVENTS_DIR
    events_by_id: dict[str, dict[str, Any]] = {}
    local_blob_hashes: dict[str, str | None] = {}
    for event_path in sorted(events_dir.glob("*.json")):
        try:
            raw = event_path.read_bytes()
            event = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"ERROR: Cannot read local event {event_path}: {exc}", file=sys.stderr)
            return 1
        event_id = event["event_id"]
        events_by_id[event_id] = event
        # Prefer the committed Git blob so core.autocrlf on Windows does not
        # make a checked-out CRLF file look different from the LF blob.
        relative_path = event_path.relative_to(root).as_posix()
        try:
            blob_result = _run_git_blob(root, "show", f"HEAD:{relative_path}")
        except OSError:
            blob_result = None
        local_blob_hashes[event_id] = (
            hashlib.sha256(blob_result.stdout).hexdigest()
            if blob_result is not None and blob_result.returncode == 0 else None
        )

    remote_label = "local events only"
    if args.remote:
        remote_events, remote_errors = collect_remote_events(root, args.remote)
        if remote_errors:
            for error in remote_errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        for event_id, item in remote_events.items():
            if event_id in local_blob_hashes:
                local_digest = local_blob_hashes[event_id]
                same_content = (
                    local_digest == item["sha256"] if local_digest is not None
                    else events_by_id[event_id] == item["data"]
                )
                if not same_content:
                    print(
                        f"ERROR: IMMUTABILITY CONFLICT: local and remote copies of {event_id} differ.",
                        file=sys.stderr,
                    )
                    return 1
            events_by_id.setdefault(event_id, item["data"])
        remote_label = f"local + fetched remote '{args.remote}' events"

    all_events = sorted(events_by_id.values(), key=lambda event: (_event_time_key(event), event["event_id"]))
    published_changes: dict[str, dict[str, Any]] = {}
    latest_reviews: dict[str, dict[str, Any]] = {}
    latest_integrations: dict[str, dict[str, Any]] = {}
    latest_tests: dict[str, dict[str, Any]] = {}
    latest_syntheses: dict[str, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []

    for event in all_events:
        event_type = event["event_type"]
        if event_type == "change_published":
            change_id = event["change_id"]
            if change_id in published_changes and published_changes[change_id] != event:
                print(f"ERROR: Duplicate change_id has conflicting publication events: {change_id}", file=sys.stderr)
                return 1
            published_changes[change_id] = event
        elif event_type == "review":
            latest_reviews[event["change_id"]] = event
        elif event_type == "integration_result":
            latest_integrations[event["change_id"]] = event
        elif event_type == "test_result":
            latest_tests[event["scope"]] = event
        elif event_type == "synthesis_candidate":
            latest_syntheses[event["synthesis_id"]] = event
        elif event_type == "blocker":
            blockers.append(event)

    members_count = len(list((root / MEMBERS_DIR).glob("*.json")))
    canonical = config["canonical_branch"]
    canonical_refs = (
        [f"refs/remotes/{args.remote}/{canonical}", f"refs/heads/{canonical}"]
        if args.remote else [f"refs/heads/{canonical}"]
    )
    canonical_ref: str | None = None
    for candidate_ref in canonical_refs:
        try:
            resolved = _run_git(root, "rev-parse", "--verify", f"{candidate_ref}^{{commit}}")
        except OSError:
            break
        if resolved.returncode == 0:
            canonical_ref = candidate_ref
            break

    def commit_is_on_canonical(commit: str) -> bool | None:
        if canonical_ref is None:
            return None
        try:
            result = _run_git(root, "merge-base", "--is-ancestor", commit, canonical_ref)
        except OSError:
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    print("GROUP STATUS")
    print(f"Group: {config['group_id']}")
    print(f"Canonical branch: {canonical}")
    print(f"Members: {members_count}")
    print(f"Events considered: {len(all_events)} ({remote_label})")
    if canonical_ref:
        print(f"Canonical Git ref inspected: {canonical_ref}")
    else:
        print("Canonical Git ref: unavailable; branch ancestry cannot be verified")

    print(f"\nCHANGES ({len(published_changes)})")
    if not published_changes:
        print("  No published changes recorded.")
    for change_id, change in sorted(published_changes.items()):
        review = latest_reviews.get(change_id)
        integration = latest_integrations.get(change_id)
        canonical_presence = commit_is_on_canonical(change["commit"])
        if not change["proposed_for_integration"]:
            state = "not proposed for integration"
        elif canonical_presence is True:
            state = "present on canonical branch (verified by Git ancestry)"
        elif integration and integration["result"] == "success":
            if integration["target_branch"] == canonical:
                state = "reported integrated on canonical branch; Git ancestry not verified"
            else:
                state = f"integrated on {integration['target_branch']} (not canonical)"
        elif integration and integration["result"] == "needs_human":
            state = "integration needs human decision"
        elif integration and integration["result"] == "failed":
            state = "integration failed"
        elif review and review["verdict"] == "reject":
            state = "rejected in latest review"
        elif review and review["verdict"] == "needs_more_evidence":
            state = "more review evidence requested"
        elif review and review["verdict"] == "accept_with_changes":
            state = "accepted with requested changes; integration pending"
        elif review and review["verdict"] == "accept":
            state = "accepted; integration pending"
        else:
            state = "awaiting review"
        print(f"  {change_id} [{state}] by {change['person_id']} — {change['summary']}")
        if review:
            print(f"    Latest review: {review['verdict']} by {review['person_id']} — {review['summary']}")
        if integration:
            print(f"    Latest integration: {integration['result']} to {integration['target_branch']} — {integration['summary']}")

    print(f"\nLATEST TEST EVIDENCE ({len(latest_tests)} scope(s))")
    if not latest_tests:
        print("  No test_result events recorded.")
    for scope, test_event in sorted(latest_tests.items()):
        print(
            f"  [{test_event['automated_result']}] {scope}: "
            f"{test_event['passed']} passed, {test_event['failed']} failed, "
            f"{test_event['skipped']} skipped; commit {test_event['commit']}"
        )
        print(f"    {test_event['summary']}")
        print("    Evidence applies to the recorded commit, not automatically to later code changes.")

    print(f"\nSYNTHESIS CANDIDATES ({len(latest_syntheses)})")
    if not latest_syntheses:
        print("  No synthesis candidates recorded.")
    known_change_ids = set(published_changes)
    for synthesis_id, synthesis in sorted(latest_syntheses.items()):
        sources = synthesis["source_change_ids"]
        missing = [source for source in sources if source not in known_change_ids]
        print(f"  {synthesis_id} [{synthesis['decision']}] — {synthesis['title']}")
        print(f"    Sources: {', '.join(sources)}")
        if missing:
            print(f"    WARNING: source changes not found in inspected events: {', '.join(missing)}")
        if synthesis["unresolved_concerns"]:
            print(f"    Unresolved concerns: {len(synthesis['unresolved_concerns'])}")
    print("  A synthesis decision records a proposal; it does not merge or integrate code.")

    print(f"\nRECORDED BLOCKERS ({len(blockers)})")
    if not blockers:
        print("  No blocker events recorded.")
    for blocker in blockers:
        target = blocker.get("blocking_change_id", "group-wide")
        print(f"  {blocker['event_id']} [{target}] by {blocker['person_id']} — {blocker['summary']}")
    if blockers:
        print("  Review blocker events manually; this protocol does not infer their resolution.")

    final_json = root / GROUP_DIR / "final" / "final.json"
    if final_json.is_file():
        try:
            final_data = read_json(final_json)
            print(f"\nFinalization: {final_data.get('status', 'recorded')} on {final_data.get('canonical_branch', canonical)} at {final_data.get('canonical_commit', 'unknown commit')}")
        except ValueError:
            print("\nFinalization: invalid final.json (see validation output above)")
    else:
        print("\nFinalization: not finalized")

    return 0


def command_event_create(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    payload_path = Path(args.payload_file)
    if not payload_path.is_absolute():
        payload_path = root / payload_path
    try:
        payload = read_json(payload_path)
        event_path = create_event(
            root=root,
            event_type=args.event_type,
            payload=payload,
            person_id=args.person_id,
            related_events=args.related_event,
        )
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Created local event: {event_path.relative_to(root)}")
    print(f"Event type: {args.event_type}")
    print("This event is local only until it is committed and pushed with Git.")
    return 0



def command_agent_context(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        context = build_agent_context(
            root,
            args.person_id,
            remote=args.remote,
            limit=args.limit,
            include_instructions=not args.no_instructions,
        )
        rendered = json.dumps(context, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            output_path = Path(args.output)
            if not output_path.is_absolute():
                output_path = root / output_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8", newline="\n")
            print(f"Agent context written: {output_path.relative_to(root) if output_path.is_relative_to(root) else output_path}")
        else:
            sys.stdout.write(rendered)
        return 0
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def command_agent_submit(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    input_path = Path(args.input_file)
    if not input_path.is_absolute():
        input_path = root / input_path
    try:
        submission = read_json(input_path)
        event_path = submit_agent_event(root, args.person_id, submission, remote=args.remote)
        event = read_json(event_path)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Created agent event: {event_path.relative_to(root)}")
    print(f"Event ID: {event['event_id']}")
    print(f"Event type: {event['event_type']}")
    print("The event is local only until a human-approved Git commit and push publish it.")
    return 0

def _git_output(root: Path, *arguments: str) -> str:
    result = _run_git(root, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(arguments)} failed."
        raise ValueError(detail)
    return result.stdout.strip()


def _require_canonical_checkout(root: Path, config: dict[str, Any]) -> tuple[str, str]:
    """Return current branch/HEAD, requiring the configured canonical checkout."""
    branch = _git_output(root, "branch", "--show-current")
    if branch != config["canonical_branch"]:
        raise ValueError(
            f"This operation must run from the configured canonical branch "
            f"{config['canonical_branch']!r}; current branch is {branch!r}."
        )
    head = _git_output(root, "rev-parse", "HEAD")
    branch_head = _git_output(root, "rev-parse", f"refs/heads/{config['canonical_branch']}")
    if head != branch_head:
        raise ValueError("HEAD is not the tip of the configured canonical branch.")
    return branch, head


def _read_payload_file(root: Path, payload_arg: str) -> dict[str, Any]:
    path = Path(payload_arg)
    if not path.is_absolute():
        path = root / path
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Payload file must contain a JSON object: {path}")
    return payload


def _require_string_list(payload: dict[str, Any], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON array of strings.")
    return value


def _markdown_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- None recorded"


def _candidate_markdown(group_id: str, payload: dict[str, Any]) -> str:
    completed_work = _require_string_list(payload, "completed_work")
    contributors = _require_string_list(payload, "contributors")
    test_evidence = _require_string_list(payload, "test_evidence")
    known_limitations = _require_string_list(payload, "known_limitations")
    unresolved_concerns = _require_string_list(payload, "unresolved_concerns")
    return (
        "# Group Final Candidate\n\n"
        f"## Assignment\nGroup `{group_id}`\n\n"
        f"## Result\n{payload['summary']}\n\n"
        f"## Canonical Commit\nBranch: `{payload['canonical_branch']}`\n"
        f"Commit: `{payload['canonical_commit']}`\n\n"
        f"## Important Changes\n{_markdown_bullets(completed_work)}\n\n"
        f"## Contributors\n{_markdown_bullets(contributors)}\n\n"
        f"## Validation\n{_markdown_bullets(test_evidence)}\n\n"
        f"## Known Limitations\n{_markdown_bullets(known_limitations)}\n\n"
        f"## Unresolved Concerns\n{_markdown_bullets(unresolved_concerns)}\n\n"
        "---\nReview this candidate. Final records must not be published until a human has confirmed it.\n"
    )


def command_candidate_create(args: argparse.Namespace) -> int:
    """Create/update a reviewable final candidate plus immutable final_candidate event."""
    root = Path(args.root).resolve()
    candidate_path = root / GROUP_DIR / "final" / "candidate.md"
    final_json = root / GROUP_DIR / "final" / "final.json"
    final_report = root / GROUP_DIR / "final" / "final_report.md"
    existed_before = candidate_path.exists()
    event_path: Path | None = None
    try:
        config = _load_valid_group(root)
        if command_validate(argparse.Namespace(root=str(root))) != 0:
            raise ValueError("Repository validation failed; refusing candidate creation.")
        payload = _read_payload_file(root, args.payload_file)
        branch, head = _require_canonical_checkout(root, config)
        canonical_branch = payload.get("canonical_branch", branch)
        canonical_commit = payload.get("canonical_commit", head)
        if canonical_branch != config["canonical_branch"]:
            raise ValueError("canonical_branch in candidate payload must match .group/group.json.")
        if canonical_commit != head:
            raise ValueError(
                "canonical_commit must equal the current canonical branch tip when the candidate is prepared. "
                f"Current tip is {head}."
            )
        if final_json.exists() or final_report.exists():
            raise ValueError("A final result already exists; refusing to replace finalization records.")
        if existed_before and not args.replace_candidate:
            raise ValueError(
                f"{candidate_path.relative_to(root)} already exists. Inspect it and use --replace-candidate only to deliberately revise the candidate."
            )
        for field in ("summary",):
            if not _is_nonempty_string(payload.get(field)):
                raise ValueError(f"{field} must be a non-empty string.")
        completed_work = _require_string_list(payload, "completed_work")
        contributors = _require_string_list(payload, "contributors")
        test_evidence = _require_string_list(payload, "test_evidence")
        known_limitations = _require_string_list(payload, "known_limitations")
        unresolved_concerns = _require_string_list(payload, "unresolved_concerns")
        person_id = _resolve_person_id(root, args.person_id)
        event_payload = {
            "canonical_branch": branch,
            "canonical_commit": head,
            "report_file": ".group/final/candidate.md",
            "summary": payload["summary"],
            "completed_work": completed_work,
            "contributors": contributors,
            "test_evidence": test_evidence,
            "known_limitations": known_limitations,
            "unresolved_concerns": unresolved_concerns,
        }
        # Validate the event before creating either artifact.
        probe = {
            "protocol_version": PROTOCOL_VERSION,
            "event_id": "E-20990101T000000Z-PROBE-0000",
            "event_type": "final_candidate",
            "group_id": config["group_id"],
            "person_id": person_id,
            "created_at": "2099-01-01T00:00:00Z",
            "related_events": args.related_event,
            **event_payload,
        }
        errors = validate_event(probe, Path(probe["event_id"] + ".json"))
        if errors:
            raise ValueError("Candidate payload is invalid: " + "; ".join(errors))

        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        markdown = _candidate_markdown(config["group_id"], {**payload, "canonical_branch": branch, "canonical_commit": head})
        if args.replace_candidate:
            candidate_path.write_text(markdown, encoding="utf-8", newline="\n")
        else:
            with candidate_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(markdown)
        try:
            event_path = create_event(root, "final_candidate", event_payload, person_id, args.related_event)
        except Exception:
            if existed_before and args.replace_candidate and old_candidate_text is not None:
                candidate_path.write_text(old_candidate_text, encoding="utf-8", newline="\n")
            elif not existed_before:
                candidate_path.unlink(missing_ok=True)
            raise
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Candidate report created: {candidate_path.relative_to(root)}")
    print(f"Candidate event created: {event_path.relative_to(root)}")
    print(f"Canonical branch: {branch}")
    print(f"Canonical commit: {head}")
    print("Show candidate.md to the human and obtain confirmation before finalizing.")
    return 0


def _final_report_markdown(group_id: str, payload: dict[str, Any]) -> str:
    return (
        "# Group Final Report\n\n"
        "## Assignment\n"
        f"Group `{group_id}`\n\n"
        "## Result\n"
        f"{payload['summary']}\n\n"
        "## Canonical Commit\n"
        f"Branch: `{payload['canonical_branch']}`\n"
        f"Commit: `{payload['canonical_commit']}`\n\n"
        "## Important Changes\n"
        f"{_markdown_bullets(_require_string_list(payload, 'completed_work'))}\n\n"
        "## Validation\n"
        f"{_markdown_bullets(_require_string_list(payload, 'test_evidence'))}\n\n"
        "## Known Limitations\n"
        f"{_markdown_bullets(_require_string_list(payload, 'known_limitations'))}\n\n"
        "## Contributors\n"
        f"{_markdown_bullets(_require_string_list(payload, 'contributors'))}\n"
    )


def command_finalize_confirmed(args: argparse.Namespace) -> int:
    """Write final report/envelope/event only after explicit human confirmation."""
    root = Path(args.root).resolve()
    report_path = root / GROUP_DIR / "final" / "final_report.md"
    envelope_path = root / GROUP_DIR / "final" / "final.json"
    created_paths: list[Path] = []
    event_path: Path | None = None
    try:
        if not args.human_confirmed:
            raise ValueError("Human confirmation is required. Review candidate.md first, then pass --human-confirmed.")
        config = _load_valid_group(root)
        # Refuse finalization if current configuration/events/members are invalid.
        validation_rc = command_validate(argparse.Namespace(root=str(root)))
        if validation_rc != 0:
            raise ValueError("Repository validation failed; refusing finalization.")
        payload = _read_payload_file(root, args.payload_file)
        branch, head = _require_canonical_checkout(root, config)
        canonical_branch = payload.get("canonical_branch", branch)
        if canonical_branch != config["canonical_branch"]:
            raise ValueError("canonical_branch in final payload must match .group/group.json.")
        requested_commit = payload.get("canonical_commit")
        if not _is_nonempty_string(requested_commit):
            raise ValueError("canonical_commit must be the exact commit proposed in candidate.md.")
        resolved_commit = _git_output(root, "rev-parse", "--verify", f"{requested_commit}^{{commit}}")
        if resolved_commit != requested_commit:
            raise ValueError("canonical_commit must use the full commit hash, not an abbreviation.")
        ancestor_check = _run_git(root, "merge-base", "--is-ancestor", resolved_commit, head)
        if ancestor_check.returncode != 0:
            raise ValueError("canonical_commit is not an ancestor of the current canonical branch tip.")
        # Permit only metadata/event commits after the candidate code commit. A later
        # source/test change would mean the candidate no longer describes the branch.
        changed_paths_text = _git_output(root, "diff", "--name-only", f"{resolved_commit}..{head}")
        changed_paths = [line for line in changed_paths_text.splitlines() if line]
        non_metadata = [name for name in changed_paths if not name.startswith(".group/")]
        if non_metadata:
            raise ValueError(
                "Non-.group files changed after the candidate canonical commit: "
                + ", ".join(non_metadata)
                + ". Regenerate the candidate for the current canonical code before finalizing."
            )
        candidate_path = root / GROUP_DIR / "final" / "candidate.md"
        if not candidate_path.is_file():
            raise ValueError("Missing .group/final/candidate.md; prepare and show a candidate first.")
        candidate_text = candidate_path.read_text(encoding="utf-8")
        if resolved_commit not in candidate_text:
            raise ValueError("candidate.md does not identify the requested canonical_commit.")
        if report_path.exists() or envelope_path.exists():
            raise ValueError("Final report/envelope already exists; refusing to overwrite finalization records.")
        for field in ("summary", "status"):
            if not _is_nonempty_string(payload.get(field)):
                raise ValueError(f"{field} must be a non-empty string.")
        if payload["status"] not in FINAL_STATUSES:
            raise ValueError("status must be completed or completed_with_known_limitations.")
        completed_work = _require_string_list(payload, "completed_work")
        contributors = _require_string_list(payload, "contributors")
        test_evidence = _require_string_list(payload, "test_evidence")
        known_limitations = _require_string_list(payload, "known_limitations")
        if payload["status"] == "completed" and known_limitations:
            raise ValueError("Use status completed_with_known_limitations when known_limitations is non-empty.")
        # Most recent candidate event must describe the same commit and have no unresolved concerns.
        candidate_events: list[dict[str, Any]] = []
        for path in (root / EVENTS_DIR).glob("*.json"):
            try:
                event = read_json(path)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event_type") == "final_candidate" and event.get("canonical_commit") == resolved_commit:
                if not validate_event(event, path):
                    candidate_events.append(event)
        if not candidate_events:
            raise ValueError("No valid final_candidate event exists for this canonical commit.")
        latest_candidate = max(candidate_events, key=lambda item: item.get("created_at", ""))
        if latest_candidate.get("unresolved_concerns"):
            raise ValueError("The latest candidate still has unresolved concerns. Revise the candidate and obtain human approval again.")
        if latest_candidate.get("known_limitations", []) != known_limitations:
            raise ValueError("known_limitations in final payload must match the approved candidate event.")
        person_id = _resolve_person_id(root, args.person_id)
        final_payload = {
            "canonical_branch": branch,
            "canonical_commit": resolved_commit,
            "report_file": ".group/final/final_report.md",
            "test_evidence": test_evidence,
            "known_limitations": known_limitations,
            "initiated_by": person_id,
            "status": payload["status"],
            "summary": payload["summary"],
        }
        probe = {
            "protocol_version": PROTOCOL_VERSION,
            "event_id": "E-20990101T000000Z-PROBE-0000",
            "event_type": "final_result",
            "group_id": config["group_id"],
            "person_id": person_id,
            "created_at": "2099-01-01T00:00:00Z",
            "related_events": args.related_event,
            **final_payload,
        }
        errors = validate_event(probe, Path(probe["event_id"] + ".json"))
        if errors:
            raise ValueError("Final result payload is invalid: " + "; ".join(errors))

        final_dir = report_path.parent
        final_dir.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(_final_report_markdown(config["group_id"], {
                **payload,
                "canonical_branch": branch,
                "canonical_commit": resolved_commit,
            }))
        created_paths.append(report_path)
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "group_id": config["group_id"],
            "canonical_branch": branch,
            "canonical_commit": resolved_commit,
            "report_file": ".group/final/final_report.md",
            "initiated_by": person_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": payload["status"],
            "known_limitations": known_limitations,
        }
        write_json(envelope_path, envelope)
        created_paths.append(envelope_path)
        event_path = create_event(root, "final_result", final_payload, person_id, args.related_event)
        post_errors = validate_final_records(root, config)
        if post_errors:
            raise ValueError("Generated final records failed validation: " + "; ".join(post_errors))
    except (ValueError, OSError) as exc:
        # Final records are uncommitted until the caller publishes them. Roll back
        # only files this invocation created so it can be safely retried.
        for path in created_paths:
            path.unlink(missing_ok=True)
        if event_path is not None:
            event_path.unlink(missing_ok=True)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Final report created: {report_path.relative_to(root)}")
    print(f"Final JSON created: {envelope_path.relative_to(root)}")
    print(f"Final event created: {event_path.relative_to(root)}")
    print(f"Status: {payload['status']}")
    print(f"Canonical branch: {branch}")
    print(f"Canonical commit: {resolved_commit}")
    print("Commit and push the finalization records using the configured group workflow.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="groupctl",
        description="Version 0.3 group collaboration and provider-neutral AI-agent bridge.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create the required .group/ scaffold")
    init_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    init_parser.add_argument("--group-id", required=True, help="shared group identifier, e.g. G-12")
    init_parser.add_argument("--person-id", required=True, help="your member identifier, e.g. P17")
    init_parser.add_argument("--display-name", required=True, help="your display name")
    init_parser.add_argument("--canonical-branch", default="main", help="configured canonical branch (default: main)")
    init_parser.add_argument("--assignment-file", default=str(DEFAULT_ASSIGNMENT_PATH).replace("\\", "/"),
                             help="repository-relative assignment path")
    init_parser.set_defaults(func=command_init)

    validate_parser = subparsers.add_parser("validate", help="validate group configuration and event files")
    validate_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    validate_parser.set_defaults(func=command_validate)

    events_parser = subparsers.add_parser("events", help="list and validate event files")
    events_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    events_parser.set_defaults(func=command_list_events)

    status_parser = subparsers.add_parser(
        "status", help="summarize the latest group collaboration state"
    )
    status_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    status_parser.add_argument(
        "--remote", help="also include events on already-fetched remote-tracking branches (does not fetch)"
    )
    status_parser.set_defaults(func=command_status)

    event_create_parser = subparsers.add_parser(
        "event-create", help="create a validated local event from a JSON payload file"
    )
    event_create_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    event_create_parser.add_argument("--event-type", required=True, choices=sorted(SUPPORTED_EVENT_TYPES))
    event_create_parser.add_argument("--payload-file", required=True,
                                     help="JSON file containing event-specific fields only")
    event_create_parser.add_argument("--person-id", help="member ID (inferred if exactly one member exists)")
    event_create_parser.add_argument("--related-event", action="append", default=[],
                                     help="related event ID; may be supplied multiple times")
    event_create_parser.set_defaults(func=command_event_create)

    remote_events_parser = subparsers.add_parser(
        "remote-events",
        help="inspect validated events on already-fetched remote-tracking branches",
    )
    remote_events_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    remote_events_parser.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    remote_events_parser.add_argument("--new-only", action="store_true",
                                      help="show only events new or changed since the local checkpoint")
    remote_events_parser.add_argument("--mark-seen", action="store_true",
                                      help="update the local-only checkpoint after the caller has inspected events")
    remote_events_parser.set_defaults(func=command_remote_events)

    agent_context_parser = subparsers.add_parser(
        "agent-context", help="export validated group context for an external AI coding agent"
    )
    agent_context_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    agent_context_parser.add_argument("--person-id", required=True, help="member identity for this agent session")
    agent_context_parser.add_argument("--remote", help="include validated events from an already-fetched remote, e.g. origin")
    agent_context_parser.add_argument("--limit", type=int, default=30, help="maximum recent events to include (1-500)")
    agent_context_parser.add_argument("--output", help="write JSON to this path instead of stdout")
    agent_context_parser.add_argument("--no-instructions", action="store_true", help="omit AGENT_INSTRUCTIONS.md from the context bundle")
    agent_context_parser.set_defaults(func=command_agent_context)

    agent_submit_parser = subparsers.add_parser(
        "agent-submit", help="validate and record a structured response from an external AI agent"
    )
    agent_submit_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    agent_submit_parser.add_argument("--person-id", required=True, help="member identity submitting the response")
    agent_submit_parser.add_argument("--input-file", required=True, help="JSON file with event_type, payload, and optional related_events")
    agent_submit_parser.add_argument("--remote", help="include validated events from an already-fetched remote, e.g. origin")
    agent_submit_parser.set_defaults(func=command_agent_submit)

    candidate_parser = subparsers.add_parser(
        "final-candidate",
        help="prepare .group/final/candidate.md and a final_candidate event from a JSON payload",
    )
    candidate_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    candidate_parser.add_argument("--payload-file", required=True, help="JSON payload for the candidate report")
    candidate_parser.add_argument("--person-id", help="initiating member ID (inferred if only one member exists)")
    candidate_parser.add_argument("--related-event", action="append", default=[], help="related event ID; may be repeated")
    candidate_parser.add_argument("--replace-candidate", action="store_true", help="deliberately replace an existing unfinalized candidate.md")
    candidate_parser.set_defaults(func=command_candidate_create)

    finalize_parser = subparsers.add_parser(
        "finalize-confirmed",
        help="write final report, final.json, and final_result event after human approval",
    )
    finalize_parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    finalize_parser.add_argument("--payload-file", required=True, help="JSON payload for final result")
    finalize_parser.add_argument("--person-id", help="initiating member ID (inferred if only one member exists)")
    finalize_parser.add_argument("--related-event", action="append", default=[], help="related event ID; may be repeated")
    finalize_parser.add_argument("--human-confirmed", action="store_true", help="assert that the human reviewed candidate.md and confirmed finalization")
    finalize_parser.set_defaults(func=command_finalize_confirmed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
