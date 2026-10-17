"""Rename video-named directories in uniquely matched show roots.

The default run is local-only. ``--apply`` permits a single directory rename
per journaled action after the source parent, directory identity, and complete
subtree manifest have been checked against a fresh AList listing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Direct ``python scripts/normalize_cloud_wrappers.py`` starts with scripts/
# as sys.path[0]; add the repository root before importing shared modules.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import reconcile_cloud_library as rec


SCHEMA_VERSION = 1
TOOL_NAME = "normalize_cloud_wrappers"
RESOURCE_SUFFIX = " [资源包]"


def _cloud_path(root: str, relative: str) -> str:
    if not relative:
        return root.rstrip("/") or "/"
    return root.rstrip("/") + "/" + relative


def _is_video_named_directory(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    extensions = "|".join(re.escape(suffix.lstrip("."))
                          for suffix in sorted(rec.VIDEO_SUFFIXES, key=len, reverse=True))
    return re.search(r"\.(?:{})(?:\s*\(\d+\))?$".format(extensions), name, re.IGNORECASE) is not None


def _unique_matched_shows(inventory: dict) -> List[Tuple[str, dict]]:
    shows = rec._iter_shows(inventory)
    subject_counts = Counter(str(show.get("subject_id")) for _, show in shows
                             if show.get("subject_id") is not None)
    path_counts = Counter(show.get("source_path") for _, show in shows
                          if isinstance(show.get("source_path"), str) and show.get("source_path"))
    selected = []
    for category, show in shows:
        subject_id = show.get("subject_id")
        source_path = show.get("source_path")
        if (show.get("matched") is not True or show.get("ambiguous") is not False
                or show.get("unmatched") is not False or subject_id is None
                or not isinstance(source_path, str) or not source_path
                or subject_counts.get(str(subject_id)) != 1 or path_counts.get(source_path) != 1):
            continue
        selected.append((category, show))
    return selected


def _action_id(file_id: str, source_relative_path: str, target_name: str) -> str:
    raw = "\0".join((file_id, source_relative_path, target_name)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def build_plan(inventory: dict, cfg: dict, *, enabled: bool = True) -> dict:
    """Build candidates from the complete inventory; this function is local-only."""
    actions: List[dict] = []
    skipped: List[dict] = []
    if not enabled:
        return {"actions": actions, "skipped": skipped, "enabled": False}

    root = inventory["inventory_root"]
    for category, show in _unique_matched_shows(inventory):
        show_relative = show.get("source_path")
        try:
            parts = rec._clean_relative_path(show_relative, field="show.source_path")
        except rec.ReconcileError as exc:
            skipped.append({"show_name": show.get("name"), "code": exc.code,
                           "state": "blocked", "message": str(exc)})
            continue
        show_node = rec._node_by_relative_path(root, show_relative)
        if (not isinstance(show_node, dict) or show_node.get("type") != "directory"
                or show_node.get("incomplete") or show_node.get("complete") is False
                or not isinstance(show_node.get("children"), list)):
            skipped.append({"subject_id": str(show.get("subject_id")), "show_name": show.get("name"),
                           "source_path": show_relative, "code": "show_tree_incomplete",
                           "state": "blocked", "message": "matched show root is missing or incomplete"})
            continue
        show_id = rec._file_id(show_node)
        parent_relative = "/".join(parts[:-1])
        parent_node = rec._node_by_relative_path(root, parent_relative)
        parent_id = rec._file_id(parent_node) if isinstance(parent_node, dict) else None
        if not show_id or not parent_id:
            skipped.append({"subject_id": str(show.get("subject_id")), "show_name": show.get("name"),
                           "source_path": show_relative, "code": "missing_parent_identity",
                           "state": "blocked", "message": "show root or its parent has no stable file_id"})
            continue

        sibling_names = {str(row.get("name", "")).casefold() for row in show_node["children"]
                         if isinstance(row, dict)}
        for child in show_node["children"]:
            if not isinstance(child, dict) or child.get("type") != "directory":
                continue
            source_name = child.get("name")
            if not _is_video_named_directory(source_name):
                continue
            file_id = rec._file_id(child)
            source_relative = show_relative + "/" + source_name if isinstance(source_name, str) else ""
            target_name = source_name + RESOURCE_SUFFIX
            target_relative = show_relative + "/" + target_name
            same_source_name = [row for row in show_node["children"]
                                if isinstance(row, dict)
                                and str(row.get("name", "")).casefold() == str(source_name).casefold()]
            action = {
                "action_id": _action_id(str(file_id or ""), source_relative, target_name),
                "kind": "rename_video_named_directory",
                "subject_id": str(show.get("subject_id")),
                "show_name": str(show.get("name") or ""),
                "category": category,
                "show_relative_path": show_relative,
                "show_name_in_cloud": show_node.get("name"),
                "show_file_id": show_id,
                "show_parent_relative_path": parent_relative,
                "show_parent_file_id": parent_id,
                "parent_relative_path": show_relative,
                "parent_file_id": show_id,
                "source_relative_path": source_relative,
                "target_relative_path": target_relative,
                "source_name": source_name,
                "target_name": target_name,
                "file_id": file_id,
                "directory_manifest": rec._directory_manifest_from_inventory(child),
                "state": "planned",
                "steps": [],
            }
            if not file_id:
                action.update({"state": "blocked", "code": "missing_directory_identity",
                               "message": "candidate directory has no stable file_id"})
            elif action["directory_manifest"] is None:
                action.update({"state": "blocked", "code": "directory_manifest_incomplete",
                               "message": "candidate subtree has no complete identity-backed inventory manifest"})
            elif len(same_source_name) != 1:
                action.update({"state": "blocked", "code": "ambiguous_source_name",
                               "message": "multiple direct children share the source name; path-based rename is ambiguous"})
            elif target_name.casefold() in sibling_names:
                action.update({"state": "blocked", "code": "target_name_conflict",
                               "message": "target name already exists beside the source directory"})
            actions.append(action)
    return {"actions": actions, "skipped": skipped, "enabled": True}


def _replace_prefixed_path(value: str, old: str, new: str, cloud_root: str) -> str:
    if value == old or value.startswith(old + "/"):
        return new + value[len(old):]
    old_absolute = _cloud_path(cloud_root, old)
    if value == old_absolute or value.startswith(old_absolute.rstrip("/") + "/"):
        new_absolute = _cloud_path(cloud_root, new)
        return new_absolute + value[len(old_absolute):]
    return value


def relocate_inventory(inventory: dict, action: dict, cloud_root: str) -> bool:
    """Apply an already verified directory rename to all inventory path views."""
    old = action["source_relative_path"]
    new = action["target_relative_path"]
    root = inventory.get("inventory_root")
    if not isinstance(root, dict):
        raise rec.ReconcileError("inventory_root is missing during path relocation", code="inventory_invalid")
    old_node = rec._node_by_relative_path(root, old)
    new_node = rec._node_by_relative_path(root, new)
    file_id = str(action["file_id"])
    if old_node is not None:
        if (old_node.get("type") != "directory" or rec._file_id(old_node) != file_id
                or old_node.get("name") != action["source_name"]):
            raise rec.ReconcileError("inventory source directory identity changed before relocation",
                                     code="inventory_relocation_conflict")
        if new_node is not None:
            raise rec.ReconcileError("inventory target path already exists before relocation",
                                     code="inventory_relocation_conflict")
        already_relocated = False
    elif new_node is not None:
        if (new_node.get("type") != "directory" or rec._file_id(new_node) != file_id
                or new_node.get("name") != action["target_name"]):
            raise rec.ReconcileError("inventory target directory identity differs from the verified cloud rename",
                                     code="inventory_relocation_conflict")
        already_relocated = True
    else:
        raise rec.ReconcileError("inventory has neither the original nor target directory path",
                                 code="inventory_relocation_conflict")

    if already_relocated:
        _append_derived_rename(inventory, action)
        return False

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if (value.get("type") == "directory" and str(rec._file_id(value) or "") == file_id
                    and value.get("name") == action["source_name"]):
                value["name"] = action["target_name"]
            for key in ("path", "source_path", "relative_path"):
                item = value.get(key)
                if isinstance(item, str):
                    value[key] = _replace_prefixed_path(item, old, new, cloud_root)
            for key, item in list(value.items()):
                if key == "dirs" and isinstance(item, list) and value.get("source_path") == action["parent_relative_path"]:
                    value[key] = [action["target_name"] if name == action["source_name"] else name
                                  for name in item]
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(inventory)
    _append_derived_rename(inventory, action)
    return True


def _append_derived_rename(inventory: dict, action: dict) -> None:
    history = inventory.get("derived_path_renames")
    if not isinstance(history, list):
        history = []
        inventory["derived_path_renames"] = history
    if any(isinstance(row, dict) and row.get("action_id") == action["action_id"] for row in history):
        return
    history.append({
        "action_id": action["action_id"],
        "file_id": str(action["file_id"]),
        "source_relative_path": action["source_relative_path"],
        "target_relative_path": action["target_relative_path"],
        "verified_at": rec.iso_now(),
        "evidence": "fresh_parent_id_and_name_readback",
    })


def _verify_source_parent(client: rec.AListClient, cfg: dict, action: dict) -> Tuple[List[dict], Optional[str]]:
    parent_path = _cloud_path(cfg["alist"]["root"], action["show_parent_relative_path"])
    try:
        parents = client.list_all(parent_path, refresh=True)
        matching_parent_id = [row for row in parents if rec._file_id(row) == str(action["show_file_id"])]
        if (len(matching_parent_id) != 1 or not rec._is_dir(matching_parent_id[0])
                or matching_parent_id[0].get("name") != action["show_name_in_cloud"]):
            return [], "show parent readback does not uniquely match the inventory ID/type/name"
        rows = client.list_all(_cloud_path(cfg["alist"]["root"], action["show_relative_path"]), refresh=True)
        return rows, None
    except rec.ReconcileError as exc:
        return [], "fresh parent listing failed ({})".format(exc.code)


def _readback_by_id(client: rec.AListClient, cfg: dict, action: dict) -> Tuple[str, Optional[str]]:
    rows, error = _verify_source_parent(client, cfg, action)
    if error:
        return "unknown", error
    matches = [row for row in rows if rec._file_id(row) == str(action["file_id"])]
    if len(matches) != 1 or not rec._is_dir(matches[0]):
        return "unknown", "directory ID is absent, duplicated, or no longer a directory"
    name = matches[0].get("name")
    if name == action["target_name"]:
        return "target", None
    if name == action["source_name"]:
        return "source", None
    return "unknown", "directory ID is present under an unexpected name"


def _ensure_live_manifest(client: rec.AListClient, cfg: dict, action: dict,
                          parent_rows: List[dict]) -> Optional[str]:
    file_id = str(action["file_id"])
    matches = [row for row in parent_rows if rec._file_id(row) == file_id]
    named_source = [row for row in parent_rows
                    if str(row.get("name", "")).casefold() == str(action["source_name"]).casefold()]
    named_target = [row for row in parent_rows
                    if str(row.get("name", "")).casefold() == str(action["target_name"]).casefold()]
    if len(matches) != 1 or not rec._is_dir(matches[0]) or matches[0].get("name") != action["source_name"]:
        return "source directory ID/type/name differs from the complete inventory"
    if len(named_source) != 1 or rec._file_id(named_source[0]) != file_id:
        return "source name is not uniquely held by the expected directory ID"
    if named_target:
        return "target name appeared in the live parent listing"
    verification_action = {
        "source_dir": _cloud_path(cfg["alist"]["root"], action["parent_relative_path"]),
        "directory_manifest": action.get("directory_manifest"),
    }
    valid, code, message = rec._verify_live_directory_manifest(client, verification_action,
                                                               action["source_name"])
    if not valid:
        return "live subtree does not match the inventory manifest ({})".format(code)
    return None


def _set_pending(action: dict, *, reason: str) -> None:
    action["state"] = "pending"
    action["pending_step"] = "rename_readback"
    action["last_readback"] = reason


def _complete_rename(inventory: dict, inventory_path: Path, report: dict,
                     report_path: Path, action: dict, cfg: dict, *, recovered: bool = False) -> None:
    # Inventory first: if the process stops before the journal is marked done,
    # the still-pending journal can read back the directory ID and idempotently
    # recognize that the local paths were already relocated.
    relocate_inventory(inventory, action, cfg["alist"]["root"])
    rec.atomic_write_json(inventory_path, inventory)
    action["state"] = "done"
    if recovered:
        action["recovered_from_pending"] = True
    action.pop("pending_step", None)
    action.pop("last_readback", None)
    action.setdefault("steps", []).append({"step": "rename", "state": "verified"})
    rec.atomic_write_json(report_path, report)


def _recover_pending(client: rec.AListClient, cfg: dict, inventory: dict,
                     inventory_path: Path, report: dict, report_path: Path,
                     action: dict) -> None:
    disposition, reason = _readback_by_id(client, cfg, action)
    if disposition == "target":
        try:
            _complete_rename(inventory, inventory_path, report, report_path, action, cfg, recovered=True)
        except rec.ReconcileError as exc:
            _set_pending(action, reason="target confirmed; local inventory relocation failed ({})".format(exc.code))
            rec.atomic_write_json(report_path, report)
    elif disposition == "source":
        _set_pending(action, reason="source ID still has the old name; write will not be retried")
        rec.atomic_write_json(report_path, report)
    else:
        _set_pending(action, reason=reason or "directory state remains unknown")
        rec.atomic_write_json(report_path, report)


def _merge_actions(report: dict, planned: List[dict]) -> Tuple[List[dict], Dict[str, dict]]:
    existing = report.get("actions")
    if not isinstance(existing, list):
        raise rec.ReconcileError("journal actions must be a list", code="journal_invalid")
    by_id: Dict[str, dict] = {}
    for action in existing:
        if not isinstance(action, dict) or not isinstance(action.get("action_id"), str):
            raise rec.ReconcileError("journal contains an invalid action", code="journal_invalid")
        by_id[action["action_id"]] = action
    for item in planned:
        previous = by_id.get(item["action_id"])
        if previous is None:
            by_id[item["action_id"]] = item
        elif previous.get("state") not in ("pending", "done"):
            # Refresh blocked/planned diagnostics without replacing persisted
            # write evidence or a pending/done state.
            old_steps = previous.get("steps", [])
            previous.update(item)
            previous["steps"] = old_steps
    return list(by_id.values()), by_id


def run(config_path: Path, inventory_path: Optional[Path] = None,
        report_path: Optional[Path] = None, *, apply: bool = False,
        client_factory: Optional[Callable[[dict], rec.AListClient]] = None) -> dict:
    raw_config = rec.read_json(Path(config_path))
    cfg = rec.normalize_config(raw_config)
    reconcile_cfg = raw_config.get("reconcile") if isinstance(raw_config.get("reconcile"), dict) else {}
    enabled = reconcile_cfg.get("normalize_video_named_directories", True)
    if not isinstance(enabled, bool):
        raise rec.ReconcileError("reconcile.normalize_video_named_directories must be a boolean",
                                 code="invalid_config")
    inventory_path = Path(inventory_path or cfg["inventory_path"])
    report_path = Path(report_path or (inventory_path.parent / "wrapper-normalization-report.json"))
    if inventory_path.resolve() == report_path.resolve():
        raise rec.ReconcileError("inventory and report paths must be different", code="invalid_config")
    inventory = rec.read_json(inventory_path)
    previous: dict = {}
    if report_path.exists():
        previous = rec.read_json(report_path)
        if previous.get("tool") != TOOL_NAME or previous.get("schema_version") != SCHEMA_VERSION:
            raise rec.ReconcileError("existing report is not a compatible wrapper-normalization journal",
                                     code="journal_invalid")
    report = previous or {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "created_at": rec.iso_now(),
        "actions": [],
        "skipped": [],
    }
    report["inventory_path"] = str(inventory_path)
    report["report_path"] = str(report_path)
    report["errors"] = []

    actions = report.get("actions")
    if not isinstance(actions, list):
        raise rec.ReconcileError("journal actions must be a list", code="journal_invalid")
    # Pending entries are read back even when the already-relocated source no
    # longer appears in the current plan. This path is read-only on the cloud.
    pending = [row for row in actions if isinstance(row, dict) and row.get("state") == "pending"]
    client = None
    if apply and pending:
        factory = client_factory or rec.AListClient
        client = factory(cfg)
        for action in pending:
            try:
                _recover_pending(client, cfg, inventory, inventory_path, report, report_path, action)
            except Exception as exc:  # preserve pending on every unresolved failure
                code = getattr(exc, "code", "recovery_error")
                _set_pending(action, reason="pending readback failed ({})".format(code))
                report["errors"].append({"action_id": action.get("action_id"), "code": code,
                                         "message": "pending action retained for a later readback"})
                rec.atomic_write_json(report_path, report)

    validation = None
    validation_error = None
    try:
        validation = rec.validate_inventory(inventory, cfg)
    except rec.ReconcileError as exc:
        validation_error = exc

    if validation_error is not None:
        # A stale snapshot can still support the readback-only recovery above,
        # but it cannot authorize new candidate writes.
        report["inventory_validation"] = {"state": "blocked", "code": validation_error.code,
                                           "message": str(validation_error)}
        report["skipped"] = []
        report["summary"] = {"state": "blocked", "candidate_count": 0,
                             "pending_count": sum(row.get("state") == "pending" for row in actions
                                                  if isinstance(row, dict))}
        rec.atomic_write_json(report_path, report)
        if apply and pending:
            return report
        raise validation_error

    plan = build_plan(inventory, cfg, enabled=enabled)
    report["inventory_validation"] = {"state": "valid", **validation}
    report["skipped"] = plan["skipped"]
    merged_actions, by_id = _merge_actions(report, plan["actions"])
    report["actions"] = merged_actions
    report["updated_at"] = rec.iso_now()
    rec.atomic_write_json(report_path, report)
    if not apply:
        _update_summary(report)
        rec.atomic_write_json(report_path, report)
        return report

    factory = client_factory or rec.AListClient
    if client is None and any(by_id[row["action_id"]].get("state") == "planned"
                              for row in plan["actions"]):
        client = factory(cfg)

    for action in plan["actions"]:
        current = by_id[action["action_id"]]
        if current.get("state") in ("blocked", "done", "pending"):
            continue
        # The pending actions were handled above. Never resend the rename if an
        # earlier request has an unresolved readback.
        if current.get("state") != "planned":
            continue
        parent_rows, error = _verify_source_parent(client, cfg, current)
        if error:
            current.update({"state": "blocked", "code": "live_parent_mismatch", "message": error})
            rec.atomic_write_json(report_path, report)
            continue
        manifest_error = _ensure_live_manifest(client, cfg, current, parent_rows)
        if manifest_error:
            current.update({"state": "blocked", "code": "live_inventory_mismatch",
                            "message": manifest_error})
            rec.atomic_write_json(report_path, report)
            continue

        # This durable pending record must exist before the one permitted write.
        current["state"] = "pending"
        current["pending_step"] = "rename"
        current["pending_since"] = rec.iso_now()
        current.setdefault("steps", []).append({"step": "rename", "state": "pending"})
        rec.atomic_write_json(report_path, report)
        request_error = None
        try:
            client.rename_file(_cloud_path(cfg["alist"]["root"], current["source_relative_path"]),
                               current["target_name"])
        except Exception as exc:
            request_error = getattr(exc, "code", "rename_request_uncertain")
        disposition, readback_error = _readback_by_id(client, cfg, current)
        if disposition == "target":
            if request_error:
                current.setdefault("steps", []).append({"step": "rename", "state": "request_uncertain",
                                                        "code": request_error})
            _complete_rename(inventory, inventory_path, report, report_path, current, cfg)
        elif disposition == "source":
            _set_pending(current, reason="readback found original name; rename will not be retried")
            if request_error:
                current["request_error_code"] = request_error
            rec.atomic_write_json(report_path, report)
        else:
            _set_pending(current, reason=readback_error or "directory state remains unknown")
            if request_error:
                current["request_error_code"] = request_error
            rec.atomic_write_json(report_path, report)

    _update_summary(report)
    rec.atomic_write_json(report_path, report)
    return report


def _update_summary(report: dict) -> None:
    actions = [row for row in report.get("actions", []) if isinstance(row, dict)]
    counts = Counter(row.get("state", "unknown") for row in actions)
    state = ("pending" if counts.get("pending") else "blocked" if counts.get("blocked")
             else "planned" if counts.get("planned") else "complete")
    report["summary"] = {
        "state": state,
        "candidate_count": len(actions),
        "done_count": counts.get("done", 0),
        "blocked_count": counts.get("blocked", 0),
        "pending_count": counts.get("pending", 0),
        "planned_count": counts.get("planned", 0),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="runtime JSON configuration")
    parser.add_argument("--inventory", type=Path, help="complete fresh inventory JSON")
    parser.add_argument("--report", type=Path, help="local report and pending-action journal")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="local-only plan (default)")
    mode.add_argument("--apply", action="store_true", help="apply verified directory renames")
    args = parser.parse_args(argv)
    try:
        result = run(args.config, args.inventory, args.report, apply=args.apply)
    except (OSError, ValueError, rec.ReconcileError) as exc:
        code = getattr(exc, "code", "error")
        print(json.dumps({"state": "error", "code": code, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"report": result.get("report_path"), **result.get("summary", {})}, ensure_ascii=False))
    summary = result.get("summary", {})
    return 1 if args.apply and (summary.get("pending_count") or summary.get("blocked_count")) else 0


if __name__ == "__main__":
    sys.exit(main())
