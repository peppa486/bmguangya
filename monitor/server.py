#!/usr/bin/env python3
"""Read-only HTTP view for the latest Bangumi/Alist inventory and pipeline progress."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_STATE = Path("/var/lib/bmguangya")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9988
INDEX_PATH = Path(__file__).with_name("index.html")
INVENTORY_PROGRESS_STALE_SECONDS = 120
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|token|password|passwd|secret|api[_-]?key|authorization)\b"
    r"(\s*['\"]?\s*[:=]\s*['\"]?)([^&\s,}\"']+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_JWT_SECRET = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_RECONCILE_ACTION_STATES = ("planned", "pending", "done", "blocked", "noop")


def read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream), None
    except FileNotFoundError:
        return None, "not_found"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "unreadable_or_invalid"


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


def _safe_text(value: Any, *, limit: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", text)
    text = _BEARER_SECRET.sub("Bearer [redacted]", text)
    text = _URL_CREDENTIALS.sub(r"\1[redacted]@", text)
    text = _JWT_SECRET.sub("[redacted]", text)
    return text[:limit]


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_duration(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def enrich_inventory_progress(progress: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Allow-list scan progress fields and expire stale `running` heartbeats."""
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    reported_status = progress.get("status")
    if not isinstance(reported_status, str) or reported_status not in {"running", "complete", "incomplete"}:
        reported_status = "unknown"
    updated_at = progress.get("updated_at")
    updated = parse_timestamp(updated_at)
    age_seconds = int((current - updated).total_seconds()) if updated else None
    status = reported_status
    if reported_status == "running" and (
        age_seconds is None or age_seconds < -INVENTORY_PROGRESS_STALE_SECONDS
        or age_seconds > INVENTORY_PROGRESS_STALE_SECONDS
    ):
        status = "stale"

    result: dict[str, Any] = {
        "status": status,
        "reported_status": reported_status,
        "phase": _safe_text(progress.get("phase"), limit=100),
        "elapsed_seconds": _safe_duration(progress.get("elapsed_seconds")),
        "directories_discovered": _safe_count(progress.get("directories_discovered")),
        "directories_started": _safe_count(progress.get("directories_started")),
        "directories_completed": _safe_count(progress.get("directories_completed")),
        "directories_succeeded": _safe_count(progress.get("directories_succeeded")),
        "directories_failed": _safe_count(progress.get("directories_failed")),
        "directories_pending": _safe_count(progress.get("directories_pending")),
        "files_seen": _safe_count(progress.get("files_seen")),
        "updated_at": _safe_text(updated_at, limit=64),
        "heartbeat_age_seconds": age_seconds if age_seconds is None or age_seconds >= 0 else None,
    }

    active_paths = progress.get("active_paths")
    safe_paths: list[str] = []
    if isinstance(active_paths, list):
        for item in active_paths[:12]:
            value = item.get("path") or item.get("relative_path") if isinstance(item, dict) else item
            text = _safe_text(value, limit=512)
            if text is not None:
                safe_paths.append(text)
    result["active_paths"] = safe_paths

    raw_counts = progress.get("error_counts")
    error_counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for key, value in list(raw_counts.items())[:40]:
            safe_key = _safe_text(str(key), limit=80)
            safe_value = _safe_count(value)
            if safe_key is not None and safe_value is not None:
                error_counts[safe_key] = safe_value
    result["error_counts"] = error_counts

    recent_errors = progress.get("recent_errors")
    safe_errors: list[Any] = []
    if isinstance(recent_errors, list):
        for item in recent_errors[-12:]:
            if isinstance(item, str):
                safe = _safe_text(item, limit=1000)
                if safe is not None:
                    safe_errors.append(safe)
            elif isinstance(item, dict):
                safe_item = {
                    key: safe for source, key, limit in (
                        ("code", "code", 100), ("phase", "phase", 100),
                        ("message", "message", 1000), ("path", "path", 512),
                    )
                    if (safe := _safe_text(item.get(source), limit=limit)) is not None
                }
                if safe_item:
                    safe_errors.append(safe_item)
    result["recent_errors"] = safe_errors
    return result


def enrich_freshness(inventory: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Return a copy with freshness recomputed from the successful scan timestamp."""
    result = dict(inventory)
    raw = inventory.get("freshness")
    freshness = dict(raw) if isinstance(raw, dict) else {}
    generated = parse_timestamp(inventory.get("generated_at") or inventory.get("updated_at"))
    try:
        ttl = int(freshness.get("ttl_seconds"))
    except (TypeError, ValueError):
        ttl = 0
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if generated is None or ttl <= 0:
        freshness.update({"state": "unknown", "age_seconds": None})
    else:
        raw_age = int((current - generated).total_seconds())
        if raw_age < -120:
            freshness.update({"state": "unknown", "age_seconds": None})
            result["freshness"] = freshness
            return result
        age = max(0, raw_age)
        complete = inventory.get("complete") is True
        freshness.update({
            "state": "fresh" if complete and age <= ttl else ("stale" if complete else "incomplete"),
            "age_seconds": age,
            "ttl_seconds": ttl,
        })
    result["freshness"] = freshness
    return result


def _timestamp_text(value: Any) -> tuple[dt.datetime | None, str | None]:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None, None
    return parsed, parsed.isoformat().replace("+00:00", "Z")


def enrich_reconcile_report(
    report: Any,
    *,
    report_error: str | None,
    inventory_updated_at: Any,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Project only reconciliation status counts and timestamps for the monitor."""
    _, inventory_timestamp = _timestamp_text(inventory_updated_at)

    def unavailable(error: str) -> dict[str, Any]:
        return {
            "available": False,
            "status": "unavailable",
            "error": error,
            "mode": None,
            "updated_at": None,
            "action_counts": None,
            "actions_total": None,
            "unresolved_blocked_count": None,
            "error_codes": {},
            "inventory_snapshot_at": inventory_timestamp,
            "inventory_reflects_apply": None,
        }

    if not isinstance(report, dict):
        return unavailable(report_error if isinstance(report_error, str)
                           and report_error in {"not_found", "unreadable_or_invalid"}
                           else "invalid_shape")
    mode = report.get("mode")
    if mode not in ("apply", "dry-run"):
        return unavailable("invalid_shape")
    report_timestamp, report_timestamp_text = _timestamp_text(report.get("updated_at"))
    actions = report.get("actions")
    errors = report.get("errors", [])
    unresolved = report.get("unresolved", [])
    if (report_timestamp is None or not isinstance(actions, list)
            or not isinstance(errors, list) or not isinstance(unresolved, list)):
        return unavailable("invalid_shape")

    action_counts = {state: 0 for state in _RECONCILE_ACTION_STATES}
    for action in actions:
        state = action.get("state") if isinstance(action, dict) else None
        if not isinstance(state, str) or state not in action_counts:
            return unavailable("invalid_shape")
        action_counts[state] += 1

    unresolved_blocked_count = 0
    for item in unresolved:
        if not isinstance(item, dict):
            return unavailable("invalid_shape")
        if item.get("state") == "blocked":
            unresolved_blocked_count += 1

    # A blocked action and its error-log row can describe the same incident.
    # Deduplicate rows that carry the same action_id while retaining separate
    # unresolved findings and standalone errors.
    error_codes: dict[str, int] = {}
    seen_errors: set[tuple[str, str]] = set()

    def add_error_code(value: Any, identity: str) -> None:
        code = value if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value) else "other"
        key = (code, identity)
        if key in seen_errors:
            return
        seen_errors.add(key)
        error_codes[code] = error_codes.get(code, 0) + 1

    for index, action in enumerate(actions):
        if action.get("state") != "blocked":
            continue
        blocked = action.get("blocked")
        code = blocked.get("code") if isinstance(blocked, dict) else action.get("code")
        identity = str(action.get("action_id") or "action-{}".format(index))
        add_error_code(code, "action:" + identity)
    for index, item in enumerate(errors):
        if not isinstance(item, dict):
            return unavailable("invalid_shape")
        identity = item.get("action_id")
        add_error_code(item.get("code"), "action:" + str(identity) if identity else "error:{}".format(index))
    for index, item in enumerate(unresolved):
        if item.get("state") != "blocked":
            continue
        identity = "unresolved:{}".format(index)
        add_error_code(item.get("code"), identity)

    has_review_items = bool(action_counts["blocked"] or unresolved_blocked_count or error_codes)
    has_unfinished_actions = bool(action_counts["planned"] or action_counts["pending"])
    report_complete = report.get("complete") is True
    inventory_timestamp_dt, _ = _timestamp_text(inventory_updated_at)
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    timestamps_are_plausible = (
        report_timestamp <= current
        and inventory_timestamp_dt is not None
        and inventory_timestamp_dt <= current
    )
    inventory_reflects_apply: bool | None
    if mode == "dry-run":
        status = "preview_needs_review" if has_review_items else "preview"
        inventory_reflects_apply = None
    elif has_review_items:
        status = "needs_review"
        inventory_reflects_apply = False
    elif has_unfinished_actions or not report_complete:
        status = "incomplete"
        inventory_reflects_apply = False
    elif (timestamps_are_plausible
          and inventory_timestamp_dt > report_timestamp):
        status = "inventoried"
        inventory_reflects_apply = True
    else:
        status = "awaiting_inventory"
        inventory_reflects_apply = False

    return {
        "available": True,
        "status": status,
        "error": None,
        "mode": mode,
        "updated_at": report_timestamp_text,
        "action_counts": action_counts,
        "actions_total": len(actions),
        "unresolved_blocked_count": unresolved_blocked_count,
        "error_codes": dict(sorted(error_codes.items())),
        "inventory_snapshot_at": inventory_timestamp,
        "inventory_reflects_apply": inventory_reflects_apply,
    }


def build_api_data(
    inventory_path: Path,
    progress_path: Path,
    failure_path: Path,
    *,
    inventory_progress_path: Path | None = None,
    reconcile_report_path: Path | None = None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], int]:
    inventory, inventory_error = read_json(inventory_path)
    progress, progress_error = read_json(progress_path)
    scan_progress, scan_progress_error = read_json(inventory_progress_path) if inventory_progress_path else (None, "not_found")
    failure, failure_error = read_json(failure_path)
    report_path = reconcile_report_path or inventory_path.parent / "reconcile-report.json"
    reconcile_report, reconcile_report_error = read_json(report_path)
    inventory_timestamp = (inventory.get("generated_at") or inventory.get("updated_at")
                           if isinstance(inventory, dict) else None)
    reconciliation = enrich_reconcile_report(
        reconcile_report, report_error=reconcile_report_error,
        inventory_updated_at=inventory_timestamp,
        now=now,
    )
    safe_scan_progress = enrich_inventory_progress(scan_progress, now=now) if isinstance(scan_progress, dict) else None
    safe_scan_progress_error = (
        None if safe_scan_progress is not None or scan_progress_error == "not_found"
        else scan_progress_error or "invalid_shape"
    )
    if not isinstance(inventory, dict):
        return ({
            "ok": False,
            "error": "inventory_unavailable",
            "inventory_error": inventory_error or "invalid_shape",
            "progress": progress if isinstance(progress, dict) else None,
            "inventory_progress": safe_scan_progress,
            "inventory_progress_error": safe_scan_progress_error,
            "reconciliation": reconciliation,
        }, 503)

    payload = enrich_freshness(inventory, now=now)
    payload["progress"] = progress if isinstance(progress, dict) else None
    payload["progress_error"] = None if isinstance(progress, dict) or progress_error == "not_found" else progress_error
    payload["inventory_progress"] = safe_scan_progress
    payload["inventory_progress_error"] = safe_scan_progress_error
    payload["latest_attempt"] = failure if isinstance(failure, dict) else None
    payload["latest_attempt_error"] = None if isinstance(failure, dict) or failure_error == "not_found" else failure_error
    payload["reconciliation"] = reconciliation
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    payload["ok"] = bool(payload.get("complete") is True and freshness.get("state") == "fresh")
    payload["inventory_error"] = None
    return payload, 200


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def make_handler(
    *, inventory_path: Path, progress_path: Path, failure_path: Path,
    inventory_progress_path: Path | None = None,
    reconcile_report_path: Path | None = None,
    index_path: Path = INDEX_PATH,
) -> type[BaseHTTPRequestHandler]:
    resolved_index = index_path.resolve()
    monitor_dir = Path(__file__).resolve().parent
    if not resolved_index.is_relative_to(monitor_dir):
        raise ValueError("index path must stay inside the monitor directory")

    class Handler(BaseHTTPRequestHandler):
        server_version = "BmGuangyaMonitor/1.0"
        sys_version = ""

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            # Routes are fixed strings. Never turn request paths into filesystem paths.
            path = urlsplit(self.path).path
            if path == "/":
                try:
                    body = resolved_index.read_bytes()
                except OSError:
                    self._send(500, b"monitor page unavailable\n", "text/plain; charset=utf-8")
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path in ("/api/data", "/api/health"):
                payload, status = build_api_data(
                    inventory_path, progress_path, failure_path,
                    inventory_progress_path=inventory_progress_path,
                    reconcile_report_path=reconcile_report_path,
                )
                if path == "/api/health":
                    scan = payload.get("inventory_progress")
                    payload = {
                        "ok": payload.get("ok", False),
                        "inventory_available": payload.get("inventory_error") is None,
                        "complete": payload.get("complete", False),
                        "freshness": payload.get("freshness"),
                        "updated_at": payload.get("updated_at"),
                        "latest_attempt": payload.get("latest_attempt"),
                        "reconciliation": payload.get("reconciliation"),
                        "inventory_progress": ({
                            "status": scan.get("status"),
                            "phase": scan.get("phase"),
                            "updated_at": scan.get("updated_at"),
                            "heartbeat_age_seconds": scan.get("heartbeat_age_seconds"),
                        } if isinstance(scan, dict) else None),
                    }
                self._send(status, _json_bytes(payload), "application/json; charset=utf-8")
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - explicitly read-only API
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

        def do_PUT(self) -> None:  # noqa: N802
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

        def log_message(self, _format: str, *_args: Any) -> None:
            # Avoid logging query strings, which may contain user-supplied data.
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--progress", type=Path)
    parser.add_argument("--inventory-progress", type=Path)
    parser.add_argument("--reconcile-report", type=Path)
    parser.add_argument("--failure", type=Path)
    parser.add_argument("--index", type=Path, default=INDEX_PATH)
    args = parser.parse_args(argv)
    inventory_path = args.inventory or args.state_dir / "library.json"
    progress_path = args.progress or args.state_dir / "sync-progress.json"
    inventory_progress_path = args.inventory_progress or args.state_dir / "library.progress.json"
    reconcile_report_path = args.reconcile_report or args.state_dir / "reconcile-report.json"
    failure_path = args.failure or Path(str(inventory_path) + ".failure.json")
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        parser.error("the monitor must bind to loopback")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    handler = make_handler(
        inventory_path=inventory_path,
        progress_path=progress_path,
        failure_path=failure_path,
        inventory_progress_path=inventory_progress_path,
        reconcile_report_path=reconcile_report_path,
        index_path=args.index,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
