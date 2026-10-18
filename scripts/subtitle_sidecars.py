"""Materialize verified GuangYa subtitle files beside generated STRMs.

Planning is entirely local and reads only the complete inventory snapshot.
Apply re-lists each source parent through the read-only GuangYa inventory API,
then downloads the file through the configured AList WebDAV endpoint. It never
changes cloud state and never replaces an existing local subtitle.
"""
from __future__ import annotations

import os
import posixpath
import tempfile
import errno
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import requests

if __package__:
    from . import sync_library_strm as sync
    from . import reconcile_cloud_library as reconcile
else:  # Direct script import from scripts/.
    import sync_library_strm as sync
    import reconcile_cloud_library as reconcile


SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".vtt"})
MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
DEFAULT_SUBTITLE_TIMEOUT_SECONDS = 30.0
MAX_SUBTITLE_REDIRECTS = 5


class SubtitleSidecarError(RuntimeError):
    """Safe local subtitle planning or write error."""


class _NoAuth(requests.auth.AuthBase):
    """Override Session.auth and netrc lookup for cross-origin CDN requests."""

    def __call__(self, request):
        request.headers.pop("Authorization", None)
        return request


_NO_AUTH = _NoAuth()


def _inventory_files(inventory: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter]:
    root = inventory.get("inventory_root")
    if not isinstance(root, dict) or root.get("type") != "directory":
        raise SubtitleSidecarError("inventory root is malformed")
    children = root.get("children")
    if not isinstance(children, list) or root.get("incomplete") is True:
        raise SubtitleSidecarError("inventory root is incomplete")

    files: list[dict[str, Any]] = []
    sibling_name_counts: Counter = Counter()

    def visit(node: Any, parent: str) -> None:
        if not isinstance(node, dict) or node.get("incomplete") is True:
            raise SubtitleSidecarError("inventory root contains an incomplete node")
        name = sync.clean_component(node.get("name"), field="inventory node name")
        relative = posixpath.join(parent, name) if parent else name
        if node.get("path") != relative:
            raise SubtitleSidecarError("inventory root contains an inconsistent path")
        sibling_name_counts[(parent, name.casefold())] += 1
        kind = node.get("type")
        if kind == "directory":
            nested = node.get("children")
            if not isinstance(nested, list):
                raise SubtitleSidecarError("inventory root directory is incomplete")
            for child in nested:
                visit(child, relative)
        elif kind == "file":
            if "children" in node:
                raise SubtitleSidecarError("inventory root file contains child entries")
            files.append({
                "name": name,
                "relative_path": relative,
                "parent": parent,
                "file_id": node.get("file_id") or node.get("id"),
                "size": node.get("size"),
            })
        else:
            raise SubtitleSidecarError("inventory root contains an unknown node type")

    for child in children:
        visit(child, "")
    return files, sibling_name_counts


def _episode_strm_map(planned: list[dict[str, Any]],
                      generated_strm_paths: set[str] | None) -> tuple[dict[str, dict], set[str]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for show in planned:
        if not isinstance(show, dict) or show.get("skip_apply") is True or show.get("review_excluded") is True:
            continue
        source_path = show.get("source_path")
        try:
            source_parts = sync.clean_relative_path(source_path, field="planned show source")
        except sync.SyncError:
            continue
        if (show.get("category") == "未标记" and len(source_parts) >= 2
                and source_parts[0] == "未标记" and source_parts[1].startswith("待核对-")):
            continue
        episodes = show.get("episodes")
        if not isinstance(episodes, list):
            continue
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            source = episode.get("relative_path")
            local = episode.get("local_path")
            key = sync.canonical_episode_key(episode.get("episode_key"))
            try:
                source_text = "/".join(sync.clean_relative_path(source, field="planned episode source"))
                local_parts = sync.clean_relative_path(local, field="planned episode target")
            except sync.SyncError:
                continue
            if (key is None or PurePosixPath(local_parts[-1]).suffix.lower() != ".strm"
                    or PurePosixPath(local_parts[-1]).stem.casefold() != key.casefold()):
                continue
            local_text = "/".join(local_parts)
            if generated_strm_paths is not None and local_text not in generated_strm_paths:
                continue
            candidates[source_text].append({
                "episode_key": key,
                "local_path": local_text,
                "local_parent": "/".join(local_parts[:-1]),
            })

    mapping: dict[str, dict] = {}
    ambiguous_sources: set[str] = set()
    for source, rows in candidates.items():
        targets = {row["local_path"].casefold() for row in rows}
        if len(rows) != 1 or len(targets) != 1:
            ambiguous_sources.add(source)
        else:
            mapping[source] = rows[0]
    return mapping, ambiguous_sources


def _find_video_candidate(subtitle: dict[str, Any], video_nodes: list[dict[str, Any]],
                          sibling_name_counts: Counter) -> tuple[dict[str, Any] | None, str | None, str | None]:
    candidates, kind = reconcile._subtitle_video_matches(subtitle, video_nodes)
    reasons = {
        "ambiguous_exact": "ambiguous_video_companion",
        "ambiguous_language_suffix": "ambiguous_video_companion",
        "unmatched_language_suffix": "video_companion_missing",
        "unknown_language_suffix": "unknown_or_missing_language_tag",
        "no_language_suffix": "unknown_or_missing_language_tag",
        "multiple_language_suffixes": "multiple_language_tags",
    }
    if len(candidates) != 1:
        return None, None, reasons.get(kind, "video_companion_missing")
    video = candidates[0]
    parent = subtitle["parent"]
    if sibling_name_counts[(parent, str(video.get("name") or "").casefold())] != 1:
        return None, None, "ambiguous_video_path"
    tag = None
    if kind == "language_suffix":
        _base, tag, _state = reconcile._subtitle_language_suffix(subtitle["name"])
    return video, tag, None


def plan_subtitle_sidecars(inventory: dict[str, Any], planned: list[dict[str, Any]],
                           cfg: dict[str, Any], *,
                           generated_strm_paths: set[str] | None = None) -> dict[str, Any]:
    """Return an offline-only plan and aggregate safe skip counts."""
    files, sibling_name_counts = _inventory_files(inventory)
    configured_root = sync.normalize_root(cfg["alist"].get("root"), field="alist.root")
    if not configured_root:
        raise SubtitleSidecarError("configured AList root is invalid")
    managed_files = set(sync._managed_inventory_file_paths(inventory, configured_root))
    episode_map, ambiguous_episode_sources = _episode_strm_map(
        planned, generated_strm_paths,
    )

    video_nodes_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in files:
        if PurePosixPath(node["name"]).suffix.lower() in sync.VIDEO_SUFFIXES:
            video_nodes_by_parent[node["parent"]].append({
                **node, "_relative_path": node["relative_path"],
            })

    subtitles = [node for node in files if PurePosixPath(node["name"]).suffix.lower() in SUBTITLE_EXTENSIONS]
    skipped: Counter = Counter()
    actions: list[dict[str, Any]] = []
    for subtitle in subtitles:
        source = subtitle["relative_path"]
        if source not in managed_files:
            skipped["source_not_in_complete_inventory"] += 1
            continue
        if sibling_name_counts[(subtitle["parent"], subtitle["name"].casefold())] != 1:
            skipped["ambiguous_subtitle_path"] += 1
            continue
        video, tag, match_error = _find_video_candidate(
            {**subtitle, "_relative_path": source},
            video_nodes_by_parent.get(subtitle["parent"], []), sibling_name_counts,
        )
        if match_error:
            skipped[match_error] += 1
            continue
        video_path = video["relative_path"]
        if video_path in ambiguous_episode_sources:
            skipped["ambiguous_episode_strm_mapping"] += 1
            continue
        episode = episode_map.get(video_path)
        if episode is None:
            skipped["video_has_no_generated_episode_strm"] += 1
            continue
        file_id = subtitle.get("file_id")
        if file_id is None or not str(file_id).strip():
            skipped["missing_file_id"] += 1
            continue
        size = subtitle.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            skipped["invalid_size"] += 1
            continue
        if size > MAX_SUBTITLE_BYTES:
            skipped["size_over_limit"] += 1
            continue
        local_name = PurePosixPath(episode["local_path"]).stem
        extension = PurePosixPath(subtitle["name"]).suffix
        tagged = tag is not None
        target_name = "{}.zh.{}{}".format(local_name, tag, extension) if tagged else local_name + extension
        target = posixpath.join(episode["local_parent"], target_name)
        try:
            sync.clean_relative_path(target, field="subtitle target")
        except sync.SyncError:
            skipped["unsafe_local_target"] += 1
            continue
        actions.append({
            "source_relative_path": source,
            "source_parent": subtitle["parent"],
            "source_name": subtitle["name"],
            "source_file_id": str(file_id),
            "source_size": size,
            "video_relative_path": video_path,
            "target_relative_path": target,
            "language_tag": tag,
            "extension": extension,
        })

    target_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in actions:
        target_groups[action["target_relative_path"].casefold()].append(action)
    collisions = {key for key, rows in target_groups.items() if len(rows) > 1}
    if collisions:
        kept: list[dict[str, Any]] = []
        for action in actions:
            if action["target_relative_path"].casefold() in collisions:
                skipped["target_name_collision"] += 1
            else:
                kept.append(action)
        actions = kept

    actions.sort(key=lambda row: (row["target_relative_path"].casefold(), row["source_relative_path"]))
    return {
        "actions": actions,
        "candidate_count": len(subtitles),
        "planned_count": len(actions),
        "skipped_count": sum(skipped.values()),
        "skipped_by_reason": dict(sorted(skipped.items())),
    }


def _native_parent_listing(raw_config: dict[str, Any], cfg: dict[str, Any], parent: str, *,
                           client_factory: Callable[[dict[str, Any]], Any] | None = None) -> list[dict[str, Any]]:
    """Resolve each parent afresh through the native read-only adapter."""
    if __package__:
        from .guangya_inventory import GuangYaInventoryClient
    else:
        from guangya_inventory import GuangYaInventoryClient
    alist = raw_config.get("alist") if isinstance(raw_config.get("alist"), dict) else {}
    remote_root = alist.get("root")
    if not isinstance(remote_root, str):
        raise SubtitleSidecarError("configured AList root is missing")
    remote_parent = posixpath.join(remote_root.rstrip("/"), parent) if parent else remote_root
    page_size = alist.get("page_size", 100)
    normalized = {"alist": {
        "root": remote_root,
        "timeout_seconds": cfg["network_timeout"],
        "page_size": page_size,
    }}
    # A new adapter per parent prevents reuse of stale path-to-ID mappings.
    client = GuangYaInventoryClient(raw_config, normalized, client_factory=client_factory)
    entries = client.list_all(remote_root)
    current_path = remote_root.rstrip("/")
    for component in sync.clean_relative_path(parent, field="subtitle source parent") if parent else ():
        matches = [row for row in entries if isinstance(row.get("name"), str)
                   and row["name"].casefold() == component.casefold()]
        if (len(matches) != 1 or matches[0].get("name") != component
                or matches[0].get("is_dir") is not True):
            raise SubtitleSidecarError("fresh subtitle parent path is missing or ambiguous")
        current_path = posixpath.join(current_path, component)
        entries = client.list_all(current_path)
    return entries


def _webdav_url(cfg: dict[str, Any], source_relative_path: str) -> str:
    parts = sync.clean_relative_path(source_relative_path, field="subtitle source")
    base = urllib.parse.urlsplit(cfg["public_url"])
    route_parts = (cfg["stream_mount"], *cfg["root_suffix"], *parts)
    route = "/dav/" + "/".join(urllib.parse.quote(part, safe="") for part in route_parts)
    return urllib.parse.urlunsplit((base.scheme, base.netloc, route, "", ""))


def _verify_fresh_row(action: dict[str, Any], rows: Any) -> bool:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return False
    name = action["source_name"]
    same_name = [row for row in rows if isinstance(row.get("name"), str)
                 and row["name"].casefold() == name.casefold()]
    if len(same_name) != 1:
        return False
    row = same_name[0]
    if row.get("name") != name or row.get("is_dir") is not False:
        return False
    current_id = row.get("file_id") or row.get("id")
    size = row.get("size")
    return (
        current_id is not None and str(current_id) == action["source_file_id"]
        and isinstance(size, int) and not isinstance(size, bool) and size == action["source_size"]
    )


def _validated_redirect_url(current_url: str, location: Any) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    try:
        target = urllib.parse.urljoin(current_url, location.strip())
        parsed = urllib.parse.urlsplit(target)
        # Accessing port validates malformed bracket/port syntax.
        parsed.port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        return None
    if (urllib.parse.urlsplit(current_url).scheme.lower() == "https"
            and parsed.scheme.lower() != "https"):
        return None
    return target


def _url_origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, parsed.hostname.casefold() if parsed.hostname else None, port


def _download_subtitle(action: dict[str, Any], cfg: dict[str, Any], session: Any) -> bytes | None:
    expected_size = action["source_size"]
    if expected_size <= 0 or expected_size > MAX_SUBTITLE_BYTES:
        return None
    url = _webdav_url(cfg, action["source_relative_path"])
    response = None
    try:
        trusted_origin = _url_origin(url)
        current_url = url
        for redirects in range(MAX_SUBTITLE_REDIRECTS + 1):
            same_origin = _url_origin(current_url) == trusted_origin
            response = session.get(
                current_url,
                auth=(cfg["username"], cfg["password"]) if same_origin else _NO_AUTH,
                headers={} if same_origin else {"Authorization": None},
                timeout=min(float(cfg["network_timeout"]), DEFAULT_SUBTITLE_TIMEOUT_SECONDS),
                stream=True,
                allow_redirects=False,
            )
            status = getattr(response, "status_code", None)
            if status in {301, 302, 303, 307, 308}:
                if redirects >= MAX_SUBTITLE_REDIRECTS:
                    return None
                next_url = _validated_redirect_url(
                    current_url, getattr(response, "headers", {}).get("Location"),
                )
                if next_url is None:
                    return None
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                response = None
                current_url = next_url
                continue
            break
        else:
            return None
        if getattr(response, "status_code", None) != 200:
            return None
        # Content-Length can describe a compressed transfer body. Validate the
        # decoded iter_content bytes below against the snapshot's file size.
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not isinstance(chunk, bytes):
                return None
            total += len(chunk)
            if total > expected_size or total > MAX_SUBTITLE_BYTES:
                return None
            chunks.append(chunk)
        if total != expected_size:
            return None
        return b"".join(chunks)
    except Exception:
        # Never serialize request exceptions: they can contain authenticated URLs.
        return None
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _preflight_target(action: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    try:
        target = sync._local_path(
            cfg["media_root"], action["target_relative_path"], field="subtitle target",
        )
    except sync.SyncError:
        return "unsafe_local_target"
    if target.is_symlink():
        return "unsafe_local_target"
    if target.exists() and not target.is_file():
        return "target_not_regular_file"
    return None


def _atomic_install_no_replace(source: Path, destination: Path) -> None:
    """Atomically install a sibling temp file without replacing any target."""
    if os.name == "nt":
        # Windows rename is atomic on one volume and refuses an existing target.
        os.rename(source, destination)
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2")
    except (AttributeError, OSError):
        # Hard-link creation is also atomic and no-clobber on the same filesystem.
        os.link(source, destination)
        return
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, "subtitle target already exists")
    if error in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        os.link(source, destination)
        return
    raise OSError(error, "atomic no-replace subtitle install failed")


def _write_new_or_unchanged(target: Path, content: bytes, cfg: dict[str, Any]) -> str:
    try:
        target = sync._local_path(cfg["media_root"], target.relative_to(cfg["media_root"]).as_posix(),
                                  field="subtitle target")
    except (ValueError, sync.SyncError):
        return "unsafe_local_target"
    if target.is_symlink():
        return "unsafe_local_target"
    if target.exists():
        if not target.is_file():
            return "target_not_regular_file"
        try:
            if target.read_bytes() == content:
                return "unchanged"
        except OSError:
            return "target_read_failed"
        return "target_conflict"

    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        # Check again after directory creation; never follow a newly introduced symlink.
        target = sync._local_path(cfg["media_root"], target.relative_to(cfg["media_root"]).as_posix(),
                                  field="subtitle target")
        if target.exists() or target.is_symlink():
            if target.is_file() and not target.is_symlink():
                try:
                    return "unchanged" if target.read_bytes() == content else "target_conflict"
                except OSError:
                    return "target_read_failed"
            return "target_conflict"
        fd, temp_name = tempfile.mkstemp(prefix=".subtitle-", suffix=".tmp", dir=str(target.parent))
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Install atomically without overwriting a concurrent local file.
            _atomic_install_no_replace(temp, target)
            temp.unlink(missing_ok=True)
            try:
                dir_fd = os.open(target.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            return "created"
        except FileExistsError:
            try:
                if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
                    return "unchanged"
                return "target_conflict"
            except OSError:
                return "target_read_failed"
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    except sync.SyncError:
        return "unsafe_local_target"
    except OSError:
        return "local_write_failed"


def apply_subtitle_plan(plan: dict[str, Any], raw_config: dict[str, Any], cfg: dict[str, Any], *,
                        parent_lister: Callable[[dict[str, Any], dict[str, Any], str], list[dict[str, Any]]] | None = None,
                        session: Any = None) -> dict[str, Any]:
    """Apply local sidecars independently; a failed item can be retried next run."""
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    lister = parent_lister or _native_parent_listing
    own_session = session is None
    active_session = session or requests.Session()
    parent_rows: dict[str, list[dict[str, Any]] | None] = {}
    created = unchanged = failed = 0
    errors: list[dict[str, str]] = []
    completed: list[dict[str, str]] = []
    try:
        for action in actions:
            target_error = _preflight_target(action, cfg)
            if target_error:
                failed += 1
                errors.append({"source_path": action["source_relative_path"],
                               "target_path": action["target_relative_path"],
                               "code": target_error})
                continue
            parent = action["source_parent"]
            if parent not in parent_rows:
                try:
                    rows = lister(raw_config, cfg, parent)
                    parent_rows[parent] = rows if isinstance(rows, list) else None
                except Exception:
                    # Drop exception text because transport failures may include URLs or credentials.
                    parent_rows[parent] = None
            rows = parent_rows[parent]
            if rows is None:
                failed += 1
                errors.append({"source_path": action["source_relative_path"],
                               "target_path": action["target_relative_path"],
                               "code": "fresh_parent_listing_failed"})
                continue
            if not _verify_fresh_row(action, rows):
                failed += 1
                errors.append({"source_path": action["source_relative_path"],
                               "target_path": action["target_relative_path"],
                               "code": "source_identity_or_size_mismatch"})
                continue
            data = _download_subtitle(action, cfg, active_session)
            if data is None:
                failed += 1
                errors.append({"source_path": action["source_relative_path"],
                               "target_path": action["target_relative_path"],
                               "code": "bounded_download_failed"})
                continue
            target = cfg["media_root"] / PurePosixPath(action["target_relative_path"])
            state = _write_new_or_unchanged(target, data, cfg)
            if state == "created":
                created += 1
                completed.append({"source_path": action["source_relative_path"],
                                  "target_path": action["target_relative_path"], "state": state})
            elif state == "unchanged":
                unchanged += 1
                completed.append({"source_path": action["source_relative_path"],
                                  "target_path": action["target_relative_path"], "state": state})
            else:
                failed += 1
                errors.append({"source_path": action["source_relative_path"],
                               "target_path": action["target_relative_path"], "code": state})
    finally:
        if own_session:
            try:
                active_session.close()
            except Exception:
                pass
    return {
        "status": "partial" if failed else "ok",
        "candidate_count": plan.get("candidate_count", 0),
        "planned_count": len(actions),
        "skipped_count": plan.get("skipped_count", 0),
        "skipped_by_reason": plan.get("skipped_by_reason", {}),
        "created_count": created,
        "unchanged_count": unchanged,
        "failed_count": failed,
        "completed": completed,
        "errors": errors,
        "planned": [{"source_path": row["source_relative_path"],
                     "target_path": row["target_relative_path"]} for row in actions],
    }
