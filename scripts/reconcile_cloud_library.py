"""Safely reconcile confidently matched episode files through the local AList API.

The default mode is a local dry-run plan. ``--apply`` enables per-file AList
move/rename requests, each journaled before the request and verified through
fresh source/destination listings afterwards. This tool never deletes files or
directories and never changes show/category directories.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests


SCHEMA_VERSION = 1
DEFAULT_INVENTORY = "/var/lib/bmguangya/library.json"
DEFAULT_REPORT = "/var/lib/bmguangya/reconcile-cloud-library.json"
DEFAULT_ALIST_ROOT = "/guangya/Anime"
VIDEO_SUFFIXES = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m2ts", ".ts",
    ".webm", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob", ".m4v",
}
SUBTITLE_SUFFIXES = {
    ".ass", ".ssa", ".srt", ".sub", ".idx", ".sup", ".vtt", ".smi", ".sami",
}
EPISODE_LABEL = re.compile(r"^(?:EP\d{2,3}(?:\.\d+)?|SP\d{2,3}|OVA\d{2,3}|OAD\d{2,3}|NCOP\d{0,3}|NCED\d{0,3})$", re.I)


class ReconcileError(RuntimeError):
    def __init__(self, message: str, *, code: str = "reconcile_error", details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ReconcileError("JSON root must be an object", code="invalid_json", details={"path": str(path)})
    return value


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def normalize_root(value: Any) -> str:
    root = str(value or DEFAULT_ALIST_ROOT).strip()
    if not root.startswith("/"):
        root = "/" + root
    parts = [part for part in root.split("/") if part]
    if any(part in (".", "..") or "\\" in part for part in parts):
        raise ReconcileError("AList root contains an unsafe path component", code="invalid_config")
    return "/" + "/".join(parts)


def normalize_config(raw: dict) -> dict:
    alist = raw.get("alist") if isinstance(raw.get("alist"), dict) else {}
    inventory = raw.get("inventory") if isinstance(raw.get("inventory"), dict) else {}
    reconcile = raw.get("reconcile") if isinstance(raw.get("reconcile"), dict) else {}
    base_url = _first(alist.get("api_base"), alist.get("base_url"), alist.get("public_url"),
                      raw.get("alist_api_base"), raw.get("alist_public_url"))
    if not base_url:
        raise ReconcileError("AList api_base/public_url is required", code="missing_config")
    base_url = str(base_url).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReconcileError("AList base URL must be an http(s) URL without credentials/query/fragment",
                             code="invalid_config")
    token = _first(alist.get("token"), alist.get("access_token"), raw.get("alist_token"))
    username = _first(alist.get("username"), raw.get("alist_username"))
    password = _first(alist.get("password"), raw.get("alist_password"))
    if not token and (not username or password is None):
        raise ReconcileError("AList token or username/password is required", code="missing_config")
    timeout = float(_first(alist.get("timeout_seconds"), raw.get("network_timeout_seconds"), 60))
    page_size = int(_first(alist.get("page_size"), 100))
    if timeout <= 0 or timeout > 600 or page_size < 1 or page_size > 1000:
        raise ReconcileError("AList timeout/page_size is outside safe bounds", code="invalid_config")
    inventory_path = _first(raw.get("inventory_path"), inventory.get("path"), inventory.get("output"),
                             DEFAULT_INVENTORY)
    report_path = _first(reconcile.get("report_path"), reconcile.get("report"),
                         raw.get("reconcile_report_path"), DEFAULT_REPORT)
    organize_unassigned = reconcile.get("organize_unassigned", raw.get("organize_unassigned", False))
    if not isinstance(organize_unassigned, bool):
        raise ReconcileError("reconcile.organize_unassigned must be a boolean", code="invalid_config")
    if not Path(str(inventory_path)).is_absolute() or not Path(str(report_path)).is_absolute():
        raise ReconcileError("inventory and report paths must be absolute", code="invalid_config")
    return {
        "alist": {
            "base_url": base_url,
            "root": normalize_root(_first(alist.get("root"), raw.get("alist_root"), raw.get("alist_path"), DEFAULT_ALIST_ROOT)),
            "token": str(token) if token else "",
            "username": str(username) if username else "",
            "password": str(password) if password is not None else "",
            "timeout_seconds": timeout,
            "page_size": page_size,
        },
        "inventory_path": str(inventory_path),
        "report_path": str(report_path),
        "inventory_max_age_seconds": int(_first(reconcile.get("max_inventory_age_seconds"),
                                                  inventory.get("freshness_ttl_seconds"), 900)),
        "organize_unassigned": organize_unassigned,
    }


def _clean_relative_path(value: Any, *, field: str) -> Tuple[str, ...]:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ReconcileError("{} must be a non-empty root-relative POSIX path".format(field), code="unsafe_path")
    parts = tuple(value.split("/"))
    if any(not part or part in (".", "..") for part in parts):
        raise ReconcileError("{} contains an unsafe path component".format(field), code="unsafe_path")
    return parts


def _join_root(root: str, relative: str) -> str:
    parts = _clean_relative_path(relative, field="relative path")
    return root.rstrip("/") + "/" + "/".join(parts)


def _is_dir(node: dict) -> bool:
    return node.get("type") == "directory" or node.get("is_dir") is True


def _node_by_relative_path(root: dict, relative: str) -> Optional[dict]:
    if relative == "":
        return root
    try:
        parts = _clean_relative_path(relative, field="inventory path")
    except ReconcileError:
        return None
    current = root
    for part in parts:
        matches = [child for child in current.get("children", []) if child.get("name") == part]
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def _walk_files(node: dict, prefix: str = "") -> Iterable[dict]:
    for child in node.get("children", []):
        path = "/".join(part for part in (prefix, str(child.get("name") or "")) if part)
        if _is_dir(child):
            yield from _walk_files(child, path)
        else:
            yield {**child, "_relative_path": path}


def _walk_directories(node: dict, prefix: str = "") -> Iterable[dict]:
    for child in node.get("children", []):
        path = "/".join(part for part in (prefix, str(child.get("name") or "")) if part)
        if _is_dir(child):
            yield {**child, "_relative_path": path}
            yield from _walk_directories(child, path)


def _iter_shows(inventory: dict) -> List[Tuple[str, dict]]:
    found = []
    for section in inventory.get("tree", []):
        if not isinstance(section, dict) or not isinstance(section.get("shows"), list):
            continue
        category = str(section.get("category") or "")
        for show in section["shows"]:
            if isinstance(show, dict):
                found.append((category, show))
    return found


def validate_inventory(inventory: dict, cfg: dict, *, now: Optional[dt.datetime] = None) -> dict:
    if inventory.get("complete") is not True:
        raise ReconcileError("inventory is not marked complete", code="inventory_incomplete")
    if inventory.get("cloud_complete") is False or inventory.get("collection_complete") is False:
        raise ReconcileError("inventory reports an incomplete source scan", code="inventory_incomplete")
    if inventory.get("errors"):
        raise ReconcileError("inventory contains source errors", code="inventory_incomplete")
    freshness = inventory.get("freshness")
    if not isinstance(freshness, dict) or freshness.get("state") != "fresh":
        raise ReconcileError("inventory is not marked fresh", code="inventory_stale")
    generated = parse_timestamp(inventory.get("generated_at") or inventory.get("updated_at"))
    if generated is None:
        raise ReconcileError("inventory has no valid generated_at timestamp", code="inventory_stale")
    current = now or utc_now()
    age = (current - generated).total_seconds()
    ttl = cfg["inventory_max_age_seconds"]
    if ttl <= 0 or age < -60 or age > ttl:
        raise ReconcileError("inventory age {:.0f}s exceeds freshness limit {}s".format(max(age, 0), ttl),
                             code="inventory_stale", details={"age_seconds": max(age, 0), "ttl_seconds": ttl})
    source = inventory.get("source")
    if not isinstance(source, dict):
        raise ReconcileError("inventory has no source metadata", code="inventory_invalid")
    cloud_root = source.get("cloud_root")
    if not isinstance(cloud_root, str) or not cloud_root.strip():
        raise ReconcileError("inventory source has no cloud_root", code="inventory_invalid")
    if normalize_root(cloud_root) != cfg["alist"]["root"]:
        raise ReconcileError("inventory cloud_root differs from configured AList root", code="root_mismatch")
    tree = inventory.get("tree")
    if not isinstance(tree, list) or not tree:
        raise ReconcileError("inventory tree is missing", code="inventory_invalid")
    cloud_tree = inventory.get("inventory_root")
    if not isinstance(cloud_tree, dict) or not isinstance(cloud_tree.get("children"), list):
        raise ReconcileError("inventory has no complete file tree", code="inventory_invalid")
    if cloud_tree.get("incomplete"):
        raise ReconcileError("inventory file tree contains incomplete directories", code="inventory_incomplete")
    return {"generated_at": generated.isoformat(timespec="seconds"), "age_seconds": max(0, int(age)),
            "ttl_seconds": ttl, "cloud_root": cfg["alist"]["root"]}


def canonical_episode_key(value: Any, *, special: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    if not EPISODE_LABEL.fullmatch(key):
        return None
    if key.startswith("EP"):
        digits, dot, fraction = key[2:].partition(".")
        key = "EP" + digits.zfill(2) + (dot + fraction if dot else "")
    elif key.startswith(("SP", "OVA", "OAD")):
        prefix = "OVA" if key.startswith("OVA") else "OAD" if key.startswith("OAD") else "SP"
        digits = key[len(prefix):]
        key = prefix + digits.zfill(2)
    elif key in ("NCOP", "NCED"):
        pass
    elif not special:
        return None
    return key


def _safe_video_suffix(name: str) -> Optional[str]:
    suffix = PurePosixPath(name).suffix
    return suffix if suffix.lower() in VIDEO_SUFFIXES else None


def _file_id(node: dict) -> Optional[str]:
    value = node.get("file_id") or node.get("id")
    return str(value) if value not in (None, "") else None


def _normalize_stem(name: str) -> str:
    return unicodedata.normalize("NFKC", PurePosixPath(name).stem).casefold()


def _action_id(subject_id: str, episode_key: str, file_id: str, source: str, target: str) -> str:
    raw = "\0".join((subject_id, episode_key, file_id, source, target)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _inventory_file_evidence(root: dict, relative_path: str, file_id: str, file_name: str) -> bool:
    node = _node_by_relative_path(root, relative_path)
    return bool(node and not _is_dir(node) and str(node.get("name")) == file_name and _file_id(node) == file_id)


def _build_file_action(category: str, show: dict, episode: dict, evidence: dict,
                       cfg: dict, inventory_root: dict, *, role: str = "video",
                       associated_episode: Optional[str] = None) -> dict:
    subject_id = str(show.get("subject_id") or "")
    show_path = show.get("source_path")
    if not isinstance(show_path, str) or not show_path:
        raise ReconcileError("matched show has no source_path", code="invalid_show")
    show_parts = _clean_relative_path(show_path, field="show.source_path")
    file_path = evidence.get("relative_path") or evidence.get("path")
    file_parts = _clean_relative_path(file_path, field="episode.relative_path")
    if len(file_parts) <= len(show_parts) or file_parts[:len(show_parts)] != show_parts:
        raise ReconcileError("episode path is not below its matched show path", code="path_mismatch")
    file_name = evidence.get("name") or evidence.get("file_name")
    if not isinstance(file_name, str) or not file_name or file_name in (".", "..") or "/" in file_name or "\\" in file_name:
        raise ReconcileError("file evidence has an unsafe name", code="invalid_evidence")
    file_id = _file_id(evidence)
    if not file_id:
        raise ReconcileError("file evidence has no stable AList file_id", code="missing_file_id")
    if not _inventory_file_evidence(inventory_root, file_path, file_id, file_name):
        raise ReconcileError("episode file evidence does not match inventory_root", code="evidence_mismatch")
    suffix = (PurePosixPath(file_name).suffix if role == "subtitle" else _safe_video_suffix(file_name))
    if not suffix or (role == "subtitle" and suffix.lower() not in SUBTITLE_SUFFIXES):
        raise ReconcileError("file extension is not a supported {} type".format(role), code="unsupported_file")
    episode_key = canonical_episode_key(associated_episode or episode.get("episode_key"),
                                        special=bool(episode.get("is_special")))
    if not episode_key:
        raise ReconcileError("episode has no safe canonical episode key", code="unknown_episode_key")
    target_name = episode_key + suffix
    target_relative = "/".join((*show_parts, target_name))
    source_parent_relative = "/".join(file_parts[:-1])
    if not source_parent_relative:
        raise ReconcileError("episode path resolves to the cloud root", code="unsafe_path")
    return {
        "action_id": _action_id(subject_id, episode_key, file_id, file_path, target_relative),
        "subject_id": subject_id,
        "show_name": str(show.get("name") or ""),
        "category": category,
        "episode_key": episode_key,
        "role": role,
        "file_id": file_id,
        "original_name": file_name,
        "source_relative_path": file_path,
        "source_dir_relative": source_parent_relative,
        "target_relative_path": target_relative,
        "target_name": target_name,
        "source_path": _join_root(cfg["alist"]["root"], file_path),
        "source_dir": _join_root(cfg["alist"]["root"], source_parent_relative),
        "show_path": _join_root(cfg["alist"]["root"], show_path),
        "move_required": source_parent_relative != show_path,
        "rename_required": file_name != target_name,
        "state": "planned",
        "steps": [],
    }


def _normalize_gcid(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        return None
    return value.lower()


def _positive_size(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _build_verified_duplicate_content_actions(inventory: dict, cfg: dict, category: str,
                                             show: dict, episodes: Sequence[dict],
                                             all_shows: Sequence[Tuple[str, dict]]) -> Tuple[List[dict], List[dict], set]:
    """Quarantine provably identical duplicate ordinary episode copies.

    The episode key and unique Bangumi match scope the comparison. GCIDs are
    never compared across shows. A duplicate group is moved only if every copy
    has the same valid 40-hex GCID and positive, equal size, and every source
    file (including exact-basename subtitles) has a stable inventory identity.
    """
    root = inventory["inventory_root"]
    subject_id = str(show.get("subject_id") or "")
    show_name = str(show.get("name") or "subject-{}".format(subject_id))
    show_path = str(show.get("source_path") or "")
    try:
        show_parts = _clean_relative_path(show_path, field="show.source_path")
    except ReconcileError:
        return [], [], set()

    same_title = [candidate for _, candidate in all_shows
                  if str(candidate.get("name") or "") == show_name
                  and str(candidate.get("subject_id") or "") != subject_id]
    title_component = show_name
    if same_title:
        title_component += " [bgm-{}]".format(subject_id)
    target_directory = "未标记/待核对-重复内容/" + title_component
    try:
        _clean_relative_path(target_directory, field="duplicate-content target directory")
    except ReconcileError as exc:
        return [], [{"subject_id": subject_id, "show_name": show_name, "state": "blocked",
                     "code": exc.code, "message": str(exc)}], set()

    show_files = [node for node in _walk_files(root)
                  if tuple(node["_relative_path"].split("/")[:len(show_parts)]) == show_parts]
    grouped: Dict[str, List[dict]] = {}
    for episode in episodes:
        if not isinstance(episode, dict) or episode.get("present") is not True or episode.get("is_special") is True:
            continue
        key = canonical_episode_key(episode.get("episode_key"), special=False)
        if key:
            grouped.setdefault(key, []).append(episode)

    mkdir_actions: Dict[str, dict] = {}
    actions: List[dict] = []
    issues: List[dict] = []
    handled_keys = set()
    target_node = _node_by_relative_path(root, target_directory)
    used_names = ({str(child.get("name", "")).casefold()
                   for child in target_node.get("children", [])}
                  if target_node is not None and _is_dir(target_node) else set())

    def require_directory(relative: str) -> Tuple[Optional[str], Optional[str]]:
        parent = ""
        dependency = None
        for part in _clean_relative_path(relative, field="duplicate-content target directory"):
            current = "/".join(filter(None, (parent, part)))
            node = _node_by_relative_path(root, current)
            if node is not None:
                if not _is_dir(node):
                    return None, "target path component is occupied by a file: " + current
                parent = current
                continue
            planned = mkdir_actions.get(current)
            if planned is None:
                action_id = hashlib.sha256(("mkdir\0" + current).encode("utf-8")).hexdigest()[:24]
                parent_path = _join_root(cfg["alist"]["root"], parent) if parent else cfg["alist"]["root"]
                planned = {
                    "action_id": action_id, "kind": "organize_mkdir", "role": "review_directory",
                    "target_relative_path": current, "target_path": _join_root(cfg["alist"]["root"], current),
                    "parent_relative_path": parent, "parent_path": parent_path, "name": part,
                    "state": "planned", "steps": [],
                }
                if dependency:
                    planned["requires_directory"] = dependency
                mkdir_actions[current] = planned
            dependency = planned["action_id"]
            parent = current
        return dependency, None

    target_dependency = None
    target_ready = False

    for key, rows in grouped.items():
        copies: List[dict] = []
        row_ids = set()
        valid = True
        invalid_reason = "duplicate episode inventory evidence is incomplete"
        for row in rows:
            episode_id = row.get("episode_id")
            if (isinstance(episode_id, bool) or not isinstance(episode_id, (int, str))
                    or str(episode_id).strip() in ("", "0")):
                valid = False
                invalid_reason = "duplicate ordinary episode has no stable Bangumi episode id"
                break
            row_ids.add(str(episode_id))
            files = row.get("present_files")
            if not isinstance(files, list) or not files:
                valid = False
                invalid_reason = "duplicate episode has no complete present_files evidence"
                break
            first = files[0]
            if (not isinstance(first, dict) or row.get("file_id") is None
                    or str(row.get("file_id")) != str(first.get("file_id") or first.get("id") or "")
                    or row.get("relative_path") != (first.get("relative_path") or first.get("path"))
                    or row.get("file_name") != (first.get("name") or first.get("file_name"))):
                valid = False
                invalid_reason = "episode summary does not agree with its first present_files entry"
                break
            copies.extend(files)
        if len(copies) < 2:
            continue
        if not valid or len(row_ids) != 1:
            issues.append({"subject_id": subject_id, "show_name": show_name, "episode_key": key,
                           "relative_paths": [row.get("relative_path") for row in copies if isinstance(row, dict)],
                           "state": "blocked", "code": "duplicate_content_unverified",
                           "message": invalid_reason if not valid else "duplicate episode rows disagree on Bangumi episode id"})
            continue

        evidence_rows = []
        gcids = set()
        sizes = set()
        seen_ids = set()
        seen_paths = set()
        for evidence in copies:
            if not isinstance(evidence, dict):
                valid = False
                invalid_reason = "duplicate present_files entry is invalid"
                break
            name = evidence.get("name") or evidence.get("file_name")
            relative = evidence.get("relative_path") or evidence.get("path")
            file_id = _file_id(evidence)
            try:
                parts = _clean_relative_path(relative, field="duplicate-content source")
            except ReconcileError:
                valid = False
                invalid_reason = "duplicate source path is invalid"
                break
            if (not isinstance(name, str) or not name or parts[-1] != name
                    or len(parts) <= len(show_parts) or parts[:len(show_parts)] != show_parts
                    or not file_id or file_id in seen_ids or relative in seen_paths
                    or not _safe_video_suffix(name)):
                valid = False
                invalid_reason = "duplicate source files do not have unique safe names, paths, and identities"
                break
            node = _node_by_relative_path(root, relative)
            gcid = _normalize_gcid(evidence.get("gcid"))
            size = _positive_size(evidence.get("size"))
            node_gcid = _normalize_gcid(node.get("gcid")) if node else None
            node_size = _positive_size(node.get("size")) if node else None
            if (node is None or _is_dir(node) or str(node.get("name")) != name
                    or _file_id(node) != file_id or gcid is None or size is None
                    or node_gcid != gcid or node_size != size):
                valid = False
                invalid_reason = "duplicate copies lack matching valid GCID and positive file size in the complete tree"
                break
            seen_ids.add(file_id)
            seen_paths.add(relative)
            gcids.add(gcid)
            sizes.add(size)
            evidence_rows.append({"name": name, "relative_path": relative, "file_id": file_id,
                                  "gcid": gcid, "size": size, "node": node})
        if valid and (len(gcids) != 1 or len(sizes) != 1):
            valid = False
            invalid_reason = "duplicate copies have different GCIDs or file sizes"
        if not valid:
            issues.append({"subject_id": subject_id, "show_name": show_name, "episode_key": key,
                           "relative_paths": [row.get("relative_path") for row in evidence_rows]
                           or [row.get("relative_path") for row in copies if isinstance(row, dict)],
                           "state": "blocked", "code": "duplicate_content_unverified",
                           "message": invalid_reason})
            continue

        canonical_name = re.compile(r"^{}\.[^.]+$".format(re.escape(key)), re.I)
        evidence_rows.sort(key=lambda row: (
            0 if (len(_clean_relative_path(row["relative_path"], field="duplicate source")) == len(show_parts) + 1
                  and canonical_name.fullmatch(row["name"])) else 1,
            len(_clean_relative_path(row["relative_path"], field="duplicate source")) - len(show_parts),
            row["relative_path"].casefold(), row["relative_path"], row["file_id"],
        ))
        kept = evidence_rows[0]
        loser_bundles = []
        for video in evidence_rows[1:]:
            source_parts = _clean_relative_path(video["relative_path"], field="duplicate source")
            source_parent = "/".join(source_parts[:-1])
            stem = _normalize_stem(video["name"])
            same_stem_videos = [node for node in show_files
                                if PurePosixPath(node["name"]).suffix.lower() in VIDEO_SUFFIXES
                                and "/".join(node["_relative_path"].split("/")[:-1]) == source_parent
                                and _normalize_stem(node["name"]) == stem]
            if len(same_stem_videos) != 1 or same_stem_videos[0]["_relative_path"] != video["relative_path"]:
                valid = False
                invalid_reason = "exact-basename subtitle association is ambiguous"
                break
            subtitles = [node for node in show_files
                         if PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_SUFFIXES
                         and "/".join(node["_relative_path"].split("/")[:-1]) == source_parent
                         and _normalize_stem(node["name"]) == stem]
            bundle = [{"name": video["name"], "relative_path": video["relative_path"],
                       "file_id": video["file_id"], "role": "video"}]
            for subtitle in subtitles:
                subtitle_id = _file_id(subtitle)
                subtitle_path = subtitle["_relative_path"]
                subtitle_node = _node_by_relative_path(root, subtitle_path)
                if (not subtitle_id or subtitle_node is None or _is_dir(subtitle_node)
                        or _file_id(subtitle_node) != subtitle_id):
                    valid = False
                    invalid_reason = "an exact-basename subtitle has no stable file identity"
                    break
                bundle.append({"name": subtitle["name"], "relative_path": subtitle_path,
                               "file_id": subtitle_id, "role": "subtitle"})
            if not valid:
                break
            loser_bundles.append(bundle)
        if not valid:
            issues.append({"subject_id": subject_id, "show_name": show_name, "episode_key": key,
                           "relative_paths": [row["relative_path"] for row in evidence_rows[1:]],
                           "state": "blocked", "code": "duplicate_content_unverified",
                           "message": invalid_reason})
            continue

        if not target_ready:
            target_dependency, target_error = require_directory(target_directory)
            if target_error:
                mkdir_actions.clear()
                issues.append({"subject_id": subject_id, "show_name": show_name, "episode_key": key,
                               "state": "blocked", "code": "duplicate_content_target_conflict",
                               "message": target_error})
                continue
            target_ready = True

        planned_bundle_actions = []
        planned_names = set(used_names)
        for bundle in loser_bundles:
            video = bundle[0]
            extension = PurePosixPath(video["name"]).suffix
            original_stem = video["name"][:-len(extension)]
            candidate_stem = original_stem
            bundle_names = [candidate_stem + PurePosixPath(item["name"]).suffix for item in bundle]
            if any(name.casefold() in planned_names for name in bundle_names):
                stable = hashlib.sha256((video["relative_path"] + "\0" + video["file_id"]).encode("utf-8")).hexdigest()[:8]
                candidate_stem = original_stem + " [copy-{}]".format(stable)
                bundle_names = [candidate_stem + PurePosixPath(item["name"]).suffix for item in bundle]
                index = 2
                while any(name.casefold() in planned_names for name in bundle_names):
                    candidate_stem = original_stem + " [copy-{}-{}]".format(stable, index)
                    bundle_names = [candidate_stem + PurePosixPath(item["name"]).suffix for item in bundle]
                    index += 1
                    if index > 1000:
                        valid = False
                        invalid_reason = "no stable collision-free destination name could be selected"
                        break
            if not valid:
                break
            planned_names.update(name.casefold() for name in bundle_names)
            video_action_id = None
            bundle_actions = []
            for item, target_name in zip(bundle, bundle_names):
                source_parts = _clean_relative_path(item["relative_path"], field="duplicate source")
                target_relative = target_directory + "/" + target_name
                action_id = hashlib.sha256(("duplicate-content\0{}\0{}\0{}\0{}".format(
                    subject_id, key, item["file_id"], item["relative_path"])
                    + "\0" + target_relative).encode("utf-8")).hexdigest()[:24]
                action = {
                    "action_id": action_id, "kind": "organize_move",
                    "role": "duplicate_content_" + item["role"], "reason": "duplicate_content",
                    "subject_id": subject_id, "show_name": show_name, "episode_key": key,
                    "content_gcid": evidence_rows[0]["gcid"], "content_size": evidence_rows[0]["size"],
                    "kept_relative_path": kept["relative_path"], "source_kind": "file",
                    "category": category, "file_id": item["file_id"],
                    "original_name": item["name"], "current_name": item["name"],
                    "source_relative_path": item["relative_path"],
                    "source_dir_relative": "/".join(source_parts[:-1]),
                    "source_path": _join_root(cfg["alist"]["root"], item["relative_path"]),
                    "source_dir": _join_root(cfg["alist"]["root"], "/".join(source_parts[:-1])),
                    "target_relative_path": target_relative,
                    "target_path": _join_root(cfg["alist"]["root"], target_relative),
                    "target_dir_relative": target_directory,
                    "target_dir": _join_root(cfg["alist"]["root"], target_directory),
                    "target_name": target_name,
                    "rename_before_move": target_name != item["name"],
                    "requires_directory": target_dependency, "move_required": True,
                    "state": "planned", "steps": [],
                }
                if item["role"] == "video":
                    video_action_id = action_id
                elif video_action_id is not None:
                    action["requires_verified_video"] = video_action_id
                bundle_actions.append(action)
            planned_bundle_actions.extend(bundle_actions)
        if not valid:
            issues.append({"subject_id": subject_id, "show_name": show_name, "episode_key": key,
                           "relative_paths": [row["relative_path"] for row in evidence_rows[1:]],
                           "state": "blocked", "code": "duplicate_content_target_conflict",
                           "message": invalid_reason})
            continue
        actions.extend(planned_bundle_actions)
        used_names = planned_names
        handled_keys.add(key)

    directory_actions = sorted(mkdir_actions.values(),
                               key=lambda row: (row["target_relative_path"].count("/"), row["target_relative_path"]))
    return directory_actions + actions, issues, handled_keys


def _block_action(action: dict, code: str, message: str, *, fatal: bool = False) -> None:
    action["state"] = "blocked"
    action["blocked"] = {"code": code, "message": message, "fatal": bool(fatal)}


def _build_organization_actions(inventory: dict, cfg: dict) -> Tuple[List[dict], List[dict]]:
    """Plan opt-in moves for unmatched cloud roots and category/root loose files."""
    root = inventory["inventory_root"]
    create_by_path: Dict[str, dict] = {}
    moves: List[dict] = []
    issues: List[dict] = []
    used_by_target: Dict[str, set] = {}

    def require_directory(relative: str) -> Tuple[Optional[str], Optional[str]]:
        parts = _clean_relative_path(relative, field="organization target directory")
        parent = ""
        dependency = None
        for part in parts:
            current = "/".join(filter(None, (parent, part)))
            node = _node_by_relative_path(root, current)
            if node is not None:
                if not _is_dir(node):
                    return None, "target path component is occupied by a file: " + current
                parent = current
                continue
            action = create_by_path.get(current)
            if action is None:
                action_id = hashlib.sha256(("mkdir\0" + current).encode("utf-8")).hexdigest()[:24]
                parent_path = _join_root(cfg["alist"]["root"], parent) if parent else cfg["alist"]["root"]
                action = {
                    "action_id": action_id, "kind": "organize_mkdir", "role": "review_directory",
                    "target_relative_path": current, "target_path": _join_root(cfg["alist"]["root"], current),
                    "parent_relative_path": parent, "parent_path": parent_path, "name": part,
                    "state": "planned", "steps": [],
                }
                if dependency:
                    action["requires_directory"] = dependency
                create_by_path[current] = action
            dependency = action["action_id"]
            parent = current
        return dependency, None

    candidates = []
    for section in inventory.get("tree", []):
        if not isinstance(section, dict):
            continue
        category = str(section.get("category") or "")
        for show in section.get("shows", []) if isinstance(section.get("shows"), list) else []:
            if not isinstance(show, dict) or not show.get("source_path"):
                continue
            source = str(show["source_path"])
            if source == "未标记" or source.startswith("未标记/"):
                continue
            if show.get("ambiguous"):
                issues.append({"show_name": show.get("name"), "relative_path": source,
                               "state": "blocked", "code": "ambiguous_unclaimed_directory",
                               "message": "ambiguous directory remains in place for manual review"})
                continue
            if show.get("unmatched") is True and show.get("subject_id") is None:
                source_category = source.split("/", 1)[0]
                original_category = source_category if source_category in ("在看", "想看", "看过") else "根目录"
                candidates.append(("directory", source, str(show.get("name") or PurePosixPath(source).name),
                                   original_category, "未标记"))

        if category in ("在看", "想看", "看过"):
            for item in section.get("loose_files", []) if isinstance(section.get("loose_files"), list) else []:
                if isinstance(item, dict):
                    source = item.get("path") or item.get("relative_path")
                    if source:
                        candidates.append(("file", str(source), str(item.get("name") or PurePosixPath(str(source)).name),
                                           category, "未标记/待核对-{}散落".format(category)))
        if category == "未标记":
            for item in section.get("unmarked_root_files", []) if isinstance(section.get("unmarked_root_files"), list) else []:
                if isinstance(item, dict):
                    source = item.get("relative_path") or item.get("path")
                    if source:
                        candidates.append(("file", str(source), str(item.get("name") or PurePosixPath(str(source)).name),
                                           "根目录", "未标记/待核对-根目录散落"))

    numbered_pack_groups: Dict[str, List[Tuple[str, str]]] = {}
    for kind, source_relative, source_name, _, _ in candidates:
        if kind != "directory":
            continue
        match = re.fullmatch(r"(.+?)\s*\((\d+)\)", source_name)
        if not match:
            continue
        base_name = match.group(1).strip()
        if not base_name or base_name in (".", "..") or "/" in base_name or "\\" in base_name:
            continue
        key = unicodedata.normalize("NFKC", base_name).casefold()
        numbered_pack_groups.setdefault(key, []).append((source_relative, base_name))
    duplicate_pack_base_by_source = {}
    for rows in numbered_pack_groups.values():
        if len(rows) >= 2:
            for source_relative, base_name in rows:
                duplicate_pack_base_by_source[source_relative] = base_name

    for kind, source_relative, source_name, category, target_directory in sorted(candidates, key=lambda row: row[1]):
        try:
            source_parts = _clean_relative_path(source_relative, field="organization source")
            if source_parts[-1] != source_name:
                raise ReconcileError("source basename does not match the inventory path", code="source_name_mismatch")
        except ReconcileError as exc:
            issues.append({"relative_path": source_relative, "state": "blocked", "code": exc.code,
                           "message": str(exc)})
            continue
        source_node = _node_by_relative_path(root, source_relative)
        if source_node is None or (_is_dir(source_node) != (kind == "directory")):
            issues.append({"relative_path": source_relative, "state": "blocked", "code": "source_tree_mismatch",
                           "message": "source is missing from the complete inventory tree or has the wrong kind"})
            continue
        file_id = _file_id(source_node)
        if kind == "file" and not file_id:
            issues.append({"relative_path": source_relative, "state": "blocked", "code": "missing_file_id",
                           "message": "loose file has no stable file_id; left untouched"})
            continue
        if kind == "directory" and not file_id:
            issues.append({"relative_path": source_relative, "state": "blocked", "code": "missing_directory_id",
                           "message": "unmatched directory has no stable file_id; left untouched"})
            continue

        duplicate_pack_base = duplicate_pack_base_by_source.get(source_relative)
        if duplicate_pack_base:
            # Repeated downloader directories (for example "pack (1)" and
            # "pack (94)") stay individually intact but are nested under one
            # review group so the unmarked root does not become another flat pile.
            target_directory = "未标记/待核对-重复资源/" + duplicate_pack_base

        dependency, target_error = require_directory(target_directory)
        source_parent = "/".join(source_parts[:-1])
        target_node = _node_by_relative_path(root, target_directory)
        target_names = used_by_target.setdefault(
            target_directory.casefold(),
            {str(child.get("name", "")).casefold() for child in target_node.get("children", [])}
            if target_node and _is_dir(target_node) else set(),
        )
        target_name = source_name
        rename_required = False
        if target_name.casefold() in target_names:
            extension = PurePosixPath(source_name).suffix if kind == "file" else ""
            stem = source_name[:-len(extension)] if extension else source_name
            base = "{} [from-{}]".format(stem, category)
            index = 1
            while True:
                candidate_name = base + ("-{}".format(index) if index > 1 else "") + extension
                if candidate_name.casefold() not in target_names:
                    target_name = candidate_name
                    rename_required = True
                    break
                index += 1
                if index > 1000:
                    target_error = "could not find a collision-free stable destination name"
                    break
        target_names.add(target_name.casefold())
        target_relative = target_directory + "/" + target_name
        source_path = _join_root(cfg["alist"]["root"], source_relative)
        source_dir = _join_root(cfg["alist"]["root"], source_parent) if source_parent else cfg["alist"]["root"]
        target_path = _join_root(cfg["alist"]["root"], target_relative)
        target_dir_path = _join_root(cfg["alist"]["root"], target_directory)
        action_id = hashlib.sha256(("organize\0{}\0{}\0{}".format(kind, source_relative, target_relative)
                                    + "\0" + str(file_id or "")).encode("utf-8")).hexdigest()[:24]
        action = {
            "action_id": action_id, "kind": "organize_move", "role": "unassigned_{}".format(kind),
            "source_kind": kind, "category": category, "file_id": file_id,
            "original_name": source_name, "current_name": source_name,
            "source_relative_path": source_relative, "source_dir_relative": source_parent,
            "source_path": source_path, "source_dir": source_dir,
            "target_relative_path": target_relative, "target_path": target_path,
            "target_dir_relative": target_directory, "target_dir": target_dir_path,
            "target_name": target_name, "rename_before_move": rename_required,
            "requires_directory": dependency, "move_required": True,
            "state": "planned", "steps": [],
        }
        if duplicate_pack_base:
            action["organization_group"] = "duplicate-numbered-directory"
            action["organization_group_base"] = duplicate_pack_base
        if target_error:
            _block_action(action, "target_directory_conflict", target_error)
        moves.append(action)

    directory_actions = sorted(create_by_path.values(), key=lambda row: (row["target_relative_path"].count("/"), row["target_relative_path"]))
    return directory_actions + moves, issues


def _build_out_of_range_actions(inventory: dict, cfg: dict,
                                planned_directories: Sequence[dict]) -> Tuple[List[dict], List[dict], List[dict]]:
    """Quarantine known ordinary videos absent from a reliable Bangumi episode list.

    These files are never relabeled with a guessed episode number. Exact-basename
    subtitles follow only when the matched video and all file identities are
    present in the complete inventory tree.
    """
    root = inventory["inventory_root"]
    directory_by_path = {row["target_relative_path"]: row for row in planned_directories
                         if row.get("kind") == "organize_mkdir"}
    new_directories: Dict[str, dict] = {}
    actions: List[dict] = []
    issues: List[dict] = []
    out_of_range_report: List[dict] = []
    used_by_target: Dict[str, set] = {}
    all_shows = _iter_shows(inventory)
    subject_counts: Dict[str, int] = {}
    for _, show in all_shows:
        if show.get("subject_id") is not None:
            sid = str(show["subject_id"])
            subject_counts[sid] = subject_counts.get(sid, 0) + 1

    def require_directory(relative: str) -> Tuple[Optional[str], Optional[str]]:
        parts = _clean_relative_path(relative, field="out-of-range target directory")
        parent = ""
        dependency = None
        for part in parts:
            current = "/".join(filter(None, (parent, part)))
            node = _node_by_relative_path(root, current)
            if node is not None:
                if not _is_dir(node):
                    return None, "target path component is occupied by a file: " + current
                parent = current
                continue
            planned = directory_by_path.get(current) or new_directories.get(current)
            if planned is None:
                action_id = hashlib.sha256(("mkdir\0" + current).encode("utf-8")).hexdigest()[:24]
                parent_path = _join_root(cfg["alist"]["root"], parent) if parent else cfg["alist"]["root"]
                planned = {
                    "action_id": action_id, "kind": "organize_mkdir", "role": "review_directory",
                    "target_relative_path": current, "target_path": _join_root(cfg["alist"]["root"], current),
                    "parent_relative_path": parent, "parent_path": parent_path, "name": part,
                    "state": "planned", "steps": [],
                }
                if dependency:
                    planned["requires_directory"] = dependency
                new_directories[current] = planned
            dependency = planned["action_id"]
            parent = current
        return dependency, None

    for category, show in all_shows:
        extras = [row for row in (show.get("extra_eps") or []) if isinstance(row, dict)
                  and row.get("reason") == "episode_not_in_bangumi" and row.get("is_special") is not True]
        if not extras:
            continue
        subject_id = show.get("subject_id")
        confident = (show.get("matched") is True and show.get("ambiguous") is not True
                     and show.get("unmatched") is not True and show.get("coverage_unknown") is not True
                     and subject_id is not None and subject_counts.get(str(subject_id)) == 1
                     and isinstance(show.get("source_path"), str) and bool(show.get("source_path"))
                     and isinstance(show.get("expected_eps_count"), int)
                     and not isinstance(show.get("expected_eps_count"), bool))
        if not confident:
            issues.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                           "code": "out_of_range_not_confident",
                           "message": "out-of-Bangumi videos are retained because episode coverage is not uniquely reliable"})
            continue

        show_path = str(show["source_path"])
        try:
            show_parts = _clean_relative_path(show_path, field="show.source_path")
        except ReconcileError as exc:
            issues.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                           "code": exc.code, "message": str(exc)})
            continue
        show_node = _node_by_relative_path(root, show_path)
        if show_node is None or not _is_dir(show_node):
            issues.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                           "code": "show_directory_missing",
                           "message": "matched show directory is absent from the complete inventory"})
            continue

        grouped: Dict[str, List[dict]] = {}
        for extra in extras:
            key = canonical_episode_key(extra.get("parsed_episode"), special=False)
            name = extra.get("name")
            if not key or not isinstance(name, str) or not _safe_video_suffix(name):
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "relative_path": extra.get("relative_path"), "state": "blocked",
                               "code": "out_of_range_evidence_invalid",
                               "message": "out-of-Bangumi row has no exact numeric episode/file evidence"})
                continue
            grouped.setdefault(key, []).append(extra)

        file_nodes = [node for node in _walk_files(root)
                      if tuple(node["_relative_path"].split("/")[:len(show_parts)]) == show_parts]
        for key, videos in grouped.items():
            if len(videos) != 1:
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "episode_key": key, "relative_paths": [row.get("relative_path") for row in videos],
                               "state": "blocked", "code": "duplicate_out_of_range_episode",
                               "message": "multiple videos claim the same out-of-Bangumi episode; all were retained"})
                continue
            video = videos[0]
            relative = video.get("relative_path")
            name = video.get("name")
            file_id = video.get("file_id")
            try:
                source_parts = _clean_relative_path(relative, field="out-of-range source")
            except ReconcileError as exc:
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "relative_path": relative, "state": "blocked", "code": exc.code,
                               "message": str(exc)})
                continue
            if (source_parts[:len(show_parts)] != show_parts or len(source_parts) <= len(show_parts)
                    or source_parts[-1] != name or not file_id):
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "relative_path": relative, "state": "blocked", "code": "out_of_range_source_invalid",
                               "message": "out-of-Bangumi row is outside its matched show or lacks file identity"})
                continue
            source_node = _node_by_relative_path(root, relative)
            if (source_node is None or _is_dir(source_node) or source_node.get("name") != name
                    or _file_id(source_node) != str(file_id)):
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "relative_path": relative, "state": "blocked", "code": "out_of_range_tree_mismatch",
                               "message": "out-of-Bangumi file evidence disagrees with the complete tree"})
                continue

            source_parent = "/".join(source_parts[:-1])
            source_stem = _normalize_stem(name)
            companions = [node for node in file_nodes
                          if PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_SUFFIXES
                          and "/".join(node["_relative_path"].split("/")[:-1]) == source_parent
                          and _normalize_stem(node["name"]) == source_stem]
            bundle = [{"name": name, "relative_path": relative, "file_id": str(file_id), "role": "video"}]
            invalid_companion = None
            for subtitle in companions:
                sub_id = _file_id(subtitle)
                if not sub_id:
                    invalid_companion = subtitle["_relative_path"]
                    break
                bundle.append({"name": subtitle["name"], "relative_path": subtitle["_relative_path"],
                               "file_id": str(sub_id), "role": "subtitle"})
            if invalid_companion:
                issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                               "relative_path": invalid_companion, "state": "blocked",
                               "code": "out_of_range_subtitle_identity_missing",
                               "message": "exact-basename subtitle has no stable file_id; the entire bundle remains"})
                continue

            base_review_directory = "未标记/待核对-" + str(show.get("name") or subject_id)
            try:
                _clean_relative_path(base_review_directory, field="out-of-range target directory")
            except ReconcileError as exc:
                issues.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                               "code": exc.code, "message": str(exc)})
                continue
            target_directory = base_review_directory
            existing_review = _node_by_relative_path(root, target_directory)
            if existing_review is not None and _is_dir(existing_review) and existing_review.get("children"):
                # Never mix a new out-of-range bundle into an already populated
                # folder whose provenance cannot be proven from this snapshot.
                stable = base_review_directory + " [bgm-{}]".format(subject_id)
                target_directory = stable
                suffix = 2
                while True:
                    existing = _node_by_relative_path(root, target_directory)
                    if existing is None or (_is_dir(existing) and not existing.get("children")):
                        break
                    target_directory = stable + "-{}".format(suffix)
                    suffix += 1
                    if suffix > 1000:
                        issues.append({"subject_id": subject_id, "show_name": show.get("name"),
                                       "state": "blocked", "code": "out_of_range_target_conflict",
                                       "message": "no collision-free review directory could be selected"})
                        target_directory = ""
                        break
                if not target_directory:
                    continue
            dependency, target_error = require_directory(target_directory)
            target_node = _node_by_relative_path(root, target_directory)
            target_names = used_by_target.setdefault(
                target_directory.casefold(),
                {str(child.get("name", "")).casefold() for child in target_node.get("children", [])}
                if target_node and _is_dir(target_node) else set(),
            )
            source_extension = PurePosixPath(name).suffix
            source_target_stem = name[:-len(source_extension)]
            candidate_stem = source_target_stem
            index = 1
            bundle_names = [candidate_stem + PurePosixPath(item["name"]).suffix for item in bundle]
            while any(candidate.casefold() in target_names for candidate in bundle_names):
                suffix = " [from-out-of-bangumi-range]" + ("-{}".format(index) if index > 1 else "")
                candidate_stem = source_target_stem + suffix
                bundle_names = [candidate_stem + PurePosixPath(item["name"]).suffix for item in bundle]
                index += 1
                if index > 1000:
                    target_error = "could not find collision-free names for out-of-range video/subtitles"
                    break
            target_names.update(candidate.casefold() for candidate in bundle_names)
            video_action_id = None
            for bundle_item, target_name in zip(bundle, bundle_names):
                source_relative = bundle_item["relative_path"]
                source_parts_item = _clean_relative_path(source_relative, field="out-of-range bundle source")
                source_dir_relative = "/".join(source_parts_item[:-1])
                target_relative = target_directory + "/" + target_name
                action_id = hashlib.sha256(("out-of-range\0{}\0{}\0{}".format(
                    subject_id, bundle_item["file_id"], source_relative) + "\0" + target_relative).encode("utf-8")).hexdigest()[:24]
                action = {
                    "action_id": action_id, "kind": "organize_move",
                    "role": "out_of_range_" + bundle_item["role"], "reason": "out_of_bangumi_range",
                    "subject_id": str(subject_id), "show_name": str(show.get("name") or ""),
                    "episode_key": key, "source_kind": "file", "category": category,
                    "file_id": bundle_item["file_id"], "original_name": bundle_item["name"],
                    "current_name": bundle_item["name"], "source_relative_path": source_relative,
                    "source_dir_relative": source_dir_relative,
                    "source_path": _join_root(cfg["alist"]["root"], source_relative),
                    "source_dir": _join_root(cfg["alist"]["root"], source_dir_relative),
                    "target_relative_path": target_relative,
                    "target_path": _join_root(cfg["alist"]["root"], target_relative),
                    "target_dir_relative": target_directory,
                    "target_dir": _join_root(cfg["alist"]["root"], target_directory),
                    "target_name": target_name, "rename_before_move": target_name != bundle_item["name"],
                    "requires_directory": dependency, "move_required": True,
                    "state": "planned", "steps": [],
                }
                if bundle_item["role"] == "video":
                    video_action_id = action_id
                elif video_action_id is not None:
                    # Keep an exact-basename subtitle with its video as one
                    # recoverable unit. If the video move cannot be verified,
                    # apply_actions leaves the subtitle in its original folder.
                    action["requires_verified_video"] = video_action_id
                if target_error:
                    _block_action(action, "target_directory_conflict", target_error)
                actions.append(action)
                if bundle_item["role"] == "video":
                    out_of_range_report.append({
                        "subject_id": str(subject_id), "show_name": str(show.get("name") or ""),
                        "episode_key": key, "reason": "out_of_bangumi_range",
                        "source_relative_path": source_relative, "target_relative_path": target_relative,
                        "file_id": bundle_item["file_id"], "state": "planned",
                    })

    new_mkdirs = sorted(new_directories.values(), key=lambda row: (row["target_relative_path"].count("/"), row["target_relative_path"]))
    return new_mkdirs + actions, issues, out_of_range_report


def build_plan(inventory: dict, cfg: dict) -> dict:
    root = inventory["inventory_root"]
    organization_enabled = bool(cfg.get("organize_unassigned", False))
    all_shows = _iter_shows(inventory)
    subjects: Dict[str, List[Tuple[str, dict]]] = {}
    unresolved: List[dict] = []
    for category, show in all_shows:
        subject_id = show.get("subject_id")
        if subject_id is not None:
            subjects.setdefault(str(subject_id), []).append((category, show))
        elif show.get("matched") is True:
            unresolved.append({"show_name": show.get("name"), "state": "blocked",
                               "code": "missing_subject_id",
                               "message": "show has matched=true but no Bangumi subject_id"})

    actions: List[dict] = []
    duplicate_content_actions: List[dict] = []
    duplicate_content_issues: List[dict] = []
    matched_roots: List[Tuple[str, str]] = []

    for subject_id, entries in subjects.items():
        matched_entries = [(category, show) for category, show in entries if show.get("matched") is True]
        if len(entries) != 1:
            unresolved.append({"subject_id": subject_id, "state": "blocked", "code": "duplicate_subject",
                               "message": "subject_id is not unique in the inventory tree"})
            continue
        if not matched_entries:
            unresolved.append({"subject_id": subject_id, "state": "blocked", "code": "unmatched_subject",
                               "message": "Bangumi subject has no uniquely matched cloud show; left untouched"})
            continue
        if len(matched_entries) != 1:
            unresolved.append({"subject_id": subject_id, "state": "blocked", "code": "ambiguous_subject",
                               "message": "subject_id has more than one matched show entry"})
            continue
        category, show = matched_entries[0]
        show_path = show.get("source_path")
        if (show.get("ambiguous") or show.get("unmatched") or show.get("coverage_unknown")
                or not isinstance(show_path, str) or not show_path):
            unresolved.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                               "code": "show_not_confidently_matched",
                               "message": "show is ambiguous, unmatched, coverage-unknown, or has no source_path"})
            continue
        matched_roots.append((subject_id, show_path))
        show_node = _node_by_relative_path(root, show_path)
        if not show_node or not _is_dir(show_node):
            unresolved.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                               "code": "show_directory_missing",
                               "message": "matched show source_path is absent from inventory_root or is not a directory"})
            continue
        episodes = show.get("episodes")
        if not isinstance(episodes, list):
            unresolved.append({"subject_id": subject_id, "show_name": show.get("name"), "state": "blocked",
                               "code": "episodes_missing", "message": "show episodes list is missing"})
            continue
        seen_keys: Dict[str, int] = {}
        for item in episodes:
            if not isinstance(item, dict) or item.get("present") is not True:
                continue
            key = canonical_episode_key(item.get("episode_key"), special=bool(item.get("is_special")))
            if not key:
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item.get("episode_key"), "state": "blocked",
                                   "code": "unknown_episode_key", "message": "present row has unknown episode key"})
                continue
            seen_keys[key] = seen_keys.get(key, 0) + 1
        duplicate_keys = {key for key, count in seen_keys.items() if count > 1}
        duplicate_keys.update(str(key).upper() for key in (show.get("duplicate_eps") or []))

        handled_duplicate_keys = set()
        show_duplicate_actions: List[dict] = []
        coverage_count = show.get("expected_eps_count")
        if (organization_enabled and show.get("matched") is True and show.get("ambiguous") is not True
                and show.get("unmatched") is not True and show.get("coverage_unknown") is not True
                and isinstance(coverage_count, int) and not isinstance(coverage_count, bool)):
            (show_duplicate_actions, show_duplicate_issues,
             handled_duplicate_keys) = _build_verified_duplicate_content_actions(
                inventory, cfg, category, show, episodes, all_shows)
            duplicate_content_actions.extend(show_duplicate_actions)
            duplicate_content_issues.extend(show_duplicate_issues)

        videos_by_key: Dict[str, List[Tuple[dict, dict]]] = {}
        all_file_nodes = list(_walk_files(root))
        show_parts = _clean_relative_path(show_path, field="show.source_path")
        show_files = [node for node in all_file_nodes
                      if tuple(node["_relative_path"].split("/")[:len(show_parts)]) == show_parts]
        for item in episodes:
            if not isinstance(item, dict) or item.get("present") is not True:
                continue
            item_key = canonical_episode_key(item.get("episode_key"), special=bool(item.get("is_special")))
            if not item_key:
                continue
            if item_key in handled_duplicate_keys:
                continue
            files = item.get("present_files")
            if not isinstance(files, list) or len(files) != 1:
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item_key, "state": "blocked", "code": "not_single_file",
                                   "message": "inventory row must have exactly one present_files entry"})
                continue
            evidence = files[0]
            if (not isinstance(evidence, dict) or item.get("present") is not True
                    or item.get("missing") is True or item.get("file_id") is None
                    or str(item.get("file_id")) != str(evidence.get("file_id"))
                    or item.get("relative_path") != evidence.get("relative_path")
                    or item.get("file_name") != evidence.get("name")):
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item_key, "state": "blocked", "code": "inconsistent_evidence",
                                   "message": "episode summary does not agree with its single present_files entry"})
                continue
            if item_key in duplicate_keys:
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item_key, "state": "blocked", "code": "duplicate_episode",
                                   "message": "episode is marked duplicate in inventory"})
                continue
            episode_id = item.get("episode_id")
            if (not item.get("is_special")
                    and (isinstance(episode_id, bool) or not isinstance(episode_id, (int, str))
                         or str(episode_id).strip() in ("", "0"))):
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item_key, "state": "blocked", "code": "unmatched_extra",
                                   "message": "file is not linked to a stable Bangumi episode id or recognized special"})
                continue
            try:
                action = _build_file_action(category, show, item, evidence, cfg, root)
            except ReconcileError as exc:
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "episode_key": item_key, "state": "blocked", "code": exc.code,
                                   "message": str(exc)})
                continue
            videos_by_key.setdefault(item_key, []).append((item, action))
            actions.append(action)

        # Only exact basename companions are moved. Unpaired subtitle files stay
        # where they are and are listed for manual review.
        paired_subtitle_paths = {
            action["source_relative_path"] for action in show_duplicate_actions
            if action.get("role") == "duplicate_content_subtitle"
        }
        for key, rows in videos_by_key.items():
            if key in duplicate_keys or len(rows) != 1:
                continue
            item, video_action = rows[0]
            video_rel = video_action["source_relative_path"]
            video_parent = "/".join(video_rel.split("/")[:-1])
            exact_stem = _normalize_stem(video_action["original_name"])
            companions = [node for node in show_files
                          if PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_SUFFIXES
                          and "/".join(node["_relative_path"].split("/")[:-1]) == video_parent
                          and _normalize_stem(node["name"]) == exact_stem]
            for subtitle in companions:
                paired_subtitle_paths.add(subtitle["_relative_path"])
                sub_evidence = {"name": subtitle["name"], "relative_path": subtitle["_relative_path"],
                                "file_id": _file_id(subtitle)}
                try:
                    action = _build_file_action(category, show, item, sub_evidence, cfg, root,
                                               role="subtitle", associated_episode=key)
                    action["group_id"] = video_action["action_id"]
                    action["requires_verified_video"] = video_action["action_id"]
                    actions.append(action)
                except ReconcileError as exc:
                    unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                       "episode_key": key, "file_name": subtitle["name"],
                                       "state": "blocked", "code": exc.code, "message": str(exc)})
        if (organization_enabled and show.get("matched") is True and show.get("ambiguous") is not True
                and show.get("unmatched") is not True and show.get("coverage_unknown") is not True
                and isinstance(show.get("expected_eps_count"), int)
                and not isinstance(show.get("expected_eps_count"), bool)):
            for extra in show.get("extra_eps", []) if isinstance(show.get("extra_eps"), list) else []:
                if (not isinstance(extra, dict) or extra.get("reason") != "episode_not_in_bangumi"
                        or extra.get("is_special") is True or not _safe_video_suffix(str(extra.get("name") or ""))):
                    continue
                extra_path = extra.get("relative_path")
                try:
                    extra_parts = _clean_relative_path(extra_path, field="out-of-range source")
                except ReconcileError:
                    continue
                if extra_parts[:len(show_parts)] != show_parts or len(extra_parts) <= len(show_parts):
                    continue
                extra_parent = "/".join(extra_parts[:-1])
                extra_stem = _normalize_stem(str(extra.get("name") or ""))
                for node in show_files:
                    if (PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_SUFFIXES
                            and "/".join(node["_relative_path"].split("/")[:-1]) == extra_parent
                            and _normalize_stem(node["name"]) == extra_stem):
                        paired_subtitle_paths.add(node["_relative_path"])
        for node in show_files:
            if PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_SUFFIXES and node["_relative_path"] not in paired_subtitle_paths:
                unresolved.append({"subject_id": subject_id, "show_name": show.get("name"),
                                   "relative_path": node["_relative_path"], "file_id": _file_id(node),
                                   "state": "blocked", "code": "unpaired_subtitle",
                                   "message": "subtitle has no unique exact-basename video companion; left untouched"})

    # Resolve target-name conflicts against the full fresh inventory and against
    # other planned files, including subtitles.
    proposals: Dict[str, List[dict]] = {}
    source_proposals: Dict[str, List[dict]] = {}
    for action in actions:
        proposals.setdefault(action["target_relative_path"].casefold(), []).append(action)
        source_proposals.setdefault(action["source_relative_path"].casefold(), []).append(action)
    for action in actions:
        source_dir = _node_by_relative_path(root, action["source_dir_relative"])
        target_dir = _node_by_relative_path(root, "/".join(action["target_relative_path"].split("/")[:-1]))
        target_name = action["target_name"]
        destination = [] if not target_dir else [child for child in target_dir.get("children", [])
                                                 if str(child.get("name", "")).casefold() == target_name.casefold()]
        if len(proposals[action["target_relative_path"].casefold()]) > 1:
            _block_action(action, "duplicate_target", "multiple planned files resolve to the same target name")
        elif len(source_proposals[action["source_relative_path"].casefold()]) > 1:
            _block_action(action, "duplicate_source", "one cloud file is claimed by multiple episode actions")
        elif destination and not (not action["move_required"] and action["original_name"].casefold() == target_name.casefold()
                                  and _file_id(destination[0]) == action["file_id"]):
            _block_action(action, "target_conflict", "target name already exists in the show directory")
        elif action["move_required"]:
            source_name_matches = [] if not target_dir else [child for child in target_dir.get("children", [])
                                                               if str(child.get("name", "")).casefold() == action["original_name"].casefold()]
            if source_name_matches:
                _block_action(action, "move_name_conflict", "original filename already exists in show directory")
        elif action["original_name"].casefold() == target_name.casefold():
            action["state"] = "noop"

    # A readable list of file-tree items that are intentionally not assigned to
    # a matched Bangumi show. No operation is planned for them.
    unassigned = []
    planned_video_paths = {action["source_relative_path"] for action in actions
                           if action.get("role") in ("video", "duplicate_content_video")}
    matched_paths = [tuple(path.split("/")) for _, path in matched_roots]
    for node in _walk_files(root):
        if PurePosixPath(node["name"]).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        relative = tuple(node["_relative_path"].split("/"))
        if node["_relative_path"] not in planned_video_paths:
            inside_matched_show = any(relative[:len(show_root)] == show_root for show_root in matched_paths)
            unassigned.append({"relative_path": node["_relative_path"], "name": node["name"],
                               "file_id": _file_id(node), "state": "unassigned",
                               "reason": ("no unique episode evidence in the matched show; left untouched"
                                          if inside_matched_show else
                                          "outside a unique matched show; left untouched")})
    empty_directories = [{"relative_path": node["_relative_path"], "name": node["name"],
                          "state": "retained_for_review", "action": "none"}
                         for node in _walk_directories(root) if node.get("empty") is True]
    organization_issues = []
    out_of_range = []
    if organization_enabled:
        organization_actions, organization_issues = _build_organization_actions(inventory, cfg)
        range_actions, range_issues, out_of_range = _build_out_of_range_actions(
            inventory, cfg, organization_actions)
        organization_issues.extend(range_issues)
        all_organization_actions = organization_actions + range_actions + duplicate_content_actions
        directory_actions = [row for row in all_organization_actions
                             if row.get("kind") == "organize_mkdir"]
        directory_actions = list({row["target_relative_path"]: row for row in directory_actions}.values())
        directory_actions.sort(key=lambda row: (row["target_relative_path"].count("/"), row["target_relative_path"]))
        organization_moves = [row for row in all_organization_actions
                              if row.get("kind") == "organize_move"]
        actions = directory_actions + organization_moves + actions
        unresolved.extend(organization_issues)
        unresolved.extend(duplicate_content_issues)
        for row in out_of_range:
            matched_video = next((item for item in unassigned
                                  if item.get("relative_path") == row["source_relative_path"]), None)
            if matched_video is not None:
                matched_video.update({"reason": "out_of_bangumi_range",
                                      "target_relative_path": row["target_relative_path"], "state": "planned"})
        for row in duplicate_content_actions:
            if row.get("role") != "duplicate_content_video":
                continue
            matched_video = next((item for item in unassigned
                                  if item.get("relative_path") == row["source_relative_path"]), None)
            if matched_video is not None:
                matched_video.update({"reason": "duplicate_content",
                                      "target_relative_path": row["target_relative_path"],
                                      "kept_relative_path": row.get("kept_relative_path"),
                                      "state": "planned"})
    return {"actions": actions, "unresolved": unresolved, "unassigned": unassigned,
            "empty_directories": empty_directories, "matched_show_roots": matched_roots,
            "organization_enabled": organization_enabled, "out_of_range": out_of_range}


class AListClient:
    def __init__(self, cfg: dict, *, session: Optional[requests.Session] = None):
        self.cfg = cfg["alist"]
        self.session = session or requests.Session()
        self.token = self.cfg["token"]

    def _post(self, endpoint: str, body: dict, *, stage: str) -> dict:
        if not self.token:
            try:
                response = self.session.post(self.cfg["base_url"] + "/api/auth/login",
                                             json={"username": self.cfg["username"],
                                                   "password": self.cfg["password"]},
                                             timeout=self.cfg["timeout_seconds"])
            except requests.RequestException as exc:
                raise ReconcileError("AList login request failed: {}".format(exc), code="alist_auth") from exc
            payload = self._decode(response, stage="alist_auth")
            data = payload.get("data") or {}
            self.token = str(data.get("token") or "") if isinstance(data, dict) else ""
            if not self.token:
                raise ReconcileError("AList login did not return a token", code="alist_auth")
        try:
            response = self.session.post(self.cfg["base_url"] + endpoint,
                                         headers={"Authorization": self.token}, json=body,
                                         timeout=self.cfg["timeout_seconds"])
        except requests.RequestException as exc:
            raise ReconcileError("AList {} request failed before confirmation: {}".format(stage, exc),
                                 code="alist_request_uncertain") from exc
        return self._decode(response, stage=stage)

    @staticmethod
    def _decode(response: requests.Response, *, stage: str) -> dict:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ReconcileError("AList {} HTTP request failed: {}".format(stage, exc), code="alist_http_error") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReconcileError("AList {} returned invalid JSON".format(stage), code="alist_invalid_json") from exc
        if not isinstance(payload, dict) or payload.get("code") not in (200, "200"):
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else "invalid response"
            raise ReconcileError("AList {} business code was not 200: {} {}".format(stage, code, message),
                                 code="alist_business_error", details={"business_code": code})
        return payload

    def list_all(self, path: str, *, refresh: bool = True) -> List[dict]:
        rows: List[dict] = []
        page = 1
        total: Optional[int] = None
        while True:
            payload = self._post("/api/fs/list", {"path": path, "password": "", "page": page,
                                                  "per_page": self.cfg["page_size"], "refresh": bool(refresh)},
                                 stage="list")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ReconcileError("AList list response has no content list", code="alist_invalid_list")
            declared = data.get("total")
            if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                raise ReconcileError("AList list response has invalid total", code="alist_invalid_list")
            if total is None:
                total = declared
            elif total != declared:
                raise ReconcileError("AList list total changed during pagination", code="alist_unstable_pagination")
            chunk = data["content"]
            if any(not isinstance(item, dict) for item in chunk) or len(chunk) > self.cfg["page_size"]:
                raise ReconcileError("AList list response has malformed entries", code="alist_invalid_list")
            rows.extend(chunk)
            if len(rows) == total:
                break
            if not chunk or len(rows) > total:
                raise ReconcileError("AList list pagination ended before its total", code="alist_truncated_list")
            page += 1
            if page > 10000:
                raise ReconcileError("AList list exceeded the page safety limit", code="alist_pagination_limit")
        names = set()
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
                raise ReconcileError("AList returned an unsafe child name", code="alist_invalid_name")
            if name.casefold() in names:
                raise ReconcileError("AList returned duplicate child names", code="alist_duplicate_name")
            names.add(name.casefold())
        return rows

    def move_file(self, source_dir: str, target_dir: str, name: str) -> dict:
        return self._post("/api/fs/move", {"src_dir": source_dir, "dst_dir": target_dir,
                                           "names": [name]}, stage="move")

    def rename_file(self, source_path: str, new_name: str) -> dict:
        return self._post("/api/fs/rename", {"path": source_path, "name": new_name}, stage="rename")

    def make_dir(self, path: str) -> dict:
        return self._post("/api/fs/mkdir", {"path": path}, stage="mkdir")


def _children_named(rows: Sequence[dict], name: str) -> List[dict]:
    return [row for row in rows if str(row.get("name", "")).casefold() == name.casefold()]


def _find_id(rows: Sequence[dict], file_id: str) -> List[dict]:
    return [row for row in rows if _file_id(row) == str(file_id)]


def _ensure_source(entries: Sequence[dict], action: dict, expected_name: str) -> dict:
    named = _children_named(entries, expected_name)
    exact_id = [row for row in named if _file_id(row) == action["file_id"] and not _is_dir(row)]
    if len(exact_id) != 1 or len(named) != 1:
        raise ReconcileError("live source listing does not contain exactly the expected file_id/name",
                             code="live_source_mismatch")
    return exact_id[0]


def _write_journal(report_path: Path, report: dict) -> None:
    report["updated_at"] = iso_now()
    atomic_write_json(report_path, report)


def _live_preflight(client: AListClient, action: dict) -> Tuple[List[dict], List[dict]]:
    source_rows = client.list_all(action["source_dir"], refresh=True)
    target_rows = client.list_all(action["show_path"], refresh=True)
    return source_rows, target_rows


def _verify_move(client: AListClient, action: dict) -> Tuple[bool, str, List[dict]]:
    source_rows = client.list_all(action["source_dir"], refresh=True)
    target_rows = client.list_all(action["show_path"], refresh=True)
    source_id = _find_id(source_rows, action["file_id"])
    target_same_name = _children_named(target_rows, action["original_name"])
    target_id = [row for row in target_same_name if _file_id(row) == action["file_id"] and not _is_dir(row)]
    if not source_id and len(target_id) == 1 and len(target_same_name) == 1:
        return True, "moved", target_rows
    if len(source_id) == 1 and not target_same_name:
        return False, "unchanged_at_source", target_rows
    return False, "uncertain", target_rows


def _verify_rename(client: AListClient, action: dict, old_name: str) -> Tuple[bool, str, List[dict]]:
    target_rows = client.list_all(action["show_path"], refresh=True)
    new_rows = _children_named(target_rows, action["target_name"])
    old_rows = _children_named(target_rows, old_name)
    new_id = [row for row in new_rows if _file_id(row) == action["file_id"] and not _is_dir(row)]
    old_id = [row for row in old_rows if _file_id(row) == action["file_id"] and not _is_dir(row)]
    if len(new_id) == 1 and not old_id and (old_name.casefold() == action["target_name"].casefold() or not old_rows):
        return True, "renamed", target_rows
    if len(old_id) == 1 and not new_rows:
        return False, "unchanged_at_source", target_rows
    return False, "uncertain", target_rows


def _add_event(action: dict, step: str, state: str, **fields: Any) -> None:
    action.setdefault("steps", []).append({"step": step, "state": state, "at": iso_now(), **fields})


def _mark_uncertain(action: dict, step: str, code: str, message: str) -> None:
    """Keep an unconfirmed write pending so a later run must inspect, never retry."""
    action["state"] = "pending"
    action["pending_step"] = step
    action["pending_error"] = {"code": code, "message": message}


def _add_error(report: dict, action_id: str, code: str, message: str, **fields: Any) -> None:
    report["errors"].append({"action_id": action_id, "code": code, "message": message, **fields})


def _report_uncertain(report: dict, action: dict, step: str, code: str, message: str,
                      **fields: Any) -> None:
    _mark_uncertain(action, step, code, message)
    _add_error(report, action["action_id"], code, message, **fields)


def _named_exact(rows: Sequence[dict], name: str) -> List[dict]:
    return [row for row in rows if row.get("name") == name]


def _organization_entry_matches(action: dict, row: dict, name: str) -> bool:
    if row.get("name") != name:
        return False
    if action["source_kind"] == "directory":
        return _is_dir(row) and _file_id(row) == action.get("file_id")
    return not _is_dir(row) and _file_id(row) == action.get("file_id")


def _apply_organization_mkdir(report: dict, report_path: Path, client: AListClient,
                              action: dict, previous: Optional[dict]) -> bool:
    was_pending = bool(previous and previous.get("state") == "pending")
    try:
        rows = client.list_all(action["parent_path"], refresh=True)
    except ReconcileError as exc:
        if was_pending:
            _report_uncertain(report, action, previous.get("pending_step", "mkdir"), exc.code, str(exc))
        else:
            _block_action(action, exc.code, str(exc), fatal=True)
            _add_error(report, action["action_id"], exc.code, str(exc))
        _write_journal(report_path, report)
        return True

    named = _named_exact(rows, action["name"])
    if len(named) == 1 and _is_dir(named[0]):
        action["state"] = "done"
        action["pending_step"] = None
        action["recovered_from_pending"] = was_pending
        _add_event(action, "mkdir", "verified_after_restart" if was_pending else "already_exists")
        _write_journal(report_path, report)
        return False
    if named:
        _block_action(action, "mkdir_target_conflict", "target path already exists and is not a directory")
        if was_pending:
            _add_error(report, action["action_id"], "mkdir_target_conflict",
                       "pending directory creation cannot be reconciled safely")
        _write_journal(report_path, report)
        return was_pending
    if was_pending:
        _report_uncertain(report, action, "mkdir", "previous_pending_uncertain",
                          "previous mkdir was not visible; it will not be blindly retried")
        _write_journal(report_path, report)
        return True
    if previous and previous.get("state") == "done":
        _block_action(action, "completed_directory_drifted", "previously created directory is now absent", fatal=True)
        _add_error(report, action["action_id"], "completed_directory_drifted",
                   "previously created directory is now absent")
        _write_journal(report_path, report)
        return True

    action["state"] = "pending"
    action["pending_step"] = "mkdir"
    action["pending_since"] = iso_now()
    _write_journal(report_path, report)
    response_error = None
    try:
        client.make_dir(action["target_path"])
    except ReconcileError as exc:
        response_error = {"code": exc.code, "message": str(exc)}
    try:
        after = client.list_all(action["parent_path"], refresh=True)
    except ReconcileError as exc:
        _add_event(action, "mkdir", "not_verified", api_error=response_error or {"code": exc.code, "message": str(exc)})
        _report_uncertain(report, action, "mkdir", exc.code, str(exc))
        _write_journal(report_path, report)
        return True
    created = _named_exact(after, action["name"])
    if len(created) == 1 and _is_dir(created[0]):
        action["state"] = "done"
        action["pending_step"] = None
        _add_event(action, "mkdir", "verified", api_error=response_error)
        _write_journal(report_path, report)
        return False
    _add_event(action, "mkdir", "not_verified", api_error=response_error)
    _report_uncertain(report, action, "mkdir", "mkdir_not_verified",
                      "directory creation was not confirmed by a refreshed parent listing")
    _write_journal(report_path, report)
    return True


def _apply_organization_move(report: dict, report_path: Path, client: AListClient,
                             action: dict, previous: Optional[dict]) -> bool:
    was_pending = bool(previous and previous.get("state") == "pending")
    current_name = (previous.get("current_name") if was_pending else None) or action["original_name"]
    if previous and previous.get("state") == "done":
        current_name = action["target_name"]

    try:
        source_rows = client.list_all(action["source_dir"], refresh=True)
        target_rows = client.list_all(action["target_dir"], refresh=True)
    except ReconcileError as exc:
        if was_pending:
            _report_uncertain(report, action, previous.get("pending_step", "organize_move"), exc.code, str(exc))
        else:
            _block_action(action, exc.code, str(exc), fatal=True)
            _add_error(report, action["action_id"], exc.code, str(exc))
        _write_journal(report_path, report)
        return True

    source_current = [row for row in source_rows if _organization_entry_matches(action, row, current_name)]
    source_original = [row for row in source_rows
                       if _organization_entry_matches(action, row, action["original_name"])]
    target_entries = [row for row in target_rows
                      if _organization_entry_matches(action, row, action["target_name"])]
    target_named = _children_named(target_rows, action["target_name"])

    if was_pending:
        step = previous.get("pending_step") or "organize_move"
        if step == "organize_rename":
            renamed = [row for row in source_rows
                       if _organization_entry_matches(action, row, action["target_name"])]
            if len(renamed) == 1 and not _named_exact(source_rows, action["original_name"]):
                current_name = action["target_name"]
                action["current_name"] = current_name
                action["state"] = "planned"
                action["pending_step"] = None
                _add_event(action, "organize_rename", "verified_after_restart")
                _write_journal(report_path, report)
                source_current = renamed
            elif len(source_original) == 1 and not _named_exact(source_rows, action["target_name"]):
                _block_action(action, "previous_pending_no_effect",
                              "previous rename was verified unchanged; automatic retry is disabled", fatal=True)
                action["pending_step"] = None
                _add_error(report, action["action_id"], "previous_pending_no_effect",
                           "previous rename was verified unchanged; automatic retry is disabled")
                _write_journal(report_path, report)
                return True
            else:
                _report_uncertain(report, action, step, "previous_pending_uncertain",
                                  "previous rename could not be uniquely reconciled")
                _write_journal(report_path, report)
                return True
        elif step == "organize_move":
            if len(target_entries) == 1 and len(target_named) == 1:
                action["state"] = "done"
                action["pending_step"] = None
                action["recovered_from_pending"] = True
                action["verified_target_path"] = action["target_relative_path"]
                _add_event(action, step, "verified_after_restart")
                _write_journal(report_path, report)
                return False
            if len(source_current) == 1 and not target_named:
                _block_action(action, "previous_pending_no_effect",
                              "previous move was verified unchanged; automatic retry is disabled", fatal=True)
                action["pending_step"] = None
                _add_error(report, action["action_id"], "previous_pending_no_effect",
                           "previous move was verified unchanged; automatic retry is disabled")
                _write_journal(report_path, report)
                return True
            _report_uncertain(report, action, step, "previous_pending_uncertain",
                              "previous move could not be uniquely reconciled")
            _write_journal(report_path, report)
            return True
    elif previous and previous.get("state") == "done":
        if len(target_entries) == 1 and len(target_named) == 1 and not source_current and not source_original:
            action["state"] = "done"
            action["recovered_from_done"] = True
            _write_journal(report_path, report)
            return False
        _block_action(action, "completed_action_drifted", "previously completed move no longer matches cloud state", fatal=True)
        _add_error(report, action["action_id"], "completed_action_drifted",
                   "previously completed move no longer matches cloud state")
        _write_journal(report_path, report)
        return True

    if len(source_current) != 1 or len(_children_named(source_rows, current_name)) != 1:
        _block_action(action, "live_source_mismatch", "source path does not uniquely match its inventoried file/directory", fatal=True)
        _add_error(report, action["action_id"], "live_source_mismatch",
                   "source path does not uniquely match its inventoried file/directory")
        _write_journal(report_path, report)
        return True
    if target_named:
        _block_action(action, "live_target_conflict", "destination name now exists; refusing overwrite")
        _write_journal(report_path, report)
        return False

    if action.get("rename_before_move") and current_name != action["target_name"]:
        if _children_named(source_rows, action["target_name"]):
            _block_action(action, "source_rename_conflict", "stable suffix already exists beside the source")
            _write_journal(report_path, report)
            return False
        action["current_name"] = action["target_name"]
        action["state"] = "pending"
        action["pending_step"] = "organize_rename"
        action["pending_since"] = iso_now()
        _write_journal(report_path, report)
        response_error = None
        try:
            client.rename_file(action["source_path"], action["target_name"])
        except ReconcileError as exc:
            response_error = {"code": exc.code, "message": str(exc)}
        try:
            renamed_rows = client.list_all(action["source_dir"], refresh=True)
        except ReconcileError as exc:
            _report_uncertain(report, action, "organize_rename", exc.code, str(exc))
            _write_journal(report_path, report)
            return True
        renamed = [row for row in renamed_rows
                   if _organization_entry_matches(action, row, action["target_name"])]
        old_name = _named_exact(renamed_rows, action["original_name"])
        _add_event(action, "organize_rename", "verified" if len(renamed) == 1 and not old_name else "not_verified",
                   api_error=response_error)
        if len(renamed) != 1 or old_name:
            if len(source_original) == 1 and not _named_exact(renamed_rows, action["target_name"]):
                _block_action(action, "rename_not_verified",
                              "source rename was verified unchanged; automatic retry is disabled", fatal=True)
                action["pending_step"] = None
                _add_error(report, action["action_id"], "rename_not_verified",
                           "source rename was verified unchanged; automatic retry is disabled")
            else:
                _report_uncertain(report, action, "organize_rename", "rename_not_verified",
                                  "source rename result could not be confirmed")
            _write_journal(report_path, report)
            return True
        current_name = action["target_name"]
        action["state"] = "planned"
        action["pending_step"] = None
        _write_journal(report_path, report)

    # Refresh both sides immediately before the move. A prior directory-creation
    # or rename result is never treated as enough evidence for this write.
    try:
        source_rows = client.list_all(action["source_dir"], refresh=True)
        target_rows = client.list_all(action["target_dir"], refresh=True)
    except ReconcileError as exc:
        _block_action(action, exc.code, str(exc), fatal=True)
        _add_error(report, action["action_id"], exc.code, str(exc))
        _write_journal(report_path, report)
        return True
    source_current = [row for row in source_rows if _organization_entry_matches(action, row, current_name)]
    target_named = _children_named(target_rows, current_name)
    if len(source_current) != 1:
        _block_action(action, "live_source_mismatch", "renamed/current source is not uniquely present", fatal=True)
        _add_error(report, action["action_id"], "live_source_mismatch",
                   "renamed/current source is not uniquely present")
        _write_journal(report_path, report)
        return True
    if target_named:
        _block_action(action, "live_target_conflict", "destination name now exists; refusing overwrite")
        _write_journal(report_path, report)
        return False

    action["current_name"] = current_name
    action["state"] = "pending"
    action["pending_step"] = "organize_move"
    action["pending_since"] = iso_now()
    _write_journal(report_path, report)
    response_error = None
    try:
        client.move_file(action["source_dir"], action["target_dir"], current_name)
    except ReconcileError as exc:
        response_error = {"code": exc.code, "message": str(exc)}
    try:
        after_source = client.list_all(action["source_dir"], refresh=True)
        after_target = client.list_all(action["target_dir"], refresh=True)
    except ReconcileError as exc:
        _report_uncertain(report, action, "organize_move", exc.code, str(exc))
        _write_journal(report_path, report)
        return True
    target_matches = [row for row in after_target
                      if _organization_entry_matches(action, row, action["target_name"])]
    source_matches = [row for row in after_source
                      if _organization_entry_matches(action, row, current_name)]
    target_names = _children_named(after_target, action["target_name"])
    _add_event(action, "organize_move", "verified" if len(target_matches) == 1
               and len(target_names) == 1 and not source_matches else "not_verified",
               api_error=response_error)
    if len(target_matches) == 1 and len(target_names) == 1 and not source_matches:
        action["state"] = "done"
        action["pending_step"] = None
        action["verified_target_path"] = action["target_relative_path"]
        _write_journal(report_path, report)
        return False
    if len(source_matches) == 1 and not target_names:
        _block_action(action, "move_not_verified", "move was verified unchanged; automatic retry is disabled", fatal=True)
        action["pending_step"] = None
        _add_error(report, action["action_id"], "move_not_verified",
                   "move was verified unchanged; automatic retry is disabled")
        _write_journal(report_path, report)
        return True
    _report_uncertain(report, action, "organize_move", "move_not_verified",
                      "move result could not be confirmed",)
    _write_journal(report_path, report)
    return True


def apply_actions(report: dict, report_path: Path, client: AListClient,
                  previous_actions: Optional[dict] = None) -> Tuple[List[dict], bool]:
    previous_actions = previous_actions or {}
    uncertain = False
    completed_actions: Dict[str, str] = {}
    for action in report["actions"]:
        if action.get("state") in ("blocked", "noop"):
            completed_actions[action["action_id"]] = action.get("state")
            continue
        action_id = action["action_id"]
        required_directory = action.get("requires_directory")
        if required_directory and completed_actions.get(required_directory) != "done":
            _block_action(action, "target_directory_unavailable",
                          "required review directory was not verified as created")
            completed_actions[action_id] = "blocked"
            _write_journal(report_path, report)
            continue
        dependency = action.get("requires_verified_video")
        if dependency and completed_actions.get(dependency) not in ("done", "noop"):
            _block_action(action, "video_not_verified",
                          "subtitle move requires its video action to be verified first")
            completed_actions[action_id] = "blocked"
            _write_journal(report_path, report)
            continue
        previous = previous_actions.get(action_id)
        if action.get("kind") == "organize_mkdir":
            uncertain = _apply_organization_mkdir(report, report_path, client, action, previous) or uncertain
            completed_actions[action_id] = action.get("state")
            continue
        if action.get("kind") == "organize_move":
            uncertain = _apply_organization_move(report, report_path, client, action, previous) or uncertain
            completed_actions[action_id] = action.get("state")
            continue
        try:
            source_rows, target_rows = _live_preflight(client, action)
        except ReconcileError as exc:
            if previous and previous.get("state") == "pending":
                action["state"] = "pending"
                action["pending_step"] = previous.get("pending_step") or "unknown"
                action["steps"] = list(previous.get("steps") or [])
            else:
                _block_action(action, exc.code, str(exc), fatal=True)
            _add_error(report, action_id, exc.code, str(exc))
            uncertain = True
            completed_actions[action_id] = action["state"]
            _write_journal(report_path, report)
            continue

        source_named = _children_named(source_rows, action["original_name"])
        source_id = _find_id(source_rows, action["file_id"])
        target_named = _children_named(target_rows, action["original_name"])
        target_id = _find_id(target_rows, action["file_id"])
        final_named = _children_named(target_rows, action["target_name"])
        target_final_id = [row for row in final_named if _file_id(row) == action["file_id"] and not _is_dir(row)]

        # Recover a journaled request from an interrupted prior invocation only
        # from refreshed source/target listings. Never repeat its write blindly.
        if previous and previous.get("state") == "pending":
            pending_step = previous.get("pending_step") or "unknown"
            final_is_exact = len(target_final_id) == 1 and len(final_named) == 1
            original_at_target = [row for row in target_named
                                  if _file_id(row) == action["file_id"] and not _is_dir(row)]
            source_exact = [row for row in source_named
                            if row.get("name") == action["original_name"]
                            and _file_id(row) == action["file_id"] and not _is_dir(row)]
            if (pending_step in ("rename", "final_verify") and final_is_exact
                    and (action["source_dir"] == action["show_path"]
                         or (not source_id and not source_named))):
                action["state"] = "done"
                action["pending_step"] = None
                action["recovered_from_pending"] = True
                action["verified_target_path"] = action["target_relative_path"]
                _add_event(action, pending_step, "verified_after_restart")
                _write_journal(report_path, report)
                completed_actions[action_id] = "done"
                continue
            if (pending_step == "move" and not source_id and not source_named
                    and len(target_named) == 1 and len(original_at_target) == 1):
                _add_event(action, "move", "verified_after_restart")
                action["state"] = "planned"
                action["pending_step"] = None
                action["move_required"] = False
                action["source_path"] = action["show_path"].rstrip("/") + "/" + action["original_name"]
                action["source_dir"] = action["show_path"]
                action["source_dir_relative"] = "/".join(action["target_relative_path"].split("/")[:-1])
                _write_journal(report_path, report)
                try:
                    target_rows = client.list_all(action["show_path"], refresh=True)
                    source_rows = target_rows
                except ReconcileError as exc:
                    _report_uncertain(report, action, "move", exc.code, str(exc))
                    uncertain = True
                    completed_actions[action_id] = "pending"
                    _write_journal(report_path, report)
                    continue
                source_named = _children_named(source_rows, action["original_name"])
                source_id = _find_id(source_rows, action["file_id"])
                target_named = _children_named(target_rows, action["original_name"])
                target_id = _find_id(target_rows, action["file_id"])
                final_named = _children_named(target_rows, action["target_name"])
                target_final_id = [row for row in final_named
                                   if _file_id(row) == action["file_id"] and not _is_dir(row)]
            elif (pending_step == "move" and len(source_exact) == 1
                  and not target_named and not final_named):
                _block_action(action, "previous_pending_no_effect",
                              "previous move was verified unchanged; automatic retry is disabled", fatal=True)
                action["pending_step"] = None
                uncertain = True
                _add_error(report, action_id, "previous_pending_no_effect",
                           "previous move was verified unchanged; automatic retry is disabled")
                _write_journal(report_path, report)
                completed_actions[action_id] = "blocked"
                continue
            elif (pending_step == "rename" and len(original_at_target) == 1 and not final_named):
                _block_action(action, "previous_pending_no_effect",
                              "previous rename was verified unchanged; automatic retry is disabled", fatal=True)
                action["pending_step"] = None
                uncertain = True
                _add_error(report, action_id, "previous_pending_no_effect",
                           "previous rename was verified unchanged; automatic retry is disabled")
                _write_journal(report_path, report)
                completed_actions[action_id] = "blocked"
                continue
            else:
                _report_uncertain(report, action, pending_step, "previous_pending_uncertain",
                                  "previous request could not be reconciled from refreshed source/target listings")
                uncertain = True
                _write_journal(report_path, report)
                completed_actions[action_id] = "pending"
                continue
        elif previous and previous.get("state") == "done":
            source_is_show = action["source_dir"] == action["show_path"]
            old_name_absent = not _children_named(source_rows, action["original_name"])
            if (len(target_final_id) == 1 and len(final_named) == 1
                    and ((not source_id and not source_named) or (source_is_show and old_name_absent))):
                action["state"] = "done"
                action["recovered_from_done"] = True
                _write_journal(report_path, report)
                completed_actions[action_id] = "done"
                continue
            _block_action(action, "completed_action_drifted", "previously completed action no longer matches cloud state", fatal=True)
            uncertain = True
            _write_journal(report_path, report)
            completed_actions[action_id] = "blocked"
            continue

        source_exact = [row for row in source_named if row.get("name") == action["original_name"]
                        and _file_id(row) == action["file_id"] and not _is_dir(row)]
        if len(source_id) != 1 or len(source_exact) != 1:
            _block_action(action, "live_source_mismatch", "fresh listing does not match the inventory source file", fatal=True)
            _add_error(report, action_id, "live_source_mismatch",
                       "fresh listing does not match the inventory source file")
            uncertain = True
            _write_journal(report_path, report)
            completed_actions[action_id] = "blocked"
            continue
        if len(final_named) > 0 and not (len(target_final_id) == 1 and not action["move_required"]):
            _block_action(action, "live_target_conflict", "fresh target listing contains the desired target name", fatal=False)
            _write_journal(report_path, report)
            completed_actions[action_id] = "blocked"
            continue
        if action["move_required"] and _children_named(target_rows, action["original_name"]):
            _block_action(action, "live_move_name_conflict", "fresh target listing contains the original filename", fatal=False)
            _write_journal(report_path, report)
            completed_actions[action_id] = "blocked"
            continue

        current_name = action["original_name"]
        current_path = action["source_path"]
        if action["move_required"]:
            action["state"] = "pending"
            action["pending_step"] = "move"
            action["pending_since"] = iso_now()
            _write_journal(report_path, report)
            response_error = None
            try:
                client.move_file(action["source_dir"], action["show_path"], current_name)
            except ReconcileError as exc:
                response_error = {"code": exc.code, "message": str(exc)}
            try:
                moved, disposition, target_rows = _verify_move(client, action)
            except ReconcileError as exc:
                moved, disposition, target_rows = False, "verification_failed", []
                response_error = response_error or {"code": exc.code, "message": str(exc)}
            _add_event(action, "move", "verified" if moved else "not_verified",
                       disposition=disposition, api_error=response_error)
            if not moved:
                uncertain = True
                if disposition == "unchanged_at_source":
                    _block_action(action, "move_not_verified",
                                  "move was verified unchanged; automatic retry is disabled", fatal=True)
                    action["pending_step"] = None
                    _add_error(report, action_id, "move_not_verified", "move was verified unchanged",
                               disposition=disposition)
                else:
                    _report_uncertain(report, action, "move", "move_not_verified",
                                      "move result could not be confirmed", disposition=disposition)
                completed_actions[action_id] = action["state"]
                _write_journal(report_path, report)
                continue
            action["pending_step"] = None
            current_path = action["show_path"].rstrip("/") + "/" + current_name
            action["state"] = "planned"
            action["verified_move"] = True
            _write_journal(report_path, report)

        if action["rename_required"] and current_name != action["target_name"]:
            # Refresh the exact destination immediately before rename; a name
            # collision blocks the operation and no overwrite flag is ever used.
            try:
                target_rows = client.list_all(action["show_path"], refresh=True)
            except ReconcileError as exc:
                _block_action(action, exc.code, str(exc), fatal=True)
                _add_error(report, action_id, exc.code, str(exc))
                uncertain = True
                _write_journal(report_path, report)
                completed_actions[action_id] = "blocked"
                continue
            conflicts = _children_named(target_rows, action["target_name"])
            if conflicts:
                _block_action(action, "rename_target_conflict", "rename target already exists", fatal=False)
                _write_journal(report_path, report)
                completed_actions[action_id] = "blocked"
                continue
            action["state"] = "pending"
            action["pending_step"] = "rename"
            action["pending_since"] = iso_now()
            _write_journal(report_path, report)
            response_error = None
            try:
                client.rename_file(current_path, action["target_name"])
            except ReconcileError as exc:
                response_error = {"code": exc.code, "message": str(exc)}
            try:
                renamed, disposition, target_rows = _verify_rename(client, action, current_name)
            except ReconcileError as exc:
                renamed, disposition, target_rows = False, "verification_failed", []
                response_error = response_error or {"code": exc.code, "message": str(exc)}
            _add_event(action, "rename", "verified" if renamed else "not_verified",
                       disposition=disposition, api_error=response_error)
            if not renamed:
                uncertain = True
                if disposition == "unchanged_at_source":
                    _block_action(action, "rename_not_verified",
                                  "rename was verified unchanged; automatic retry is disabled", fatal=True)
                    action["pending_step"] = None
                    _add_error(report, action_id, "rename_not_verified",
                               "rename was verified unchanged; automatic retry is disabled")
                else:
                    _report_uncertain(report, action, "rename", "rename_not_verified",
                                      "rename result could not be confirmed")
                completed_actions[action_id] = action["state"]
                _write_journal(report_path, report)
                continue
            action["pending_step"] = None
            current_path = action["show_path"].rstrip("/") + "/" + action["target_name"]

        # A final independent live read determines whether the operation may be
        # recorded complete. Existing empty source folders are intentionally kept.
        try:
            final_rows = client.list_all(action["show_path"], refresh=True)
            final_entries = _children_named(final_rows, action["target_name"])
            final_ids = [row for row in final_entries if _file_id(row) == action["file_id"] and not _is_dir(row)]
        except ReconcileError as exc:
            _report_uncertain(report, action, "final_verify", exc.code, str(exc))
            uncertain = True
            _write_journal(report_path, report)
            completed_actions[action_id] = "pending"
            continue
        if len(final_ids) == 1 and len(final_entries) == 1:
            action["state"] = "done"
            action["pending_step"] = None
            action["verified_target_path"] = action["target_relative_path"]
            _add_event(action, "final_verify", "verified")
        else:
            _report_uncertain(report, action, "final_verify", "final_verify_failed",
                              "final target name and file_id were not uniquely confirmed")
            uncertain = True
        completed_actions[action_id] = action["state"]
        _write_journal(report_path, report)

    # Record newly emptied source directories for review, never remove them.
    emptied = set()
    for action in report["actions"]:
        if action.get("state") != "done" or not action.get("move_required"):
            continue
        relative = action["source_dir_relative"]
        try:
            rows = client.list_all(action["source_dir"], refresh=True)
        except ReconcileError:
            continue
        if not rows:
            emptied.add(relative)
    report["emptied_source_directories"] = [{"relative_path": path, "state": "retained_for_review", "action": "none"}
                                            for path in sorted(emptied)]
    return report["actions"], uncertain


def _previous_actions(report_path: Path) -> dict:
    if not report_path.exists():
        return {}
    try:
        payload = read_json(report_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReconcileError("existing reconcile journal is unreadable; refusing to overwrite it",
                             code="journal_unreadable") from exc
    return {str(item.get("action_id")): item for item in payload.get("actions", [])
            if isinstance(item, dict) and item.get("action_id")}


def run_reconcile(config_path: Path, inventory_path: Optional[Path] = None,
                  report_path: Optional[Path] = None, *, apply: bool = False,
                  client: Optional[AListClient] = None,
                  now: Optional[dt.datetime] = None) -> Tuple[dict, int]:
    try:
        raw_cfg = read_json(config_path)
        cfg = normalize_config(raw_cfg)
        inventory_file = inventory_path or Path(cfg["inventory_path"])
        report_file = report_path or Path(cfg["report_path"])
        if not inventory_file.is_absolute() or not report_file.is_absolute():
            raise ReconcileError("inventory and report paths must be absolute", code="invalid_config")
        if (inventory_file.resolve() == report_file.resolve()
                or config_path.resolve() == report_file.resolve()):
            raise ReconcileError("report path must not overwrite the runtime config or inventory",
                                 code="invalid_config")
        inventory = read_json(inventory_file)
        freshness = validate_inventory(inventory, cfg, now=now)
        previous_actions = _previous_actions(report_file)
        if not apply and any(item.get("state") == "pending" for item in previous_actions.values()):
            raise ReconcileError("existing report has pending cloud writes; preserving journal and refusing dry-run overwrite",
                                 code="journal_pending")
        plan = build_plan(inventory, cfg)
    except (OSError, ValueError, json.JSONDecodeError, ReconcileError) as exc:
        issue = {"code": exc.code if isinstance(exc, ReconcileError) else "input_error", "message": str(exc)}
        fatal = {"schema_version": SCHEMA_VERSION, "mode": "apply" if apply else "dry-run",
                 "started_at": iso_now(), "updated_at": iso_now(), "complete": False, "blocked": False,
                 "errors": [issue], "actions": [], "unresolved": [], "unassigned": [],
                 "empty_directories": [], "emptied_source_directories": [], "out_of_range": [],
                 "organization_enabled": False}
        failure_path = Path(str(report_path) + ".failure.json") if report_path else Path(str(config_path) + ".failure.json")
        fatal["report_path"] = str(failure_path)
        atomic_write_json(failure_path, fatal)
        return fatal, 1

    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry-run",
        "started_at": iso_now(),
        "updated_at": iso_now(),
        "report_path": str(report_file),
        "complete": True,
        "applied": False,
        "inventory_path": str(inventory_file),
        "inventory_generated_at": freshness["generated_at"],
        "inventory_age_seconds": freshness["age_seconds"],
        "cloud_root": freshness["cloud_root"],
        "actions": plan["actions"],
        "unresolved": plan["unresolved"],
        "unassigned": plan["unassigned"],
        "out_of_range": plan["out_of_range"],
        "organization_enabled": plan["organization_enabled"],
        "empty_directories": plan["empty_directories"],
        "emptied_source_directories": [],
        "errors": [],
    }
    if not apply:
        _sync_out_of_range_states(report)
        report["blocked_count"] = sum(1 for item in report["actions"] if item.get("state") == "blocked") + len(report["unresolved"])
        report["blocked"] = report["blocked_count"] > 0
        report["action_counts"] = _action_counts(report["actions"])
        _write_journal(report_file, report)
        return report, 0

    report["applied"] = True
    _write_journal(report_file, report)
    active_client = client or AListClient(cfg)
    _, uncertain = apply_actions(report, report_file, active_client, previous_actions)
    _sync_out_of_range_states(report)
    report["blocked_count"] = sum(1 for item in report["actions"] if item.get("state") == "blocked") + len(report["unresolved"])
    report["blocked"] = report["blocked_count"] > 0
    report["action_counts"] = _action_counts(report["actions"])
    report["complete"] = not uncertain and not bool(report["errors"])
    _write_journal(report_file, report)
    return report, 2 if uncertain or report["errors"] else 0


def _action_counts(actions: Sequence[dict]) -> dict:
    result = {"planned": 0, "noop": 0, "done": 0, "blocked": 0, "pending": 0}
    for item in actions:
        state = item.get("state")
        if state in result:
            result[state] += 1
    return result


def _sync_out_of_range_states(report: dict) -> None:
    actions = {row.get("source_relative_path"): row for row in report.get("actions", [])
               if row.get("role") == "out_of_range_video"}
    for row in report.get("out_of_range", []):
        action = actions.get(row.get("source_relative_path"))
        if action:
            row["state"] = action.get("state", row.get("state", "planned"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="runtime JSON configuration")
    parser.add_argument("--inventory", type=Path, help="complete fresh inventory JSON")
    parser.add_argument("--report", type=Path, help="atomic reconcile plan/journal path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="write a local plan only (default)")
    mode.add_argument("--apply", action="store_true", help="apply only reviewed, safe AList move/rename actions")
    args = parser.parse_args(argv)
    result, code = run_reconcile(args.config, args.inventory, args.report, apply=args.apply)
    print(json.dumps({"report": result.get("report_path", str(args.report) if args.report else None),
                      "complete": result.get("complete"), "blocked": result.get("blocked"),
                      "blocked_count": result.get("blocked_count"), "action_counts": result.get("action_counts"),
                      "errors": result.get("errors", [])}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
