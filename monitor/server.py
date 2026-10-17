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


def build_api_data(
    inventory_path: Path,
    progress_path: Path,
    failure_path: Path,
    *,
    inventory_progress_path: Path | None = None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], int]:
    inventory, inventory_error = read_json(inventory_path)
    progress, progress_error = read_json(progress_path)
    scan_progress, scan_progress_error = read_json(inventory_progress_path) if inventory_progress_path else (None, "not_found")
    failure, failure_error = read_json(failure_path)
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
        }, 503)

    payload = enrich_freshness(inventory, now=now)
    payload["progress"] = progress if isinstance(progress, dict) else None
    payload["progress_error"] = None if isinstance(progress, dict) or progress_error == "not_found" else progress_error
    payload["inventory_progress"] = safe_scan_progress
    payload["inventory_progress_error"] = safe_scan_progress_error
    payload["latest_attempt"] = failure if isinstance(failure, dict) else None
    payload["latest_attempt_error"] = None if isinstance(failure, dict) or failure_error == "not_found" else failure_error
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    payload["ok"] = bool(payload.get("complete") is True and freshness.get("state") == "fresh")
    payload["inventory_error"] = None
    return payload, 200


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def make_handler(
    *, inventory_path: Path, progress_path: Path, failure_path: Path,
    inventory_progress_path: Path | None = None, index_path: Path = INDEX_PATH
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
    parser.add_argument("--failure", type=Path)
    parser.add_argument("--index", type=Path, default=INDEX_PATH)
    args = parser.parse_args(argv)
    inventory_path = args.inventory or args.state_dir / "library.json"
    progress_path = args.progress or args.state_dir / "sync-progress.json"
    inventory_progress_path = args.inventory_progress or args.state_dir / "library.progress.json"
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
