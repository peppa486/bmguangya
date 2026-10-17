"""Read-only GuangYa inventory adapter for the library scanner.

The scanner consumes the same normalized entry contract as its AList adapter,
but this module reads GuangYa's file-list API directly. GuangYa refresh-token
rotation remains owned by AList: credentials are read through ``cloud_api``
and its 401 refresh hook asks AList to refresh the mounted storage.
"""

from __future__ import annotations

import copy
import posixpath
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

if __package__:
    from .cloud_api import GuangYaAPIError, GuangYaClient
else:  # Direct execution from scripts/ keeps the same adapter usable as a CLI.
    from cloud_api import GuangYaAPIError, GuangYaClient


class GuangYaInventoryError(RuntimeError):
    """Safe inventory error that never includes credentials or raw responses."""

    def __init__(self, message: str, *, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class GuangYaInventoryClient:
    """Direct, read-only GuangYa listing with the ``AlistClient.list_all`` API.

    ``client_factory`` is an injection seam for offline tests. In production a
    separate GuangYaClient and requests.Session is created for each calling
    thread because the inventory scanner lists sibling directories in parallel.
    """

    def __init__(self, raw_config: dict, normalized_config: dict, *,
                 client_factory: Optional[Callable[[dict], Any]] = None):
        raw = raw_config if isinstance(raw_config, dict) else {}
        normalized = normalized_config if isinstance(normalized_config, dict) else {}
        raw_alist = raw.get("alist") if isinstance(raw.get("alist"), dict) else {}
        normalized_alist = (normalized.get("alist")
                            if isinstance(normalized.get("alist"), dict) else {})
        self.alist_config = dict(raw_alist)
        self.alist_config.update(normalized_alist)
        if not self.alist_config.get("database_path"):
            db_path = raw.get("database_path")
            if db_path:
                self.alist_config["database_path"] = db_path

        guangya = raw.get("guangya") if isinstance(raw.get("guangya"), dict) else {}
        if not guangya and isinstance(normalized.get("guangya"), dict):
            guangya = normalized["guangya"]
        self.root_dir = str(guangya.get("root_dir") or raw.get("root_dir") or "Anime").strip()
        if not self.root_dir or self.root_dir in {".", ".."} or "/" in self.root_dir or "\\" in self.root_dir:
            raise GuangYaInventoryError("configured GuangYa inventory root directory is invalid")

        mount = str(self.alist_config.get("guangya_mount_path") or "/guangya")
        self.mount_path = self._normalize_path(mount, field="AList GuangYa mount path")
        root_value = self.alist_config.get("root") or raw.get("alist_root") or "/guangya/Anime"
        self.remote_root = self._normalize_path(str(root_value), field="configured AList root")
        if self.remote_root == self.mount_path:
            relative_parts: List[str] = []
        elif self.remote_root.startswith(self.mount_path.rstrip("/") + "/"):
            suffix = self.remote_root[len(self.mount_path.rstrip("/")):].strip("/")
            relative_parts = suffix.split("/") if suffix else []
        else:
            raise GuangYaInventoryError("configured AList root is outside the GuangYa mount")
        if not relative_parts or relative_parts[0] != self.root_dir:
            raise GuangYaInventoryError("configured AList root does not begin at guangya.root_dir")
        self.root_parts = relative_parts

        client_config = copy.deepcopy(guangya)
        client_config["alist"] = copy.deepcopy(self.alist_config)
        self._client_config = client_config
        self._client_factory = client_factory
        self._local = threading.local()
        self._map_lock = threading.RLock()
        self._directory_ids: Dict[str, str] = {}

    @staticmethod
    def _normalize_path(path: str, *, field: str) -> str:
        if not isinstance(path, str) or not path.strip() or "\\" in path:
            raise GuangYaInventoryError("{} is invalid".format(field))
        parts = [part for part in path.strip().split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise GuangYaInventoryError("{} contains an unsafe component".format(field))
        return "/" + "/".join(parts)

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            if self._client_factory:
                client = self._client_factory(copy.deepcopy(self._client_config))
            else:
                client = GuangYaClient(self._client_config, session=requests.Session())
            self._local.client = client
        return client

    @staticmethod
    def _storage_root_id(client) -> str:
        provider = getattr(client, "credentials", None)
        getter = getattr(provider, "get", None)
        if not callable(getter):
            raise GuangYaInventoryError("GuangYa credential provider cannot expose storage root metadata")
        try:
            credentials = getter()
        except GuangYaAPIError as exc:
            raise GuangYaInventoryError("unable to read GuangYa storage metadata", code=exc.code) from None
        except Exception:
            raise GuangYaInventoryError("unable to read GuangYa storage metadata") from None
        if not isinstance(credentials, dict) or "root_path" not in credentials:
            raise GuangYaInventoryError("AList GuangYa storage metadata has no root_path")
        value = credentials.get("root_path")
        if value is None or isinstance(value, (dict, list, bool)):
            raise GuangYaInventoryError("AList GuangYa storage root_path is invalid")
        root_id = str(value).strip()
        return "" if root_id in {"", "/"} else root_id

    @staticmethod
    def _parse_total(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GuangYaInventoryError("GuangYa directory response has an invalid total")
        return value

    @staticmethod
    def _parse_size(entry: dict) -> Optional[int]:
        field = ("fileSize" if entry.get("fileSize") is not None
                 else "size" if "size" in entry else None)
        if field is None or entry[field] is None:
            return None
        value = entry[field]
        if isinstance(value, bool):
            raise GuangYaInventoryError("GuangYa directory response has an invalid file size")
        if isinstance(value, int):
            size = value
        elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
            size = int(value.strip())
        else:
            raise GuangYaInventoryError("GuangYa directory response has an invalid file size")
        if size < 0:
            raise GuangYaInventoryError("GuangYa directory response has an invalid file size")
        return size

    @staticmethod
    def _parse_modified(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        try:
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, (int, float)):
                timestamp = float(value)
                if abs(timestamp) > 100_000_000_000:
                    timestamp /= 1000.0
                parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            elif isinstance(value, str):
                text = value.strip()
                if re.fullmatch(r"\d+(?:\.\d+)?", text):
                    timestamp = float(text)
                    if abs(timestamp) > 100_000_000_000:
                        timestamp /= 1000.0
                    parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                else:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError
                    parsed = parsed.astimezone(timezone.utc)
            else:
                raise ValueError
        except (OverflowError, OSError, TypeError, ValueError):
            raise GuangYaInventoryError("GuangYa directory response has an invalid utime") from None
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def _normalize_entry(cls, entry: Any) -> dict:
        if not isinstance(entry, dict):
            raise GuangYaInventoryError("GuangYa directory response has a malformed entry")
        raw_id = entry.get("fileId")
        name = entry.get("fileName")
        if raw_id in (None, "") or not isinstance(name, str) or not name.strip():
            raise GuangYaInventoryError("GuangYa directory entry has no fileId or fileName")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise GuangYaInventoryError("GuangYa directory entry has an unsafe fileName")
        res_type = entry.get("resType")
        if isinstance(res_type, bool):
            raise GuangYaInventoryError("GuangYa directory entry has an unknown resType")
        try:
            res_type = int(res_type)
        except (TypeError, ValueError):
            raise GuangYaInventoryError("GuangYa directory entry has an unknown resType") from None
        if res_type not in {1, 2}:
            raise GuangYaInventoryError("GuangYa directory entry has an unknown resType")
        size = cls._parse_size(entry)
        modified = cls._parse_modified(entry.get("utime"))
        file_id = str(raw_id)
        normalized = {"name": name, "is_dir": res_type == 2, "file_id": file_id,
                      "id": file_id, "size": size, "modified": modified}
        gcid = entry.get("gcid")
        if isinstance(gcid, str) and re.fullmatch(r"[0-9a-fA-F]{40}", gcid):
            normalized["gcid"] = gcid.lower()
        return normalized

    def _list_directory(self, client, parent_id: str, page_size: int, *,
                        deadline_monotonic: Optional[float] = None) -> List[dict]:
        entries: List[dict] = []
        seen_ids = set()
        expected_total: Optional[int] = None
        for page in range(10000):
            try:
                request_kwargs = {"parent_id": parent_id, "page_size": page_size, "page": page}
                if deadline_monotonic is not None:
                    request_kwargs["deadline_monotonic"] = deadline_monotonic
                response = client.get_file_list(**request_kwargs)
            except GuangYaAPIError as exc:
                raise GuangYaInventoryError("GuangYa inventory API request failed", code=exc.code) from None
            except Exception:
                raise GuangYaInventoryError("GuangYa inventory API request failed") from None
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                raise GuangYaInventoryError("GuangYa directory response has no data object")
            total = self._parse_total(data.get("total"))
            chunk = data.get("list")
            if chunk is None and total == 0:
                # The provider omits list or returns null for some truly empty
                # directories. Only accept that representation when total is
                # explicitly the integer zero.
                chunk = []
            if not isinstance(chunk, list):
                raise GuangYaInventoryError("GuangYa directory response has no data.list")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise GuangYaInventoryError("GuangYa directory total changed during pagination")
            if len(chunk) > page_size:
                raise GuangYaInventoryError("GuangYa directory page exceeds requested page size")
            if len(entries) + len(chunk) > total:
                raise GuangYaInventoryError("GuangYa directory returned more entries than total")
            normalized = [self._normalize_entry(item) for item in chunk]
            for item in normalized:
                if item["file_id"] in seen_ids:
                    raise GuangYaInventoryError("GuangYa directory pagination repeated a fileId")
                seen_ids.add(item["file_id"])
                entries.append(item)
            if len(entries) == total:
                return entries
            if not chunk:
                raise GuangYaInventoryError("GuangYa directory pagination ended before total")
        raise GuangYaInventoryError("GuangYa directory pagination exceeded the safety limit")

    @staticmethod
    def _unique_child_directory(entries: List[dict], name: str) -> str:
        matches = [entry for entry in entries if entry["is_dir"] and entry["name"] == name]
        if len(matches) != 1:
            if not matches:
                raise GuangYaInventoryError("configured GuangYa inventory directory was not found")
            raise GuangYaInventoryError("configured GuangYa inventory directory is ambiguous")
        return matches[0]["file_id"]

    def _resolve_root_id(self, client, page_size: int, *,
                         deadline_monotonic: Optional[float] = None) -> str:
        with self._map_lock:
            cached = self._directory_ids.get(self.remote_root)
            if cached:
                return cached
            parent_id = self._storage_root_id(client)
            current_path = self.mount_path
            for component in self.root_parts:
                entries = self._list_directory(
                    client, parent_id, page_size, deadline_monotonic=deadline_monotonic,
                )
                parent_id = self._unique_child_directory(entries, component)
                current_path = posixpath.join(current_path, component)
                self._directory_ids[current_path] = parent_id
            self._directory_ids[self.remote_root] = parent_id
            return parent_id

    def _directory_id(self, client, path: str, page_size: int, *,
                      deadline_monotonic: Optional[float] = None) -> str:
        if path == self.remote_root:
            return self._resolve_root_id(client, page_size, deadline_monotonic=deadline_monotonic)
        with self._map_lock:
            file_id = self._directory_ids.get(path)
        if file_id:
            return file_id
        raise GuangYaInventoryError("directory path was not discovered from a scanned parent")

    def _cache_child_directories(self, parent_path: str, entries: List[dict]) -> None:
        child_directories: Dict[str, str] = {}
        for entry in entries:
            if not entry["is_dir"]:
                continue
            child_path = posixpath.join(parent_path, entry["name"])
            if child_path in child_directories:
                raise GuangYaInventoryError("GuangYa parent contains duplicate directory names")
            child_directories[child_path] = entry["file_id"]
        with self._map_lock:
            for child_path, file_id in child_directories.items():
                previous = self._directory_ids.get(child_path)
                if previous and previous != file_id:
                    raise GuangYaInventoryError("GuangYa directory path maps to conflicting fileIds")
            self._directory_ids.update(child_directories)

    def list_all(self, remote_path: str, page_size: int = 100, *,
                 deadline_monotonic: Optional[float] = None) -> List[dict]:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise GuangYaInventoryError("GuangYa page_size must be between 1 and 1000")
        path = self._normalize_path(remote_path, field="GuangYa inventory path")
        if path != self.remote_root and not path.startswith(self.remote_root.rstrip("/") + "/"):
            raise GuangYaInventoryError("inventory path is outside the configured AList root")
        client = self._client()
        parent_id = self._directory_id(
            client, path, page_size, deadline_monotonic=deadline_monotonic,
        )
        entries = self._list_directory(
            client, parent_id, page_size, deadline_monotonic=deadline_monotonic,
        )
        self._cache_child_directories(path, entries)
        return entries
