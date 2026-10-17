#!/usr/bin/env python3
"""Create Jellyfin STRMs from a complete, fresh, read-only AList inventory.

The cloud is read-only. Apply mode changes local STRM/NFO indexes only and
archives superseded local indexes outside the Jellyfin library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import re
import sqlite3
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import requests


DEFAULT_CONFIG = Path("/etc/bmguangya/config.json")
DEFAULT_INVENTORY = Path("/var/lib/bmguangya/library.json")
DEFAULT_REPORT = Path("/var/lib/bmguangya/strm-sync-report.json")
CATEGORIES = ("在看", "未标记", "想看", "看过")
VIDEO_SUFFIXES = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m2ts", ".ts",
    ".webm", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob", ".m4v",
}
EPISODE_KEY = re.compile(r"(?:EP\d{1,3}(?:\.\d+)?|SP\d{1,3}|OVA\d{1,3}|OAD\d{1,3}|NCOP\d{0,2}|NCED\d{0,2})\Z", re.I)
NFO_MARKER = b"<!-- bmguangya-managed -->"


class SyncError(RuntimeError):
    """A fail-closed configuration, inventory, or local reconciliation error."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError("cannot read JSON input: {}".format(path)) from exc
    if not isinstance(value, dict):
        raise SyncError("JSON input root must be an object: {}".format(path))
    return value


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def freshness_status(inventory: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    freshness = inventory.get("freshness")
    if not isinstance(freshness, dict):
        raise SyncError("inventory has no freshness metadata")
    generated = parse_timestamp(inventory.get("generated_at") or inventory.get("updated_at"))
    if generated is None:
        raise SyncError("inventory has no valid generated_at timestamp")
    try:
        ttl = int(freshness.get("ttl_seconds"))
    except (TypeError, ValueError) as exc:
        raise SyncError("inventory has invalid freshness TTL") from exc
    if ttl <= 0:
        raise SyncError("inventory freshness TTL must be positive")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    age = int((current - generated).total_seconds())
    if age < -120:
        raise SyncError("inventory timestamp is unexpectedly in the future")
    age = max(0, age)
    state = "fresh" if age <= ttl else "stale"
    if inventory.get("complete") is not True:
        state = "incomplete"
    if state != "fresh":
        raise SyncError("inventory is {}, age={}s, ttl={}s".format(state, age, ttl))
    errors = inventory.get("errors")
    if errors:
        raise SyncError("inventory reports collection errors; refusing to apply")
    return {"state": state, "age_seconds": age, "ttl_seconds": ttl}


def clean_relative_path(value: Any, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise SyncError("{} must be a POSIX relative path".format(field))
    if value == "" and allow_empty:
        return ()
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise SyncError("{} is not a safe POSIX relative path".format(field))
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SyncError("{} contains an empty or traversal component".format(field))
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise SyncError("{} contains a control character".format(field))
    return tuple(parts)


def clean_component(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise SyncError("{} must be text".format(field))
    value = value.strip()
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise SyncError("{} is not a safe path component".format(field))
    if any(ord(char) < 32 for char in value):
        raise SyncError("{} contains a control character".format(field))
    return value


def normalize_root(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "\x00" in value:
        raise SyncError("{} must be an absolute POSIX path".format(field))
    parts = tuple(part for part in value.split("/") if part)
    if not parts or any(part in (".", "..") for part in parts):
        raise SyncError("{} contains an invalid path component".format(field))
    return parts


def validate_inventory(inventory: dict[str, Any], config: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    freshness = freshness_status(inventory, now=now)
    source = inventory.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("cloud_root"), str):
        raise SyncError("inventory has no source.cloud_root")
    cfg_root = normalize_root((config.get("alist") or {}).get("root"), field="alist.root")
    inventory_root = normalize_root(source["cloud_root"], field="inventory.source.cloud_root")
    if cfg_root != inventory_root:
        raise SyncError("inventory cloud root does not match configured AList root")
    if source.get("cloud_refresh") is not True:
        raise SyncError("inventory does not confirm a live AList refresh")
    tree = inventory.get("tree")
    if not isinstance(tree, list):
        raise SyncError("inventory tree must be an array")
    categories = [item.get("category") for item in tree if isinstance(item, dict)]
    if set(categories) != set(CATEGORIES) or len(categories) != len(CATEGORIES):
        raise SyncError("inventory tree must contain each of the four library categories exactly once")
    return freshness


def _walk_file_nodes(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "directory":
            children = node.get("children")
            if isinstance(children, list):
                yield from _walk_file_nodes(children)
        elif node.get("type") == "file":
            yield node


def validate_source_path(show: dict[str, Any]) -> tuple[str, ...] | None:
    source_path = show.get("source_path")
    if source_path is None:
        if int(show.get("count") or 0) > 0:
            raise SyncError("show has video count but no source_path")
        return None
    parts = clean_relative_path(source_path, field="show.source_path")
    display_path = show.get("path")
    if display_path is not None:
        path_parts = clean_relative_path(display_path, field="show.path")
        if path_parts != parts:
            raise SyncError("show.path and show.source_path disagree")
    return parts


def canonical_episode_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    if not EPISODE_KEY.fullmatch(key):
        return None
    match = re.fullmatch(r"(EP|SP|OVA|OAD)(\d{1,3})(\.\d+)?", key)
    if match:
        prefix, digits, fraction = match.groups()
        key = prefix + digits.zfill(2) + (fraction or "")
    return key


def _config_value(cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    return cfg.get(key, default)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    alist = config.get("alist")
    jellyfin = config.get("jellyfin")
    guangya = config.get("guangya")
    strm = config.get("strm", {})
    if not isinstance(alist, dict) or not isinstance(jellyfin, dict):
        raise SyncError("config requires alist and jellyfin objects")
    if not isinstance(strm, dict):
        raise SyncError("strm config must be an object")
    include_review_directories = strm.get("include_review_directories", False)
    if not isinstance(include_review_directories, bool):
        raise SyncError("strm.include_review_directories must be a boolean")
    public_url = str(alist.get("public_url") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(public_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SyncError("alist.public_url must be an http(s) URL without credentials, query, or fragment")
    username = str(alist.get("username") or "")
    password = str(alist.get("password") or "")
    if not username or not password:
        raise SyncError("alist.username and alist.password are required for STRM stream URLs")
    mount = clean_component(alist.get("stream_mount") or "", field="alist.stream_mount")
    root_parts = normalize_root(alist.get("root"), field="alist.root")
    if root_parts[0] != mount:
        raise SyncError("alist.root must be below the configured WebDAV stream mount")
    root_suffix = root_parts[1:]
    root_dir = (guangya or {}).get("root_dir") if isinstance(guangya, dict) else None
    if root_dir and root_suffix and root_suffix[-1] != clean_component(root_dir, field="guangya.root_dir"):
        raise SyncError("guangya.root_dir does not match the configured AList root")
    try:
        timeout = float(_config_value(config, "network_timeout_seconds", jellyfin.get("timeout_seconds", 30)))
    except (TypeError, ValueError) as exc:
        raise SyncError("network timeout must be numeric") from exc
    if timeout <= 0 or timeout > 600:
        raise SyncError("network timeout must be between 0 and 600 seconds")
    jf_url = str(jellyfin.get("base_url") or "").strip().rstrip("/")
    jf_parsed = urllib.parse.urlsplit(jf_url)
    if jf_parsed.scheme not in ("http", "https") or not jf_parsed.netloc or jf_parsed.username or jf_parsed.password or jf_parsed.query or jf_parsed.fragment:
        raise SyncError("jellyfin.base_url must be an http(s) URL without credentials, query, or fragment")
    media_root = Path(str(_config_value(config, "media_root", "/opt/jellyfin/media/Anime"))).expanduser()
    archive_root = Path(str(_config_value(config, "archive_root", "/var/lib/bmguangya/archive"))).expanduser()
    report_path = Path(str(_config_value(config, "report_path", DEFAULT_REPORT))).expanduser()
    if not media_root.is_absolute() or not archive_root.is_absolute() or not report_path.is_absolute():
        raise SyncError("media_root, archive_root, and report_path must be absolute paths")
    media_abs = Path(os.path.abspath(media_root))
    archive_abs = Path(os.path.abspath(archive_root))
    if media_abs == archive_abs or media_abs in archive_abs.parents or archive_abs in media_abs.parents:
        raise SyncError("archive_root must be outside the Jellyfin media root")
    return {
        "alist": alist,
        "public_url": public_url,
        "stream_mount": mount,
        "root_suffix": tuple(root_suffix),
        "username": username,
        "password": password,
        "network_timeout": timeout,
        "jellyfin": jellyfin,
        "jellyfin_url": jf_url,
        "media_root": media_abs,
        "archive_root": archive_abs,
        "report_path": Path(os.path.abspath(report_path)),
        "include_review_directories": include_review_directories,
    }


def authenticated_strm_url(config_data: dict[str, Any], relative_path: str) -> str:
    rel_parts = clean_relative_path(relative_path, field="episode.relative_path")
    base = urllib.parse.urlsplit(config_data["public_url"])
    host = base.netloc
    userinfo = "{}:{}@".format(
        urllib.parse.quote(config_data["username"], safe=""),
        urllib.parse.quote(config_data["password"], safe=""),
    )
    route_parts = (config_data["stream_mount"], *config_data["root_suffix"], *rel_parts)
    route = "/dav/" + "/".join(urllib.parse.quote(part, safe="") for part in route_parts)
    return urllib.parse.urlunsplit((base.scheme, userinfo + host, route, "", ""))


def _review_directory_root(category: str, source_parts: tuple[str, ...], *, include: bool) -> str | None:
    if (include or category != "未标记" or len(source_parts) < 2
            or source_parts[0] != "未标记" or not source_parts[1].startswith("待核对-")):
        return None
    return PurePosixPath(source_parts[0], source_parts[1]).as_posix()


def _review_video_paths(show: dict[str, Any], source_parts: tuple[str, ...]) -> list[str]:
    videos = show.get("vids")
    if not isinstance(videos, list):
        raise SyncError("show.vids must be an array for review directory {}".format(
            "/".join(source_parts)))
    paths = set()
    for video in videos:
        if not isinstance(video, dict) or not isinstance(video.get("relative_path"), str):
            raise SyncError("review directory has a video without an exact relative_path")
        video_parts = clean_relative_path(video["relative_path"], field="show.vids.relative_path")
        if video_parts[:len(source_parts)] != source_parts or len(video_parts) <= len(source_parts):
            raise SyncError("review video path is outside its inventory show: {}".format(
                video["relative_path"]))
        if PurePosixPath(video_parts[-1]).suffix.lower() not in VIDEO_SUFFIXES:
            raise SyncError("inventory vid has an unsupported media extension")
        paths.add("/".join(video_parts))
    return sorted(paths)


def build_show_plan(inventory: dict[str, Any], config_data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    planned: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    seen_sources: dict[tuple[str, ...], str] = {}
    seen_targets: dict[str, str] = {}
    tree = inventory.get("tree") or []
    for section in tree:
        category = section["category"]
        shows = section.get("shows")
        if not isinstance(shows, list):
            raise SyncError("inventory category '{}' has no show list".format(category))
        if category not in CATEGORIES:
            raise SyncError("inventory contains an unsupported library category")
        for show in shows:
            if not isinstance(show, dict):
                raise SyncError("inventory contains a non-object show entry")
            source_parts = validate_source_path(show)
            source_text = "/".join(source_parts) if source_parts is not None else None
            raw_subject_id = str(show.get("subject_id") or "")
            valid_subject_id = bool(re.fullmatch(r"\d+", raw_subject_id))
            ambiguous = show.get("ambiguous") is True
            is_matched = show.get("matched") is True and valid_subject_id and not ambiguous
            title = show.get("name") or show.get("cloud_name") or (source_parts[-1] if source_parts else raw_subject_id)
            if not isinstance(title, str):
                raise SyncError("inventory show title is not text")
            # Unmatched entries use the real cloud directory name, never an inferred Bangumi title.
            if source_parts is not None and show.get("matched") is not True:
                title = show.get("cloud_name") or source_parts[-1]
            title = clean_component(title, field="show.title")

            if source_parts is None:
                if show.get("matched") is True and not valid_subject_id:
                    unresolved.append({"subject": title, "episode": "-", "reason": "matched_subject_has_invalid_id"})
                # Keep Bangumi-only stubs in the plan so their IDs protect any old
                # local NFO/STRM from being mistaken for a removed subject.
                planned.append({
                    "category": category,
                    "title": title,
                    "subject_id": None,
                    "inventory_subject_id": raw_subject_id if valid_subject_id else None,
                    "source_path": None,
                    "local_show": None,
                    "episodes": [],
                    "nfo_title": None,
                    "skip_apply": True,
                    "cleanup_safe": False,
                })
                continue

            review_root = _review_directory_root(
                category, source_parts, include=config_data["include_review_directories"],
            )
            if review_root is not None:
                if source_parts in seen_sources:
                    conflicts.append({"source_path": source_text or "", "reason": "duplicate_inventory_source_path"})
                    continue
                seen_sources[source_parts] = raw_subject_id or str(show.get("name") or source_text)
                planned.append({
                    "category": category,
                    "title": title,
                    "subject_id": None,
                    "inventory_subject_id": raw_subject_id if valid_subject_id else None,
                    "source_path": source_text,
                    "local_show": review_root,
                    "episodes": [],
                    "nfo_title": None,
                    "skip_apply": True,
                    "cleanup_safe": False,
                    "review_excluded": True,
                    "review_root": review_root,
                    "excluded_review_files": _review_video_paths(show, source_parts),
                })
                continue

            if source_parts in seen_sources:
                conflicts.append({"source_path": source_text or "", "reason": "duplicate_inventory_source_path"})
                continue
            seen_sources[source_parts] = raw_subject_id or str(show.get("name") or source_text)
            local_show = PurePosixPath(category, title)
            target_prefix = local_show.as_posix()
            prior = seen_targets.get(target_prefix)
            if prior:
                conflicts.append({"local_show": target_prefix, "reason": "duplicate_local_title"})
                continue
            seen_targets[target_prefix] = source_text or ""

            unresolved_before = len(unresolved)
            if ambiguous:
                unresolved.append({"subject": title, "episode": "-", "reason": "ambiguous_show_preserved"})
                planned.append({
                    "category": category,
                    "title": title,
                    "subject_id": None,
                    "inventory_subject_id": raw_subject_id if valid_subject_id else None,
                    "source_path": source_text,
                    "local_show": target_prefix,
                    "episodes": [],
                    "nfo_title": None,
                    "skip_apply": True,
                    "cleanup_safe": False,
                })
                continue
            if show.get("coverage_unknown") is True:
                unresolved.append({"subject": title, "episode": "-", "reason": "bangumi_episode_coverage_unknown"})
                planned.append({
                    "category": category,
                    "title": title,
                    "subject_id": None,
                    "inventory_subject_id": raw_subject_id if valid_subject_id else None,
                    "source_path": source_text,
                    "local_show": target_prefix,
                    "episodes": [],
                    "nfo_title": None,
                    "skip_apply": True,
                    "cleanup_safe": False,
                })
                continue
            if show.get("incomplete") is True:
                unresolved.append({"subject": title, "episode": "-", "reason": "incomplete_show_inventory"})

            videos = show.get("vids")
            if not isinstance(videos, list):
                raise SyncError("show.vids must be an array for {}".format(source_text))
            indexed_files: set[str] = set()
            for video in videos:
                if not isinstance(video, dict) or not isinstance(video.get("relative_path"), str):
                    raise SyncError("show has a video without an exact relative_path: {}".format(source_text))
                video_parts = clean_relative_path(video["relative_path"], field="show.vids.relative_path")
                if video_parts[:len(source_parts)] != source_parts or len(video_parts) <= len(source_parts):
                    raise SyncError("video path is outside its inventory show: {}".format(video["relative_path"]))
                if PurePosixPath(video_parts[-1]).suffix.lower() not in VIDEO_SUFFIXES:
                    raise SyncError("inventory vid has an unsupported media extension")
                indexed_files.add("/".join(video_parts))

            episodes = show.get("episodes")
            if not isinstance(episodes, list):
                episodes = []
            expected_count = show.get("expected_eps_count")
            if not isinstance(expected_count, int) or isinstance(expected_count, bool):
                expected_count = show.get("total_eps")
            coverage_known = (
                show.get("coverage_unknown") is not True
                and isinstance(expected_count, int)
                and not isinstance(expected_count, bool)
                and expected_count >= 0
            )
            duplicate_keys = {canonical_episode_key(key) or str(key).upper() for key in (show.get("duplicate_eps") or [])}
            keyed: dict[str, list[tuple[dict[str, Any], tuple[str, ...]]]] = {}
            unresolved_extra_files: set[str] = set()
            for episode in episodes:
                if not isinstance(episode, dict) or episode.get("present") is not True:
                    continue
                raw_key = episode.get("episode_key")
                key = canonical_episode_key(raw_key)
                raw_files = episode.get("present_files")
                file_paths: list[tuple[str, ...]] = []
                if isinstance(raw_files, list) and raw_files:
                    for item in raw_files:
                        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                            unresolved.append({"subject": title, "episode": str(raw_key or "?"), "reason": "present_file_missing_path"})
                            file_paths = []
                            break
                        file_paths.append(clean_relative_path(item["relative_path"], field="episode.present_files.relative_path"))
                elif isinstance(episode.get("relative_path"), str):
                    file_paths = [clean_relative_path(episode["relative_path"], field="episode.relative_path")]
                if key is None:
                    unresolved.append({"subject": title, "episode": str(raw_key or episode.get("file_name") or "?"), "reason": "unrecognized_episode_key"})
                    continue
                if key.upper() in duplicate_keys or len(file_paths) > 1:
                    unresolved.append({"subject": title, "episode": key, "reason": "duplicate_episode_mapping"})
                    continue
                if not file_paths:
                    unresolved.append({"subject": title, "episode": key, "reason": "present_episode_missing_path"})
                    continue
                file_parts = file_paths[0]
                if file_parts[:len(source_parts)] != source_parts or len(file_parts) <= len(source_parts):
                    raise SyncError("episode path is outside its inventory show: {}".format(episode.get("relative_path")))
                rel_text = "/".join(file_parts)
                if rel_text not in indexed_files:
                    raise SyncError("episode path is not present in show.vids: {}".format(rel_text))
                if PurePosixPath(file_parts[-1]).suffix.lower() not in VIDEO_SUFFIXES:
                    raise SyncError("episode path does not point to a supported video file")
                episode_id = episode.get("episode_id")
                has_stable_episode_id = episode_id is not None and str(episode_id).strip() != ""
                if (
                    is_matched
                    and coverage_known
                    and not has_stable_episode_id
                    and episode.get("reason") == "episode_not_in_bangumi"
                    and re.fullmatch(r"EP\d{1,3}(?:\.\d+)?", key)
                ):
                    episode_number = float(key[2:])
                    reason = "out_of_range_episode" if episode_number > expected_count else "ordinary_extra_without_bangumi_id"
                    unresolved.append({"subject": title, "episode": key, "reason": reason})
                    unresolved_extra_files.add(rel_text)
                    continue
                keyed.setdefault(key, []).append((episode, file_parts))

            episode_items: list[dict[str, Any]] = []
            mapped_files: set[str] = set()
            for key, rows in keyed.items():
                if len(rows) != 1:
                    unresolved.append({"subject": title, "episode": key, "reason": "duplicate_episode_key"})
                    continue
                episode, file_parts = rows[0]
                file_text = "/".join(file_parts)
                if file_text in mapped_files:
                    unresolved.append({"subject": title, "episode": key, "reason": "duplicate_present_file_mapping"})
                    continue
                episode_items.append({
                    "episode_key": key,
                    "relative_path": file_text,
                    "local_path": str(local_show / (key + ".strm")),
                    "episode_id": str(episode.get("episode_id")) if episode.get("episode_id") is not None else None,
                })
                mapped_files.add(file_text)

            for video_path in sorted(indexed_files - mapped_files):
                if video_path in unresolved_extra_files:
                    continue
                unresolved.append({
                    "subject": title,
                    "episode": PurePosixPath(video_path).name,
                    "reason": "unmapped_cloud_video",
                })

            if show.get("matched") is True and not valid_subject_id:
                unresolved.append({"subject": title, "episode": "-", "reason": "matched_subject_has_invalid_id"})
            if show.get("matched") is True and ambiguous:
                unresolved.append({"subject": title, "episode": "-", "reason": "matched_subject_is_ambiguous"})
            cleanup_safe = (
                len(unresolved) == unresolved_before
                and mapped_files == indexed_files
                and show.get("incomplete") is not True
                and show.get("coverage_unknown") is not True
                and ambiguous is not True
            )
            planned.append({
                "category": category,
                "title": title,
                "subject_id": raw_subject_id if is_matched else None,
                "inventory_subject_id": raw_subject_id if valid_subject_id else None,
                "source_path": source_text,
                "local_show": target_prefix,
                "episodes": sorted(episode_items, key=lambda item: item["episode_key"]),
                "nfo_title": title if is_matched else None,
                "skip_apply": False,
                "cleanup_safe": cleanup_safe,
            })
    if conflicts:
        raise SyncError("inventory contains duplicate source or local title paths; refusing to apply")
    return planned, unresolved, conflicts


def _local_path(root: Path, relative: str, *, field: str) -> Path:
    parts = clean_relative_path(relative, field=field)
    _assert_no_symlink_ancestors(root, field=field)
    path = root
    for index, part in enumerate(parts):
        path = path / part
        if path.is_symlink():
            raise SyncError("{} contains a symlink component".format(field))
        if path.exists() and index < len(parts) - 1 and not path.is_dir():
            raise SyncError("{} parent is not a directory".format(field))
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise SyncError("{} escapes its configured root".format(field)) from exc
    return path


def _assert_no_symlink_ancestors(path: Path, *, field: str) -> None:
    current = Path(os.path.abspath(path))
    while True:
        if current.is_symlink():
            raise SyncError("{} path contains a symlink component".format(field))
        if current == current.parent:
            return
        current = current.parent


def _target_file_rows(planned: list[dict[str, Any]], cfg: dict[str, Any]) -> list[tuple[Path, bytes, str]]:
    output: list[tuple[Path, bytes, str]] = []
    claimed: set[Path] = set()
    for show in planned:
        if show.get("skip_apply") is True:
            continue
        show_dir = _local_path(cfg["media_root"], show["local_show"], field="media target")
        if show["nfo_title"] is not None:
            nfo_path = _local_path(cfg["media_root"], show["local_show"] + "/tvshow.nfo", field="media target")
            if nfo_path in claimed:
                raise SyncError("duplicate generated NFO target")
            if nfo_path.exists():
                existing_id = _bangumi_nfo_subject_id(nfo_path)
                if existing_id is None:
                    raise SyncError("refusing to overwrite an unmanaged tvshow.nfo")
                if existing_id != show["subject_id"]:
                    raise SyncError("refusing to replace a different Bangumi identity at the same local path")
            claimed.add(nfo_path)
            output.append((nfo_path, make_show_nfo(show["subject_id"], show["nfo_title"]), "nfo"))
        for episode in show["episodes"]:
            target = _local_path(cfg["media_root"], episode["local_path"], field="media target")
            if target in claimed:
                raise SyncError("duplicate generated STRM target")
            claimed.add(target)
            body = authenticated_strm_url(cfg, episode["relative_path"]).encode("utf-8") + b"\n"
            output.append((target, body, "strm"))
    return output


def make_show_nfo(subject_id: str, title: str) -> bytes:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "sorttitle").text = title
    ET.SubElement(root, "uniqueid", {"type": "bangumi", "default": "true"}).text = subject_id
    ET.SubElement(root, "id").text = subject_id
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n" + NFO_MARKER + b"\n"


def _ensure_parent(path: Path) -> None:
    current = path.parent
    while True:
        if current.is_symlink():
            raise SyncError("local target parent must not contain symlinks")
        if current.exists() and not current.is_dir():
            raise SyncError("local target parent is not a directory")
        if current == current.parent:
            break
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)


def _stage_file(target: Path, body: bytes) -> Path:
    _ensure_parent(target)
    if target.is_symlink():
        raise SyncError("refusing to replace a symlink: {}".format(target))
    if target.exists() and not target.is_file():
        raise SyncError("generated target already exists and is not a regular file")
    fd, temp_name = tempfile.mkstemp(prefix=".bmguangya-", suffix=".tmp", dir=str(target.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        return temp
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _archive_candidates(media_root: Path, archive_root: Path, desired: set[Path],
                        cleanup_roots: set[Path]) -> list[tuple[Path, Path]]:
    _assert_no_symlink_ancestors(media_root, field="media_root")
    _assert_no_symlink_ancestors(archive_root, field="archive_root")
    if archive_root == media_root or media_root in archive_root.parents or archive_root in media_root.parents:
        raise SyncError("archive_root must stay outside media_root")
    if not media_root.exists():
        return []
    files: set[Path] = set()
    for cleanup_root in cleanup_roots:
        try:
            relative_root = cleanup_root.relative_to(media_root)
        except ValueError as exc:
            raise SyncError("cleanup root escapes the Jellyfin media root") from exc
        if len(relative_root.parts) < 2:
            raise SyncError("cleanup root must identify one show directory")
        _assert_no_symlink_ancestors(cleanup_root, field="cleanup_root")
        if not cleanup_root.exists():
            continue
        if not cleanup_root.is_dir():
            raise SyncError("cleanup root is not a directory")
        for directory, dirnames, filenames in os.walk(cleanup_root, followlinks=False):
            base = Path(directory)
            dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
            for filename in filenames:
                path = base / filename
                if path.suffix.lower() not in (".strm", ".nfo") or path in desired:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise SyncError("refusing to archive a symlink or non-file index: {}".format(path))
                if path.suffix.lower() == ".strm" or _is_bangumi_nfo(path):
                    files.add(path)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    rows = []
    for source in sorted(files):
        relative = source.relative_to(media_root)
        destination = archive_root / run_id / relative
        if destination.exists():
            raise SyncError("archive destination collision")
        rows.append((source, destination))
    return rows


def _is_bangumi_nfo(path: Path) -> bool:
    """Only archive NFO files owned by this syncer; preserve other providers' metadata."""
    return _bangumi_nfo_subject_id(path) is not None


def _bangumi_nfo_subject_id(path: Path) -> str | None:
    try:
        payload = path.read_bytes()
        if NFO_MARKER not in payload:
            return None
        root = ET.fromstring(payload)
    except (OSError, ET.ParseError):
        return None
    ids = {
        (element.text or "").strip()
        for element in root.iter()
        if element.tag == "uniqueid"
        and str(element.attrib.get("type", "")).casefold() == "bangumi"
        and re.fullmatch(r"\d+", (element.text or "").strip())
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _cleanup_roots(planned: list[dict[str, Any]], cfg: dict[str, Any]) -> set[Path]:
    media_root = cfg["media_root"]
    inventory_subject_ids = {
        show["inventory_subject_id"] for show in planned if show.get("inventory_subject_id")
    }
    current_by_id = {
        show["subject_id"]: media_root / show["local_show"]
        for show in planned
        if show.get("subject_id") and show.get("nfo_title") is not None
    }
    cleanup_safe_ids = {
        show["subject_id"] for show in planned
        if show.get("subject_id") and show.get("cleanup_safe") is True
    }
    protected_roots = {
        media_root / show["local_show"]
        for show in planned
        if show.get("local_show") and show.get("cleanup_safe") is not True
    }
    protected_categories = {
        show["category"] for show in planned
        if show.get("local_show") and show.get("cleanup_safe") is not True
    }
    roots: set[Path] = {
        media_root / show["local_show"]
        for show in planned
        if show.get("skip_apply") is not True and show.get("cleanup_safe") is True and show.get("local_show")
    }
    roots.update(
        media_root / show["review_root"]
        for show in planned
        if show.get("review_excluded") is True and show.get("review_root")
    )

    # A generated Bangumi NFO is the only reliable key for an old category/title
    # location. Archive it only after the current show has a complete, unique map;
    # IDs still present as unmatched/ambiguous stubs are protected.
    _assert_no_symlink_ancestors(media_root, field="media_root")
    if not media_root.exists():
        return roots
    for directory, dirnames, filenames in os.walk(media_root, followlinks=False):
        base = Path(directory)
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        if "tvshow.nfo" not in filenames:
            continue
        nfo = base / "tvshow.nfo"
        if nfo.is_symlink() or not nfo.is_file():
            continue
        subject_id = _bangumi_nfo_subject_id(nfo)
        if subject_id is None:
            continue
        relative = base.relative_to(media_root)
        if len(relative.parts) < 2:
            continue
        current = current_by_id.get(subject_id)
        if subject_id not in inventory_subject_ids:
            # Do not infer that a local index is obsolete while its category has
            # a cloud show whose episode mapping could not be safely rebuilt.
            # The explicit legacy _Removed bucket is safe to clear from the live
            # library because it is already outside the four user categories.
            if relative.parts[0] == "_Removed" or (
                base not in protected_roots and relative.parts[0] not in protected_categories
            ):
                roots.add(base)
        elif subject_id in cleanup_safe_ids and current is not None and current != base:
            roots.add(base)  # Exact ID and complete mapping confirm a category/title move.
    return roots


def _copy_to_archive(source: Path, destination: Path) -> None:
    _ensure_parent(destination)
    if source.is_symlink() or not source.is_file():
        raise SyncError("refusing to move a non-regular index into the archive")
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    fd, temp_name = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=str(destination.parent))
    temp = Path(temp_name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp, destination)
        source.unlink()
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _prune_empty_index_directories(paths: Iterable[Path], media_root: Path) -> None:
    """Remove only now-empty show/nested folders vacated by archived indexes."""
    for source in paths:
        directory = source.parent
        while directory != media_root:
            try:
                relative = directory.relative_to(media_root)
            except ValueError:
                break
            if len(relative.parts) <= 1:
                break  # Keep the four category roots intact.
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def apply_plan(planned: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    output_files = _target_file_rows(planned, cfg)
    desired = {target for target, _body, _kind in output_files}
    staged: list[tuple[Path, Path, str]] = []
    try:
        for target, body, kind in output_files:
            staged.append((target, _stage_file(target, body), kind))
        cleanup_roots = _cleanup_roots(planned, cfg)
        old_indexes = _archive_candidates(cfg["media_root"], cfg["archive_root"], desired, cleanup_roots)
        archived = []
        for source, destination in old_indexes:
            _copy_to_archive(source, destination)
            archived.append({"from": str(source), "to": str(destination)})
        _prune_empty_index_directories((source for source, _destination in old_indexes), cfg["media_root"])
        created = updated = unchanged = 0
        desired_by_target = {(target, kind): body for target, body, kind in output_files}
        for target, temp, kind in staged:
            existed = target.exists()
            # A same-path update is atomic; unchanged files keep their prior inode.
            current = None
            if existed:
                try:
                    current = target.read_bytes()
                except OSError as exc:
                    raise SyncError("cannot read existing local index file") from exc
            desired_body = desired_by_target[(target, kind)]
            if current == desired_body:
                temp.unlink(missing_ok=True)
                unchanged += 1
            else:
                os.replace(temp, target)
                if existed:
                    updated += 1
                else:
                    created += 1
        return {"created": created, "updated": updated, "unchanged": unchanged,
                "archived": archived, "archive_count": len(archived), "files_written": len(output_files)}
    finally:
        for _target, temp, _kind in staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def jellyfin_token(config_data: dict[str, Any]) -> str:
    settings = config_data["jellyfin"]
    token = str(settings.get("token") or "")
    if token:
        return token
    database = Path(str(settings.get("db_path") or "/opt/jellyfin/config/data/jellyfin.db"))
    if not database.is_absolute():
        raise SyncError("jellyfin.db_path must be absolute")
    if not database.is_file():
        raise SyncError("Jellyfin token is not configured and its database is unavailable")
    uri = "file:{}?mode=ro".format(urllib.parse.quote(str(database), safe="/:\\"))
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        username = str(settings.get("username") or "root")
        user = connection.execute("SELECT Id FROM Users WHERE Username = ?", (username,)).fetchone()
        if not user:
            raise SyncError("configured Jellyfin user was not found")
        row = connection.execute(
            "SELECT AccessToken FROM Devices WHERE UserId = ? AND AccessToken IS NOT NULL "
            "ORDER BY DateLastActivity DESC LIMIT 1", (user[0],),
        ).fetchone()
        if not row or not row[0]:
            raise SyncError("no active Jellyfin session token is available")
        return str(row[0])
    except sqlite3.Error as exc:
        raise SyncError("cannot read a Jellyfin session token from the read-only database") from exc
    finally:
        if connection is not None:
            connection.close()


def refresh_jellyfin(config_data: dict[str, Any]) -> dict[str, Any]:
    token = jellyfin_token(config_data)
    url = config_data["jellyfin_url"] + "/Library/Refresh"
    try:
        response = requests.post(
            url,
            headers={"X-Emby-Token": token, "Accept": "application/json"},
            timeout=config_data["network_timeout"],
        )
    except requests.RequestException as exc:
        # Never include request headers, response bodies, or token text in the report.
        raise SyncError("Jellyfin library refresh POST failed: {}".format(type(exc).__name__)) from exc
    if not 200 <= response.status_code < 300:
        raise SyncError("Jellyfin library refresh POST returned HTTP {}".format(response.status_code))
    return {"requested": True, "status_code": int(response.status_code), "verified": True}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, temp_name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def run(config_path: Path, inventory_path: Path, report_path: Path, *, apply: bool,
        now: dt.datetime | None = None) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "started_at": (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry-run",
        "inventory_path": str(inventory_path),
        "applied": False,
    }
    try:
        config = read_json(config_path)
        cfg = validate_config(config)
        inventory = read_json(inventory_path)
        report["inventory_generated_at"] = inventory.get("generated_at") or inventory.get("updated_at")
        report["freshness"] = validate_inventory(inventory, config, now=now)
        planned, unresolved, conflicts = build_show_plan(inventory, cfg)
        excluded_review_groups: dict[str, dict[str, Any]] = {}
        skipped_review_files: set[str] = set()
        for show in planned:
            if show.get("review_excluded") is not True:
                continue
            root = str(show["review_root"])
            group = excluded_review_groups.setdefault(root, {
                "path": root, "inventory_show_paths": set(), "video_files": set(),
            })
            group["inventory_show_paths"].add(str(show["source_path"]))
            group["video_files"].update(show["excluded_review_files"])
            skipped_review_files.update(show["excluded_review_files"])
        report.update({
            "show_count": len(planned),
            "planned_strm_count": sum(len(show["episodes"]) for show in planned),
            "planned_nfo_count": sum(1 for show in planned if show["nfo_title"] is not None),
            "skipped_review_files": len(skipped_review_files),
            "excluded_review_paths": [
                {"path": root, "inventory_show_paths": sorted(group["inventory_show_paths"]),
                 "video_file_count": len(group["video_files"])}
                for root, group in sorted(excluded_review_groups.items())
            ],
            "unresolved": unresolved,
            "conflicts": conflicts,
            "planned_shows": [
                {"category": show["category"], "title": show["title"], "subject_id": show["subject_id"],
                 "local_show": show["local_show"], "episodes": [ep["episode_key"] for ep in show["episodes"]],
                 "excluded_review_directory": show.get("review_excluded", False)}
                for show in planned
            ],
        })
        if apply:
            report["apply_stats"] = apply_plan(planned, cfg)
            # A successful disk write is reported separately from Jellyfin's verified refresh POST.
            report["applied"] = True
            report["jellyfin_refresh"] = refresh_jellyfin(cfg)
        report["status"] = "ok"
        code = 0
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc) if isinstance(exc, SyncError) else "{}: operation failed".format(type(exc).__name__)
        code = 2
    atomic_write_json(report_path, report)
    return report, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="build and save a plan only; this is also the default")
    mode.add_argument("--apply", action="store_true", help="write local STRM/NFO files, archive stale local indexes, then POST Jellyfin refresh")
    args = parser.parse_args(argv)
    report, code = run(args.config, args.inventory, args.report, apply=bool(args.apply))
    summary = {key: report.get(key) for key in (
        "status", "mode", "applied", "show_count", "planned_strm_count", "planned_nfo_count",
        "skipped_review_files", "excluded_review_paths", "unresolved", "error", "jellyfin_refresh",
    ) if key in report}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
