#!/usr/bin/env python3
"""Bangumi → Mikan → GuangYa offline download.

Primary path:
- Read tracked Bangumi anime collections
- Resolve Mikan RSS candidates with the same scoring/filter rules as the qB flow
- Submit selected torrents to GuangYa offline download directly
- Use clean Bangumi-aligned Season/Episode naming without release-group noise

KEY RULE: Each anime gets AT MOST one file per episode.
  - For movies/OVAs (single file): submit only the single best version
  - For TV shows: submit one best version per episode
  - Collection packs/batches are rejected; GuangYa cannot split them safely
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(1, "/opt/anime-stack/scripts")
sys.path.insert(2, "/opt/guangya-webdav")

if TYPE_CHECKING:
    from app.guangya_client import GuangYaClient  # type: ignore

from app.log import logger  # type: ignore
from bangumi_mikan_qb_sync import (  # type: ignore
    BangumiClient,
    COLLECTION_TYPE_LABELS,
    MikanClient,
    extract_episode_span,
    extract_subgroup,
    item_matches_subject_partition,
    load_state,
    now,
    parse_episode_number,
    score_item,
    write_json,
)
from import_completed_anime_to_library import infer_season, normalize_series_title  # type: ignore


CONFIG_FILE = "/opt/guangya-webdav/config/config.json"
SYNC_CONFIG = "/opt/anime-stack/config/bangumi-sync.json"
DEFAULT_STATE_PATH = "/opt/anime-stack/state/bangumi-sync-state.json"
DEFAULT_ROOT_DIR = "Anime"
PROGRESS_FILE = "/opt/anime-stack/state/sync-progress.json"
CLOUD_CREATE_TASK_URL = "https://api.guangyapan.com/cloudcollection/v1/create_task"
CLOUD_LIST_TASK_URL = "https://api.guangyapan.com/cloudcollection/v1/list_task"
CLOUD_RESOLVE_TORRENT_URL = "https://api.guangyapan.com/cloudcollection/v1/resolve_torrent"
COLLECTION_DIR_LABELS = {
    1: "想看",
    2: "看过",
    3: "在看",
}


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_progress(status: str, current: int = 0, total: int = 0,
                   submitted: int = 0, max_tasks: int = 0,
                   current_show: str = "", extra: Optional[dict] = None) -> None:
    """Write live sync progress for the monitor dashboard to poll."""
    payload = {
        "status": status,
        "current_subject": current,
        "total_subjects": total,
        "submitted_count": submitted,
        "max_tasks": max_tasks,
        "current_show": current_show,
        "updated_at": now(),
    }
    if extra:
        payload.update(extra)
    try:
        write_json(PROGRESS_FILE, payload)
    except Exception:
        pass


def sanitize_component(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' -_')
    return text[:120] or 'unknown'


def detect_season_index(*texts: str) -> int:
    patterns = [
        r'第\s*([0-9]{1,2})\s*[期季部篇]',
        r'第\s*([一二三四五六七八九十两]{1,3})\s*[期季部篇]',
        r'\bseason\s*([0-9]{1,2})\b',
        r'\bs\s*([0-9]{1,2})\b',
    ]
    cn_map = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '两': 2,
    }
    for text in texts:
        if not text:
            continue
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if not match:
                continue
            raw = match.group(1)
            if raw.isdigit():
                return max(1, int(raw))
            if raw == '十':
                return 10
            if len(raw) == 2 and raw.startswith('十') and raw[1] in cn_map:
                return 10 + cn_map[raw[1]]
            if len(raw) == 2 and raw.endswith('十') and raw[0] in cn_map:
                return cn_map[raw[0]] * 10
            if raw in cn_map:
                return cn_map[raw]
    return max(1, int(infer_season(*texts) or 1))


def normalize_bangumi_display_name(display_name: str) -> str:
    # Preserve the exact Bangumi display name because it often already encodes
    # season/part/movie distinctions (e.g. 第三季, 第2期, 前篇). Stripping those
    # makes different subjects collide in the same GuangYa folder.
    return sanitize_component(display_name or '')


def build_subject_path(display_name: str, collection_type: int) -> Tuple[str, str]:
    category_dir = COLLECTION_DIR_LABELS.get(int(collection_type or 0), COLLECTION_TYPE_LABELS.get(int(collection_type or 0), str(collection_type or "unknown")))
    show_dir = normalize_bangumi_display_name(display_name)
    return sanitize_component(category_dir), show_dir


def extract_special_label(*texts: str) -> str:
    joined = " ".join(t or "" for t in texts)
    upper = joined.upper()
    for token in ("NCOP", "NCED"):
        match = re.search(rf"(?<![A-Z0-9]){token}\s*([0-9]{{1,2}})?(?![A-Z0-9])", upper)
        if match:
            num = match.group(1)
            return f"{token}{int(num):02d}" if num else token
    for token in ("SP", "OVA", "OAD"):
        match = re.search(rf"(?<![A-Z0-9]){token}\s*[-_ ]?([0-9]{{1,2}})?(?![A-Z0-9])", upper)
        if match:
            num = match.group(1)
            return f"{token}{int(num or 1):02d}"
    return ""


def extract_chinese_episode_number(*texts: str) -> Optional[int]:
    for text in texts:
        if not text:
            continue
        for pattern in [
            r"第\s*(\d{1,3})\s*[话話集]",
            r"[【\[(（]\s*第\s*(\d{1,3})\s*[话話集]\s*[】\])）]",
        ]:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
    return None


def extract_strict_episode_number(*texts: str) -> Optional[int]:
    """Extract the actual episode number without confusing season/resolution.

    Avoid old false positives like S01E06 -> 01 or 1080p -> 108.
    Handles common Mikan forms: S01E06, 第06话, [06v2], - 06, 【06】.
    """
    patterns = [
        r"\bS\d{1,2}E(\d{1,3})(?:v\d+)?\b",
        r"第\s*(\d{1,3})\s*[话話集]",
        r"[\[【(（]\s*(\d{1,3})(?:v\d+)?\s*[\]】)）]",
        r"(?:^|[\s_\-])(?:EP|E)\s*(\d{1,3})(?:v\d+)?(?:\b|[^0-9])",
        r"\s-\s*(\d{1,3})(?:v\d+)?(?:\s|\[|【|$)",
    ]
    for text in texts:
        if not text:
            continue
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if not match:
                continue
            ep = int(match.group(1))
            if 0 < ep < 200:
                return ep
    return None


def extract_episode_label(source_title: str, resolved_file_name: str) -> str:
    """Extract episode/special label for filename.

    Returns:
      - "01" for single episode
      - "SP01" / "OVA01" / "OAD01" for specials
      - "NCOP" / "NCED" for creditless OP/ED
      - "01-12" for episode range (filtered before submission)
      - "" if no episode info found
    """
    special = extract_special_label(source_title, resolved_file_name)
    if special:
        return special

    # Prefer strict single-episode patterns before generic span parsing, because
    # old parsers may read S01E06 as episode 01.
    ep = extract_strict_episode_number(source_title, resolved_file_name)
    if ep is not None:
        return f"{ep:02d}"

    span = extract_episode_span(source_title)
    if span:
        start, end = span
        if start == end:
            return f"{start:02d}"
        return f"{start:02d}-{end:02d}"

    ep = parse_episode_number(source_title) or parse_episode_number(resolved_file_name)
    if ep is not None and 0 < int(ep) < 200:
        return f"{int(ep):02d}"

    return ""


def build_target_names(display_name: str, source_title: str, resolved_file_name: str) -> Tuple[str, str]:
    folder = normalize_bangumi_display_name(display_name)

    suffix = Path(resolved_file_name or source_title).suffix.lower()
    if not suffix or suffix == '.torrent':
        suffix = '.mkv'  # default for offline tasks where real suffix unknown
    ep_label = extract_episode_label(source_title, resolved_file_name)
    if ep_label:
        filename = f"{ep_label}{suffix}"
    else:
        # Movie/OVA/single file with no episode number → use "01" as default
        filename = f"01{suffix}"
    return folder, filename


def is_single_episode_candidate(title: str) -> bool:
    """Return True only for resources that should create one playable file.

    GuangYa offline-download cannot split batch torrents for us. Reject multi-episode
    ranges and explicit batch/complete-pack releases so they do not become
    ``01-12.mkv`` folders/files beside normal episodes. Movie/OVA titles with no
    parsed episode number are still allowed unless they contain batch keywords.
    """
    text = title or ""
    lower = text.lower()
    # More aggressive keyword filtering for collections/bundles
    if any(keyword in lower for keyword in [
        "合集", "全集", "全话", "全卷", "全季", "整季", "打包",
        "batch", "complete", "complete batch", "season batch",
        "vol.", "box", "pack",
    ]):
        return False

    # Some Mikan titles wrap ranges in Chinese brackets, e.g. 【第01-99话】.
    # The shared extract_episode_span() may parse those as the first episode only,
    # so reject explicit multi-episode ranges here before trusting the parser.
    explicit_range_patterns = [
        r"第\s*\d{1,3}\s*[-~～—–]\s*\d{1,3}\s*[话話集]",
        # [01-13Fin], [01-13 END], 【第01-99话】, (01~12)
        r"[\[【(（]\s*(?:第\s*)?\d{1,3}(?:v\d{1,2})?\s*[-~～—–]\s*\d{1,3}(?:v\d{1,2})?\s*(?:[A-Za-z0-9]{1,6})?\s*(?:[话話集])?\s*[\]】)）]",
        r"\b(?:ep|e)?\s*\d{1,3}(?:v\d{1,2})?\s*[-~～—–]\s*\d{1,3}(?:v\d{1,2})?\s*(?:fin|end)?\b",
        r"\d{1,3}(?:v\d{1,2})?\s*[-~～—–]\s*\d{1,3}(?:v\d{1,2})?\s*(?:话|話|集|ep|fin|end)",
        r"(?:sp|ova|oad|特典)\s*\d{1,3}\s*[-~～—–]\s*\d{1,3}",
    ]
    for pattern in explicit_range_patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        nums = [int(x) for x in re.findall(r"\d{1,3}", match.group(0))[:2]]
        if len(nums) == 2 and nums[0] != nums[1]:
            return False

    span = extract_episode_span(text)
    if span and span[0] != span[1]:
        return False
    return True


def build_existing_target_index(state: dict) -> set:
    index = set()
    for task in (state.get("cloud_tasks") or {}).values():
        if not isinstance(task, dict):
            continue
        target_dir = (task.get("target_dir") or "").strip("/")
        target_name = (task.get("target_name") or "").strip("/")
        if target_dir and target_name:
            index.add(f"{target_dir}/{target_name}")
    return index


def plan_subject_sync_actions(tracked_subjects: Dict[str, dict], subject_locations: Dict[str, dict], removed_subject_ids: List[str]) -> List[dict]:
    actions: List[dict] = []
    removed = set(str(x) for x in removed_subject_ids)

    for subject_id in sorted(removed):
        location = subject_locations.get(subject_id)
        if not location or not location.get("folder_id"):
            continue
        actions.append({
            "action": "delete",
            "subject_id": subject_id,
            "title": location.get("title") or location.get("show_dir") or subject_id,
            "category_dir": location.get("category_dir"),
            "show_dir": location.get("show_dir"),
            "folder_id": location.get("folder_id"),
        })

    for subject_id, tracked in sorted(tracked_subjects.items()):
        location = subject_locations.get(subject_id)
        if not location or not location.get("folder_id"):
            continue
        target_category, target_show_dir = build_subject_path(tracked.get("title") or tracked.get("name_cn") or tracked.get("name") or subject_id, int(tracked.get("collection_type") or 0))
        if location.get("category_dir") != target_category:
            actions.append({
                "action": "move",
                "subject_id": subject_id,
                "title": tracked.get("title") or target_show_dir,
                "from_category": location.get("category_dir"),
                "to_category": target_category,
                "show_dir": target_show_dir,
                "folder_id": location.get("folder_id"),
                "collection_type": int(tracked.get("collection_type") or 0),
            })
        elif location.get("show_dir") != target_show_dir:
            actions.append({
                "action": "rename",
                "subject_id": subject_id,
                "title": tracked.get("title") or target_show_dir,
                "category_dir": target_category,
                "old_show_dir": location.get("show_dir"),
                "show_dir": target_show_dir,
                "folder_id": location.get("folder_id"),
                "collection_type": int(tracked.get("collection_type") or 0),
            })
    return actions


def build_create_task_payload(
    resource_url: str,
    parent_id: str,
    target_name: str,
    file_indexes: Optional[List[int]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "url": resource_url,
        "parentId": parent_id,
        "newName": target_name,
    }
    if file_indexes:
        payload["fileIndexes"] = [int(x) for x in file_indexes]
    return payload


def init_client(config_path: str = CONFIG_FILE) -> "GuangYaClient":
    from app.guangya_client import GuangYaClient  # type: ignore

    config = read_json(config_path)

    client = GuangYaClient(
        access_token=config["access_token"],
        refresh_token=config["refresh_token"],
        client_id=config.get("client_id"),
        device_id=config.get("device_id"),
    )

    def on_token_refresh(new_access: str, new_refresh: str) -> None:
        config["access_token"] = new_access
        config["refresh_token"] = new_refresh
        write_json(config_path, config)

    client._on_token_refresh = on_token_refresh
    return client


def get_list_items(result: Dict[str, Any]) -> List[dict]:
    return ((result or {}).get("data") or {}).get("list") or []


def get_root_dir_id(client: GuangYaClient, root_name: str) -> str:
    root_items = get_list_items(client.get_file_list(parent_id="", page_size=200))
    for item in root_items:
        if item.get("fileName") == root_name and int(item.get("resType") or 0) == 2:
            return str(item["fileId"])
    created = client.create_dir("", root_name, fail_if_exist=False)
    file_id = ((created.get("data") or {}).get("fileId"))
    if not file_id:
        raise RuntimeError(f"failed to create root dir: {root_name}: {created}")
    return str(file_id)


def ensure_child_dir(client: GuangYaClient, parent_id: str, dir_name: str) -> str:
    items = get_list_items(client.get_file_list(parent_id=parent_id, page_size=500))
    for item in items:
        if item.get("fileName") == dir_name and int(item.get("resType") or 0) == 2:
            return str(item["fileId"])
    created = client.create_dir(parent_id, dir_name, fail_if_exist=False)
    file_id = ((created.get("data") or {}).get("fileId"))
    if not file_id:
        raise RuntimeError(f"failed to create dir {dir_name!r} under {parent_id}: {created}")
    return str(file_id)


def canonical_target_name(name: str) -> str:
    """Normalize GuangYa duplicate suffixes like 01(1).mkv back to 01.mkv."""
    return re.sub(r"\(\d+\)(?=(?:\.[^./]+)?$)", "", (name or "").strip())


def get_remote_folder_name_index(
    client: GuangYaClient,
    folder_id: str,
    cache: Dict[str, set],
) -> set:
    """Return canonical names already present in a GuangYa folder.

    This complements state-based de-duplication: if files were created before
    state tracking existed, or were manually moved into the folder, GuangYa would
    otherwise auto-rename a new task to 01(1).mkv.
    """
    if folder_id in cache:
        return cache[folder_id]
    names = set()
    page = 0
    while True:
        result = client.get_file_list(parent_id=folder_id, page_size=500, page=page)
        items = get_list_items(result)
        if not items:
            break
        for item in items:
            file_name = item.get("fileName") or ""
            if file_name:
                names.add(canonical_target_name(file_name))
        if len(items) < 500:
            break
        page += 1
    cache[folder_id] = names
    return names


def apply_subject_sync_actions(client: Optional[GuangYaClient], root_dir_id: str, actions: List[dict], state: dict, dry_run: bool) -> List[dict]:
    applied: List[dict] = []
    subject_locations = state.setdefault("subject_locations", {})

    for action in actions:
        subject_id = str(action["subject_id"])
        op = action["action"]
        record = dict(action)
        record["dry_run"] = dry_run

        if dry_run or client is None:
            if op == "delete":
                subject_locations.pop(subject_id, None)
            elif op in {"move", "rename"}:
                location = subject_locations.get(subject_id, {}).copy()
                if op == "move":
                    location["collection_type"] = action.get("collection_type")
                    location["category_dir"] = action.get("to_category")
                    location["show_dir"] = action.get("show_dir")
                else:
                    location["show_dir"] = action.get("show_dir")
                subject_locations[subject_id] = location
            applied.append(record)
            continue

        if op == "delete":
            client.delete_file([action["folder_id"]])
            subject_locations.pop(subject_id, None)
        elif op == "move":
            target_category_id = ensure_child_dir(client, root_dir_id, action["to_category"])
            client.move_file([action["folder_id"]], target_category_id)
            location = subject_locations.get(subject_id, {}).copy()
            location["collection_type"] = action.get("collection_type")
            location["category_dir"] = action.get("to_category")
            location["show_dir"] = action.get("show_dir")
            subject_locations[subject_id] = location
        elif op == "rename":
            client.rename(action["folder_id"], action["show_dir"])
            location = subject_locations.get(subject_id, {}).copy()
            location["show_dir"] = action.get("show_dir")
            subject_locations[subject_id] = location
        applied.append(record)

    return applied


def resolve_torrent(client: GuangYaClient, torrent_url: str) -> Dict[str, Any]:
    content = requests.get(torrent_url, timeout=30).content
    headers = client._session.headers.copy()
    headers.update(client._get_auth_headers())
    headers.pop("Content-Type", None)
    response = requests.post(
        CLOUD_RESOLVE_TORRENT_URL,
        headers=headers,
        files={"torrent": ("mikan.torrent", content, "application/x-bittorrent")},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def create_cloud_task(client: GuangYaClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    return client._request("POST", CLOUD_CREATE_TASK_URL, data=payload)


VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}


def choose_single_video_file_index(bt_info: dict, source_title: str, target_name: str) -> Optional[List[int]]:
    # Choose exactly one video subfile from a resolved torrent. GuangYa may
    # otherwise add every file in a multi-file torrent, so a 200-task run can
    # consume ~1000 cloud-add quota. Return None for single-file torrents,
    # a one-item list for safe multi-file torrents, or [] when ambiguous/unsafe.
    subfiles = bt_info.get("subfiles") or []
    if not subfiles:
        return None

    target_label = Path(target_name).stem
    video_files = []
    for sub in subfiles:
        name = sub.get("fileName") or ""
        if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        idx = sub.get("fileIndex")
        if idx is None:
            continue
        video_files.append(sub)

    if len(video_files) == 1:
        return [int(video_files[0]["fileIndex"])]

    matching = []
    for sub in video_files:
        name = sub.get("fileName") or ""
        label = extract_episode_label(name, name)
        if not label:
            special = extract_special_label(name)
            strict_ep = extract_strict_episode_number(name)
            if special:
                label = special
            elif strict_ep is not None:
                label = f"{strict_ep:02d}"
            else:
                tail_match = re.search(r"(?:^|[\s._-])(\d{1,3})(?=\.[^.]+$)", name)
                if tail_match:
                    label = f"{int(tail_match.group(1)):02d}"
        if label and label == target_label:
            matching.append(sub)

    if len(matching) == 1:
        return [int(matching[0]["fileIndex"])]
    return []


def refresh_cloud_tasks(client: GuangYaClient, state: dict) -> None:
    cloud_tasks = state.get("cloud_tasks") or {}
    pending = {
        guid: task
        for guid, task in cloud_tasks.items()
        if task.get("task_id") and task.get("status") in {"submitted", "running"}
    }
    if not pending:
        return

    task_ids = [task["task_id"] for task in pending.values()]
    details = client._request("POST", CLOUD_LIST_TASK_URL, data={"taskIds": task_ids})
    by_id = {str(item.get("taskId")): item for item in get_list_items(details)}
    for guid, task in pending.items():
        detail = by_id.get(str(task["task_id"]))
        if not detail:
            continue
        status = int(detail.get("status") or 0)
        task["remote_status"] = status
        task["remote_file_name"] = detail.get("fileName")
        task["exist"] = bool(detail.get("exist"))
        if status == 1:
            task["status"] = "submitted"
        elif status == 2:
            task["status"] = "completed"
        elif status == 3:
            task["status"] = "failed"
        elif status == 5:
            task["status"] = "running"
        else:
            task["status"] = f"status_{status}"


def should_skip_watched(row: dict, item: dict) -> bool:
    from bangumi_mikan_qb_sync import extract_episode_span as _ees  # type: ignore

    watched_ep = int(row.get("ep_status") or 0)
    episode_span = _ees(item["title"])
    if episode_span is None or watched_ep <= 0:
        return False
    _, end_ep = episode_span
    return end_ep <= watched_ep


def extract_release_version(*texts: str) -> int:
    """Extract release revision like v2/v3 from torrent titles.

    No explicit vN means version 1. This lets v2 replace v1 for the same
    episode instead of downloading both.
    """
    max_version = 1
    for text in texts:
        if not text:
            continue
        for match in re.finditer(r"(?<![A-Za-z0-9])v\s*(\d{1,2})(?![A-Za-z0-9])", text, flags=re.I):
            max_version = max(max_version, int(match.group(1)))
        # Common compact forms in episode labels: 05v2, [05v2]
        for match in re.finditer(r"(?<!\d)\d{1,3}\s*v\s*(\d{1,2})(?![A-Za-z0-9])", text, flags=re.I):
            max_version = max(max_version, int(match.group(1)))
    return max_version




def episode_selection_sort_key(item: dict) -> tuple:
    key = str(item.get("episode_key") or "")
    m = re.match(r"ep-(\d{3})$", key)
    if m:
        return (2, int(m.group(1)), str(item.get("title") or ""))
    m = re.match(r"range-(\d{3})-(\d{3})$", key)
    if m:
        return (1, int(m.group(2)), str(item.get("title") or ""))
    return (0, 0, str(item.get("title") or ""))


def apply_episode_limit(chosen_items: List[dict], cfg: dict, collection_type: int) -> List[dict]:
    raw_limit = int(cfg.get("max_episodes_per_subject_per_run", cfg.get("daily_plan_episodes_per_subject", 1)) or 0)
    if raw_limit <= 0 or len(chosen_items) <= raw_limit:
        return chosen_items
    mode = str(cfg.get("episode_selection_mode") or "latest").lower()
    reverse = mode != "oldest"
    ordered = sorted(chosen_items, key=episode_selection_sort_key, reverse=reverse)
    limited = ordered[:raw_limit]
    return sorted(limited, key=episode_selection_sort_key)

def pick_best_per_episode(
    rss_items: List[dict],
    filters: dict,
    locked_subgroup: Optional[str],
) -> List[dict]:
    """Select exactly one best item per episode key from RSS items.

    Unlike choose_best_items which may return items from multiple subgroups,
    this function:
    1. Scores all items with the locked subgroup
    2. Groups by episode key
    3. Returns only one item per episode
    4. If an episode has v2/v3, chooses the highest release version and rejects v1

    If locked_subgroup produces no results, falls back to no lock.
    """
    grouped: Dict[str, dict] = {}

    for item in rss_items:
        ok, sc, reasons = score_item(item["title"], filters, locked_subgroup=locked_subgroup)
        if not ok:
            continue
        if not is_single_episode_candidate(item["title"]):
            continue

        # Determine episode key with the same strict rules used for final
        # filenames. This prevents S01E06 and 06v2 from being grouped as ep-001.
        special_key = extract_special_label(item["title"])
        strict_ep = extract_strict_episode_number(item["title"])
        span = extract_episode_span(item["title"])
        ep = parse_episode_number(item["title"])
        if special_key:
            ep_key = f"special-{special_key}"
        elif strict_ep is not None:
            ep_key = f"ep-{strict_ep:03d}"
        elif span:
            start, end = span
            if start == end:
                ep_key = f"ep-{start:03d}"
            else:
                ep_key = f"range-{start:03d}-{end:03d}"
        elif ep is not None:
            ep_key = f"ep-{int(ep):03d}"
        else:
            # Movie/OVA/single file — use a generic key so only ONE is kept
            ep_key = "single"

        cand = dict(item)
        cand["score"] = sc
        cand["release_version"] = extract_release_version(item["title"])
        cand["reasons"] = reasons
        cand["matched_subgroup"] = extract_subgroup(item["title"], filters)
        cand["episode_key"] = ep_key

        prev = grouped.get(ep_key)
        if prev is None:
            grouped[ep_key] = cand
            continue
        # Highest release revision wins first: v2/v3 replaces v1 for the same
        # episode. Only compare score when release version is equal.
        if cand["release_version"] > int(prev.get("release_version") or 1):
            grouped[ep_key] = cand
        elif cand["release_version"] == int(prev.get("release_version") or 1) and cand["score"] > prev["score"]:
            grouped[ep_key] = cand

    # If locked subgroup yielded nothing, retry without lock
    if not grouped and locked_subgroup:
        return pick_best_per_episode(rss_items, filters, locked_subgroup=None)

    return sorted(grouped.values(), key=lambda x: (-x["score"], x["title"]))


def process_collections(cfg: dict, client: Optional[GuangYaClient], dry_run: bool) -> dict:
    state_path = cfg.get("state_path", DEFAULT_STATE_PATH)
    raw_limit = int(cfg.get("max_new_cloud_tasks_per_run", 0) or 0)
    max_new_tasks_per_run: Optional[int] = raw_limit if raw_limit > 0 else None
    raw_subject_limit = int(cfg.get("max_download_subjects_per_run", 0) or 0)
    max_download_subjects_per_run: Optional[int] = raw_subject_limit if raw_subject_limit > 0 else None
    download_subject_ids_cfg = cfg.get("download_subject_ids") or []
    download_subject_ids = {str(x) for x in download_subject_ids_cfg if str(x).strip()}
    skip_watched = bool(cfg.get("skip_watched", True))
    persisted_state = load_state(state_path)
    state = copy.deepcopy(persisted_state) if dry_run else persisted_state
    state.setdefault("seen_guids", [])
    state.setdefault("subject_to_mikan", {})
    state.setdefault("subject_subgroups", {})
    state.setdefault("tracked_subjects", {})
    state.setdefault("removed_subject_ids", [])
    state.setdefault("cloud_tasks", {})
    state.setdefault("subject_locations", {})
    seen = set(state["seen_guids"])
    existing_targets = build_existing_target_index(state)
    remote_folder_name_cache: Dict[str, set] = {}

    bangumi = BangumiClient(cfg["bangumi"])
    mikan = MikanClient(cfg.get("mikan", {}))
    tracked_collection_types = [int(x) for x in cfg.get("tracked_collection_types", [1, 2, 3])]
    download_collection_type_order = [int(x) for x in cfg.get("download_collection_types", [3, 1, 2])]
    download_collection_types = set(download_collection_type_order)
    collections = bangumi.get_combined_anime_collections(tracked_collection_types)
    # Priority download order matters: e.g. [3, 1, 2] means spend the daily
    # quota on Bangumi "doing" first, then wish/collect only if quota remains.
    priority_rank = {ctype: idx for idx, ctype in enumerate(download_collection_type_order)}
    collections = sorted(
        collections,
        key=lambda row: (
            priority_rank.get(int(row.get("collection_type") or row.get("type") or 0), 999),
            str(row.get("updated_at") or ""),
            str(row.get("subject_id") or ((row.get("subject") or {}).get("id")) or ""),
        ),
    )

    type_counts: Dict[str, int] = {}
    for row in collections:
        label = COLLECTION_TYPE_LABELS.get(int(row.get("collection_type") or 0), str(row.get("collection_type") or "unknown"))
        type_counts[label] = type_counts.get(label, 0) + 1
    logger.info("Bangumi tracked anime count: %s type_counts=%s", len(collections), type_counts)

    if client is not None:
        refresh_cloud_tasks(client, state)
        root_dir_id = get_root_dir_id(client, (cfg.get("guangya") or {}).get("root_dir", DEFAULT_ROOT_DIR))
    else:
        root_dir_id = "dry-run-root"

    previous_tracked = set(str(x) for x in state.get("tracked_subjects", {}).keys())
    current_tracked = set()
    tracked_subjects: Dict[str, dict] = {}

    for row in collections:
        subject = row.get("subject", {})
        subject_id = str(row.get("subject_id") or subject.get("id") or "")
        title_cn = subject.get("name_cn") or ""
        title_jp = subject.get("name") or ""
        display = title_cn or title_jp or subject_id
        collection_type = int(row.get("collection_type") or row.get("type") or 0)
        collection_label = COLLECTION_TYPE_LABELS.get(collection_type, str(collection_type))
        current_tracked.add(subject_id)
        tracked_subjects[subject_id] = {
            "subject_id": subject_id,
            "title": display,
            "name_cn": title_cn,
            "name": title_jp,
            "collection_type": collection_type,
            "collection_label": collection_label,
            "ep_status": row.get("ep_status") or 0,
            "vol_status": row.get("vol_status") or 0,
            "updated_at": row.get("updated_at"),
        }

    removed_subject_ids = sorted(previous_tracked - current_tracked)
    sync_actions = plan_subject_sync_actions(tracked_subjects, state.get("subject_locations", {}), removed_subject_ids)
    applied_sync_actions = apply_subject_sync_actions(client, root_dir_id, sync_actions, state, dry_run=dry_run)

    submitted: List[dict] = []
    unresolved: List[dict] = []
    mismatched: List[dict] = []
    skipped_watched: List[dict] = []
    skipped_existing_targets: List[dict] = []
    dedup_stats = {"total_rss_items": 0, "after_dedup": 0, "subjects_processed": 0}
    quota_exhausted = False
    downloaded_subjects: List[dict] = []
    downloaded_subject_id_set = set()
    total_collections = len(collections)

    write_progress("running", 0, total_collections, 0, max_new_tasks_per_run or 0, "")

    subject_idx = 0
    for row in collections:
        if max_new_tasks_per_run is not None and len(submitted) >= max_new_tasks_per_run:
            break
        if max_download_subjects_per_run is not None and len(downloaded_subject_id_set) >= max_download_subjects_per_run:
            break
        subject = row.get("subject", {})
        subject_id = str(row.get("subject_id") or subject.get("id") or "")
        title_cn = subject.get("name_cn") or ""
        title_jp = subject.get("name") or ""
        display = title_cn or title_jp or subject_id
        collection_type = int(row.get("collection_type") or row.get("type") or 0)
        collection_label = COLLECTION_TYPE_LABELS.get(collection_type, str(collection_type))
        category_dir, show_dir = build_subject_path(display, collection_type)
        logger.info("Processing: %s (subject_id=%s, collection=%s)", display, subject_id, collection_label)
        subject_idx += 1
        write_progress("running", subject_idx, total_collections, len(submitted), max_new_tasks_per_run or 0, display)

        if collection_type not in download_collection_types:
            logger.info("Track only, skip download: %s (%s)", display, collection_label)
            continue
        if download_subject_ids and subject_id not in download_subject_ids:
            logger.info("Not in approved download plan, skip download: %s (subject_id=%s)", display, subject_id)
            continue

        mikan_id = state["subject_to_mikan"].get(subject_id)
        best_bangumi = {"bangumi_id": mikan_id, "title": "cached"} if mikan_id else None
        if not best_bangumi:
            candidates = mikan.search_bangumi([title_cn, title_jp])
            best_bangumi = mikan.choose_best_bangumi(row, candidates)
            if best_bangumi and best_bangumi.get("bangumi_id"):
                state["subject_to_mikan"][subject_id] = best_bangumi["bangumi_id"]
                logger.info("Mapped to Mikan bangumiId=%s title=%s score=%s", best_bangumi["bangumi_id"], best_bangumi.get("title"), best_bangumi.get("score"))
        if not best_bangumi:
            logger.info("Unresolved on Mikan: %s", display)
            unresolved.append({"subject_id": subject_id, "title": display, "collection": collection_label})
            continue

        rss_items = mikan.fetch_rss_items(best_bangumi["bangumi_id"])
        locked_subgroup = state["subject_subgroups"].get(subject_id)

        # --- KEY CHANGE: pick best per episode, NOT all best items ---
        chosen_items = pick_best_per_episode(rss_items, cfg.get("filters", {}), locked_subgroup=locked_subgroup)
        chosen_items = apply_episode_limit(chosen_items, cfg, collection_type)
        dedup_stats["total_rss_items"] += len(rss_items)
        dedup_stats["after_dedup"] += len(chosen_items)
        dedup_stats["subjects_processed"] += 1

        if not chosen_items:
            logger.info("No qualifying items for: %s (locked_subgroup=%s)", display, locked_subgroup)
            continue

        logger.info("Selected %d items from %d RSS entries for: %s", len(chosen_items), len(rss_items), display)

        # Mismatch detection: Bangumi says multi-episode TV series but Mikan only has movie/compilation
        ep_status = int(row.get("ep_status") or 0)
        if ep_status >= 3 and len(chosen_items) <= 2:
            movie_kw = ("剧场版", "劇場版", "MOVIE", "Movie", "movie", "剧场", "劇場", "合集", "全集", "RE:cycle")
            titles_joined = " ".join(item.get("title", "") for item in chosen_items)
            if any(kw in titles_joined for kw in movie_kw):
                mikan_title = (best_bangumi or {}).get("title", "")
                logger.warning(
                    "Mismatch: %s has %d eps on Bangumi but Mikan only has movie/compilation: %s",
                    display, ep_status, mikan_title,
                )
                mismatched.append({
                    "subject_id": subject_id,
                    "title": display,
                    "collection": collection_label,
                    "ep_status": ep_status,
                    "mikan_title": mikan_title,
                    "reason": f"Mikan只有剧场版/合集，Bangumi标记了{ep_status}集",
                })
                continue

        related_subject_rows = [candidate for candidate in collections if candidate is not row]

        for item in chosen_items:
            if max_new_tasks_per_run is not None and len(submitted) >= max_new_tasks_per_run:
                break
            guid = item["guid"]
            if guid in seen:
                continue
            if not item_matches_subject_partition(row, item["title"], related_subject_rows):
                continue

            torrent_url = item.get("torrent_url") or item.get("link") or item.get("url")
            if not torrent_url:
                continue

            # Skip already-watched episodes (unless skip_watched is disabled)
            if skip_watched and should_skip_watched(row, item):
                watched_ep = int(row.get("ep_status") or 0)
                logger.info("Skip watched ep (watched=%d): %s :: %s", watched_ep, display, item["title"])
                skipped_watched.append({"subject_id": subject_id, "title": display, "item_title": item["title"]})
                continue

            if dry_run:
                resolved_name = Path(urllib_basename(torrent_url) or item["title"]).name
                _, file_name = build_target_names(display, item["title"], resolved_name)
                folder_id = f"dry-run-{subject_id}"
                task_id = "dry-run-task"
                payload = build_create_task_payload(torrent_url, folder_id, file_name)
            else:
                resolve_result = resolve_torrent(client, torrent_url)
                bt_info = ((resolve_result.get("data") or {}).get("btResInfo") or {})
                info_hash = bt_info.get("infoHash")
                resolved_name = bt_info.get("fileName") or item["title"]
                if not info_hash:
                    logger.warning("skip unresolved torrent: %s :: %s", display, torrent_url)
                    continue
                _, file_name = build_target_names(display, item["title"], resolved_name)
                file_indexes = choose_single_video_file_index(bt_info, item["title"], file_name)
                if file_indexes == []:
                    logger.info(
                        "Skip ambiguous multi-file torrent: %s :: %s (target=%s, subfiles=%s)",
                        display,
                        item["title"],
                        file_name,
                        bt_info.get("subfilesNum") or len(bt_info.get("subfiles") or []),
                    )
                    skipped_existing_targets.append({
                        "subject_id": subject_id,
                        "show": display,
                        "title": item["title"],
                        "target": f"{category_dir}/{show_dir}/{file_name}",
                        "reason": "ambiguous_multi_file_torrent",
                    })
                    continue
                category_id = ensure_child_dir(client, root_dir_id, category_dir)
                folder_id = ensure_child_dir(client, category_id, show_dir)
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
                payload = build_create_task_payload(magnet, folder_id, file_name, file_indexes=file_indexes)

            target_key = f"{category_dir}/{show_dir}/{file_name}"
            canonical_file_name = canonical_target_name(file_name)
            duplicate_reason = "state" if target_key in existing_targets else ""
            if not duplicate_reason and not dry_run and client is not None:
                remote_names = get_remote_folder_name_index(client, folder_id, remote_folder_name_cache)
                if canonical_file_name in remote_names:
                    duplicate_reason = "remote"
            if duplicate_reason:
                logger.info(
                    "Skip duplicate target path (%s): %s :: %s",
                    duplicate_reason,
                    target_key,
                    item["title"],
                )
                skipped_existing_targets.append({
                    "subject_id": subject_id,
                    "show": display,
                    "title": item["title"],
                    "target": target_key,
                    "reason": duplicate_reason,
                })
                continue

            if not dry_run:
                task = create_cloud_task(client, payload)
                task_id = ((task.get("data") or {}).get("taskId"))
                if not task_id:
                    err_code = (task.get("code") or 0)
                    logger.warning("cloud task create failed for %s: %s", display, task)
                    if err_code == 354:
                        logger.error("GuangYa daily quota exhausted (code 354). Stopping submission.")
                        quota_exhausted = True
                        break
                    continue
                logger.info(
                    "Submitted GuangYa offline task: %s :: %s -> %s/%s/%s (task_id=%s)",
                    display,
                    item["title"],
                    category_dir,
                    show_dir,
                    file_name,
                    task_id,
                )

            existing_targets.add(target_key)
            if not dry_run and folder_id in remote_folder_name_cache:
                remote_folder_name_cache[folder_id].add(canonical_file_name)
            seen.add(guid)
            state["seen_guids"].append(guid)
            matched_subgroup = item.get("matched_subgroup")
            if matched_subgroup and not state["subject_subgroups"].get(subject_id):
                state["subject_subgroups"][subject_id] = matched_subgroup

            state["subject_locations"][subject_id] = {
                "subject_id": subject_id,
                "title": display,
                "collection_type": collection_type,
                "category_dir": category_dir,
                "show_dir": show_dir,
                "folder_id": folder_id,
            }
            state["cloud_tasks"][guid] = {
                "guid": guid,
                "subject_id": subject_id,
                "show": display,
                "collection": collection_label,
                "category_dir": category_dir,
                "source_title": item["title"],
                "torrent_url": torrent_url,
                "target_dir": f"{category_dir}/{show_dir}",
                "target_name": file_name,
                "folder_id": folder_id,
                "task_id": task_id,
                "status": "submitted",
                "submitted_at": now(),
            }
            submitted.append({
                "subject_id": subject_id,
                "collection": collection_label,
                "category_dir": category_dir,
                "show": display,
                "title": item["title"],
                "torrent_url": torrent_url,
                "target_dir": f"{category_dir}/{show_dir}",
                "target_name": file_name,
                "task_id": task_id,
                "payload": payload,
            })
            if subject_id not in downloaded_subject_id_set:
                downloaded_subject_id_set.add(subject_id)
                downloaded_subjects.append({
                    "subject_id": subject_id,
                    "show": display,
                    "collection": collection_label,
                    "category_dir": category_dir,
                    "show_dir": show_dir,
                })
        if quota_exhausted:
            break

    state["tracked_subjects"] = tracked_subjects
    state["removed_subject_ids"] = removed_subject_ids
    state["mismatched_subjects"] = mismatched
    state["last_run"] = now()
    state["last_cloud_sync"] = now()
    if not dry_run:
        write_json(state_path, state)

    final_status = "quota_exhausted" if quota_exhausted else "done"
    write_progress(final_status, total_collections, total_collections, len(submitted),
                   max_new_tasks_per_run or 0, "",
                   extra={"unresolved": len(unresolved), "mismatched": len(mismatched)})

    return {
        "submitted_count": len(submitted),
        "unresolved_count": len(unresolved),
        "mismatched_count": len(mismatched),
        "skipped_watched_count": len(skipped_watched),
        "skipped_existing_target_count": len(skipped_existing_targets),
        "dry_run": dry_run,
        "tracked_count": len(collections),
        "tracked_type_counts": type_counts,
        "removed_subject_ids": removed_subject_ids,
        "download_collection_types": download_collection_type_order,
        "max_download_subjects_per_run": max_download_subjects_per_run,
        "download_subject_ids": sorted(download_subject_ids),
        "downloaded_subjects": downloaded_subjects,
        "sync_actions": applied_sync_actions,
        "dedup_stats": dedup_stats,
        "submitted": submitted,
        "unresolved": unresolved,
        "mismatched": mismatched,
        "skipped_watched": skipped_watched[:20],
        "skipped_existing_targets": skipped_existing_targets[:20],
    }


def urllib_basename(url: str) -> str:
    from urllib.parse import urlparse

    return os.path.basename(urlparse(url).path or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Bangumi collections to GuangYa offline download using Mikan")
    parser.add_argument("--config", default=SYNC_CONFIG, help="Path to bangumi-sync JSON config")
    parser.add_argument("--dry-run", action="store_true", help="Do not submit tasks to GuangYa")
    args = parser.parse_args()

    cfg = read_json(args.config)
    dry_run = bool(args.dry_run or cfg.get("dry_run", False))
    client = None if dry_run else init_client()
    summary = process_collections(cfg, client, dry_run=dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
