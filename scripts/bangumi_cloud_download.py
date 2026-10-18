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
  - A batch may be split only when every selected file maps to an exact, known
    missing episode of one unambiguous Bangumi subject.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import logging
import json
import os
import re
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from cloud_api import GuangYaAPIError, GuangYaClient  # type: ignore
from bangumi_mikan_qb_sync import (  # type: ignore
    BangumiClient,
    COLLECTION_TYPE_LABELS,
    MikanClient,
    extract_episode_span,
    extract_subgroup,
    item_matches_subject_partition,
    load_state,
    now,
    normalize_mikan_title,
    parse_episode_number,
    score_item,
    validate_cached_mikan_mapping,
    write_json,
)

logger = logging.getLogger("bmguangya.download")


CONFIG_FILE = "/etc/bmguangya/config.json"
SYNC_CONFIG = "/etc/bmguangya/config.json"
DEFAULT_STATE_PATH = "/var/lib/bmguangya/state.json"
DEFAULT_ROOT_DIR = "Anime"
PROGRESS_FILE = "/var/lib/bmguangya/sync-progress.json"
MAX_SAFE_BATCH_EPISODES = 52
CLOUD_CREATE_TASK_URL = "https://api.guangyapan.com/cloudcollection/v1/create_task"
CLOUD_LIST_TASK_URL = "https://api.guangyapan.com/cloudcollection/v1/list_task"
CLOUD_RESOLVE_TORRENT_URL = "https://api.guangyapan.com/cloudcollection/v1/resolve_torrent"
COLLECTION_DIR_LABELS = {
    1: "想看",
    2: "看过",
    3: "在看",
    4: "未标记",
    5: "未标记",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def infer_season(*texts: str) -> int:
    """Infer a season conservatively from an explicit season marker."""
    for text in texts:
        if not text:
            continue
        match = re.search(r"(?:第\s*)?(\d{1,2})\s*(?:季|期|部)|\bS(\d{1,2})\b", text, flags=re.I)
        if match:
            return int(match.group(1) or match.group(2) or 1)
    return 1


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_progress(status: str, current: int = 0, total: int = 0,
                   submitted: int = 0, max_tasks: int = 0,
                   current_show: str = "", extra: Optional[dict] = None,
                   *, enabled: bool = True, progress_path: Optional[str] = None) -> None:
    """Write live sync progress for the monitor dashboard to poll."""
    if not enabled:
        return
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
        write_json(progress_path or PROGRESS_FILE, payload)
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


def build_subject_path(display_name: str, collection_type: int, *,
                       subject_id: Optional[str] = None, disambiguate: bool = False) -> Tuple[str, str]:
    category_dir = COLLECTION_DIR_LABELS.get(int(collection_type or 0), COLLECTION_TYPE_LABELS.get(int(collection_type or 0), str(collection_type or "unknown")))
    show_dir = normalize_bangumi_display_name(display_name)
    if disambiguate:
        if not subject_id:
            raise ValueError("subject_id is required to disambiguate duplicate Bangumi titles")
        show_dir = sanitize_component(f"{show_dir} [bgm-{subject_id}]")
    return sanitize_component(category_dir), show_dir


def duplicate_subject_title_ids(tracked_subjects: Dict[str, dict]) -> set:
    by_title: Dict[str, List[str]] = {}
    for subject_id, record in tracked_subjects.items():
        title = record.get("title") or record.get("name_cn") or record.get("name") or subject_id
        key = sanitize_component(str(title)).casefold()
        by_title.setdefault(key, []).append(str(subject_id))
    return {subject_id for subject_ids in by_title.values() if len(subject_ids) > 1
            for subject_id in subject_ids}


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
            if 0 <= ep < 200:
                return ep
    return None


def extract_episode_label(source_title: str, resolved_file_name: str) -> str:
    """Extract episode/special label for filename.

    Returns:
      - "EP01" for a single episode (including "EP00")
      - "SP01" / "OVA01" / "OAD01" for specials
      - "NCOP" / "NCED" for creditless OP/ED
      - "EP01-EP12" for episode range (filtered before submission)
      - "" if no episode info found
    """
    special = extract_special_label(source_title, resolved_file_name)
    if special:
        return special

    # Prefer strict single-episode patterns before generic span parsing, because
    # old parsers may read S01E06 as episode 01.
    ep = extract_strict_episode_number(source_title, resolved_file_name)
    if ep is not None:
        return f"EP{ep:02d}"

    span = extract_episode_span(source_title)
    if span:
        start, end = span
        if start == end:
            return f"EP{start:02d}"
        return f"EP{start:02d}-EP{end:02d}"

    ep = parse_episode_number(source_title)
    if ep is None:
        ep = parse_episode_number(resolved_file_name)
    if ep is not None and 0 <= int(ep) < 200:
        return f"EP{int(ep):02d}"

    return ""


def build_target_names(display_name: str, source_title: str, resolved_file_name: str) -> Tuple[str, str]:
    folder = normalize_bangumi_display_name(display_name)

    suffix = Path(resolved_file_name or source_title).suffix.lower()
    if suffix == '.torrent':
        suffix = ''
    ep_label = extract_episode_label(source_title, resolved_file_name)
    if ep_label:
        filename = f"{ep_label}{suffix}"
    else:
        # Do not invent an episode number for an unnumbered source.
        filename = ""
    return folder, filename


def is_single_episode_candidate(title: str) -> bool:
    """Return True for a source intended to create one playable file.

    Multi-episode torrents are handled separately, and only after exact subfile
    indexes have been mapped to known missing episodes. Movie/OVA titles with no
    parsed episode number remain eligible unless they contain batch keywords.
    """
    text = title or ""
    lower = text.lower()
    # More aggressive keyword filtering for collections/bundles
    if any(keyword in lower for keyword in [
        "合集", "全集", "全话", "全卷", "全季", "整季", "打包",
        "batch", "complete", "complete batch", "season batch",
        "外挂字幕", "外掛字幕",
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
    duplicate_ids = duplicate_subject_title_ids(tracked_subjects)

    for subject_id in sorted(removed):
        location = subject_locations.get(subject_id)
        if not location or not location.get("folder_id"):
            continue
        actions.append({
            "action": "move",
            "subject_id": subject_id,
            "title": location.get("title") or location.get("show_dir") or subject_id,
            "from_category": location.get("category_dir"),
            "to_category": "未标记",
            "show_dir": location.get("show_dir"),
            "folder_id": location.get("folder_id"),
            "collection_type": 0,
        })

    for subject_id, tracked in sorted(tracked_subjects.items()):
        location = subject_locations.get(subject_id)
        if not location or not location.get("folder_id"):
            continue
        target_category, target_show_dir = build_subject_path(
            tracked.get("title") or tracked.get("name_cn") or tracked.get("name") or subject_id,
            int(tracked.get("collection_type") or 0), subject_id=subject_id,
            disambiguate=subject_id in duplicate_ids,
        )
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
        if location.get("show_dir") != target_show_dir:
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


def init_client(config: dict) -> GuangYaClient:
    client_config = dict(config.get("guangya") or {})
    client_config["alist"] = config.get("alist") or client_config.get("alist") or {}
    return GuangYaClient(client_config, session=requests.Session())


def get_list_items(result: Dict[str, Any]) -> List[dict]:
    return ((result or {}).get("data") or {}).get("list") or []


def list_all_children(client: GuangYaClient, parent_id: str,
                      cache: Optional[Dict[str, List[dict]]] = None) -> List[dict]:
    if cache is not None and parent_id in cache:
        return cache[parent_id]
    # The GuangYa endpoint may clamp pageSize. Keep requests small and use its
    # declared total rather than treating a short page as the end of the scan.
    page_size = 100
    page = 0
    items: List[dict] = []
    seen_ids = set()
    expected_total: Optional[int] = None
    while page < 1000:
        result = client.get_file_list(parent_id=parent_id, page_size=page_size, page=page)
        data = (result or {}).get("data") or {}
        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise RuntimeError("GuangYa directory response has invalid total")
        chunk = data.get("list")
        if chunk is None and total == 0:
            # Some GuangYa empty-directory responses omit list or set it to
            # null. Accept only the explicit total=0 case.
            chunk = []
        if not isinstance(chunk, list):
            raise RuntimeError("GuangYa directory response has no list")
        added = 0
        for item in chunk:
            if not isinstance(item, dict):
                raise RuntimeError("GuangYa directory response has malformed entry")
            file_id = str(item.get("fileId") or "")
            if not file_id:
                raise RuntimeError("GuangYa directory entry has no fileId")
            if file_id in seen_ids:
                raise RuntimeError("GuangYa directory pagination repeated an entry")
            seen_ids.add(file_id)
            items.append(item)
            added += 1
        current_total = total
        if expected_total is not None and current_total != expected_total:
            raise RuntimeError("GuangYa directory total changed during pagination")
        expected_total = current_total
        if expected_total is not None and len(items) == expected_total:
            break
        if expected_total is not None and len(items) > expected_total:
            raise RuntimeError("GuangYa directory exceeded its reported total")
        if not chunk:
            raise RuntimeError("GuangYa directory ended before its reported total")
        if added == 0:
            raise RuntimeError("GuangYa directory pagination repeated a page")
        page += 1
    else:
        raise RuntimeError("GuangYa directory pagination exceeded safety limit")
    if cache is not None:
        cache[parent_id] = items
    return items


def find_directory(items: List[dict], name: str) -> Optional[str]:
    matches = [item for item in items
               if item.get("fileName") == name and int(item.get("resType") or 0) == 2]
    if len(matches) > 1:
        raise RuntimeError(f"multiple GuangYa directories named {name!r}; refusing ambiguous placement")
    return str(matches[0].get("fileId") or "") if matches else None


def get_root_dir_id(client: GuangYaClient, root_name: str,
                    cache: Optional[Dict[str, List[dict]]] = None) -> str:
    root_id = find_directory(list_all_children(client, "", cache), root_name)
    if root_id:
        return root_id
    created = client.create_dir("", root_name, fail_if_exist=False)
    file_id = ((created.get("data") or {}).get("fileId"))
    if not file_id:
        raise RuntimeError(f"failed to create GuangYa root directory {root_name!r}")
    if cache is not None:
        cache.pop("", None)
    return str(file_id)


def ensure_child_dir(client: GuangYaClient, parent_id: str, dir_name: str,
                     cache: Optional[Dict[str, List[dict]]] = None) -> str:
    items = list_all_children(client, parent_id, cache)
    existing = find_directory(items, dir_name)
    if existing:
        return existing
    created = client.create_dir(parent_id, dir_name, fail_if_exist=False)
    file_id = ((created.get("data") or {}).get("fileId"))
    if not file_id:
        raise RuntimeError(f"failed to create GuangYa directory {dir_name!r}")
    if cache is not None:
        items.append({"fileId": str(file_id), "fileName": dir_name, "resType": 2})
    return str(file_id)


def find_child_dir_id(client: GuangYaClient, parent_id: str, dir_name: str,
                      cache: Optional[Dict[str, List[dict]]] = None) -> Optional[str]:
    return find_directory(list_all_children(client, parent_id, cache), dir_name)


def canonical_target_name(name: str) -> str:
    """Normalize GuangYa duplicate suffixes like 01(1).mkv back to 01.mkv."""
    return re.sub(r"\(\d+\)(?=(?:\.[^./]+)?$)", "", (name or "").strip())


def get_remote_folder_name_index(
    client: GuangYaClient,
    folder_id: str,
    cache: Dict[str, set],
    children_cache: Optional[Dict[str, List[dict]]] = None,
) -> set:
    """Return canonical names already present in a GuangYa folder.

    This complements state-based de-duplication: if files were created before
    state tracking existed, or were manually moved into the folder, GuangYa would
    otherwise auto-rename a new task to 01(1).mkv.
    """
    if folder_id in cache:
        return cache[folder_id]
    names = {
        canonical_target_name(item.get("fileName") or "")
        for item in list_all_children(client, folder_id, children_cache)
        if item.get("fileName")
    }
    cache[folder_id] = names
    return names


def _episode_keys_from_remote_video_name(file_name: str) -> set:
    """Extract only episode labels that can be tied to a video filename.

    GuangYa may store a batch under a generated name such as
    ``Show BATCH-EP06-EP12.mkv``. Preserve that range so a later single-episode
    task cannot duplicate one of the batch's episodes.
    """
    stem = Path(file_name).stem
    range_match = re.search(
        r"(?<![A-Z0-9])EP\s*0*(\d{1,3})\s*[-~—–]\s*EP?\s*0*(\d{1,3})(?!\d)",
        stem, flags=re.I,
    )
    if range_match:
        start, end = (int(value) for value in range_match.groups())
        if 0 <= start <= end < 200:
            return {f"EP{number:02d}" for number in range(start, end + 1)}

    bracket_range = re.search(
        r"(?<!\d)[\[【(（]\s*(?:EP\s*)?0*(\d{1,3})\s*[-~—–]\s*"
        r"(?:EP\s*)?0*(\d{1,3})\s*[\]】)）]",
        stem, flags=re.I,
    )
    if bracket_range:
        start, end = (int(value) for value in bracket_range.groups())
        if 0 <= start <= end < 200:
            return {f"EP{number:02d}" for number in range(start, end + 1)}

    key = canonical_episode_key(extract_episode_label(file_name, file_name))
    if key:
        return {key}
    bare_number = re.fullmatch(r"\s*(\d{1,3})(?:v\d+)?\s*", stem, flags=re.I)
    if bare_number and int(bare_number.group(1)) < 200:
        return {canonical_episode_key(bare_number.group(1))}
    return set()


def get_remote_episode_file_index(
    client: GuangYaClient,
    folder_id: str,
    episode_keys: List[str],
    *,
    inventory_show: Optional[dict] = None,
) -> Dict[str, List[str]]:
    """Freshly list a destination folder and map matching episode videos to names.

    This intentionally bypasses ``children_cache``: the inventory may predate a
    manually or externally added file. Only explicit file rows with a supported
    video extension count; a directory named ``EP01.mkv`` is not a video.
    """
    expected = {canonical_episode_key(key) for key in episode_keys if key}
    if not expected:
        return {}
    existing: Dict[str, List[str]] = {}
    for row in list_all_children(client, folder_id):
        raw_type = row.get("resType")
        if isinstance(raw_type, bool):
            raise RuntimeError("GuangYa target directory response has invalid resType")
        try:
            res_type = int(raw_type)
        except (TypeError, ValueError):
            raise RuntimeError("GuangYa target directory response has invalid resType") from None
        if res_type == 2:
            continue
        if res_type != 1:
            raise RuntimeError("GuangYa target directory response has invalid resType")
        file_name = row.get("fileName")
        if not isinstance(file_name, str) or not file_name:
            raise RuntimeError("GuangYa target directory response has invalid fileName")
        if Path(file_name).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        parsed_keys = _episode_keys_from_remote_video_name(file_name)
        comparable_keys = _remote_file_episode_keys(parsed_keys, inventory_show)
        for key in comparable_keys & expected:
            existing.setdefault(key, []).append(file_name)
    return existing


class SubjectSyncSafetyError(RuntimeError):
    """A folder move/rename is not verified; stop before any repeat write."""


def _subject_sync_journal(state: dict) -> List[dict]:
    sync_state = state.setdefault("subject_sync", {})
    if not isinstance(sync_state, dict):
        raise RuntimeError("invalid subject_sync state")
    journal = sync_state.setdefault("journal", [])
    if not isinstance(journal, list) or any(not isinstance(item, dict) for item in journal):
        raise RuntimeError("invalid subject_sync journal")
    return journal


def _subject_sync_has_pending(state: dict) -> bool:
    sync_state = state.get("subject_sync")
    if sync_state is None:
        return False
    if not isinstance(sync_state, dict):
        raise RuntimeError("invalid subject_sync state")
    journal = sync_state.get("journal", [])
    if not isinstance(journal, list) or any(not isinstance(item, dict) for item in journal):
        raise RuntimeError("invalid subject_sync journal")
    return any(item.get("status") == "pending" for item in journal)


def _sync_checkpoint(checkpoint: Optional[Callable[[], None]]) -> None:
    if checkpoint is not None:
        checkpoint()


def _sync_category_id(client: GuangYaClient, root_dir_id: str, category_name: str,
                      *, create: bool = False) -> Optional[str]:
    rows = list_all_children(client, root_dir_id)
    named = [row for row in rows
             if str(row.get("fileName") or "").casefold() == category_name.casefold()]
    if any(int(row.get("resType") or 0) != 2 for row in named):
        raise SubjectSyncSafetyError("subject sync category name is occupied by a non-directory")
    if len(named) > 1:
        raise SubjectSyncSafetyError("subject sync category name is ambiguous")
    if named:
        if named[0].get("fileName") != category_name:
            raise SubjectSyncSafetyError("subject sync category name differs only by case")
        return str(named[0].get("fileId") or "")
    if not create:
        return None

    created_id = None
    try:
        created = client.create_dir(root_dir_id, category_name, fail_if_exist=True)
        created_id = str(((created.get("data") or {}).get("fileId")) or "") or None
    except Exception:
        # A competing run may have created the exact category. Accept only a
        # unique fresh readback; otherwise the caller must stop safely.
        pass
    rows = list_all_children(client, root_dir_id)
    named = [row for row in rows
             if str(row.get("fileName") or "").casefold() == category_name.casefold()]
    if (len(named) != 1 or named[0].get("fileName") != category_name
            or int(named[0].get("resType") or 0) != 2
            or (created_id is not None and str(named[0].get("fileId") or "") != created_id)):
        raise SubjectSyncSafetyError("new subject sync category could not be verified by ID")
    return str(named[0].get("fileId") or "")


def _subject_sync_spec(client: GuangYaClient, root_dir_id: str, action: dict,
                       subject_locations: dict) -> dict:
    op = str(action.get("action") or "")
    subject_id = str(action.get("subject_id") or "")
    folder_id = str(action.get("folder_id") or "")
    location = subject_locations.get(subject_id) or {}
    if op == "move":
        source_category = (action.get("from_category") or location.get("category_dir") or "")
        target_category = str(action.get("to_category") or "")
        source_name = str(action.get("source_show_dir") or action.get("old_show_dir")
                           or location.get("show_dir") or action.get("show_dir") or "")
        # A move preserves the folder's actual current name. A separate rename
        # action, if any, is responsible for changing it afterward.
        target_name = source_name
    elif op == "rename":
        source_category = str(action.get("category_dir") or location.get("category_dir") or "")
        target_category = source_category
        source_name = str(action.get("source_show_dir") or action.get("old_show_dir")
                           or location.get("show_dir") or "")
        target_name = str(action.get("show_dir") or "")
    else:
        raise SubjectSyncSafetyError("unsupported subject sync operation")
    if not subject_id or not folder_id or not source_category or not target_category or not source_name or not target_name:
        raise SubjectSyncSafetyError("subject sync action is missing folder identity or path")

    source_parent_id = _sync_category_id(client, root_dir_id, str(source_category))
    target_parent_id = _sync_category_id(
        client, root_dir_id, str(target_category), create=(op == "move"),
    )
    if not source_parent_id or not target_parent_id:
        raise SubjectSyncSafetyError("subject sync source or target category is missing")
    return {
        "action": op,
        "subject_id": subject_id,
        "folder_id": folder_id,
        "source_category": str(source_category),
        "target_category": str(target_category),
        "source_parent_id": source_parent_id,
        "target_parent_id": target_parent_id,
        "source_name": source_name,
        "target_name": target_name,
        "title": action.get("title") or location.get("title") or source_name,
        "collection_type": action.get("collection_type", location.get("collection_type")),
    }


def _subject_sync_probe(client: GuangYaClient, spec: dict) -> str:
    """Freshly identify the folder by ID and classify source/target/conflict."""
    source_parent = str(spec["source_parent_id"])
    target_parent = str(spec["target_parent_id"])
    source_rows = list_all_children(client, source_parent)
    target_rows = source_rows if target_parent == source_parent else list_all_children(client, target_parent)
    folder_id = str(spec["folder_id"])

    source_matches = [row for row in source_rows if str(row.get("fileId") or "") == folder_id]
    target_matches = [row for row in target_rows if str(row.get("fileId") or "") == folder_id]
    if len(source_matches) > 1 or len(target_matches) > 1:
        return "unknown"
    source_item = source_matches[0] if source_matches else None
    target_item = target_matches[0] if target_matches else None
    if source_item is None and target_item is None:
        # Even without the expected folder ID, a colliding name must not be
        # mistaken for an empty target.
        target_name = str(spec["target_name"]).casefold()
        if any(str(row.get("fileName") or "").casefold() == target_name for row in target_rows):
            return "conflict"
        return "unknown"
    if source_parent != target_parent and source_item is not None and target_item is not None:
        return "unknown"

    for row in (source_item, target_item):
        if row is not None and int(row.get("resType") or 0) != 2:
            return "unknown"

    source_name = str(spec["source_name"]).casefold()
    target_name = str(spec["target_name"]).casefold()
    source_collisions = [row for row in source_rows
                         if str(row.get("fileName") or "").casefold() == source_name
                         and str(row.get("fileId") or "") != folder_id]
    target_collisions = [row for row in target_rows
                         if str(row.get("fileName") or "").casefold() == target_name
                         and str(row.get("fileId") or "") != folder_id]
    if source_collisions or target_collisions:
        return "conflict"

    if target_item is not None and target_item.get("fileName") == spec["target_name"]:
        return "target"

    if source_item is not None and source_item.get("fileName") == spec["source_name"]:
        return "source"
    return "unknown"


def _commit_subject_sync_location(state: dict, spec: dict) -> None:
    subject_id = spec["subject_id"]
    locations = state.setdefault("subject_locations", {})
    location = dict(locations.get(subject_id) or {})
    location.update({
        "subject_id": subject_id,
        "folder_id": spec["folder_id"],
        "title": spec["title"],
        "category_dir": spec["target_category"],
        "show_dir": spec["target_name"],
    })
    if spec.get("collection_type") is not None:
        location["collection_type"] = spec["collection_type"]
    locations[subject_id] = location


def _recover_subject_sync_pending(client: GuangYaClient, root_dir_id: str, state: dict,
                                  checkpoint: Optional[Callable[[], None]]) -> None:
    journal = _subject_sync_journal(state)
    for entry in journal:
        if entry.get("status") != "pending":
            continue
        required = ("action", "subject_id", "folder_id", "source_parent_id", "target_parent_id",
                    "source_name", "target_name", "source_category", "target_category")
        if any(not entry.get(key) for key in required):
            raise SubjectSyncSafetyError("pending subject sync journal entry is incomplete")
        try:
            current_source_parent = _sync_category_id(
                client, root_dir_id, str(entry["source_category"]),
            )
            current_target_parent = _sync_category_id(
                client, root_dir_id, str(entry["target_category"]),
            )
            if (current_source_parent != str(entry["source_parent_id"])
                    or current_target_parent != str(entry["target_parent_id"])):
                raise SubjectSyncSafetyError(
                    "pending subject sync category ID changed; outcome remains unresolved"
                )
            outcome = _subject_sync_probe(client, entry)
        except Exception as exc:
            raise SubjectSyncSafetyError(
                "pending subject sync outcome is still unknown; no folder write was repeated"
            ) from exc
        if outcome == "target":
            _commit_subject_sync_location(state, entry)
            entry["status"] = "done"
            entry["recovered"] = "target_verified"
            _sync_checkpoint(checkpoint)
        elif outcome == "source":
            # A remote move may still be queued after a timeout. Seeing the
            # original path is not enough to authorize a second write.
            entry["last_readback"] = "source_still_present"
            _sync_checkpoint(checkpoint)
            raise SubjectSyncSafetyError(
                "pending subject sync is still at its source; no folder write was repeated"
            )
        else:
            raise SubjectSyncSafetyError(
                "pending subject sync outcome is still unknown; no folder write was repeated"
            )


def apply_subject_sync_actions(client: Optional[GuangYaClient], root_dir_id: str, actions: List[dict], state: dict, dry_run: bool,
                               children_cache: Optional[Dict[str, List[dict]]] = None,
                               checkpoint: Optional[Callable[[], None]] = None) -> List[dict]:
    applied: List[dict] = []
    subject_locations = state.setdefault("subject_locations", {})

    if not dry_run and client is not None:
        _recover_subject_sync_pending(client, root_dir_id, state, checkpoint)

    for action in actions:
        subject_id = str(action["subject_id"])
        op = action["action"]
        record = dict(action)
        record["dry_run"] = dry_run

        if op == "delete":
            # Old state files may still produce this action. Deletion is never
            # allowed by the automatic sync path.
            record["action"] = "blocked_delete"
            applied.append(record)
            continue

        if op not in {"move", "rename"}:
            applied.append(record)
            continue

        if dry_run or client is None:
            location = subject_locations.get(subject_id, {}).copy()
            if op == "move":
                source_name = (action.get("source_show_dir") or action.get("old_show_dir")
                               or location.get("show_dir") or action.get("show_dir"))
                location["collection_type"] = action.get("collection_type")
                location["category_dir"] = action.get("to_category")
                location["show_dir"] = source_name
            else:
                location["show_dir"] = action.get("show_dir")
            subject_locations[subject_id] = location
            applied.append(record)
            continue

        spec = _subject_sync_spec(client, root_dir_id, action, subject_locations)
        try:
            outcome = _subject_sync_probe(client, spec)
        except Exception as exc:
            raise SubjectSyncSafetyError("cannot freshly verify subject folder path; no write attempted") from exc
        if outcome == "target":
            _commit_subject_sync_location(state, spec)
            record["sync_status"] = "already_at_target"
            _sync_checkpoint(checkpoint)
            if children_cache is not None:
                children_cache.clear()
            applied.append(record)
            continue
        if outcome == "conflict":
            raise SubjectSyncSafetyError("refusing subject folder write: target name belongs to another object")
        if outcome != "source":
            raise SubjectSyncSafetyError("refusing subject folder write: source folder ID/name is not verified")

        journal_entry = dict(spec, status="pending")
        _subject_sync_journal(state).append(journal_entry)
        # Persist the pending intent before the one remote mutation.
        _sync_checkpoint(checkpoint)
        write_error = None
        try:
            if op == "move":
                client.move_file([spec["folder_id"]], spec["target_parent_id"])
            else:
                client.rename(spec["folder_id"], spec["target_name"])
        except Exception as exc:
            write_error = exc

        try:
            outcome = _subject_sync_probe(client, spec)
        except Exception as exc:
            journal_entry["last_error"] = type(exc).__name__
            _sync_checkpoint(checkpoint)
            raise SubjectSyncSafetyError(
                "subject folder write outcome is unknown; pending journal retained and no retry allowed"
            ) from exc

        if outcome == "target":
            _commit_subject_sync_location(state, spec)
            journal_entry["status"] = "done"
            journal_entry["verified"] = "target"
            if children_cache is not None:
                children_cache.clear()
            _sync_checkpoint(checkpoint)
            record["sync_status"] = "done"
            applied.append(record)
            continue

        journal_entry["last_error"] = type(write_error).__name__ if write_error else "readback_not_at_target"
        if outcome == "source":
            journal_entry["last_readback"] = "source_still_present"
        _sync_checkpoint(checkpoint)
        raise SubjectSyncSafetyError(
            "subject folder write outcome is unknown; pending journal retained and no retry allowed"
        ) from write_error

    return applied


def resolve_torrent(client: GuangYaClient, torrent_url: str) -> Dict[str, Any]:
    return client.resolve_torrent(torrent_url)


def create_cloud_task(client: GuangYaClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    return client.create_task(payload)


VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
SUBTITLE_SUFFIXES = {".ass", ".ssa", ".srt", ".vtt", ".sup", ".idx", ".sub"}


def has_external_subtitle_files(bt_info: dict) -> bool:
    """Reject multi-file torrents with sidecar subtitles we cannot preserve."""
    return any(Path(str(item.get("fileName") or "")).suffix.lower() in SUBTITLE_SUFFIXES
               for item in (bt_info.get("subfiles") or []) if isinstance(item, dict))


def choose_single_video_file_index(bt_info: dict, source_title: str, target_name: str) -> Optional[List[int]]:
    # Choose exactly one video subfile from a resolved torrent. GuangYa may
    # otherwise add every file in a multi-file torrent, so a 200-task run can
    # consume ~1000 cloud-add quota. Return None for single-file torrents,
    # a one-item list for safe multi-file torrents, or [] when ambiguous/unsafe.
    subfiles = bt_info.get("subfiles") or []
    if not subfiles:
        return None

    target_label = canonical_episode_key(Path(target_name).stem)
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
        label = canonical_episode_key(label) if label else ""
        if label and label == target_label:
            matching.append(sub)

    if len(matching) == 1:
        return [int(matching[0]["fileIndex"])]
    return []


def is_single_video_resolution(bt_info: dict) -> bool:
    """Prove an unnumbered one-shot resolves to exactly one video file."""
    subfiles = bt_info.get("subfiles")
    # A top-level filename alone cannot prove the torrent contains one file;
    # require the resolved file list so a collection or sidecar is not lost.
    if not isinstance(subfiles, list) or not subfiles:
        return False
    videos = []
    for item in subfiles:
        if not isinstance(item, dict) or not item.get("fileName"):
            return False
        if Path(str(item["fileName"])).suffix.lower() in VIDEO_SUFFIXES:
            videos.append(item)
    if len(videos) != 1:
        return False
    try:
        index = int(videos[0]["fileIndex"])
    except (KeyError, TypeError, ValueError):
        return False
    return index >= 0


def choose_exact_batch_candidate(rss_items: List[dict], filters: dict, missing: Dict[str, dict],
                                 inventory_show: dict, subject_row: dict,
                                 related_subject_rows: List[dict],
                                 locked_subgroup: Optional[str] = None,
                                 max_episode_count: int = MAX_SAFE_BATCH_EPISODES) -> Optional[dict]:
    """Choose one-season ranges that contain only this subject's known EPs.

    The torrent may include already-present episodes. The resolved subfiles are
    narrowed to the exact missing keys before submission.
    """
    if (not isinstance(inventory_show, dict) or inventory_show.get("ambiguous")
            or inventory_show.get("coverage_unknown")
            or not _episode_numbering_is_usable(inventory_show)
            or _has_nonidentity_episode_numbering(inventory_show)):
        return None

    def numbers_for(items):
        numbers = []
        for key in items:
            match = re.fullmatch(r"EP(\d{2,3})", canonical_episode_key(key))
            if not match:
                return None
            numbers.append(int(match.group(1)))
        return numbers

    expected_numbers = numbers_for(missing)
    known_keys = [
        canonical_episode_key(episode.get("episode_key") or "")
        for episode in inventory_show.get("episodes") or []
        if isinstance(episode, dict) and episode.get("episode_id") is not None
    ]
    # Inventory also records cloud-only specials such as NCOP/SP01. They do not
    # make an otherwise complete ordinary-episode range unsafe, so exclude them
    # from the candidate's known ordinary episode set.
    ordinary_keys = [key for key in known_keys if re.fullmatch(r"EP\d{2,3}", key)]
    known_numbers = numbers_for(ordinary_keys)
    if expected_numbers is None or known_numbers is None:
        return None
    expected_numbers.sort()
    known_numbers.sort()
    if (len(expected_numbers) < 2 or len(expected_numbers) > max_episode_count
            or len(set(expected_numbers)) != len(expected_numbers)
            or len(set(known_numbers)) != len(known_numbers)):
        return None
    if not set(expected_numbers).issubset(set(known_numbers)):
        return None
    for key in missing:
        match = re.fullmatch(r"EP(\d{2,3})", canonical_episode_key(key))
        if not match:
            return None

    candidates = []
    for item in rss_items:
        title = str(item.get("title") or "")
        if not title or any(word in title.lower() for word in ("外挂字幕", "外掛字幕")):
            continue
        span = extract_episode_span(title)
        if not span or span[0] >= span[1]:
            continue
        span_numbers = list(range(span[0], span[1] + 1))
        if not set(expected_numbers).issubset(set(span_numbers)):
            continue
        if not set(span_numbers).issubset(set(known_numbers)):
            continue
        if span[1] - span[0] + 1 > max_episode_count:
            continue
        if not item_matches_subject_partition(subject_row, title, related_subject_rows):
            continue
        guid = str(item.get("guid") or "")
        torrent_url = item.get("torrent_url") or item.get("link") or item.get("url")
        if not guid or not torrent_url:
            continue
        accepted, score, reasons = score_item(title, filters, locked_subgroup=locked_subgroup)
        if not accepted:
            continue
        candidate = dict(item)
        candidate.update(
            score=score,
            reasons=reasons,
            release_version=extract_release_version(title),
            matched_subgroup=extract_subgroup(title, filters),
            batch_episode_keys=[f"EP{number:02d}" for number in expected_numbers],
            batch_span=(span[0], span[1]),
            batch_span_count=len(span_numbers),
        )
        candidates.append(candidate)

    if not candidates and locked_subgroup:
        return choose_exact_batch_candidate(rss_items, filters, missing, inventory_show, subject_row,
                                            related_subject_rows, locked_subgroup=None,
                                            max_episode_count=max_episode_count)
    if not candidates:
        return None
    return max(candidates, key=lambda row: (int(row.get("release_version") or 1),
                                            int(row.get("score") or 0),
                                            -int(row.get("batch_span_count") or 0),
                                            str(row.get("title") or "")))


def limit_batch_missing_episodes(missing: Dict[str, dict], subject_row: dict, cfg: dict) -> Dict[str, dict]:
    """Keep batch selection within the configured airing-subject plan limit."""
    if not bool(cfg.get("limit_airing_subjects_to_latest", True)) or not is_subject_airing(subject_row, cfg):
        return missing
    limit = int(cfg.get("airing_episodes_per_subject_per_run",
                        cfg.get("daily_plan_episodes_per_subject", 1)) or 0)
    if limit <= 0 or len(missing) <= limit:
        return missing
    mode = str(cfg.get("episode_selection_mode") or "latest").lower()
    ordered = sorted(
        missing.items(),
        key=lambda pair: int(canonical_episode_key(pair[0])[2:])
        if re.fullmatch(r"EP\d{2,3}", canonical_episode_key(pair[0])) else -1,
        reverse=(mode != "oldest"),
    )
    return dict(ordered[:limit])


def choose_batch_video_file_indexes(bt_info: dict, expected_episode_keys: List[str]) -> Optional[List[int]]:
    """Map a resolved torrent to exactly one file index per expected episode."""
    if not isinstance(bt_info.get("subfiles"), list) or has_external_subtitle_files(bt_info):
        return None
    expected_order = [canonical_episode_key(key) for key in expected_episode_keys]
    if (not expected_order or len(set(expected_order)) != len(expected_order)
            or any(not re.fullmatch(r"EP\d{2,3}", key) for key in expected_order)):
        return None
    expected = set(expected_order)
    found: Dict[str, List[int]] = {}
    for subfile in bt_info.get("subfiles") or []:
        if not isinstance(subfile, dict):
            return None
        file_name = str(subfile.get("fileName") or "")
        if Path(file_name).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        label = canonical_episode_key(extract_episode_label(file_name, file_name))
        if not label:
            # Some torrent metadata strips release-group brackets and leaves a
            # bare `06.mkv`; accept only an all-numeric stem, never a number
            # embedded in a title where it could be a year or season marker.
            bare_number = re.fullmatch(r"\s*(\d{1,3})(?:v\d+)?\s*", Path(file_name).stem, re.I)
            if bare_number:
                label = canonical_episode_key(bare_number.group(1))
        if label not in expected:
            continue
        try:
            index = int(subfile["fileIndex"])
        except (KeyError, TypeError, ValueError):
            return None
        found.setdefault(label, []).append(index)
    if set(found) != expected or any(len(indexes) != 1 for indexes in found.values()):
        return None
    indexes = [found[key][0] for key in expected_order]
    if len(set(indexes)) != len(indexes):
        return None
    return indexes


def refresh_cloud_tasks(client: Optional[GuangYaClient], state: dict,
                        remote_tasks: Optional[List[dict]] = None) -> None:
    """Mirror GuangYa task state without calling task status "completed" evidence."""
    if remote_tasks is None:
        if client is None:
            return
        remote_tasks = client.list_all_tasks()
    by_id = {str(item.get("taskId")): item for item in remote_tasks if item.get("taskId")}
    for task in (state.get("cloud_tasks") or {}).values():
        if not isinstance(task, dict) or not task.get("task_id"):
            continue
        detail = by_id.get(str(task["task_id"]))
        if not detail:
            continue
        status = int(detail.get("status") or 0)
        task["remote_status"] = status
        task["remote_file_name"] = detail.get("fileName")
        if status == 1:
            task.update(status="submitted", quota_reserved=True)
        elif status == 2:
            task.update(status="remote_finished_waiting_for_file", quota_reserved=True)
        elif status == 3:
            task.update(status="failed", quota_reserved=False)
        elif status == 5:
            task.update(status="running", quota_reserved=True)
        else:
            task["status"] = f"remote_status_{status}"


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


def iter_inventory_shows(snapshot: dict):
    """Yield show records from the inventory's category tree."""
    tree = snapshot.get("tree")
    def walk(node, category=""):
        if isinstance(node, list):
            for child in node:
                yield from walk(child, category)
        elif isinstance(node, dict):
            if "subject_id" in node and isinstance(node.get("episodes"), list):
                yield node, category
                return
            for key, value in node.items():
                next_category = key if key in {"在看", "想看", "看过", "未标记"} else category
                yield from walk(value, next_category)
    yield from walk(tree)


def load_verified_inventory(cfg: dict) -> dict:
    path = cfg.get("inventory_path", "/var/lib/bmguangya/library.json")
    snapshot = read_json(path)
    if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
        raise RuntimeError("inventory is missing or incomplete; refusing to infer missing episodes")
    freshness = snapshot.get("freshness") or {}
    if freshness.get("state") != "fresh":
        raise RuntimeError("inventory is not marked fresh; refusing to submit downloads")
    try:
        age = float(freshness["age_seconds"])
        ttl = float(freshness["ttl_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("inventory freshness metadata is invalid") from exc
    max_age = float(cfg.get("max_inventory_age_seconds", ttl))
    if age < 0 or ttl <= 0 or age > min(ttl, max_age):
        raise RuntimeError("inventory snapshot is stale; refusing to submit downloads")
    configured_root = str(((cfg.get("alist") or {}).get("root")) or "").rstrip("/")
    inventory_root = str(((snapshot.get("source") or {}).get("cloud_root")) or "").rstrip("/")
    if configured_root and inventory_root and configured_root != inventory_root:
        raise RuntimeError("inventory cloud root does not match configured GuangYa root")
    if not isinstance(snapshot.get("tree"), (dict, list)):
        raise RuntimeError("inventory has no usable tree")
    return snapshot


def inventory_shows_by_subject(snapshot: dict) -> Dict[str, List[dict]]:
    result: Dict[str, List[dict]] = {}
    for show, category in iter_inventory_shows(snapshot):
        subject_id = str(show.get("subject_id") or "")
        if subject_id:
            record = dict(show)
            record.setdefault("inventory_category", category)
            result.setdefault(subject_id, []).append(record)
    return result


def inventory_episode_key(item: dict) -> str:
    label = extract_episode_label(str(item.get("title") or ""), "")
    if not label:
        return ""
    if label.startswith("EP") or label.startswith(("SP", "OVA", "OAD", "NCOP", "NCED")):
        return label
    return "EP" + label


def map_unlabelled_single_to_only_episode(item: dict, inventory_show: dict) -> str:
    """Map a truly unnumbered movie to EP01 only for a one-episode subject.

    Do not infer from the set of missing episodes: a 12-episode series with
    only EP01 missing is still a series, not an unnumbered one-shot.
    """
    title = str(item.get("title") or "").strip()
    if (item.get("episode_key") != "single" or not title
            or not is_single_episode_candidate(title)
            or extract_special_label(title)
            or extract_episode_label(title, "")):
        return ""
    partition_markers = (
        r"第\s*[0-9一二三四五六七八九十两]+\s*(?:季|期|部|篇|クール)",
        r"\b(?:season|s|part|cour)\s*(?:\d{1,2}|[ivx]{1,4})\b",
    )
    if any(re.search(pattern, title, flags=re.I) for pattern in partition_markers):
        return ""
    if not is_safe_inventory_subject(inventory_show):
        return ""
    expected = {
        canonical_episode_key(episode.get("episode_key") or "")
        for episode in inventory_show.get("episodes") or []
        if isinstance(episode, dict) and episode.get("episode_id") is not None
        and re.fullmatch(r"EP\d{1,3}", canonical_episode_key(episode.get("episode_key") or ""))
    }
    return next(iter(expected)) if len(expected) == 1 else ""


def candidate_inventory_episode_key(item: dict, inventory_show: dict) -> str:
    key = inventory_episode_key(item)
    numbering = _episode_numbering_info(inventory_show)
    if numbering is None:
        if key:
            return key
        return map_unlabelled_single_to_only_episode(item, inventory_show)
    if not _episode_numbering_is_usable(inventory_show):
        return ""
    if not key:
        return map_unlabelled_single_to_only_episode(item, inventory_show)
    number = _ordinary_episode_number(key)
    if number is None:
        # Specials are not part of the ordinary ep/sort numbering map.
        return key
    ep_to_sort = numbering["ep_to_sort"]
    sort_domain = numbering["sort_domain"]
    if numbering["mode"] == "global_sort" and not ep_to_sort:
        # Standard first-season inventories use Bangumi sort numbers directly
        # and intentionally have no ep-to-sort alias map.
        return key
    mapped = ep_to_sort.get(number)
    in_sort_domain = number in sort_domain
    if in_sort_domain and mapped is not None and mapped != number:
        # The same bare EPn could name two different Bangumi episodes across
        # the season-relative and global domains. Do not guess which was meant.
        return ""
    if in_sort_domain:
        return canonical_episode_key(str(number))
    if mapped is not None:
        return canonical_episode_key(str(mapped))
    return ""


_EPISODE_NUMBERING_MODES = {"season_relative", "global_sort", "metadata_alias"}


def _ordinary_episode_number(value: Any) -> Optional[int]:
    key = canonical_episode_key(str(value or ""))
    match = re.fullmatch(r"EP(\d{1,3})", key)
    return int(match.group(1)) if match else None


def _parse_number_map(value: Any) -> Tuple[Dict[int, int], bool]:
    if value is None:
        return {}, True
    if not isinstance(value, dict):
        return {}, False
    parsed: Dict[int, int] = {}
    for raw_key, raw_value in value.items():
        if isinstance(raw_value, bool):
            return {}, False
        key = _ordinary_episode_number(str(raw_key))
        if key is None:
            try:
                key = int(str(raw_key))
            except (TypeError, ValueError):
                return {}, False
        if key < 0 or key > 999:
            return {}, False
        if isinstance(raw_value, int):
            mapped = raw_value
        else:
            try:
                mapped = int(str(raw_value))
            except (TypeError, ValueError):
                return {}, False
        if mapped < 0 or mapped > 999:
            return {}, False
        if key in parsed and parsed[key] != mapped:
            return {}, False
        parsed[key] = mapped
    return parsed, True


def _episode_numbering_info(show: Optional[dict]) -> Optional[dict]:
    """Return normalized numbering metadata, or None for legacy inventories."""
    if not isinstance(show, dict) or "episode_numbering" not in show:
        return None
    numbering = show.get("episode_numbering")
    if not isinstance(numbering, dict):
        return {"mode": "invalid", "ep_to_sort": {}, "sort_to_ep": {},
                "sort_domain": set(), "valid": False}
    mode = str(numbering.get("mode") or "").strip().lower()
    ep_to_sort, ep_valid = _parse_number_map(numbering.get("ep_to_sort"))
    sort_to_ep, sort_valid = _parse_number_map(numbering.get("sort_to_ep"))
    valid = mode in _EPISODE_NUMBERING_MODES and ep_valid and sort_valid
    if valid and bool(ep_to_sort) != bool(sort_to_ep):
        valid = False
    if valid and mode != "global_sort" and not ep_to_sort:
        valid = False
    if valid and ep_to_sort and sort_to_ep:
        valid = (all(sort_to_ep.get(sort) == ep for ep, sort in ep_to_sort.items())
                 and all(ep_to_sort.get(ep) == sort for sort, ep in sort_to_ep.items()))
    sort_domain = set(sort_to_ep)
    if not sort_domain:
        sort_domain = set(ep_to_sort.values())
    return {"mode": mode, "ep_to_sort": ep_to_sort, "sort_to_ep": sort_to_ep,
            "sort_domain": sort_domain, "valid": valid}


def _episode_numbering_is_usable(show: Optional[dict]) -> bool:
    numbering = _episode_numbering_info(show)
    return numbering is None or bool(numbering.get("valid"))


def _has_nonidentity_episode_numbering(show: Optional[dict]) -> bool:
    numbering = _episode_numbering_info(show)
    return bool(numbering and numbering.get("valid")
                and any(ep != sort for ep, sort in numbering["ep_to_sort"].items()))


def _remote_file_episode_keys(parsed_keys: set, show: Optional[dict]) -> set:
    """Map cloud filenames to possible Bangumi sort keys for fresh de-duplication.

    In a season-relative tree EP03 is the local spelling for its mapped sort
    key. Global-sort inventories keep the literal filename. Metadata aliases
    are treated as both possible spellings so a fresh target check fails safe.
    """
    numbering = _episode_numbering_info(show)
    if numbering is None or not numbering.get("valid"):
        return set(parsed_keys)
    result = set()
    for key in parsed_keys:
        number = _ordinary_episode_number(key)
        if number is None:
            result.add(key)
            continue
        mapped = numbering["ep_to_sort"].get(number)
        if numbering["mode"] == "season_relative":
            if mapped is not None:
                result.add(canonical_episode_key(str(mapped)))
                if number in numbering["sort_domain"] and mapped != number:
                    result.add(canonical_episode_key(str(number)))
            elif number in numbering["sort_domain"]:
                result.add(canonical_episode_key(str(number)))
            else:
                result.add(canonical_episode_key(str(number)))
        elif numbering["mode"] == "metadata_alias":
            result.add(canonical_episode_key(str(number)))
            if mapped is not None:
                result.add(canonical_episode_key(str(mapped)))
        else:
            # Global-sort describes the current inventory, not files that may
            # have appeared since it was scanned. Check both plausible labels
            # against a fresh directory listing to avoid duplicate downloads.
            result.add(canonical_episode_key(str(number)))
            if mapped is not None:
                result.add(canonical_episode_key(str(mapped)))
    return result


def validate_inventory_show(show: Optional[dict]) -> bool:
    """Strict predicate for an existing cloud directory that can be seeded."""
    return bool(
        show
        and show.get("matched") is True
        and not show.get("ambiguous")
        and not show.get("unmatched")
        and not show.get("coverage_unknown")
        and isinstance(show.get("episodes"), list)
        and show.get("source_path")
    )


def is_safe_inventory_subject(show: Optional[dict]) -> bool:
    """Allow existing matches or an unambiguous Bangumi-only new target."""
    if not _episode_numbering_is_usable(show):
        return False
    if validate_inventory_show(show):
        return True
    if (not isinstance(show, dict) or not show.get("subject_id") or show.get("ambiguous")
            or show.get("coverage_unknown")):
        return False
    episodes = show.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        return False
    # A new Bangumi-only node may be marked unmatched because it has no cloud
    # directory yet. It is safe only when each expected record has stable identity
    # and explicit inventory booleans, with no duplicate episode keys.
    seen = set()
    has_expected = False
    for episode in episodes:
        if not isinstance(episode, dict):
            return False
        key = canonical_episode_key(episode.get("episode_key") or "")
        if episode.get("episode_id") is not None:
            if (not key or key in seen or not isinstance(episode.get("present"), bool)
                    or not isinstance(episode.get("missing"), bool)):
                return False
            seen.add(key)
            has_expected = True
        elif episode.get("present") is True:
            # Extra cloud files without a Bangumi episode id are not source
            # metadata for a new target.
            continue
    return has_expected and (show.get("unmatched") is True or show.get("matched") is True)


def canonical_episode_key(value: str) -> str:
    value = str(value or "").upper().strip()
    if re.fullmatch(r"\d{1,3}", value):
        return f"EP{int(value):02d}"
    match = re.fullmatch(r"EP[-_ ]?(\d{1,3})", value)
    if match:
        return f"EP{int(match.group(1)):02d}"
    match = re.fullmatch(r"(SP|OVA|OAD)[-_ ]?(\d{1,3})", value)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    return value


def missing_episode_map(show: dict) -> Dict[str, dict]:
    entries: Dict[str, dict] = {}
    duplicates = set()
    for episode in show.get("episodes") or []:
        key = canonical_episode_key(episode.get("episode_key") or "")
        if not key:
            continue
        if key in entries:
            duplicates.add(key)
        entries[key] = episode
    for key in duplicates:
        entries.pop(key, None)
    return {
        key: ep for key, ep in entries.items()
        if ep.get("episode_id") is not None and ep.get("missing") is True and ep.get("present") is not True
    }


def parse_mikan_subject_overrides(mikan_config: Any) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Validate the optional, user-maintained Bangumi-to-Mikan map.

    Invalid per-subject entries are returned as unresolved reasons so one bad
    override cannot affect other subjects. An invalid map or subject key fails
    before any remote directory action can run.
    """
    if not isinstance(mikan_config, dict):
        raise ValueError("mikan configuration must be an object")
    if "subject_overrides" not in mikan_config:
        return {}, {}
    raw_overrides = mikan_config["subject_overrides"]
    if not isinstance(raw_overrides, dict):
        raise ValueError("mikan.subject_overrides must be an object")

    overrides: Dict[str, dict] = {}
    errors: Dict[str, str] = {}
    for subject_id, raw_override in raw_overrides.items():
        if not isinstance(subject_id, str) or not re.fullmatch(r"[1-9]\d*", subject_id):
            raise ValueError("mikan.subject_overrides keys must be positive numeric Bangumi IDs")
        if not isinstance(raw_override, dict) or set(raw_override) != {"bangumi_id", "title"}:
            errors[subject_id] = "mikan_override_invalid_structure"
            continue

        raw_bangumi_id = raw_override["bangumi_id"]
        if isinstance(raw_bangumi_id, bool):
            bangumi_id = None
        elif isinstance(raw_bangumi_id, int) and raw_bangumi_id > 0:
            bangumi_id = str(raw_bangumi_id)
        elif (isinstance(raw_bangumi_id, str)
              and re.fullmatch(r"[1-9]\d*", raw_bangumi_id)):
            bangumi_id = raw_bangumi_id
        else:
            bangumi_id = None
        if bangumi_id is None:
            errors[subject_id] = "mikan_override_invalid_id"
            continue

        title = raw_override["title"]
        if not isinstance(title, str) or not title.strip() or not normalize_mikan_title(title):
            errors[subject_id] = "mikan_override_invalid_title"
            continue
        overrides[subject_id] = {"bangumi_id": bangumi_id, "title": title.strip()}
    return overrides, errors


def resolve_mikan_subject_override(mikan: MikanClient, subject_row: dict,
                                   override: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Verify a configured ID/title pair against Mikan before using it."""
    title = override["title"]
    expected_title = normalize_mikan_title(title)
    try:
        candidates = mikan.search_bangumi([title])
    except Exception:
        return None, "mikan_override_verification_failed"
    verified = [candidate for candidate in candidates
                if isinstance(candidate, dict)
                and str(candidate.get("bangumi_id") or "") == override["bangumi_id"]
                and normalize_mikan_title(candidate.get("title") or "") == expected_title]
    if len(verified) != 1:
        return None, "mikan_override_id_title_unverified"

    # The explicit title is an exact alias only for this already-verified ID.
    # Season and format checks still run against the Bangumi subject metadata.
    selected = mikan.choose_best_bangumi(
        subject_row, verified, extra_exact_titles=[title],
    )
    if not selected:
        return None, "mikan_override_conflicts_with_subject"
    return selected, None


@contextlib.contextmanager
def single_instance_lock(state_path: str):
    lock_path = f"{state_path}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another Bangumi-to-GuangYa sync is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another Bangumi-to-GuangYa sync is already running") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def task_created_today(task: dict, today: date) -> bool:
    value = task.get("createTime") or task.get("create_time") or task.get("created_at")
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date() == today
    except (TypeError, ValueError, OSError, OverflowError):
        # Missing timestamps are counted conservatively against today's cap.
        return True


def extract_info_hash(value: Any) -> str:
    match = re.search(r"(?:urn:btih:)?([A-Fa-f0-9]{40}|[A-Z2-7]{32})", str(value or ""))
    return match.group(1).lower() if match else ""


def has_active_hash_task(state: dict, remote_tasks: List[dict], info_hash: str) -> bool:
    target_hash = extract_info_hash(info_hash)
    if not target_hash:
        return False
    terminal = {"failed", "rejected", "quota_rejected", "completed"}
    completed_remote_ids = {
        str(task.get("task_id")) for task in (state.get("cloud_tasks") or {}).values()
        if isinstance(task, dict) and task.get("status") == "completed"
        and task.get("file_evidence") and task.get("task_id")
    }
    for task in (state.get("cloud_tasks") or {}).values():
        if isinstance(task, dict) and task.get("status") not in terminal:
            if extract_info_hash(task.get("info_hash")) == target_hash:
                return True
    for task in remote_tasks:
        if str(task.get("taskId") or "") in completed_remote_ids:
            continue
        try:
            remote_status = int(task.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if remote_status in {1, 2, 5} and extract_info_hash(task.get("res")) == target_hash:
            return True
    return False


def is_subject_airing(row: dict, cfg: dict) -> bool:
    subject = row.get("subject") or {}
    date_text = str(subject.get("date") or "").strip()
    if not date_text:
        return False
    try:
        start = date.fromisoformat(date_text[:10])
    except Exception:
        return False
    today = datetime.now().astimezone().date()
    if start > today:
        return True
    eps = int(subject.get("eps") or 0)
    grace_days = int(cfg.get("airing_completion_grace_days", 14) or 14)
    if eps > 0:
        # Weekly TV anime: ep1 on start date, final ep roughly start+(eps-1)*7.
        end = start + timedelta(days=max(eps - 1, 0) * 7 + grace_days)
    else:
        # Unknown length: treat recent shows as airing, old shows as completed.
        end = start + timedelta(days=int(cfg.get("unknown_eps_airing_days", 180) or 180))
    return today <= end


def apply_episode_limit(chosen_items: List[dict], cfg: dict, row: dict, collection_type: int) -> List[dict]:
    if not bool(cfg.get("limit_airing_subjects_to_latest", True)):
        return chosen_items
    if not is_subject_airing(row, cfg):
        return chosen_items
    raw_limit = int(cfg.get("airing_episodes_per_subject_per_run", cfg.get("daily_plan_episodes_per_subject", 1)) or 0)
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
    mikan_config = cfg.get("mikan", {})
    subject_overrides, invalid_subject_overrides = parse_mikan_subject_overrides(mikan_config)
    state_path = cfg.get("state_path", DEFAULT_STATE_PATH)
    raw_limit = int(cfg.get("max_new_cloud_tasks_per_run", 50) or 0)
    max_new_tasks_per_run = raw_limit if raw_limit > 0 else 1000
    max_subjects = int(cfg.get("max_download_subjects_per_run", 0) or 0)
    max_batch_episode_span = max(0, int(cfg.get("max_batch_episode_span", MAX_SAFE_BATCH_EPISODES) or 0))
    download_subject_ids = {str(value) for value in cfg.get("download_subject_ids", []) if str(value).strip()}

    # A download decision is allowed only from a fresh, complete inventory.
    inventory = load_verified_inventory(cfg)
    inventory_by_subject = inventory_shows_by_subject(inventory)

    state = copy.deepcopy(load_state(state_path))
    for key, default in {
        "subject_to_mikan": {}, "subject_to_mikan_title": {},
        "subject_subgroups": {}, "tracked_subjects": {}, "removed_subject_ids": [],
        "cloud_tasks": {}, "subject_locations": {},
    }.items():
        state.setdefault(key, default)
    state["cloud_tasks"] = dict(state["cloud_tasks"] or {})

    bangumi = BangumiClient(cfg["bangumi"])
    mikan = MikanClient(mikan_config)
    tracked_types = [int(value) for value in cfg.get("tracked_collection_types", [1, 2, 3, 4, 5])]
    download_order = [int(value) for value in cfg.get("download_collection_types", [3, 1, 2])]
    collections = bangumi.get_combined_anime_collections(tracked_types)
    priority = {ctype: index for index, ctype in enumerate(download_order)}

    def sort_key(row):
        ctype = int(row.get("collection_type") or row.get("type") or 0)
        subject_id = str(row.get("subject_id") or (row.get("subject") or {}).get("id") or "")
        updated = str(row.get("updated_at") or "")
        return (priority.get(ctype, 999), 0 if is_subject_airing(row, cfg) else 1,
                "".join(chr(255 - ord(char)) for char in updated), subject_id)

    collections.sort(key=sort_key)
    type_counts: Dict[str, int] = {}
    tracked_subjects: Dict[str, dict] = {}
    for row in collections:
        subject = row.get("subject") or {}
        subject_id = str(row.get("subject_id") or subject.get("id") or "")
        if not subject_id:
            raise RuntimeError("Bangumi returned a collection without subject_id")
        ctype = int(row.get("collection_type") or row.get("type") or 0)
        display = subject.get("name_cn") or subject.get("name") or subject_id
        collection_label = COLLECTION_TYPE_LABELS.get(ctype, str(ctype))
        type_counts[collection_label] = type_counts.get(collection_label, 0) + 1
        tracked_subjects[subject_id] = {
            "subject_id": subject_id, "title": display,
            "name_cn": subject.get("name_cn") or "", "name": subject.get("name") or "",
            "collection_type": ctype, "collection_label": collection_label,
            "ep_status": row.get("ep_status") or 0, "vol_status": row.get("vol_status") or 0,
            "updated_at": row.get("updated_at"),
        }
    duplicate_title_ids = duplicate_subject_title_ids(tracked_subjects)

    # Full cursor traversal is required before quota and active-task decisions.
    remote_tasks = client.list_all_tasks() if client is not None else []
    refresh_cloud_tasks(client, state, remote_tasks=remote_tasks)
    children_cache: Dict[str, List[dict]] = {}
    if client is not None and _subject_sync_has_pending(state):
        root_name = (cfg.get("guangya") or {}).get("root_dir", DEFAULT_ROOT_DIR)
        root_id = find_directory(list_all_children(client, ""), root_name)
        if not root_id:
            raise SubjectSyncSafetyError("pending subject sync root directory cannot be verified")
    else:
        root_id = get_root_dir_id(
            client, (cfg.get("guangya") or {}).get("root_dir", DEFAULT_ROOT_DIR), children_cache,
        ) if client else "dry-run-root"

    # Seed subject locations from the verified tree on the first managed run so
    # later collection changes move the existing folder instead of making a copy.
    if client is not None:
        for subject_id, shows in inventory_by_subject.items():
            if subject_id in state["subject_locations"] or len(shows) != 1:
                continue
            show = shows[0]
            if not validate_inventory_show(show):
                continue
            parts = [part for part in str(show.get("source_path") or "").strip("/").split("/") if part]
            parent = root_id
            for part in parts:
                child = find_child_dir_id(client, parent, part, children_cache)
                if not child:
                    parent = ""
                    break
                parent = child
            if parent and parts:
                record = tracked_subjects.get(subject_id, {})
                state["subject_locations"][subject_id] = {
                    "subject_id": subject_id, "title": record.get("title") or show.get("name") or subject_id,
                    "collection_type": record.get("collection_type", 0),
                    "category_dir": parts[0] if len(parts) > 1 else "未标记",
                    "show_dir": parts[-1], "folder_id": parent,
                }

    previous_ids = set(str(value) for value in state.get("tracked_subjects", {}))
    removed_subject_ids = sorted(previous_ids - set(tracked_subjects))
    actions = plan_subject_sync_actions(tracked_subjects, state.get("subject_locations", {}), removed_subject_ids)
    applied_actions = apply_subject_sync_actions(client, root_id, actions, state, dry_run=dry_run,
                                                 children_cache=children_cache,
                                                 checkpoint=(lambda: write_json(state_path, state))
                                                 if not dry_run else None)
    state["tracked_subjects"] = tracked_subjects
    state["removed_subject_ids"] = removed_subject_ids
    if not dry_run:
        # Persist successful moves and refreshed task statuses before submitting.
        write_json(state_path, state)

    inventory_present = set()
    for subject_id, shows in inventory_by_subject.items():
        for show in shows:
            for episode in show.get("episodes") or []:
                if episode.get("present") is True:
                    key = canonical_episode_key(episode.get("episode_key") or "")
                    if key:
                        inventory_present.add((subject_id, key))
    for task in state["cloud_tasks"].values():
        if not isinstance(task, dict):
            continue
        task_keys = [canonical_episode_key(value) for value in task.get("episode_keys") or []]
        if not task_keys:
            task_key = canonical_episode_key(task.get("episode_key") or "")
            if not task_key:
                task_key = canonical_episode_key(extract_episode_label(str(task.get("target_name") or ""), ""))
            task_keys = [task_key] if task_key else []
        subject_id = str(task.get("subject_id") or "")
        if task_keys and all((subject_id, key) in inventory_present for key in task_keys):
            task.update(status="completed", quota_reserved=False, file_evidence=True)

    if client is not None:
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        remote_ids = {str(task.get("taskId")) for task in remote_tasks if task.get("taskId")}
        remote_today = sum(1 for task in remote_tasks if task_created_today(task, local_now.date()))
        reservations = sum(
            1 for task in state["cloud_tasks"].values()
            if isinstance(task, dict) and task.get("quota_reserved")
            and (not task.get("task_id") or str(task.get("task_id")) not in remote_ids)
        )
        used_today = remote_today + reservations
    else:
        used_today = 0
    daily_limit = min(1000, max(0, int(cfg.get("daily_cloud_task_quota", 1000))))
    remaining_quota = max(0, daily_limit - used_today)
    run_budget = min(max_new_tasks_per_run, remaining_quota)

    submitted: List[dict] = []
    planned: List[dict] = []
    unresolved: List[dict] = []
    skipped: List[dict] = []
    dedup_stats = {"rss_items": 0, "eligible_missing_episodes": 0, "subjects_processed": 0}
    quota_exhausted = remaining_quota == 0
    unknown_submission = False
    api_error_count = 0
    stop_after_api_error = False
    processed_subjects = set()

    if not dry_run:
        write_progress("running", 0, len(collections), 0, run_budget, "", enabled=True,
                       progress_path=cfg.get("progress_path", PROGRESS_FILE))

    for row in collections:
        if len(submitted) >= run_budget or len(planned) >= run_budget:
            break
        subject = row.get("subject") or {}
        subject_id = str(row.get("subject_id") or subject.get("id") or "")
        collection_type = int(row.get("collection_type") or row.get("type") or 0)
        display = subject.get("name_cn") or subject.get("name") or subject_id
        collection_label = COLLECTION_TYPE_LABELS.get(collection_type, str(collection_type))
        category_dir, show_dir = build_subject_path(
            display, collection_type, subject_id=subject_id,
            disambiguate=subject_id in duplicate_title_ids,
        )
        if collection_type not in set(download_order):
            continue
        if download_subject_ids and subject_id not in download_subject_ids:
            continue
        if max_subjects > 0 and len(processed_subjects) >= max_subjects and subject_id not in processed_subjects:
            continue

        shows = inventory_by_subject.get(subject_id, [])
        if len(shows) != 1 or not is_safe_inventory_subject(shows[0]):
            reason = "inventory_episode_numbering_unusable" if (
                len(shows) == 1 and not _episode_numbering_is_usable(shows[0])
            ) else "inventory_subject_missing_or_ambiguous"
            unresolved.append({"subject_id": subject_id, "title": display,
                               "reason": reason})
            continue
        inventory_show = shows[0]
        if subject_id in invalid_subject_overrides:
            unresolved.append({"subject_id": subject_id, "title": display,
                               "reason": invalid_subject_overrides[subject_id]})
            continue
        missing = missing_episode_map(inventory_show)
        if not missing:
            continue

        override = subject_overrides.get(subject_id)
        if override:
            best, override_error = resolve_mikan_subject_override(mikan, row, override)
            if not best:
                unresolved.append({"subject_id": subject_id, "title": display,
                                   "reason": override_error or "mikan_override_unresolved"})
                continue
        else:
            cached_id = state["subject_to_mikan"].get(subject_id)
            cached_title = state["subject_to_mikan_title"].get(subject_id, "")
            best = validate_cached_mikan_mapping(row, str(cached_id or ""), cached_title)
            # Old ID-only and incompatible mappings are re-searched under the same
            # exact title, season, and format checks as new candidate selection.
            if not best or not best.get("title"):
                candidates = mikan.search_bangumi([subject.get("name_cn") or "", subject.get("name") or ""])
                best = mikan.choose_best_bangumi(row, candidates)
                if best and best.get("bangumi_id"):
                    state["subject_to_mikan"][subject_id] = str(best["bangumi_id"])
                    state["subject_to_mikan_title"][subject_id] = str(best.get("title") or "")
                    if not dry_run:
                        write_json(state_path, state)
        if not best or not best.get("bangumi_id"):
            unresolved.append({"subject_id": subject_id, "title": display,
                               "reason": "mikan_match_not_confident"})
            continue

        rss_items = mikan.fetch_rss_items(str(best["bangumi_id"]))
        dedup_stats["rss_items"] += len(rss_items)
        chosen = pick_best_per_episode(
            rss_items, cfg.get("filters", {}),
            locked_subgroup=state["subject_subgroups"].get(subject_id),
        )
        chosen = apply_episode_limit(chosen, cfg, row, collection_type)
        related = [other for other in collections if other is not row]
        eligible = []
        batch_missing = {
            key: episode for key, episode in missing.items()
            if not has_active_episode_task(state, subject_id, key)
        }
        batch_missing = limit_batch_missing_episodes(batch_missing, row, cfg)
        batch_candidate = None
        if max_batch_episode_span >= 2:
            if _has_nonidentity_episode_numbering(inventory_show):
                has_batch_source = any(
                    (span := extract_episode_span(str(item.get("title") or "")))
                    and span[0] != span[1]
                    and score_item(str(item.get("title") or ""), cfg.get("filters", {}),
                                   locked_subgroup=state["subject_subgroups"].get(subject_id))[0]
                    and item_matches_subject_partition(row, str(item.get("title") or ""), related)
                    for item in rss_items
                )
                if has_batch_source:
                    skipped.append({"subject_id": subject_id, "episode_key": "BATCH",
                                    "reason": "batch_episode_numbering_translation_unsupported"})
            else:
                batch_candidate = choose_exact_batch_candidate(
                    rss_items, cfg.get("filters", {}), batch_missing, inventory_show, row, related,
                    locked_subgroup=state["subject_subgroups"].get(subject_id),
                    max_episode_count=max_batch_episode_span,
                )
        if batch_candidate:
            guid = str(batch_candidate.get("guid") or "")
            torrent_url = batch_candidate.get("torrent_url") or batch_candidate.get("link") or batch_candidate.get("url")
            prior_attempt = state["cloud_tasks"].get(guid) if guid else None
            if (guid and torrent_url
                    and not (isinstance(prior_attempt, dict)
                             and prior_attempt.get("status") not in {"failed", "rejected", "quota_rejected"})):
                batch_keys = list(batch_candidate.get("batch_episode_keys") or [])
                eligible.append((f"BATCH-{batch_keys[0]}-{batch_keys[-1]}", batch_candidate,
                                 str(torrent_url), guid, True, batch_keys))
            else:
                skipped.append({"subject_id": subject_id, "episode_key": "BATCH",
                                "reason": "batch_source_already_attempted_or_incomplete"})
        for item in chosen:
            source_episode_key = inventory_episode_key(item)
            episode_key = candidate_inventory_episode_key(item, inventory_show)
            if not episode_key:
                if source_episode_key and _episode_numbering_info(inventory_show) is not None:
                    skipped.append({"subject_id": subject_id,
                                    "episode_key": source_episode_key,
                                    "reason": "episode_numbering_unmapped_or_ambiguous"})
                continue
            if episode_key not in missing:
                continue
            if not is_single_episode_candidate(item.get("title") or ""):
                continue
            if not item_matches_subject_partition(row, item.get("title") or "", related):
                continue
            guid = str(item.get("guid") or "")
            torrent_url = item.get("torrent_url") or item.get("link") or item.get("url")
            prior_attempt = state["cloud_tasks"].get(guid) if guid else None
            if not guid or not torrent_url:
                continue
            if isinstance(prior_attempt, dict) and prior_attempt.get("status") not in {"failed", "rejected", "quota_rejected"}:
                continue
            if (subject_id, episode_key) in inventory_present or has_active_episode_task(state, subject_id, episode_key):
                skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                "reason": "present_or_task_already_active"})
                continue
            eligible.append((episode_key, item, str(torrent_url), guid, False, [episode_key]))
        dedup_stats["eligible_missing_episodes"] += sum(len(row[5]) for row in eligible)
        if not eligible:
            continue
        dedup_stats["subjects_processed"] += 1
        processed_subjects.add(subject_id)

        for episode_key, item, torrent_url, guid, is_batch, episode_keys in eligible:
            source_episode_key = inventory_episode_key(item)
            if len(submitted) >= run_budget or len(planned) >= run_budget:
                break
            if dry_run:
                planned.append({"subject_id": subject_id, "collection": collection_label,
                                "target_dir": f"{category_dir}/{show_dir}",
                                "episode_key": episode_key, "episode_keys": episode_keys,
                                "batch": is_batch, "title": item.get("title") or ""})
                if is_batch:
                    break
                continue
            if client is None:
                raise RuntimeError("GuangYa client is required outside dry-run")

            resolved = resolve_torrent(client, torrent_url)
            bt_info = ((resolved.get("data") or {}).get("btResInfo") or {})
            info_hash = str(bt_info.get("infoHash") or "")
            if not info_hash:
                skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                "reason": "torrent_info_hash_missing"})
                continue
            if has_active_hash_task(state, remote_tasks, info_hash):
                skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                "reason": "torrent_hash_already_active"})
                continue
            if is_batch:
                if has_external_subtitle_files(bt_info):
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "external_subtitles_not_supported"})
                    continue
                indexes = choose_batch_video_file_indexes(bt_info, episode_keys)
                if not indexes:
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "batch_files_do_not_map_exactly_to_missing_episodes"})
                    continue
                file_name = sanitize_component(f"{show_dir} {episode_key}")
            else:
                unlabelled_single = (item.get("episode_key") == "single"
                                     and not inventory_episode_key(item))
                if unlabelled_single and not is_single_video_resolution(bt_info):
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "unlabelled_single_source_is_not_one_video"})
                    continue
                indexes = choose_single_video_file_index(
                    bt_info, item.get("title") or "", source_episode_key or episode_key,
                )
                if indexes == []:
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "ambiguous_multi_file_torrent"})
                    continue
                if has_external_subtitle_files(bt_info):
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "external_subtitles_not_supported"})
                    continue
                selected = None
                if indexes:
                    selected = next((sub for sub in bt_info.get("subfiles") or []
                                     if int(sub.get("fileIndex", -1)) == int(indexes[0])), None)
                resolved_name = (selected or {}).get("fileName") or bt_info.get("fileName") or ""
                suffix = Path(str(resolved_name)).suffix.lower()
                if suffix not in VIDEO_SUFFIXES:
                    skipped.append({"subject_id": subject_id, "episode_key": episode_key,
                                    "reason": "resolved_video_extension_missing"})
                    continue
                file_name = f"{episode_key}{suffix}"
            category_id = ensure_child_dir(client, root_id, category_dir, children_cache)
            folder_id = ensure_child_dir(client, category_id, show_dir, children_cache)
            existing_episode_files = get_remote_episode_file_index(
                client, folder_id, episode_keys, inventory_show=inventory_show,
            )
            if existing_episode_files:
                skipped.append({
                    "subject_id": subject_id,
                    "episode_key": episode_key,
                    "reason": "target_episode_already_present",
                    "existing_episode_keys": sorted(existing_episode_files),
                    "existing_target_files": existing_episode_files,
                })
                continue
            payload = build_create_task_payload(f"magnet:?xt=urn:btih:{info_hash}", folder_id,
                                                file_name, file_indexes=indexes)
            task_entry = {
                "guid": guid, "subject_id": subject_id, "show": display,
                "collection": collection_label, "category_dir": category_dir,
                "episode_key": episode_key, "episode_keys": episode_keys,
                "batch": is_batch, "source_title": item.get("title") or "",
                "target_dir": f"{category_dir}/{show_dir}", "target_name": file_name,
                "folder_id": folder_id, "task_id": None, "status": "submitting",
                "info_hash": extract_info_hash(info_hash), "file_indexes": indexes or [],
                "selected_indexes": indexes or [],
                "quota_reserved": True, "submitted_at": now(),
            }
            state["cloud_tasks"][guid] = task_entry
            state["subject_locations"][subject_id] = {
                "subject_id": subject_id, "title": display,
                "collection_type": collection_type, "category_dir": category_dir,
                "show_dir": show_dir, "folder_id": folder_id,
            }
            # Persist the reservation before any non-idempotent cloud write.
            write_json(state_path, state)
            try:
                response = create_cloud_task(client, payload)
                task_id = (response.get("data") or {}).get("taskId")
            except GuangYaAPIError as exc:
                task_entry["api_error_code"] = exc.code
                if exc.code == 354:
                    task_entry.update(status="quota_rejected", quota_reserved=False)
                    quota_exhausted = True
                    write_json(state_path, state)
                    break
                if exc.code is None:
                    task_entry.update(status="submission_unknown", quota_reserved=True)
                    unknown_submission = True
                    used_today += 1
                    write_json(state_path, state)
                    break
                task_entry.update(status="rejected", quota_reserved=False)
                api_error_count += 1
                write_json(state_path, state)
                # Code 117 is an authentication failure. Do not fan out more
                # write requests with the same stale credentials in this run.
                if exc.code == 117:
                    stop_after_api_error = True
                    break
                continue
            except Exception:
                task_entry.update(status="submission_unknown", quota_reserved=True)
                unknown_submission = True
                used_today += 1
                write_json(state_path, state)
                break

            if not task_id:
                # A success-shaped response without an id may still be accepted.
                task_entry.update(status="submission_unknown", quota_reserved=True)
                unknown_submission = True
                used_today += 1
                write_json(state_path, state)
                break
            task_entry.update(status="submitted", task_id=str(task_id), quota_reserved=True)
            used_today += 1
            write_json(state_path, state)
            submitted.append({"subject_id": subject_id, "collection": collection_label,
                              "target_dir": f"{category_dir}/{show_dir}", "target_name": file_name,
                              "episode_key": episode_key, "episode_keys": episode_keys,
                              "batch": is_batch, "task_id": str(task_id)})
            matched_subgroup = item.get("matched_subgroup")
            if matched_subgroup and not state["subject_subgroups"].get(subject_id):
                state["subject_subgroups"][subject_id] = matched_subgroup
                write_json(state_path, state)
            if is_batch:
                break
        if quota_exhausted or unknown_submission or stop_after_api_error:
            break
        if not dry_run and len(submitted) >= run_budget:
            break

    state["tracked_subjects"] = tracked_subjects
    state["removed_subject_ids"] = removed_subject_ids
    state["last_run"] = now()
    state["last_cloud_sync"] = now()
    if not dry_run:
        write_json(state_path, state)
        status = ("quota_exhausted" if quota_exhausted else
                  "submission_unknown" if unknown_submission else
                  "api_error" if api_error_count else "done")
        write_progress(status, len(collections), len(collections), len(submitted), run_budget, "",
                       extra={"unresolved": len(unresolved), "api_errors": api_error_count}, enabled=True,
                       progress_path=cfg.get("progress_path", PROGRESS_FILE))

    status = ("quota_exhausted" if quota_exhausted else
              "submission_unknown" if unknown_submission else
              "api_error" if api_error_count else "done")
    return {
        "status": status,
        "submitted_count": len(submitted), "planned_count": len(planned),
        "api_error_count": api_error_count,
        "unresolved_count": len(unresolved), "skipped_count": len(skipped),
        "dry_run": dry_run, "tracked_count": len(collections),
        "tracked_type_counts": type_counts, "removed_subject_ids": removed_subject_ids,
        "download_collection_types": download_order, "daily_quota_limit": daily_limit,
        "daily_quota_used": used_today, "daily_quota_remaining": max(0, daily_limit - used_today),
        "sync_actions": applied_actions, "dedup_stats": dedup_stats,
        "submitted": submitted, "planned": planned, "unresolved": unresolved, "skipped": skipped[:50],
    }


def has_active_episode_task(state: dict, subject_id: str, episode_key: str) -> bool:
    terminal = {"failed", "rejected", "quota_rejected", "completed"}
    for task in (state.get("cloud_tasks") or {}).values():
        if not isinstance(task, dict) or str(task.get("subject_id") or "") != str(subject_id):
            continue
        if task.get("status") in terminal:
            continue
        task_keys = {canonical_episode_key(key) for key in task.get("episode_keys") or []}
        if task_keys and canonical_episode_key(episode_key) in task_keys:
            return True
        task_key = canonical_episode_key(task.get("episode_key") or "")
        if not task_key:
            task_key = canonical_episode_key(extract_episode_label(str(task.get("target_name") or ""), ""))
        if task_key == episode_key:
            return True
    return False


def urllib_basename(url: str) -> str:
    from urllib.parse import urlparse

    return os.path.basename(urlparse(url).path or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Bangumi collections to GuangYa offline download using Mikan")
    parser.add_argument("--config", default=SYNC_CONFIG, help="Path to bangumi-sync JSON config")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Do not submit or write production state")
    mode.add_argument("--apply", action="store_true", help="Run production sync even if config has dry_run=true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = read_json(args.config)
        dry_run = bool(args.dry_run or (cfg.get("dry_run", False) and not args.apply))
        client = None if dry_run else init_client(cfg)
        if dry_run:
            summary = process_collections(cfg, client, dry_run=True)
        else:
            state_path = cfg.get("state_path", DEFAULT_STATE_PATH)
            with single_instance_lock(state_path):
                summary = process_collections(cfg, client, dry_run=False)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary.get("status") in {"submission_unknown", "api_error"} else 0
    except Exception as exc:
        # Exception text from HTTP libraries can contain signed URLs or headers.
        logger.error("Bangumi to GuangYa sync failed (%s)", type(exc).__name__)
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
