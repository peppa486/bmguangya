#!/usr/bin/env python3
"""Build a read-only Bangumi-to-AList library inventory for the GUI.

The successful report keeps the historical ``stats/tree/updated_at`` fields,
then adds an auditable source, freshness, matching, and episode-coverage model.
Only complete scans replace the last successful report. Failed attempts are
written to ``<output>.failure.json`` instead.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests


SCHEMA_VERSION = 1
COLLECTION_CATEGORY = {1: "想看", 2: "看过", 3: "在看", 4: "未标记", 5: "未标记"}
CATEGORY_COLLECTION_TYPE = {"想看": 1, "看过": 2, "在看": 3, "未标记": None}
CATEGORY_ORDER = ("在看", "想看", "看过", "未标记")
VIDEO_SUFFIXES = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m2ts", ".ts",
    ".webm", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob", ".m4v",
}
DEFAULT_OUTPUT = "/var/lib/bmguangya/library.json"
DEFAULT_ALIST_ROOT = "/guangya/Anime"
DEFAULT_FRESHNESS_TTL = 900
DEFAULT_EPISODE_CACHE_TTL = 24 * 60 * 60
MAX_WORKERS = 4
DEFAULT_MAX_RUNTIME_SECONDS = 1800
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_PROGRESS_INTERVAL_SECONDS = 15


class InventoryError(RuntimeError):
    def __init__(self, message: str, *, stage: str, code: str = "inventory_error",
                 details: Optional[dict] = None):
        super().__init__(message)
        self.issue = {"stage": stage, "code": code, "message": message}
        if details:
            self.issue.update(details)


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


def age_seconds(value: Any, now: Optional[dt.datetime] = None) -> Optional[int]:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int(((now or utc_now()) - parsed).total_seconds()))


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def normalize_config(raw: dict) -> dict:
    alist = raw.get("alist") if isinstance(raw.get("alist"), dict) else {}
    bangumi = raw.get("bangumi") if isinstance(raw.get("bangumi"), dict) else {}
    cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
    inventory = raw.get("inventory") if isinstance(raw.get("inventory"), dict) else {}
    guangya = raw.get("guangya") if isinstance(raw.get("guangya"), dict) else {}

    public_url = _first_value(alist.get("public_url"), alist.get("url"),
                              alist.get("base_url"), raw.get("alist_public_url"), alist.get("api_base"),
                              raw.get("alist_api_base"))
    if not public_url:
        raise InventoryError("missing alist.public_url or alist_public_url", stage="config", code="missing_config")
    public_url = str(public_url).rstrip("/")
    api_base = _first_value(alist.get("api_base"), raw.get("alist_api_base"), public_url)
    api_base = str(api_base).rstrip("/")

    root = _first_value(alist.get("root"), raw.get("alist_root"), raw.get("alist_path"), DEFAULT_ALIST_ROOT)
    root = str(root).strip()
    if not root.startswith("/"):
        root = "/" + root
    path_parts = [part for part in root.split("/") if part and part != "."]
    if any(part == ".." for part in path_parts) or ".." in root.split("/"):
        raise InventoryError("alist.root cannot contain '..'", stage="config", code="invalid_config")
    root = "/" + "/".join(path_parts)
    if root == "//" or not root:
        root = "/"

    alist_token = _first_value(alist.get("token"), alist.get("access_token"), raw.get("alist_token"))
    alist_username = _first_value(alist.get("username"), raw.get("alist_username"))
    alist_password = _first_value(alist.get("password"), raw.get("alist_password"))
    if not alist_token and (not alist_username or alist_password is None):
        raise InventoryError("AList token or username/password is required", stage="config", code="missing_config")

    username = _first_value(bangumi.get("username"), raw.get("bangumi_username"))
    access_token = _first_value(bangumi.get("access_token"), raw.get("bangumi_access_token"))
    if not username or not access_token:
        raise InventoryError("bangumi.username and bangumi.access_token are required", stage="config", code="missing_config")

    workers = int(_first_value(alist.get("max_workers"), inventory.get("max_workers"), 4))
    max_attempts = int(_first_value(alist.get("max_attempts"), alist.get("retry_attempts"),
                                    bangumi.get("max_attempts"), bangumi.get("retry_attempts"),
                                    DEFAULT_MAX_ATTEMPTS))
    retry_backoff = float(_first_value(alist.get("retry_backoff_seconds"),
                                       bangumi.get("retry_backoff_seconds"),
                                       DEFAULT_RETRY_BACKOFF_SECONDS))
    if max_attempts < 1 or max_attempts > 5:
        raise InventoryError("max_attempts must be between 1 and 5", stage="config", code="invalid_config")
    if retry_backoff < 0 or retry_backoff > 30:
        raise InventoryError("retry_backoff_seconds must be between 0 and 30", stage="config", code="invalid_config")
    max_runtime = int(_first_value(inventory.get("max_runtime_seconds"), DEFAULT_MAX_RUNTIME_SECONDS))
    if max_runtime < 1:
        raise InventoryError("inventory.max_runtime_seconds must be positive", stage="config", code="invalid_config")
    cloud_source = str(_first_value(inventory.get("cloud_source"), "alist")).strip().lower()
    if cloud_source not in {"alist", "guangya_direct"}:
        raise InventoryError("inventory.cloud_source must be 'alist' or 'guangya_direct'",
                             stage="config", code="invalid_config")
    progress_interval = float(_first_value(inventory.get("progress_interval_seconds"),
                                            DEFAULT_PROGRESS_INTERVAL_SECONDS))
    if not 1 <= progress_interval <= 300:
        raise InventoryError("inventory.progress_interval_seconds must be between 1 and 300",
                             stage="config", code="invalid_config")
    return {
        "alist": {
            "public_url": public_url,
            "api_base": api_base,
            "root": root,
            "token": str(alist_token) if alist_token else "",
            "username": str(alist_username) if alist_username else "",
            "password": str(alist_password) if alist_password is not None else "",
            "timeout_seconds": int(_first_value(alist.get("timeout_seconds"), 60)),
            "max_workers": min(MAX_WORKERS, max(1, workers)),
            "max_depth": int(_first_value(alist.get("max_depth"), 32)),
            "max_directories": int(_first_value(alist.get("max_directories"), 20000)),
            "page_size": min(1000, max(1, int(_first_value(alist.get("page_size"), 100)))),
            "max_attempts": max_attempts,
            "retry_backoff_seconds": retry_backoff,
            "max_runtime_seconds": max_runtime,
        },
        "bangumi": {
            "base_url": str(_first_value(bangumi.get("base_url"), raw.get("bangumi_base_url"), "https://api.bgm.tv")).rstrip("/"),
            "username": str(username),
            "access_token": str(access_token),
            "user_agent": str(_first_value(bangumi.get("user_agent"), "bmguangya-library-inventory/1.0")),
            "timeout_seconds": int(_first_value(bangumi.get("timeout_seconds"), 45)),
            "page_size": min(100, max(1, int(_first_value(bangumi.get("page_size"), 100)))),
            "max_attempts": max_attempts,
            "retry_backoff_seconds": retry_backoff,
            "max_runtime_seconds": max_runtime,
        },
        "cache": {
            "path": _first_value(cache.get("path"), inventory.get("cache_path")),
            "ttl_seconds": int(_first_value(cache.get("ttl_seconds"), DEFAULT_EPISODE_CACHE_TTL)),
        },
        "inventory": {
            "output": _first_value(inventory.get("output"), raw.get("inventory_output"), DEFAULT_OUTPUT),
            "progress_path": _first_value(inventory.get("progress_path"),
                                           raw.get("inventory_progress_path")),
            "progress_interval_seconds": progress_interval,
            "freshness_ttl_seconds": int(_first_value(inventory.get("freshness_ttl_seconds"), DEFAULT_FRESHNESS_TTL)),
            "max_runtime_seconds": max_runtime,
            "cloud_source": cloud_source,
            "guangya_api_base": str(_first_value(guangya.get("api_base"), "https://api.guangyapan.com")).rstrip("/"),
        },
    }


def _request_timeout(cfg: dict, *, stage: str) -> float:
    deadline = cfg.get("runtime_deadline_monotonic")
    timeout = float(cfg["timeout_seconds"])
    if deadline is None:
        return timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise InventoryError("inventory exceeded its {} second runtime limit".format(
            cfg.get("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS)),
            stage=stage, code="runtime_limit")
    return min(timeout, remaining)


def _request_with_retries(request_fn, url: str, *, cfg: dict, stage: str, **kwargs) -> requests.Response:
    attempts = int(cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    backoff = float(cfg.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS))
    last_error = None
    for attempt in range(attempts):
        timeout = _request_timeout(cfg, stage=stage)
        try:
            response = request_fn(url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            retryable = True
        else:
            status = int(getattr(response, "status_code", 200))
            if status not in (408, 429) and status < 500:
                return response
            last_error = requests.HTTPError("HTTP {}".format(status))
            retryable = True
        if not retryable or attempt + 1 >= attempts:
            break
        remaining = None
        deadline = cfg.get("runtime_deadline_monotonic")
        if deadline is not None:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                break
        delay = backoff * (2 ** attempt)
        if remaining is not None:
            delay = min(delay, remaining)
        if delay > 0:
            time.sleep(delay)
    if cfg.get("runtime_deadline_monotonic") is not None and time.monotonic() >= cfg["runtime_deadline_monotonic"]:
        _request_timeout(cfg, stage=stage)
    raise InventoryError("HTTP request failed after {} attempt(s): {} ({})".format(
        min(attempts, attempt + 1), last_error, url), stage=stage, code="http_error") from last_error


def _json_response(response: requests.Response, *, stage: str, url: str) -> dict:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise InventoryError("HTTP request failed: {} ({})".format(exc, url), stage=stage, code="http_error") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise InventoryError("API returned invalid JSON ({})".format(url), stage=stage, code="invalid_json") from exc
    if not isinstance(body, dict):
        raise InventoryError("API JSON root was not an object ({})".format(url), stage=stage, code="invalid_response")
    return body


class AlistClient:
    """AList read-only client; each scan worker owns its own requests.Session."""

    def __init__(self, cfg: dict, session_factory=requests.Session):
        self.cfg = cfg
        self.session_factory = session_factory
        self._local = threading.local()
        self._token = cfg["alist"]["token"]
        self._token_lock = threading.Lock()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self.session_factory()
            session.headers.update({"Accept": "application/json", "User-Agent": "bmguangya-library-inventory/1.0"})
            self._local.session = session
        return session

    def _login(self) -> str:
        alist = self.cfg["alist"]
        url = alist["api_base"] + "/api/auth/login"
        response = _request_with_retries(
            self._session().post, url, cfg=alist, stage="alist_auth",
            json={"username": alist["username"], "password": alist["password"]},
        )
        body = _json_response(response, stage="alist_auth", url=url)
        if body.get("code") != 200:
            raise InventoryError("AList login business code was {}: {}".format(body.get("code"), body.get("message", "")),
                                 stage="alist_auth", code="business_error")
        data = body.get("data") or {}
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise InventoryError("AList login response did not contain a token", stage="alist_auth", code="invalid_response")
        return str(token)

    def ensure_token(self) -> None:
        if self._token:
            return
        with self._token_lock:
            if not self._token:
                self._token = self._login()

    def list_page(self, remote_path: str, page: int, page_size: int, refresh: bool = True) -> dict:
        self.ensure_token()
        alist = self.cfg["alist"]
        url = alist["api_base"] + "/api/fs/list"
        headers = {"Authorization": self._token}
        request_body = {"path": remote_path, "password": "", "page": page,
                        "per_page": page_size, "refresh": bool(refresh)}
        try:
            response = _request_with_retries(self._session().post, url, cfg=alist, stage="alist_scan",
                                             json=request_body, headers=headers)
        except InventoryError as exc:
            if "path" not in exc.issue:
                exc.issue["path"] = remote_path
            raise
        body = _json_response(response, stage="alist_scan", url=url)
        if body.get("code") != 200:
            raise InventoryError("AList list business code was {} at {}: {}".format(
                body.get("code"), remote_path, body.get("message", "")),
                stage="alist_scan", code="business_error", details={"path": remote_path})
        data = body.get("data")
        if not isinstance(data, dict):
            raise InventoryError("AList list response has no data object at {}".format(remote_path),
                                 stage="alist_scan", code="invalid_response", details={"path": remote_path})
        content = data.get("content")
        if content is None:
            content = []
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            raise InventoryError("AList content is not a list of entries at {}".format(remote_path),
                                 stage="alist_scan", code="invalid_response", details={"path": remote_path})
        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise InventoryError("AList response has no valid total at {}".format(remote_path),
                                 stage="alist_scan", code="invalid_pagination", details={"path": remote_path})
        return {"content": content, "total": total}

    def list_all(self, remote_path: str, page_size: int, max_pages: int = 10000) -> List[dict]:
        entries: List[dict] = []
        page = 1
        total: Optional[int] = None
        while True:
            if page > max_pages:
                raise InventoryError("AList pagination exceeded {} pages at {}".format(max_pages, remote_path),
                                     stage="alist_scan", code="pagination_limit", details={"path": remote_path})
            # Refresh once to establish a current AList listing, then read the
            # remaining pages from that same server-side cached snapshot.
            # Refreshing every page can force AList to rescan a large remote
            # directory repeatedly and produce quadratic work or drifting pages.
            result = self.list_page(remote_path, page, page_size, refresh=(page == 1))
            chunk = result["content"]
            if total is None:
                total = result["total"]
            elif result["total"] != total:
                raise InventoryError("AList total changed between pages at {}".format(remote_path),
                                     stage="alist_scan", code="unstable_pagination", details={"path": remote_path})
            if len(chunk) > page_size or len(entries) + len(chunk) > total:
                raise InventoryError("AList page exceeds the declared total at {}".format(remote_path),
                                     stage="alist_scan", code="invalid_pagination", details={"path": remote_path})
            entries.extend(chunk)
            if len(entries) == total:
                break
            if not chunk:
                raise InventoryError("AList pagination stopped at {} of {} entries for {}".format(
                    len(entries), total, remote_path), stage="alist_scan", code="truncated_pagination",
                    details={"path": remote_path})
            page += 1
        names = set()
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip() or name in (".", "..") or "/" in name or "\\" in name:
                raise InventoryError("AList returned an invalid entry name at {}".format(remote_path),
                                     stage="alist_scan", code="invalid_entry_name", details={"path": remote_path})
            if name in names:
                raise InventoryError("AList returned duplicate entry name {} at {}".format(name, remote_path),
                                     stage="alist_scan", code="duplicate_entry", details={"path": remote_path})
            names.add(name)
        return entries


class BangumiClient:
    def __init__(self, cfg: dict, session_factory=requests.Session):
        self.cfg = cfg["bangumi"]
        self.session_factory = session_factory
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self.session_factory()
            session.headers.update({
                "Accept": "application/json",
                "User-Agent": self.cfg["user_agent"],
                "Authorization": "Bearer " + self.cfg["access_token"],
            })
            self._local.session = session
        return session

    def _get_page(self, url: str, params: dict, *, stage: str = "bangumi") -> dict:
        try:
            response = _request_with_retries(self._session().get, url, cfg=self.cfg, stage=stage, params=params)
        except InventoryError:
            raise
        return _json_response(response, stage=stage, url=url)

    @staticmethod
    def _pagination_fields(payload: dict, *, stage: str, offset: int) -> Tuple[List[dict], int]:
        chunk = payload.get("data")
        total = payload.get("total")
        if not isinstance(chunk, list) or any(not isinstance(row, dict) for row in chunk):
            raise InventoryError("Bangumi response has no data list at offset {}".format(offset),
                                 stage=stage, code="invalid_pagination")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise InventoryError("Bangumi response has no valid total at offset {}".format(offset),
                                 stage=stage, code="invalid_pagination")
        return chunk, total

    def collections(self) -> List[dict]:
        rows: List[dict] = []
        seen: Dict[str, int] = {}
        limit = self.cfg["page_size"]
        user = quote(self.cfg["username"], safe="")
        url = "{}/v0/users/{}/collections".format(self.cfg["base_url"], user)
        for collection_type in (1, 2, 3, 4, 5):
            offset = 0
            expected_total: Optional[int] = None
            received = 0
            while True:
                payload = self._get_page(url, {
                    "subject_type": 2, "type": collection_type, "limit": limit, "offset": offset,
                }, stage="bangumi_collections")
                chunk, total = self._pagination_fields(payload, stage="bangumi_collections", offset=offset)
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    raise InventoryError("Bangumi collection total changed during paging for type {}".format(collection_type),
                                         stage="bangumi_collections", code="unstable_pagination")
                if len(chunk) > limit or received + len(chunk) > total:
                    raise InventoryError("Bangumi collection page exceeds total for type {}".format(collection_type),
                                         stage="bangumi_collections", code="invalid_pagination")
                for row in chunk:
                    subject = row.get("subject") or {}
                    subject_id = str(row.get("subject_id") or subject.get("id") or "")
                    if not subject_id:
                        raise InventoryError("Bangumi collection row is missing subject_id", stage="bangumi_collections",
                                             code="invalid_subject")
                    if subject_id in seen:
                        raise InventoryError("Bangumi subject {} appeared more than once (types {} and {})".format(
                            subject_id, seen[subject_id], collection_type), stage="bangumi_collections",
                            code="duplicate_subject", details={"subject_id": subject_id})
                    seen[subject_id] = collection_type
                    row = dict(row)
                    row["subject_id"] = subject_id
                    row["collection_type"] = collection_type
                    row["subject"] = subject if isinstance(subject, dict) else {}
                    rows.append(row)
                received += len(chunk)
                if received == total:
                    break
                if not chunk:
                    raise InventoryError("Bangumi paging stopped at {} of {} rows for type {}".format(
                        received, total, collection_type), stage="bangumi_collections", code="truncated_pagination")
                offset += len(chunk)
        return rows

    def episodes(self, subject_id: str) -> List[dict]:
        url = self.cfg["base_url"] + "/v0/episodes"
        limit = self.cfg["page_size"]
        offset = 0
        expected_total: Optional[int] = None
        received = 0
        items: List[dict] = []
        seen_ids = set()
        while True:
            payload = self._get_page(url, {"subject_id": subject_id, "type": 0, "limit": limit, "offset": offset},
                                     stage="bangumi_episodes")
            chunk, total = self._pagination_fields(payload, stage="bangumi_episodes", offset=offset)
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise InventoryError("Bangumi episode total changed for subject {}".format(subject_id),
                                     stage="bangumi_episodes", code="unstable_pagination",
                                     details={"subject_id": subject_id})
            if len(chunk) > limit or received + len(chunk) > total:
                raise InventoryError("Bangumi episode page exceeds total for subject {}".format(subject_id),
                                     stage="bangumi_episodes", code="invalid_pagination",
                                     details={"subject_id": subject_id})
            for episode in chunk:
                episode_id = str(episode.get("id") or "")
                if episode_id and episode_id in seen_ids:
                    raise InventoryError("duplicate Bangumi episode id {} for subject {}".format(episode_id, subject_id),
                                         stage="bangumi_episodes", code="duplicate_episode",
                                         details={"subject_id": subject_id})
                if episode_id:
                    seen_ids.add(episode_id)
                ep_subject_id = episode.get("subject_id")
                if ep_subject_id is not None and str(ep_subject_id) != subject_id:
                    raise InventoryError("Bangumi episode belongs to a different subject ({})".format(subject_id),
                                         stage="bangumi_episodes", code="subject_mismatch",
                                         details={"subject_id": subject_id})
                ep_type = episode.get("type")
                if ep_type is not None and ep_type != 0:
                    raise InventoryError("Bangumi returned non-ordinary episode type for subject {}".format(subject_id),
                                         stage="bangumi_episodes", code="episode_type_mismatch",
                                         details={"subject_id": subject_id})
                items.append(episode)
            received += len(chunk)
            if received == total:
                break
            if not chunk:
                raise InventoryError("Bangumi episode paging stopped at {} of {} for subject {}".format(
                    received, total, subject_id), stage="bangumi_episodes", code="truncated_pagination",
                    details={"subject_id": subject_id})
            offset += len(chunk)
        return items


def title_key(value: str) -> str:
    text = re.sub(r"\s*\[bgm-\d+\]\s*$", "", str(value or ""), flags=re.I)
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]", "", text)


def subject_titles(row: dict) -> List[str]:
    subject = row.get("subject") or {}
    values = [subject.get("name_cn"), subject.get("name"), row.get("title"), row.get("name")]
    titles: List[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and title_key(value) and value not in titles:
            titles.append(value)
    return titles


def ep_key(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    # Bangumi uses sort=0 for a real main-story episode such as 第0話.
    if not number.is_finite() or number < 0:
        return None
    normalized = number.normalize()
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return format(normalized, "f").rstrip("0").rstrip(".")


def episode_key(episode: dict) -> Optional[str]:
    return ep_key(episode.get("sort") if episode.get("sort") not in (None, "") else episode.get("ep"))


def episode_sort_key(value: str) -> Tuple[int, Decimal, str]:
    try:
        return (0, Decimal(value), str(value))
    except InvalidOperation:
        return (1, Decimal(0), str(value))


def aired_state(episode: dict, today: Optional[dt.date] = None) -> Tuple[str, Optional[str]]:
    raw = str(episode.get("airdate") or "").strip()
    if not raw:
        return "unknown", None
    try:
        airdate = dt.date.fromisoformat(raw[:10])
    except ValueError:
        return "unknown", raw
    return ("aired" if airdate <= (today or dt.date.today()) else "future"), airdate.isoformat()


def episode_records(raw_episodes: Sequence[dict], subject_id: str,
                    today: Optional[dt.date] = None) -> List[dict]:
    output = []
    seen_keys = set()
    for index, raw in enumerate(raw_episodes, start=1):
        key = episode_key(raw)
        mapping_issue = None
        if key is None:
            mapping_issue = {
                "stage": "episode_mapping", "code": "unknown_episode_number",
                "message": "Bangumi episode has no numeric sort/ep; coverage is unknown",
                "subject_id": subject_id, "episode_id": raw.get("id"),
            }
        elif key in seen_keys:
            mapping_issue = {
                "stage": "episode_mapping", "code": "duplicate_episode_number",
                "message": "Bangumi has duplicate ordinary episode number {}; coverage is unknown".format(key),
                "subject_id": subject_id, "episode_id": raw.get("id"), "episode_key": key,
            }
        if mapping_issue:
            # Keep every source row visible and unique without inventing an episode
            # number. This makes the affected show reviewable while disabling its
            # automatic missing-episode decisions.
            key = "UNKNOWN-{}-{}".format(raw.get("id") or "ROW", index)
            mapping_issue["sort"] = raw.get("sort")
            mapping_issue["ep"] = raw.get("ep")
        else:
            seen_keys.add(key)
        air_state_value, airdate = aired_state(raw, today=today)
        record = {
            "key": key,
            "episode_id": raw.get("id"),
            "sort": raw.get("sort"),
            "ep": raw.get("ep"),
            "name": raw.get("name") or "",
            "name_cn": raw.get("name_cn") or "",
            "airdate": airdate,
            "air_state": air_state_value,
            "present": [],
            "missing": False,
            "coverage_unknown": mapping_issue is not None,
        }
        if mapping_issue:
            record["mapping_issue"] = mapping_issue
            record["source_episode"] = dict(raw)
        output.append(record)
    return sorted(output, key=lambda item: episode_sort_key(item["key"]))


def parse_video_episode(filename: str) -> Optional[str]:
    """Parse an unambiguous episode number from a video filename stem."""
    stem = Path(filename).stem
    upper = stem.upper()
    if re.search(r"(?<!\d)\d{1,3}(?:v\d+)?\s*[-~至]\s*\d{1,3}(?!\d)", stem, flags=re.I):
        return None
    if re.search(r"(?<![A-Z0-9])(?:NCOP|NCED|SP|OVA|OAD)(?:\s*[-_ ]?\d{0,3})(?![A-Z0-9])", upper):
        return None
    patterns = (
        r"\bS\d{1,2}\s*E\s*(\d{1,3}(?:\.\d+)?)\b",
        r"\bEP\s*[-_ ]?\s*(\d{1,3}(?:\.\d+)?)\b",
        r"\bE\s*[-_ ]?\s*(\d{1,3}(?:\.\d+)?)\b",
        r"第\s*(\d{1,3}(?:\.\d+)?)\s*[话話集]",
        r"^[\s\[【(（._-]*(\d{1,3}(?:\.\d+)?)(?:v\d+)?[\s\]】)）._-]*$",
        r"(?:^|[\s\[【(（._-])(\d{1,3}(?:\.\d+)?)(?:v\d+)?(?=$|[\s\]】)）._-])",
    )
    for pattern in patterns:
        match = re.search(pattern, stem, flags=re.I)
        if match:
            key = ep_key(match.group(1))
            if key is not None:
                return key
    return None


def _is_dir(entry: dict) -> bool:
    flag = entry.get("is_dir")
    if flag is True or (isinstance(flag, str) and flag.strip().lower() in ("true", "1", "yes")):
        return True
    return str(entry.get("type") or "").lower() in ("dir", "directory", "folder")


def _normalize_gcid(value: Any) -> Optional[str]:
    """Return a canonical GuangYa content id only when it is a 40-digit hash."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        return None
    return value.lower()


def _join_remote(parent: str, name: str) -> str:
    return (parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name


class CloudScanner:
    def __init__(self, cfg: dict, client: Optional[AlistClient] = None, *, workers: Optional[int] = None,
                 max_depth: Optional[int] = None, max_directories: Optional[int] = None,
                 progress: Optional["InventoryProgress"] = None):
        self.cfg = cfg
        self.client = client or AlistClient(cfg)
        self.workers = min(MAX_WORKERS, max(1, int(workers or cfg["alist"]["max_workers"])))
        self.max_depth = int(max_depth if max_depth is not None else cfg["alist"]["max_depth"])
        self.max_directories = int(max_directories if max_directories is not None else cfg["alist"]["max_directories"])
        self.errors: List[dict] = []
        self._listed: Dict[str, dict] = {}
        self.progress = progress

    def _list_directory(self, path: str) -> List[dict]:
        return self.client.list_all(path, self.cfg["alist"]["page_size"])

    def scan(self) -> dict:
        root_path = self.cfg["alist"]["root"]
        pending = [(root_path, 0)]
        seen = {root_path}
        started = time.monotonic()
        completed = 0
        attempted = 0
        if self.progress:
            self.progress.publish(
                status="running", phase="cloud_scan", root=root_path,
                directories_discovered=1, directories_started=0, directories_completed=0,
                directories_succeeded=0, directories_failed=0, directories_pending=1,
                files_seen=0, active_paths=[], errors=self.errors, force=True,
            )

        def record_directory_error(path: str, exc: Exception) -> None:
            if isinstance(exc, InventoryError):
                issue = exc.issue.copy()
                issue.setdefault("error_type", type(exc).__name__)
            else:
                source_code = getattr(exc, "code", None)
                issue = {"stage": "alist_scan", "code": "directory_error",
                         "error_type": type(exc).__name__, "message": str(exc)}
                if source_code is not None:
                    # GuangYa's adapter deliberately exposes only its business
                    # error code here, never the original response or token.
                    issue["source_error_code"] = source_code
            issue.setdefault("stage", "alist_scan")
            issue.setdefault("path", path)
            self.errors.append(issue)

        def publish(active_paths: Sequence[str], *, force: bool = False) -> None:
            if not self.progress:
                return
            active_pending = len(active_paths)
            self.progress.publish(
                status="running", phase="cloud_scan", root=root_path,
                directories_discovered=len(seen), directories_started=attempted,
                directories_completed=completed,
                directories_succeeded=len(self._listed),
                directories_failed=len(self.errors),
                directories_pending=len(pending) + active_pending,
                files_seen=sum(sum(1 for entry in item["entries"] if not _is_dir(entry))
                               for item in self._listed.values()),
                active_paths=list(active_paths), errors=self.errors, force=force,
            )

        futures = {}
        deadline_reached = False
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while pending or futures:
                if not deadline_reached:
                    try:
                        _request_timeout(self.cfg["alist"], stage="alist_scan")
                    except InventoryError as exc:
                        self.errors.append(exc.issue)
                        deadline_reached = True

                while pending and len(futures) < self.workers and not deadline_reached:
                    path, depth = pending.pop(0)
                    futures[pool.submit(self._list_directory, path)] = (path, depth)
                    attempted += 1
                active_paths = [details[0] for details in futures.values()]
                publish(active_paths)

                if not futures:
                    break
                interval = self.progress.interval_seconds if self.progress else DEFAULT_PROGRESS_INTERVAL_SECONDS
                done, _ = wait(tuple(futures), timeout=interval, return_when=FIRST_COMPLETED)
                if not done:
                    # Keep a durable heartbeat with the exact directory paths
                    # currently waiting on the remote API.
                    publish([details[0] for details in futures.values()], force=True)
                    continue

                for future in done:
                    path, depth = futures.pop(future)
                    completed += 1
                    try:
                        entries = future.result()
                        self._listed[path] = {"path": path, "depth": depth, "entries": entries}
                    except Exception as exc:
                        record_directory_error(path, exc)
                        continue
                    for entry in entries:
                        if not _is_dir(entry):
                            continue
                        name = str(entry.get("name") or "")
                        if not name or name in (".", "..") or "/" in name or "\\" in name:
                            self.errors.append({"stage": "alist_scan", "code": "invalid_entry_name",
                                                "message": "AList returned an invalid directory name", "path": path})
                            continue
                        child_path = _join_remote(path, name)
                        if child_path in seen:
                            self.errors.append({"stage": "alist_scan", "code": "directory_cycle",
                                                "message": "AList directory path repeated", "path": child_path})
                            continue
                        if depth + 1 > self.max_depth:
                            self.errors.append({"stage": "alist_scan", "code": "depth_limit",
                                                "message": "Maximum directory depth reached", "path": child_path})
                            continue
                        if len(seen) >= self.max_directories:
                            self.errors.append({"stage": "alist_scan", "code": "directory_limit",
                                                "message": "Maximum directory count reached", "path": child_path})
                            continue
                        seen.add(child_path)
                        pending.append((child_path, depth + 1))
                publish([details[0] for details in futures.values()])

        final_status = "complete" if not self.errors and not pending else "incomplete"
        if self.progress:
            self.progress.publish(
                status=final_status, phase="cloud_scan_finished", root=root_path,
                directories_discovered=len(seen), directories_started=attempted,
                directories_completed=completed, directories_succeeded=len(self._listed),
                directories_failed=len(self.errors), directories_pending=len(pending),
                files_seen=sum(sum(1 for entry in item["entries"] if not _is_dir(entry))
                               for item in self._listed.values()),
                active_paths=[], errors=self.errors, elapsed_seconds=round(time.monotonic() - started, 1),
                force=True,
            )
        return self._build_tree(root_path)

    def _build_tree(self, root_path: str) -> dict:
        def build(path: str, name: str, entry_meta: Optional[dict] = None) -> dict:
            listed = self._listed.get(path)
            if listed is None:
                return {
                    "name": name, "type": "directory", "path": _relative_to_root(root_path, path),
                    "children": [], "size": None, "modified": None, "empty": False, "incomplete": True,
                }
            children = []
            for entry in listed["entries"]:
                child_name = str(entry.get("name") or "")
                if not child_name or child_name in (".", "..") or "/" in child_name or "\\" in child_name:
                    continue
                child_path = _join_remote(path, child_name)
                if _is_dir(entry):
                    child = build(child_path, child_name, entry)
                else:
                    child = {
                        "name": child_name,
                        "type": "file",
                        "path": _relative_to_root(root_path, child_path),
                        "size": entry.get("size"),
                        "modified": entry.get("modified"),
                        "file_id": entry.get("file_id") or entry.get("id"),
                    }
                    gcid = _normalize_gcid(entry.get("gcid"))
                    if gcid is not None:
                        child["gcid"] = gcid
                children.append(child)
            children.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold(), item["name"]))
            node = {
                "name": name, "type": "directory", "path": _relative_to_root(root_path, path),
                "children": children, "size": (entry_meta or {}).get("size"),
                "modified": (entry_meta or {}).get("modified"),
                "file_id": (entry_meta or {}).get("file_id") or (entry_meta or {}).get("id"),
                "empty": not children,
            }
            if any(error.get("path") == path for error in self.errors):
                node["incomplete"] = True
            return node
        return build(root_path, PurePosixPath(root_path).name or "/")


def _relative_to_root(root: str, path: str) -> str:
    root_parts = PurePosixPath(root).parts
    path_parts = PurePosixPath(path).parts
    if path_parts[:len(root_parts)] != root_parts:
        raise ValueError("path is outside configured AList root")
    return "/".join(path_parts[len(root_parts):])


def _walk_files(node: dict) -> Iterable[dict]:
    for child in node.get("children", []):
        if child.get("type") == "directory":
            yield from _walk_files(child)
        else:
            yield child


def _walk_dirs(node: dict, *, include_self: bool = False) -> Iterable[dict]:
    if include_self:
        yield node
    for child in node.get("children", []):
        if child.get("type") == "directory":
            yield child
            yield from _walk_dirs(child)


def _file_summary(file_node: dict) -> dict:
    summary = {"name": file_node["name"], "relative_path": file_node["path"], "path": file_node["path"],
               "size": file_node.get("size"), "modified": file_node.get("modified"),
               "file_id": file_node.get("file_id")}
    gcid = _normalize_gcid(file_node.get("gcid"))
    if gcid is not None:
        summary["gcid"] = gcid
    return summary


def _cloud_counts(root: dict) -> dict:
    dirs = list(_walk_dirs(root))
    files = list(_walk_files(root))
    videos = [item for item in files if Path(item["name"]).suffix.lower() in VIDEO_SUFFIXES]
    return {"cloud_directories": len(dirs), "cloud_files": len(files), "cloud_video_files": len(videos)}


def _episode_file_match(video: dict, expected_keys: set) -> Optional[str]:
    parsed = parse_video_episode(video["name"])
    return parsed if parsed in expected_keys else None


def show_node_from_cloud(cloud_node: dict, *, subject: Optional[dict] = None,
                         episodes: Optional[List[dict]] = None, ambiguous: bool = False,
                         unmatched: bool = False, expected_known: bool = False,
                         target_category: Optional[str] = None,
                         coverage_unknown: bool = False,
                         coverage_issues: Optional[List[dict]] = None) -> dict:
    videos = [item for item in _walk_files(cloud_node)
              if Path(item["name"]).suffix.lower() in VIDEO_SUFFIXES]
    files_by_episode: Dict[str, List[dict]] = {}
    extras = []
    episode_rows = [dict(item, present=[]) for item in (episodes or [])]
    by_key = {item["key"]: item for item in episode_rows}
    for video in videos:
        summary = _file_summary(video)
        parsed = parse_video_episode(video["name"])
        if parsed is not None and parsed in by_key:
            by_key[parsed]["present"].append(summary)
            files_by_episode.setdefault(parsed, []).append(summary)
        else:
            reason = "episode_not_in_bangumi" if parsed is not None else "unrecognized_episode_filename"
            extras.append({**summary, "parsed_episode": parsed, "reason": reason})

    today = dt.date.today()
    # A collection row itself carries an unambiguous Bangumi subject id even when
    # the cloud directory has not been created yet. In that case aired episodes
    # are confirmed absent from the inventory and can be safely seeded by the
    # downloader. Ambiguous subject/folder matches and missing episode metadata
    # never produce download candidates.
    eligible_for_comparison = bool(subject is not None and expected_known and not ambiguous and not coverage_unknown)
    missing = []
    expected_keys = [item["key"] for item in episode_rows]
    for item in episode_rows:
        item["episode_key"] = _episode_label(item["key"])
        item["is_special"] = False
        present_files = item["present"]
        item["present_files"] = present_files
        item["present"] = bool(present_files)
        item["relative_path"] = present_files[0]["relative_path"] if present_files else None
        item["file_name"] = present_files[0]["name"] if present_files else None
        item["cloud_file_name"] = item["file_name"]
        item["file_id"] = present_files[0].get("file_id") if present_files else None
        item["missing"] = (eligible_for_comparison and item["air_state"] == "aired"
                            and not item["present"])
        if item["missing"]:
            missing.append(item["episode_key"])
    present = sorted((_episode_label(key) for key in files_by_episode), key=_episode_label_sort_key)
    future = [_episode_label(item["key"]) for item in episode_rows if item["air_state"] == "future"]
    unknown_airdate = [_episode_label(item["key"]) for item in episode_rows if item["air_state"] == "unknown"]
    duplicates = [_episode_label(key) for key, files in files_by_episode.items() if len(files) > 1]
    extra_episode_rows_by_key: Dict[str, dict] = {}
    for extra in extras:
        label, is_special = parse_episode_label(extra["name"])
        episode_label = label or "FILE:" + extra["relative_path"]
        item = extra_episode_rows_by_key.get(episode_label)
        if item is None:
            item = {
                "episode_key": episode_label,
                "episode_id": None,
                "sort": extra.get("parsed_episode"),
                "present": True,
                "missing": False,
                "relative_path": extra["relative_path"],
                "file_id": extra.get("file_id"),
                "file_name": extra["name"],
                "cloud_file_name": extra["name"],
                "is_special": is_special,
                "airdate": None,
                "air_state": "extra",
                "reason": extra["reason"],
                "present_files": [],
            }
            extra_episode_rows_by_key[episode_label] = item
        item["present_files"].append(extra)
    title = ""
    subject_id = None
    collection_type = None
    if subject is not None:
        subject_data = subject.get("subject") or {}
        title_candidates = subject_titles(subject)
        title = title_candidates[0] if title_candidates else "subject-{}".format(subject.get("subject_id", "unknown"))
        subject_id = str(subject.get("subject_id") or "") or None
        collection_type = int(subject.get("collection_type") or 0)
    else:
        title = cloud_node["name"]

    total_eps = len(episode_rows) if expected_known and not coverage_unknown else None
    return {
        "name": title if subject is not None else cloud_node["name"],
        "cloud_name": cloud_node["name"] if subject is not None else None,
        "subject_id": subject_id,
        "collection_type": collection_type,
        "category": target_category,
        "path": cloud_node.get("path"),
        "source_path": cloud_node.get("path"),
        "type": "directory",
        "matched": bool(subject is not None and not unmatched and not ambiguous),
        "ambiguous": bool(ambiguous),
        "unmatched": bool(unmatched),
        "coverage_unknown": bool(coverage_unknown),
        "coverage_issues": list(coverage_issues or []),
        "empty": bool(cloud_node.get("empty")) if cloud_node else True,
        "incomplete": bool(cloud_node.get("incomplete", False)) if cloud_node else False,
        "vids": [{"name": item["name"], "relative_path": item["path"], "path": item["path"]} for item in videos],
        "dirs": [child["name"] for child in cloud_node.get("children", []) if child.get("type") == "directory"],
        "children": cloud_node.get("children", []),
        "count": len(videos),
        "total_eps": total_eps,
        "expected_eps": [_episode_label(key) for key in expected_keys],
        "present_eps": present,
        "missing_eps": missing,
        "extra_eps": extras,
        "future_eps": future,
        "unknown_airdate_eps": unknown_airdate,
        "duplicate_eps": sorted(duplicates, key=_episode_label_sort_key),
        "episodes": episode_rows + list(extra_episode_rows_by_key.values()),
        "expected_eps_count": len(expected_keys) if expected_known and not coverage_unknown else None,
        "present_eps_count": len(present),
        "missing_eps_count": len(missing),
        "extra_eps_count": len(extras),
    }


def _subject_id_from_name(value: str) -> Optional[str]:
    match = re.search(r"\[bgm-(\d+)\]", value, flags=re.I)
    return match.group(1) if match else None


def _episode_label(key: str) -> str:
    try:
        value = Decimal(key)
    except InvalidOperation:
        if str(key).startswith("UNKNOWN-"):
            return str(key)
        return "EP" + str(key)
    number = int(value) if value == value.to_integral_value() else value
    text = str(number)
    if value == value.to_integral_value():
        text = "{:02d}".format(int(value))
    else:
        text = format(value.normalize(), "f")
        whole, dot, fraction = text.partition(".")
        text = whole.zfill(2) + (dot + fraction if dot else "")
    return "EP" + text


def _episode_label_sort_key(label: str) -> Tuple[int, Decimal, str]:
    match = re.fullmatch(r"EP(\d+(?:\.\d+)?)", label)
    return (0, Decimal(match.group(1)), label) if match else (1, Decimal(0), label)


def parse_episode_label(filename: str) -> Tuple[Optional[str], bool]:
    stem = Path(filename).stem
    upper = stem.upper()
    special = re.search(r"(?<![A-Z0-9])(NCOP|NCED|SP|OVA|OAD)(?:\s*[-_ ]?(\d{1,3}))?(?![A-Z0-9])", upper)
    if special:
        token = special.group(1)
        number = special.group(2)
        if token in ("NCOP", "NCED"):
            return token + ("{:02d}".format(int(number)) if number else ""), True
        return token + "{:02d}".format(int(number or "1")), True
    numeric = parse_video_episode(filename)
    return (_episode_label(numeric), False) if numeric else (None, False)


def _cloud_show_dirs(root: dict) -> Tuple[dict, List[dict], List[dict]]:
    by_category: Dict[str, List[dict]] = {category: [] for category in CATEGORY_ORDER}
    category_roots: Dict[str, dict] = {}
    root_loose_files = []
    category_loose_files: Dict[str, List[dict]] = {category: [] for category in CATEGORY_ORDER}
    for child in root.get("children", []):
        if child.get("type") == "directory" and child.get("name") in CATEGORY_ORDER:
            category_roots[child["name"]] = child
            category = child["name"]
            for show in child.get("children", []):
                if show.get("type") == "directory":
                    by_category[category].append(show)
                else:
                    category_loose_files[category].append(show)
        elif child.get("type") == "directory":
            by_category["未标记"].append(child)
        else:
            root_loose_files.append(child)
    return {"roots": category_roots, "shows": by_category, "loose_files": category_loose_files,
            "root_loose_files": root_loose_files}, root_loose_files, [
        item for item in root.get("children", []) if item.get("type") == "directory"
        and item.get("name") not in COLLECTION_CATEGORY.values()
    ]


def match_subjects(subjects: Sequence[dict], cloud_shows: Sequence[dict]) -> Tuple[dict, dict]:
    """Match directories using exact normalized titles or explicit [bgm-ID]."""
    subject_ids = {str(row["subject_id"]): row for row in subjects}
    subject_keys: Dict[str, set] = {}
    for row in subjects:
        subject_keys[str(row["subject_id"])] = {title_key(title) for title in subject_titles(row) if title_key(title)}

    matches: Dict[str, List[dict]] = {subject_id: [] for subject_id in subject_ids}
    reverse: Dict[str, List[str]] = {}
    for cloud in cloud_shows:
        explicit_id = _subject_id_from_name(cloud["name"])
        if explicit_id is not None:
            candidates = [explicit_id] if explicit_id in subject_ids else []
        else:
            key = title_key(cloud["name"])
            candidates = [sid for sid, keys in subject_keys.items() if key and key in keys]
        reverse[cloud["path"]] = candidates
        for sid in candidates:
            matches[sid].append(cloud)

    selected: Dict[str, dict] = {}
    ambiguous_subject_ids = set()
    for subject_id, candidates in matches.items():
        if len(candidates) == 1:
            # Another subject may have the same exact title and point at this directory.
            shared = [sid for sid, keys in subject_keys.items()
                      if sid != subject_id and (title_key(candidates[0]["name"]) in keys
                                                 if _subject_id_from_name(candidates[0]["name"]) is None else False)]
            if not shared:
                selected[subject_id] = candidates[0]
            else:
                ambiguous_subject_ids.add(subject_id)
                ambiguous_subject_ids.update(shared)
        elif len(candidates) > 1:
            ambiguous_subject_ids.add(subject_id)
    return {sid: cloud for sid, cloud in selected.items() if sid not in ambiguous_subject_ids}, {
        "matches": matches, "ambiguous_subject_ids": ambiguous_subject_ids, "reverse": reverse,
    }


def _new_cache() -> dict:
    return {"schema_version": SCHEMA_VERSION, "updated_at": iso_now(), "episodes": {}}


def load_episode_cache(path: Path) -> Tuple[dict, Optional[str]]:
    if not path.exists():
        return _new_cache(), None
    try:
        payload = read_json(path)
        episodes = payload.get("episodes")
        if not isinstance(episodes, dict):
            return _new_cache(), "episode cache had no valid episodes map; refetching metadata"
        return payload, None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _new_cache(), "episode cache could not be read; refetching metadata: {}".format(exc)


def _cached_episodes(cache: dict, subject_id: str, ttl_seconds: int,
                     now: Optional[dt.datetime] = None) -> Optional[dict]:
    item = cache.get("episodes", {}).get(subject_id)
    if not isinstance(item, dict) or not isinstance(item.get("data"), list):
        return None
    age = age_seconds(item.get("fetched_at"), now=now)
    if age is None or age > ttl_seconds:
        return None
    return item


def fetch_episode_metadata(subjects: Sequence[dict], client: BangumiClient, cache: dict,
                           cache_ttl: int, *, workers: int = 4,
                           now: Optional[dt.datetime] = None) -> Tuple[dict, dict, List[dict]]:
    """Return subject episode rows, updated cache, and errors; cached data is source-timestamped."""
    episodes_by_subject: Dict[str, dict] = {}
    updated_cache = {**cache, "episodes": dict(cache.get("episodes") or {})}
    errors: List[dict] = []
    pending = []
    for row in subjects:
        sid = str(row["subject_id"])
        cached = _cached_episodes(updated_cache, sid, cache_ttl, now=now)
        if cached is not None:
            episodes_by_subject[sid] = {"data": cached["data"], "fetched_at": cached["fetched_at"],
                                        "source": cached.get("source", "bangumi_api"), "cache_hit": True}
        else:
            pending.append(sid)
    if pending:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, workers))) as pool:
            future_to_sid = {pool.submit(client.episodes, sid): sid for sid in pending}
            for future in as_completed(future_to_sid):
                sid = future_to_sid[future]
                try:
                    data = future.result()
                    fetched_at = (now or utc_now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
                    client_cfg = getattr(client, "cfg", {})
                    base_url = client_cfg.get("base_url", "https://api.bgm.tv")
                    entry = {"fetched_at": fetched_at,
                             "source": base_url + "/v0/episodes?type=0", "data": data}
                    updated_cache["episodes"][sid] = entry
                    episodes_by_subject[sid] = {**entry, "cache_hit": False}
                except Exception as exc:
                    issue = exc.issue.copy() if isinstance(exc, InventoryError) else {
                        "stage": "bangumi_episodes", "code": "episode_fetch_error", "message": str(exc),
                    }
                    issue.setdefault("subject_id", sid)
                    errors.append(issue)
    return episodes_by_subject, updated_cache, errors


def _cloud_node_index(root: dict) -> Tuple[List[dict], dict]:
    categorized, _, unknown_roots = _cloud_show_dirs(root)
    all_shows = []
    for rows in categorized["shows"].values():
        all_shows.extend(rows)
    path_to_category = {}
    for category, rows in categorized["shows"].items():
        for row in rows:
            path_to_category[row["path"]] = category
    return all_shows, {"categorized": categorized, "unknown_roots": unknown_roots,
                       "path_to_category": path_to_category}


def build_gui_tree(subjects: Sequence[dict], episodes_by_subject: dict, cloud_root: dict,
                   *, today: Optional[dt.date] = None) -> Tuple[List[dict], dict]:
    all_cloud_shows, cloud_info = _cloud_node_index(cloud_root)
    selected, match_info = match_subjects(subjects, all_cloud_shows)
    ambiguous_sids = match_info["ambiguous_subject_ids"]
    matched_paths = {node["path"]: sid for sid, node in selected.items()}
    ambiguous_paths = set()
    for sid in ambiguous_sids:
        for node in match_info["matches"].get(sid, []):
            ambiguous_paths.add(node["path"])

    # Build one result per Bangumi target, including subjects with no cloud folder.
    by_collection: Dict[int, List[dict]] = {1: [], 2: [], 3: [], 4: [], 5: []}
    for subject in subjects:
        sid = str(subject["subject_id"])
        raw_meta = episodes_by_subject.get(sid, {})
        records = episode_records(raw_meta.get("data", []), sid, today=today)
        coverage_issues = [record["mapping_issue"] for record in records if record.get("mapping_issue")]
        coverage_unknown = bool(coverage_issues)
        cloud = selected.get(sid)
        is_ambiguous = sid in ambiguous_sids
        node = show_node_from_cloud(
            cloud if cloud is not None else {"name": subject_titles(subject)[0] if subject_titles(subject) else sid,
                                             "path": None, "children": [], "empty": True},
            subject=subject,
            episodes=records,
            ambiguous=is_ambiguous,
            unmatched=cloud is None and not is_ambiguous,
            expected_known=(sid in episodes_by_subject and not coverage_unknown),
            target_category=COLLECTION_CATEGORY[int(subject["collection_type"])],
            coverage_unknown=coverage_unknown,
            coverage_issues=coverage_issues,
        )
        if cloud is None:
            node["name"] = subject_titles(subject)[0] if subject_titles(subject) else "subject-{}".format(sid)
            node["cloud_name"] = None
            node["path"] = None
            node["source_path"] = None
        node["episode_cache"] = {
            "fetched_at": raw_meta.get("fetched_at"), "source": raw_meta.get("source"),
            "cache_hit": bool(raw_meta.get("cache_hit", False)),
        }
        by_collection[int(subject["collection_type"])].append(node)

    # Add all unclaimed cloud directories to their actual category. This preserves
    # folders which are no longer in Bangumi and every unknown root directory.
    tree = []
    for category in CATEGORY_ORDER:
        root_node = cloud_info["categorized"]["roots"].get(category)
        category_shows: List[dict] = []
        target_types = [collection_type for collection_type, label in COLLECTION_CATEGORY.items() if label == category]
        for target_type in target_types:
            category_shows.extend(by_collection[target_type])
        for cloud in cloud_info["categorized"]["shows"].get(category, []):
            if cloud["path"] in matched_paths:
                continue
            reverse_candidates = match_info["reverse"].get(cloud["path"], [])
            is_ambiguous = cloud["path"] in ambiguous_paths or len(reverse_candidates) > 1
            category_shows.append(show_node_from_cloud(
                cloud,
                ambiguous=is_ambiguous,
                unmatched=not is_ambiguous,
                target_category=category,
            ))
        if category == "未标记":
            for cloud in cloud_info["unknown_roots"]:
                reverse_candidates = match_info["reverse"].get(cloud["path"], [])
                is_ambiguous = cloud["path"] in ambiguous_paths or len(reverse_candidates) > 1
                category_shows.append(show_node_from_cloud(
                    cloud, ambiguous=is_ambiguous, unmatched=not is_ambiguous,
                    target_category="未标记",
                ))
        tree.append({
            "category": category,
            "path": root_node.get("path") if root_node else None,
            "empty": bool(root_node.get("empty", True)) if root_node else True,
            "dirs": [child["name"] for child in root_node.get("children", []) if child.get("type") == "directory"] if root_node else [],
            "loose_files": [child for child in root_node.get("children", []) if child.get("type") != "directory"] if root_node else [],
            "unmarked_root_files": ([{"name": item["name"], "relative_path": item["path"], "path": item["path"],
                                      "size": item.get("size"), "modified": item.get("modified")}
                                     for item in cloud_info["categorized"]["root_loose_files"]]
                                    if category == CATEGORY_ORDER[3] else []),
            "shows": sorted(category_shows, key=lambda item: (title_key(item.get("name", "")), item.get("name", ""))),
        })
    match_summary = {
        "matched_subject_ids": sorted(selected),
        "ambiguous_subject_ids": sorted(ambiguous_sids),
        "unmatched_subject_ids": sorted(set(str(row["subject_id"]) for row in subjects) - set(selected) - set(ambiguous_sids)),
        "matched_paths": matched_paths,
    }
    return tree, match_summary


def build_report(cfg: dict, subjects: Sequence[dict], episodes_by_subject: dict,
                 cloud_root: dict, cloud_errors: Sequence[dict], cache: dict,
                 *, started_at: str, completed_at: str, freshness_ttl: int, episode_cache_ttl: int,
                 cache_path: Path, output_path: Path,
                 source_timestamps: Optional[dict] = None,
                 clock_now: Optional[dt.datetime] = None) -> dict:
    tree, match = build_gui_tree(subjects, episodes_by_subject, cloud_root)
    actual = _cloud_counts(cloud_root)
    target_counts = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for row in subjects:
        key = str(row["collection_type"])
        target_counts[key] += 1
    matched = len(match["matched_subject_ids"])
    ambiguous = len(match["ambiguous_subject_ids"])
    unmatched = len(match["unmatched_subject_ids"])
    missing_episode_count = sum(show["missing_eps_count"] for section in tree for show in section["shows"]
                                if show.get("subject_id"))
    category_stats = {}
    for section in tree:
        shows = section["shows"]
        category_stats[section["category"]] = {
            "shows": len(shows),
            "videos": sum(show["count"] for show in shows),
            "bangumi_targets": sum(1 for show in shows if show.get("subject_id")),
            "cloud_directories": sum(1 for show in shows if show.get("source_path")),
        }
    errors = list(cloud_errors)
    coverage_warnings = []
    coverage_unknown_shows = []
    for section in tree:
        for show in section["shows"]:
            if show.get("coverage_unknown"):
                coverage_unknown_shows.append(show)
                coverage_warnings.append({
                    "stage": "episode_mapping", "code": "coverage_unknown",
                    "message": "Bangumi episode rows are retained, but automatic completeness decisions are disabled for this show",
                    "subject_id": show.get("subject_id"), "issues": show.get("coverage_issues", []),
                })
    error_stages = {str(issue.get("stage", "")) for issue in errors}
    cloud_complete = not bool(error_stages.intersection({"alist_auth", "alist_scan"}))
    episode_metadata_complete = not bool(error_stages.intersection({"bangumi", "bangumi_episodes"}))
    source_timestamps = source_timestamps or {}
    completed_dt = parse_timestamp(completed_at) or (clock_now or utc_now())
    source_instant = {
        "bangumi_collections": source_timestamps.get("bangumi_collections", {}).get("completed_at"),
        "bangumi_episodes": source_timestamps.get("bangumi_episodes", {}).get("oldest_fetched_at"),
        "alist": source_timestamps.get("alist", {}).get("completed_at"),
    }
    source_ages = {key: age_seconds(value, now=completed_dt) for key, value in source_instant.items()}
    known_source_ages = [value for value in source_ages.values() if value is not None]
    snapshot_age = age_seconds(completed_at, now=clock_now or completed_dt)
    return {
        "schema_version": SCHEMA_VERSION,
        "stats": {
            "total_shows": sum(len(section["shows"]) for section in tree),
            "total_vids": actual["cloud_video_files"],
            "total_files": actual["cloud_files"],
            "bangumi_target_shows": len(subjects),
            "cloud_directories": actual["cloud_directories"],
            "matched_subjects": matched,
            "unmatched_subjects": unmatched,
            "ambiguous_subjects": ambiguous,
            "missing_episodes": missing_episode_count,
            "coverage_unknown_shows": len(coverage_unknown_shows),
            "categories": category_stats,
        },
        "counts": {
            "bangumi_targets": len(subjects),
            "bangumi_by_type": target_counts,
            **actual,
            "matched_subjects": matched,
            "unmatched_subjects": unmatched,
            "ambiguous_subjects": ambiguous,
            "missing_episodes": missing_episode_count,
            "coverage_unknown_shows": len(coverage_unknown_shows),
        },
        "tree": tree,
        "updated_at": completed_at,
        "generated_at": completed_at,
        "last_success_at": completed_at,
        "started_at": started_at,
        "scan_started_at": started_at,
        "complete": not bool(errors),
        "collection_complete": "bangumi_collections" not in error_stages,
        "episode_metadata_complete": episode_metadata_complete,
        "episode_coverage_complete": (not coverage_unknown_shows and ambiguous == 0
                                      and episode_metadata_complete),
        "cloud_complete": cloud_complete,
        "freshness": {
            "state": "fresh" if not errors else "incomplete",
            "age_seconds": snapshot_age,
            "ttl_seconds": freshness_ttl,
            "checked_at": completed_at,
            "source_ages_seconds": source_ages,
            "oldest_source_age_seconds": max(known_source_ages) if known_source_ages else None,
        },
        "source": {
            "bangumi": cfg["bangumi"]["base_url"],
            "cloud": (cfg["inventory"]["guangya_api_base"]
                      if cfg["inventory"]["cloud_source"] == "guangya_direct"
                      else cfg["alist"]["api_base"]),
            "cloud_root": cfg["alist"]["root"],
            "cloud_refresh": True,
            "cloud_kind": cfg["inventory"]["cloud_source"],
        },
        "source_timestamps": source_timestamps,
        "errors": errors,
        "warnings": coverage_warnings,
        "cache_metadata": {
            "path": str(cache_path),
            "episode_ttl_seconds": episode_cache_ttl,
            "updated_at": cache.get("updated_at"),
            "source": "Bangumi /v0/episodes?type=0",
        },
        "matching": match,
        "inventory_root": cloud_root,
        "inventory_output": str(output_path),
    }


def write_failure(output_path: Path, errors: Sequence[dict], *, attempted_at: str,
                  previous_output: Optional[dict] = None) -> Path:
    sidecar = Path(str(output_path) + ".failure.json")
    previous_timestamp = None
    if isinstance(previous_output, dict):
        previous_timestamp = previous_output.get("last_success_at") or previous_output.get("generated_at")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "attempted_at": attempted_at,
        "complete": False,
        "freshness": {"state": "incomplete", "age_seconds": None, "ttl_seconds": None},
        "last_success_at": previous_timestamp,
        "last_success_path": str(output_path) if previous_timestamp else None,
        "errors": list(errors),
    }
    atomic_write_json(sidecar, payload)
    return sidecar


class InventoryProgress:
    """Atomic, credential-free progress snapshot and throttled stdout heartbeat."""

    def __init__(self, path: Path, *, source: str, interval_seconds: float):
        self.path = path
        self.source = source
        self.interval_seconds = float(interval_seconds)
        self.started_monotonic = time.monotonic()
        self.started_at = iso_now()
        self.last_log_monotonic = None
        self.last_write_monotonic = None
        self.write_error_type = None
        self.snapshot: Dict[str, Any] = {}

    @staticmethod
    def _error_summary(errors: Sequence[dict]) -> Tuple[dict, List[dict]]:
        counts: Dict[str, int] = {}
        recent = []
        for issue in errors:
            if not isinstance(issue, dict):
                continue
            code = issue.get("code", "unknown_error")
            error_type = issue.get("error_type", "InventoryError")
            key = "{}:{}".format(error_type, code)
            counts[key] = counts.get(key, 0) + 1
            if issue.get("path"):
                recent.append({"path": issue["path"], "error_type": error_type, "code": code,
                               "source_error_code": issue.get("source_error_code")})
        return counts, recent[-5:]

    def publish(self, *, status: str, phase: str, force: bool = False, **fields: Any) -> None:
        now = time.monotonic()
        error_counts, recent_errors = self._error_summary(fields.get("errors", []))
        self.snapshot.update({key: value for key, value in fields.items() if key != "errors"})
        payload = {
            **self.snapshot,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "phase": phase,
            "source": self.source,
            "started_at": self.started_at,
            "updated_at": iso_now(),
            "elapsed_seconds": round(now - self.started_monotonic, 1),
            "error_counts": error_counts,
            "recent_errors": recent_errors,
            "progress_write_error_type": self.write_error_type,
        }
        should_write = (force or self.last_write_monotonic is None
                        or now - self.last_write_monotonic >= min(5.0, self.interval_seconds))
        if should_write:
            try:
                atomic_write_json(self.path, payload)
                self.last_write_monotonic = now
            except OSError as exc:
                self.write_error_type = type(exc).__name__

        should_log = force or self.last_log_monotonic is None or now - self.last_log_monotonic >= self.interval_seconds
        if should_log:
            self.last_log_monotonic = now
            summary = {key: payload.get(key) for key in (
                "status", "phase", "source", "root", "directories_discovered", "directories_started",
                "directories_completed", "directories_succeeded", "directories_failed",
                "directories_pending", "files_seen", "active_paths", "error_counts", "recent_errors",
                "elapsed_seconds",
            )}
            print(json.dumps({"event": "inventory_progress", **summary}, ensure_ascii=False,
                             separators=(",", ":")), flush=True)


def run_inventory(config_path: Path, output_path: Optional[Path] = None, cache_path: Optional[Path] = None,
                  *, workers: Optional[int] = None, now: Optional[dt.datetime] = None,
                  alist_client: Optional[AlistClient] = None,
                  bangumi_client: Optional[BangumiClient] = None,
                  progress_path: Optional[Path] = None) -> Tuple[dict, int]:
    def timestamp() -> str:
        return (now or utc_now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    started = timestamp()
    previous: Optional[dict] = None
    errors: List[dict] = []
    progress: Optional[InventoryProgress] = None
    if output_path and output_path.exists():
        try:
            previous = read_json(output_path)
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None
    try:
        raw_cfg = read_json(config_path)
        cfg = normalize_config(raw_cfg)
        out = output_path or Path(str(cfg["inventory"]["output"]))
        if cache_path:
            cache = cache_path
        elif cfg["cache"]["path"]:
            cache = Path(str(cfg["cache"]["path"]))
        else:
            cache = out.with_name(out.stem + ".cache.json")
        configured_progress = (progress_path or
                               (Path(str(cfg["inventory"]["progress_path"]))
                                if cfg["inventory"]["progress_path"] else
                                out.with_name(out.stem + ".progress.json")))
        protected_paths = {config_path.resolve(), out.resolve(), cache.resolve()}
        if configured_progress.resolve() in protected_paths:
            raise InventoryError("inventory progress_path must differ from config, output, and cache paths",
                                 stage="config", code="invalid_config")
        progress = InventoryProgress(configured_progress,
                                     source=cfg["inventory"]["cloud_source"],
                                     interval_seconds=cfg["inventory"]["progress_interval_seconds"])
        progress.publish(status="running", phase="bangumi_collections", root=cfg["alist"]["root"],
                         directories_discovered=0, directories_started=0, directories_completed=0,
                         directories_succeeded=0, directories_failed=0, directories_pending=0,
                         files_seen=0, active_paths=[], errors=[], force=True)
        if previous is None and out.exists():
            try:
                previous = read_json(out)
            except (OSError, ValueError, json.JSONDecodeError):
                previous = None
        cache_data, cache_warning = load_episode_cache(cache)
        deadline = time.monotonic() + cfg["inventory"]["max_runtime_seconds"]
        for client_cfg in (cfg["alist"], cfg["bangumi"]):
            client_cfg["runtime_deadline_monotonic"] = deadline
            client_cfg["max_runtime_seconds"] = cfg["inventory"]["max_runtime_seconds"]
        client = bangumi_client or BangumiClient(cfg)
        cloud_source = cfg["inventory"]["cloud_source"]
        if alist_client is not None:
            alist = alist_client
        elif cloud_source == "guangya_direct":
            # Keep this adapter optional for the default AList-only mode and load
            # it only when explicitly selected in the runtime config.
            if __package__:
                from .guangya_inventory import GuangYaInventoryClient
            else:
                from guangya_inventory import GuangYaInventoryClient
            alist = GuangYaInventoryClient(raw_cfg, cfg)
        else:
            alist = AlistClient(cfg)
        source_timestamps = {
            "bangumi_collections": {"started_at": timestamp(), "source": cfg["bangumi"]["base_url"] + "/v0/users/{}/collections".format(cfg["bangumi"]["username"])},
            "bangumi_episodes": {"started_at": None, "completed_at": None,
                                 "oldest_fetched_at": None, "newest_fetched_at": None},
            "alist": {"started_at": None, "completed_at": None,
                      "source": (cfg["inventory"]["guangya_api_base"]
                                 if cloud_source == "guangya_direct"
                                 else cfg["alist"]["api_base"] + "/api/fs/list"),
                      "refresh": True, "kind": cloud_source},
        }
        subjects = client.collections()
        progress.publish(status="running", phase="bangumi_episodes", root=cfg["alist"]["root"],
                         directories_discovered=0, directories_started=0, directories_completed=0,
                         directories_succeeded=0, directories_failed=0, directories_pending=0,
                         files_seen=0, active_paths=[], errors=[])
        source_timestamps["bangumi_collections"]["completed_at"] = timestamp()
        source_timestamps["bangumi_episodes"]["started_at"] = timestamp()
        ep_by_subject, updated_cache, ep_errors = fetch_episode_metadata(
            subjects, client, cache_data, cfg["cache"]["ttl_seconds"],
            workers=workers or cfg["alist"]["max_workers"], now=now,
        )
        source_timestamps["bangumi_episodes"]["completed_at"] = timestamp()
        fetched_at_values = [meta.get("fetched_at") for meta in ep_by_subject.values() if meta.get("fetched_at")]
        parsed_fetched_at = [parse_timestamp(value) for value in fetched_at_values]
        parsed_fetched_at = [value for value in parsed_fetched_at if value is not None]
        if parsed_fetched_at:
            source_timestamps["bangumi_episodes"]["oldest_fetched_at"] = min(parsed_fetched_at).isoformat(timespec="seconds")
            source_timestamps["bangumi_episodes"]["newest_fetched_at"] = max(parsed_fetched_at).isoformat(timespec="seconds")
        source_timestamps["bangumi_episodes"]["subjects_with_metadata"] = len(ep_by_subject)

        # Persist successful Bangumi metadata independently from cloud scanning. This
        # prevents a transient AList timeout from forcing hundreds of refetches next run.
        updated_cache["updated_at"] = timestamp()
        cache_write_warning = None
        try:
            atomic_write_json(cache, updated_cache)
        except OSError as exc:
            cache_write_warning = "episode cache could not be written: {}".format(exc)

        source_timestamps["alist"]["started_at"] = timestamp()
        errors.extend(ep_errors)
        progress.publish(status="running", phase="cloud_scan_starting", root=cfg["alist"]["root"],
                         directories_discovered=0, directories_started=0, directories_completed=0,
                         directories_succeeded=0, directories_failed=0, directories_pending=0,
                         files_seen=0, active_paths=[], errors=ep_errors)
        scanner = CloudScanner(cfg, alist, workers=workers, progress=progress)
        cloud_root = scanner.scan()
        source_timestamps["alist"]["completed_at"] = timestamp()
        errors.extend(scanner.errors)
        completed = timestamp()
        report = build_report(
            cfg, subjects, ep_by_subject, cloud_root, errors, updated_cache,
            started_at=started, completed_at=completed,
            freshness_ttl=cfg["inventory"]["freshness_ttl_seconds"],
            episode_cache_ttl=cfg["cache"]["ttl_seconds"],
            cache_path=cache, output_path=out,
            source_timestamps=source_timestamps, clock_now=now or parse_timestamp(completed),
        )
        warnings = list(report.get("warnings", []))
        if cache_warning:
            warnings.append({"stage": "cache", "code": "cache_refetched", "message": cache_warning})
        if cache_write_warning:
            warnings.append({"stage": "cache", "code": "cache_write_failed", "message": cache_write_warning})
        if warnings:
            report["warnings"] = warnings
        if not report["complete"]:
            failure = write_failure(out, report["errors"], attempted_at=started, previous_output=previous)
            progress.publish(status="incomplete", phase="inventory_finished",
                             active_paths=[], errors=report["errors"], force=True)
            return {"report": report, "output": str(out), "failure": str(failure),
                    "progress": str(configured_progress)}, 2
        # Retry the cache save after a fully successful scan if the early save failed.
        if cache_write_warning:
            try:
                atomic_write_json(cache, updated_cache)
            except OSError:
                pass
        atomic_write_json(out, report)
        failure = Path(str(out) + ".failure.json")
        try:
            failure.unlink(missing_ok=True)
        except OSError:
            pass
        progress.publish(status="complete", phase="inventory_finished",
                         active_paths=[], errors=[], force=True)
        return {"report": report, "output": str(out), "failure": None,
                "progress": str(configured_progress)}, 0
    except Exception as exc:
        out = output_path or Path(DEFAULT_OUTPUT)
        issue = exc.issue.copy() if isinstance(exc, InventoryError) else {
            "stage": "inventory", "code": "inventory_failed", "message": str(exc),
        }
        sidecar = write_failure(out, [issue], attempted_at=started, previous_output=previous)
        if progress is not None:
            progress.publish(status="incomplete", phase="inventory_failed", errors=[issue],
                             active_paths=[], force=True)
        return {"report": None, "output": str(out), "failure": str(sidecar),
                "progress": str(progress.path) if progress else None, "errors": [issue]}, 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON inventory config")
    parser.add_argument("--output", type=Path, help="successful inventory JSON path")
    parser.add_argument("--cache", type=Path, help="Bangumi episode metadata cache path")
    parser.add_argument("--progress", type=Path, help="atomic inventory progress snapshot path")
    parser.add_argument("--workers", type=int, help="directory and metadata concurrency (capped at 4)")
    parser.add_argument("--dry-run", action="store_true", help="accepted for compatibility; inventory reads are always read-only")
    args = parser.parse_args(argv)
    result, code = run_inventory(args.config, args.output, args.cache, workers=args.workers,
                                 progress_path=args.progress)
    if result.get("report"):
        report = result["report"]
        summary = {
            "complete": report["complete"], "updated_at": report["updated_at"],
            "output": result["output"], "failure": result.get("failure"),
            "progress": result.get("progress"),
            "counts": report["counts"], "errors": report["errors"],
        }
    else:
        summary = {key: value for key, value in result.items() if key != "report"}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
