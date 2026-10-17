"""Minimal GuangYa cloud-collection API client.

Authentication is read from the local AList storage config. AList remains the
only owner of GuangYa refresh-token rotation; this adapter never writes its DB
and asks AList to refresh its mounted storage before re-reading credentials.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests


API_BASE = "https://api.guangyapan.com"
ACCOUNT_BASE = "https://account.guangyapan.com"
DEFAULT_CLIENT_ID = "aMe-8VSlkrbQXpUR"
DEFAULT_READONLY_REQUEST_TIMEOUT_SECONDS = 45.0
DEFAULT_READONLY_MAX_ATTEMPTS = 2
DEFAULT_CLOUD_TASK_SCAN_TIMEOUT_SECONDS = 900.0
MAX_READONLY_REQUEST_TIMEOUT_SECONDS = 120.0
MAX_READONLY_ATTEMPTS = 4
MAX_CLOUD_TASK_SCAN_TIMEOUT_SECONDS = 3600.0
logger = logging.getLogger(__name__)

# AList owns GuangYa refresh-token rotation. Multiple inventory workers can see
# the same expired token at once, so coordinate refreshes by AList database and
# mount. Waiting workers re-read the DB and reuse the token rotated by the first
# worker instead of racing further AList refreshes.
_REFRESH_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def _refresh_lock(database_path: str, mount_path: str) -> threading.Lock:
    key = (str(Path(database_path).resolve()), str(mount_path))
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _bounded_seconds(config: dict, key: str, default: float, maximum: float) -> float:
    value = config.get(key, default)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = float("nan")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(numeric) or not 1 <= numeric <= maximum):
        raise GuangYaAPIError(
            f"Invalid GuangYa {key}; expected a number from 1 to {maximum:g} seconds"
        )
    return numeric


def _bounded_attempts(config: dict) -> int:
    value = config.get("readonly_max_attempts", DEFAULT_READONLY_MAX_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_READONLY_ATTEMPTS:
        raise GuangYaAPIError(
            f"Invalid GuangYa readonly_max_attempts; expected an integer from 1 to {MAX_READONLY_ATTEMPTS}"
        )
    return value


class GuangYaAPIError(RuntimeError):
    def __init__(self, message: str, *, code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AListTokenProvider:
    """Read GuangYa credentials from config or AList SQLite in read-only mode."""

    def __init__(self, config: dict, *, session: Optional[requests.Session] = None):
        self.config = config
        self.session = session or requests.Session()

    def _from_config_file(self) -> Optional[dict]:
        path = self.config.get("config_path")
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return None
        if not isinstance(payload, dict):
            raise GuangYaAPIError("GuangYa credential config must be a JSON object")
        nested = payload.get("guangya") if isinstance(payload.get("guangya"), dict) else payload
        if nested.get("access_token"):
            return nested
        return None

    def _from_alist_db(self) -> Optional[dict]:
        alist = self.config.get("alist") or {}
        db_path = alist.get("database_path") or alist.get("db_path") or self.config.get("database_path")
        if not db_path:
            return None
        mount_path = str(alist.get("guangya_mount_path") or self.config.get("mount_path") or "/guangya")
        uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = connection.execute(
                    "SELECT addition FROM x_storages WHERE mount_path = ? LIMIT 1", (mount_path,)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise GuangYaAPIError(f"Unable to read AList GuangYa credentials: {exc}") from exc
        if not row or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise GuangYaAPIError("AList GuangYa storage config is invalid JSON") from exc
        payload["client_id"] = payload.get("client_id") or self.config.get("client_id") or DEFAULT_CLIENT_ID
        return payload

    def _refresh_through_alist(self) -> None:
        alist = self.config.get("alist") or {}
        base_url = str(alist.get("base_url") or "http://127.0.0.1:5244").rstrip("/")
        admin_token = alist.get("token")
        if not admin_token:
            raise GuangYaAPIError("AList admin token is required to refresh GuangYa credentials")
        try:
            response = self.session.post(
                f"{base_url}/api/fs/list",
                headers={"Authorization": str(admin_token)},
                json={"path": "/guangya", "page": 1, "per_page": 100, "refresh": True},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise GuangYaAPIError("AList could not refresh the GuangYa mount") from exc
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 200:
            raise GuangYaAPIError("AList GuangYa mount refresh was not confirmed")

    def get(self, *, refresh: bool = False, expired_access_token: Optional[str] = None) -> dict:
        alist = self.config.get("alist") or {}
        has_db = bool(alist.get("database_path") or alist.get("db_path") or self.config.get("database_path"))
        if has_db:
            if refresh:
                db_path = (alist.get("database_path") or alist.get("db_path")
                           or self.config.get("database_path"))
                mount_path = str(alist.get("guangya_mount_path")
                                 or self.config.get("mount_path") or "/guangya")
                with _refresh_lock(db_path, mount_path):
                    # A concurrent worker may already have refreshed the token
                    # while this request waited for the lock. Avoid rotating it
                    # again; this also prevents invalidating the other workers'
                    # fresh credentials in a burst of simultaneous 401s.
                    creds = self._from_alist_db()
                    if (not expired_access_token or not creds
                            or creds.get("access_token") == expired_access_token):
                        self._refresh_through_alist()
                        creds = self._from_alist_db()
            else:
                creds = self._from_alist_db()
            if not creds:
                raise GuangYaAPIError("No GuangYa credentials found in AList database")
            if not creds.get("access_token") or not creds.get("device_id"):
                raise GuangYaAPIError("AList GuangYa credentials are missing access_token or device_id")
            return creds
        config_creds = self._from_config_file()
        if config_creds:
            if refresh:
                raise GuangYaAPIError("Static GuangYa credentials cannot be refreshed safely")
            if not config_creds.get("access_token") or not config_creds.get("device_id"):
                raise GuangYaAPIError("Static GuangYa credentials are missing access_token or device_id")
            return config_creds
        raise GuangYaAPIError("No GuangYa credentials found in config or AList database")


class GuangYaClient:
    """Requests-only client for cloud tasks and file operations used by this app."""

    def __init__(self, config: dict, *, session: Optional[requests.Session] = None,
                 credential_provider: Optional[AListTokenProvider] = None):
        self.config = config
        self.session = session or requests.Session()
        self.credentials = credential_provider or AListTokenProvider(config, session=self.session)
        self.api_base = str(config.get("api_base") or API_BASE).rstrip("/")
        self.account_base = str(config.get("account_base") or ACCOUNT_BASE).rstrip("/")
        self.readonly_request_timeout_seconds = _bounded_seconds(
            config, "readonly_request_timeout_seconds",
            DEFAULT_READONLY_REQUEST_TIMEOUT_SECONDS,
            MAX_READONLY_REQUEST_TIMEOUT_SECONDS,
        )
        self.readonly_max_attempts = _bounded_attempts(config)
        self.cloud_task_scan_timeout_seconds = _bounded_seconds(
            config, "cloud_task_scan_timeout_seconds",
            DEFAULT_CLOUD_TASK_SCAN_TIMEOUT_SECONDS,
            MAX_CLOUD_TASK_SCAN_TIMEOUT_SECONDS,
        )
        self._credentials: Optional[dict] = None

    def _load_credentials(self, *, refresh: bool = False) -> dict:
        if isinstance(self.credentials, AListTokenProvider):
            expired_token = ((self._credentials or {}).get("access_token") if refresh else None)
            self._credentials = self.credentials.get(
                refresh=refresh, expired_access_token=expired_token,
            )
        else:
            # Preserve the small injectable provider interface used by callers
            # that supply their own credentials source.
            self._credentials = self.credentials.get(refresh=refresh)
        return self._credentials

    def _auth_headers(self) -> dict:
        creds = self._credentials or self._load_credentials()
        device_id = str(creds.get("device_id") or "")
        return {
            "Authorization": f"Bearer {creds['access_token']}",
            "Did": device_id,
            "did": device_id,
            "Dt": "4",
            "dt": "4",
            "accessToken": str(creds["access_token"]),
            "X-Client-Id": str(creds.get("client_id") or DEFAULT_CLIENT_ID),
            "X-Client-Version": "0.0.1",
            "X-Device-Id": device_id,
            "X-Device-Model": str(creds.get("device_model") or "chrome%2F147.0.0.0"),
            "X-Device-Name": str(creds.get("device_name") or "PC-Chrome"),
            "X-Device-Sign": str(creds.get("device_sign") or f"wdi10.{device_id}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
            "X-Net-Work-Type": "NONE",
            "X-Os-Version": str(creds.get("os_version") or "Win32"),
            "X-Platform-Version": str(creds.get("platform_version") or "1"),
            "X-Protocol-Version": "301",
            "X-Provider-Name": "NONE",
            "X-Sdk-Version": "9.0.2",
            "Referer": "https://www.guangyapan.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
        }

    @staticmethod
    def _decode(response: requests.Response, *, expected_data: Optional[str] = None,
                allow_empty_list_without_rows: bool = False) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GuangYaAPIError("GuangYa returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GuangYaAPIError("GuangYa returned an invalid response envelope")
        code = payload.get("code")
        msg = str(payload.get("msg") or "").lower()
        if msg in {"error", "failed", "failure", "fail"}:
            try:
                numeric_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                numeric_code = None
            raise GuangYaAPIError("GuangYa request reported failure", code=numeric_code)
        if code not in (None, 0, 200, "0", "200"):
            try:
                numeric_code = int(code)
            except (TypeError, ValueError):
                numeric_code = None
            raise GuangYaAPIError("GuangYa business request failed", code=numeric_code)
        data = payload.get("data")
        if msg not in {"success", "ok"} and code not in (0, 200, "0", "200"):
            raise GuangYaAPIError("GuangYa request failed")
        if expected_data and not isinstance(data, dict):
            raise GuangYaAPIError(f"GuangYa response is missing {expected_data}")
        if allow_empty_list_without_rows and expected_data == "list":
            if msg == "success" and data == {}:
                # A confirmed empty directory may omit both default fields.
                # Keep this normalization at the file-list API boundary so
                # arbitrary incomplete response objects still fail closed.
                data["total"] = 0
                data["list"] = []
            rows = data.get("list")
            if rows is None:
                if type(data.get("total")) is not int or data.get("total") != 0:
                    raise GuangYaAPIError("GuangYa response is missing list")
                # GuangYa sometimes represents a genuinely empty directory as
                # total=0 with list absent or null. Normalize this one exact
                # case for downstream consumers; malformed pages remain errors.
                data["list"] = []
            elif not isinstance(rows, list):
                raise GuangYaAPIError("GuangYa directory list is malformed")
        elif expected_data and expected_data not in data:
            raise GuangYaAPIError(f"GuangYa response is missing {expected_data}")
        return payload

    def _request(self, method: str, path: str, *, data: Optional[dict] = None,
                 files: Optional[dict] = None, readonly: bool = False,
                 expected_data: Optional[str] = None,
                 allow_empty_list_without_rows: bool = False,
                 deadline_monotonic: Optional[float] = None) -> dict:
        url = path if path.startswith("http") else f"{self.api_base}/{path.lstrip('/')}"
        attempts = (getattr(self, "readonly_max_attempts", DEFAULT_READONLY_MAX_ATTEMPTS)
                    if readonly else 1)
        auth_refreshed = False
        for attempt in range(attempts):
            headers = self._auth_headers()
            timeout: Any = 45
            if readonly:
                read_timeout = getattr(
                    self, "readonly_request_timeout_seconds",
                    DEFAULT_READONLY_REQUEST_TIMEOUT_SECONDS,
                )
                connect_timeout = min(10.0, read_timeout)
                if deadline_monotonic is not None:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise GuangYaAPIError("GuangYa read-only request deadline exceeded",
                                              retryable=True)
                    read_timeout = min(read_timeout, remaining)
                    connect_timeout = min(connect_timeout, remaining)
                timeout = (connect_timeout, read_timeout)
            try:
                response = self.session.request(method, url, headers=headers, json=data,
                                                files=files, timeout=timeout)
            except requests.RequestException as exc:
                transient_transport_error = isinstance(
                    exc,
                    (requests.Timeout, requests.ConnectionError,
                     requests.exceptions.ChunkedEncodingError),
                )
                if readonly and transient_transport_error and attempt + 1 < attempts:
                    self._pause_before_readonly_retry(attempt, deadline_monotonic)
                    continue
                raise GuangYaAPIError("GuangYa request failed before confirmation",
                                      retryable=readonly and transient_transport_error) from None
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise GuangYaAPIError("GuangYa read-only request deadline exceeded", retryable=True)
            if response.status_code == 401 and readonly:
                if not auth_refreshed and attempt + 1 < attempts:
                    self._load_credentials(refresh=True)
                    auth_refreshed = True
                    continue
                raise GuangYaAPIError("GuangYa authentication failed", code=401)
            try:
                response.raise_for_status()
            except requests.HTTPError:
                retryable_status = (response.status_code in {408, 429}
                                    or response.status_code >= 500)
                if readonly and retryable_status and attempt + 1 < attempts:
                    self._pause_before_readonly_retry(attempt, deadline_monotonic)
                    continue
                raise GuangYaAPIError(f"GuangYa HTTP request failed ({response.status_code})",
                                      retryable=readonly and retryable_status) from None
            try:
                return self._decode(
                    response, expected_data=expected_data,
                    allow_empty_list_without_rows=allow_empty_list_without_rows,
                )
            except GuangYaAPIError as exc:
                if (readonly and exc.code == 117 and not auth_refreshed
                        and attempt + 1 < attempts):
                    self._load_credentials(refresh=True)
                    auth_refreshed = True
                    continue
                raise
        raise GuangYaAPIError("GuangYa read-only request failed after bounded retries",
                              retryable=readonly)

    @staticmethod
    def _pause_before_readonly_retry(attempt: int,
                                     deadline_monotonic: Optional[float] = None) -> None:
        delay = min(0.5 * (2 ** attempt), 3.0)
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= delay:
                raise GuangYaAPIError("GuangYa read-only request deadline exceeded",
                                      retryable=True)
        time.sleep(delay)

    def get_file_list(self, parent_id: str = "", page_size: int = 200, page: int = 0,
                      *, deadline_monotonic: Optional[float] = None) -> dict:
        return self._request("POST", "/nd.bizuserres.s/v1/file/get_file_list",
                             data={"parentId": parent_id or "", "page": page,
                                   "pageSize": page_size, "orderBy": 3,
                                   "sortType": 1, "fileTypes": []},
                             readonly=True, expected_data="list",
                             allow_empty_list_without_rows=True,
                             deadline_monotonic=deadline_monotonic)

    def create_dir(self, parent_id: str, name: str, fail_if_exist: bool = False) -> dict:
        return self._request("POST", "/nd.bizuserres.s/v1/file/create_dir",
                             data={"parentId": parent_id or "", "dirName": name,
                                   "failIfNameExist": bool(fail_if_exist)}, expected_data="fileId")

    def move_file(self, file_ids: List[str], parent_id: str) -> dict:
        return self._request("POST", "/nd.bizuserres.s/v1/file/move_file",
                             data={"fileIds": file_ids, "parentId": parent_id})

    def rename(self, file_id: str, name: str) -> dict:
        return self._request("POST", "/nd.bizuserres.s/v1/file/rename",
                             data={"fileId": file_id, "newName": name})

    def resolve_torrent(self, torrent_url: str) -> dict:
        try:
            source = self.session.get(torrent_url, timeout=40)
            source.raise_for_status()
            for attempt in range(2):
                response = self.session.post(
                    f"{self.api_base}/cloudcollection/v1/resolve_torrent",
                    headers=self._auth_headers(),
                    files={"torrent": ("source.torrent", source.content, "application/x-bittorrent")},
                    timeout=90,
                )
                if response.status_code == 401 and attempt == 0:
                    self._load_credentials(refresh=True)
                    continue
                response.raise_for_status()
                break
        except requests.RequestException as exc:
            raise GuangYaAPIError("Unable to resolve torrent safely") from exc
        try:
            return self._decode(response, expected_data="btResInfo")
        except GuangYaAPIError as exc:
            if exc.code != 117:
                raise
            self._load_credentials(refresh=True)
            try:
                response = self.session.post(
                    f"{self.api_base}/cloudcollection/v1/resolve_torrent",
                    headers=self._auth_headers(),
                    files={"torrent": ("source.torrent", source.content, "application/x-bittorrent")},
                    timeout=90,
                )
                response.raise_for_status()
            except requests.RequestException as retry_exc:
                raise GuangYaAPIError("Unable to retry torrent resolution after AList refresh") from retry_exc
            return self._decode(response, expected_data="btResInfo")

    def create_task(self, payload: dict) -> dict:
        # A write is deliberately never retried: a timeout may follow a successful
        # remote submission, so the caller must reserve quota and mark it unknown.
        return self._request("POST", "/cloudcollection/v1/create_task", data=payload)

    def list_all_tasks(self, page_size: int = 100) -> List[dict]:
        cursor: Optional[str] = None
        seen_cursors = set()
        seen_task_ids = set()
        results: List[dict] = []
        expected_total: Optional[int] = None
        started = time.monotonic()
        deadline = started + getattr(
            self, "cloud_task_scan_timeout_seconds",
            DEFAULT_CLOUD_TASK_SCAN_TIMEOUT_SECONDS,
        )
        for page_index in range(10000):
            if time.monotonic() >= deadline:
                raise GuangYaAPIError("GuangYa cloud task scan exceeded its configured deadline",
                                      retryable=True)
            body = {"page": 0, "pageSize": page_size}
            if cursor:
                body["cursor"] = cursor
            payload = self._request("POST", "/cloudcollection/v1/list_task", data=body,
                                    readonly=True,
                                    deadline_monotonic=deadline)
            elapsed = time.monotonic() - started
            if elapsed >= deadline - started:
                raise GuangYaAPIError("GuangYa cloud task scan exceeded its configured deadline",
                                      retryable=True)
            pages_read = page_index + 1
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise GuangYaAPIError("GuangYa task page has no valid data object")
            if payload.get("msg") == "success" and data == {}:
                # A new account with no cloud tasks omits all protobuf-default
                # fields. Normalize only this exact success envelope here.
                data = {"total": 0, "list": []}
            rows = data.get("list")
            if not isinstance(rows, list):
                raise GuangYaAPIError("GuangYa task page has no list")
            total = data.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise GuangYaAPIError("GuangYa task page has no valid total")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise GuangYaAPIError("GuangYa task total changed during cursor pagination")
            for row in rows:
                if not isinstance(row, dict) or not row.get("taskId"):
                    raise GuangYaAPIError("GuangYa task page contains malformed row")
                task_id = str(row["taskId"])
                if task_id not in seen_task_ids:
                    seen_task_ids.add(task_id)
                    results.append(row)
            if len(seen_task_ids) > expected_total:
                raise GuangYaAPIError("GuangYa task pagination exceeded the reported total")
            if pages_read % 10 == 0:
                logger.info(
                    "GuangYa task scan progress pages=%d rows=%d total=%d elapsed_seconds=%.1f",
                    pages_read, len(results), expected_total, elapsed,
                )
            has_more_present = "hasMore" in data
            if has_more_present and not isinstance(data["hasMore"], bool):
                raise GuangYaAPIError("GuangYa task page has no valid hasMore flag")
            if not has_more_present:
                if len(seen_task_ids) != expected_total:
                    raise GuangYaAPIError("GuangYa task page omitted hasMore before reaching total")
                has_more = False
            else:
                has_more = data["hasMore"]
            if not has_more:
                if len(seen_task_ids) != expected_total:
                    raise GuangYaAPIError("GuangYa task pagination ended before the reported total")
                if time.monotonic() >= deadline:
                    raise GuangYaAPIError("GuangYa cloud task scan exceeded its configured deadline",
                                          retryable=True)
                return results
            if not rows:
                raise GuangYaAPIError("GuangYa task pagination returned an empty non-final page")
            if len(seen_task_ids) >= expected_total:
                raise GuangYaAPIError("GuangYa task pagination hasMore conflicts with total")
            next_cursor = data.get("cursor")
            if not next_cursor or str(next_cursor) in seen_cursors:
                raise GuangYaAPIError("GuangYa task pagination did not advance")
            cursor = str(next_cursor)
            seen_cursors.add(cursor)
        raise GuangYaAPIError("GuangYa task pagination exceeded safety limit")
