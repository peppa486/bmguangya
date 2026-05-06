#!/opt/anime-stack/monitor/venv/bin/python3
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from collections import defaultdict, Counter
from flask import Flask, render_template_string, jsonify

STATE_DIR = "/opt/anime-stack/state"
CONFIG = "/opt/anime-stack/config/bangumi-sync.json"
QBITTORRENT_BASE = "http://127.0.0.1:8080"
QBITTORRENT_USER = "admin"
QBITTORRENT_PASS = "MoonQB!gOr0iV1OvGRz_A"

app = Flask(__name__)

# ========== helpers ==========
def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_catalog() -> dict:
    return read_json(f"{STATE_DIR}/bangumi-collections-catalog.json")


def get_state() -> dict:
    return read_json(f"{STATE_DIR}/bangumi-sync-state.json")


def get_config() -> dict:
    return read_json(CONFIG)


def format_size(b: int) -> str:
    if b == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"


def format_speed(bps: int) -> str:
    if bps == 0:
        return "0 B/s"
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if abs(bps) < 1024:
            return f"{bps:.2f} {unit}"
        bps /= 1024
    return f"{bps:.2f} TB/s"


def format_dt(iso: str) -> str:
    if not iso:
        return "从未"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso)


# ========== qBittorrent (enhanced) ==========
_qb_lock = threading.Lock()
_gy_progress_cache = {"ts": 0.0, "data": None}
_qb_opener = None
_qb_cookie_jar = None


def _qb_get_opener():
    global _qb_opener, _qb_cookie_jar
    if _qb_opener is None:
        _qb_cookie_jar = CookieJar()
        _qb_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(_qb_cookie_jar)
        )
    return _qb_opener


def _qb_login() -> bool:
    try:
        opener = _qb_get_opener()
        data = urllib.parse.urlencode(
            {"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{QBITTORRENT_BASE}/api/v2/auth/login",
            data=data,
            headers={"User-Agent": "anime-monitor/0.2"},
        )
        with opener.open(req, timeout=8) as r:
            body = r.read().decode().strip()
            return body == "Ok."
    except Exception:
        return False


def _qb_api(path: str) -> dict:
    with _qb_lock:
        opener = _qb_get_opener()
        url = f"{QBITTORRENT_BASE}/api/v2{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "anime-monitor/0.2"})
        try:
            with opener.open(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if _qb_login():
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "anime-monitor/0.2"}
                    )
                    with opener.open(req, timeout=10) as r:
                        return json.loads(r.read().decode("utf-8"))
            raise


def _qb_raw_text(path: str) -> str:
    with _qb_lock:
        opener = _qb_get_opener()
        url = f"{QBITTORRENT_BASE}/api/v2{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "anime-monitor/0.2"})
        try:
            with opener.open(req, timeout=10) as r:
                return r.read().decode("utf-8").strip()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if _qb_login():
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "anime-monitor/0.2"}
                    )
                    with opener.open(req, timeout=10) as r:
                        return r.read().decode("utf-8").strip()
            raise


def get_qb_data() -> dict:
    try:
        if not _qb_login():
            return {"ok": False, "error": "登录失败", "version": ""}

        version = ""
        try:
            version = _qb_raw_text("/app/version")
        except Exception:
            pass

        torrents = _qb_api("/torrents/info?limit=500")
        maindata = _qb_api("/sync/maindata")

        downloading = []
        completed = []
        for t in torrents:
            item = {
                "name": t.get("name", "未知"),
                "progress": round(t.get("progress", 0) * 100, 1),
                "size": format_size(t.get("size", 0)),
                "size_bytes": t.get("size", 0),
                "dlspeed": t.get("dlspeed", 0),
                "upspeed": t.get("upspeed", 0),
                "state": t.get("state", "未知"),
                "hash": t.get("hash", ""),
            }
            state = t.get("state", "")
            if state in ("downloading", "stalledDL", "queuedDL", "metaDL", "forcedDL"):
                downloading.append(item)
            elif state in ("uploading", "stalledUP", "queuedUP", "checkingUP", "forcedUP"):
                completed.append(item)
            elif t.get("progress", 0) >= 1:
                completed.append(item)
            else:
                downloading.append(item)

        srv = maindata.get("server_state", {})
        global_stats = {
            "dl_speed": srv.get("dl_info_speed", 0),
            "up_speed": srv.get("up_info_speed", 0),
            "dl_total": srv.get("alltime_dl", 0),
            "up_total": srv.get("alltime_ul", 0),
            "free_space": srv.get("free_space_on_disk", 0),
            "dht_nodes": srv.get("dht_nodes", 0),
            "connection_status": srv.get("connection_status", "未知"),
        }

        return {
            "ok": True,
            "version": version,
            "downloading": downloading[:10],
            "completed": completed[:10],
            "global_stats": global_stats,
            "torrent_count": len(torrents),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_qb_full_data() -> dict:
    try:
        if not _qb_login():
            return {"ok": False, "error": "登录失败", "version": ""}

        version = ""
        try:
            version = _qb_raw_text("/app/version")
        except Exception:
            pass

        prefs = {}
        try:
            prefs = _qb_api("/app/preferences")
        except Exception:
            pass

        torrents = _qb_api("/torrents/info?limit=500")
        maindata = _qb_api("/sync/maindata")

        states = Counter(t.get("state", "unknown") for t in torrents)
        state_labels = {
            "downloading": "下载中", "stalledDL": "等待下载", "queuedDL": "排队下载",
            "metaDL": "获取元数据", "forcedDL": "强制下载",
            "uploading": "做种中", "stalledUP": "等待做种", "queuedUP": "排队做种",
            "checkingUP": "校验中", "forcedUP": "强制做种",
            "pausedDL": "暂停下载", "pausedUP": "暂停做种",
            "checkingDL": "校验下载", "moving": "移动中", "missingFiles": "文件缺失",
            "error": "错误", "unknown": "未知",
        }

        qb_by_subject = defaultdict(list)
        for t in torrents:
            txt = t.get("name", "") + " " + t.get("save_path", "")
            m = re.search(r"bgm-(\d+)", txt)
            sid = m.group(1) if m else None
            qb_by_subject[sid].append({
                "name": t.get("name", ""),
                "state": t.get("state", ""),
                "progress": round(t.get("progress", 0) * 100, 1),
                "size_bytes": t.get("size", 0),
                "dlspeed": t.get("dlspeed", 0),
                "upspeed": t.get("upspeed", 0),
                "save_path": t.get("save_path", ""),
                "hash": t.get("hash", ""),
            })

        active_dl = [t for t in torrents if t.get("state") == "downloading"]
        queued = [t for t in torrents if t.get("state") == "queuedDL"]
        seeding = [t for t in torrents if t.get("state") in ("uploading", "stalledUP", "queuedUP")]

        max_active_dl = prefs.get("max_active_downloads", 0)
        max_active = prefs.get("max_active_torrents", 0)
        queueing = prefs.get("queueing_enabled", False)
        bottleneck = False
        if queueing and max_active_dl > 0 and len(queued) > max_active_dl * 2:
            bottleneck = True

        srv = maindata.get("server_state", {})
        return {
            "ok": True,
            "version": version,
            "torrent_count": len(torrents),
            "states": {k: {"count": v, "label": state_labels.get(k, k)} for k, v in states.items()},
            "active_downloads": len(active_dl),
            "queued_downloads": len(queued),
            "seeding": len(seeding),
            "bottleneck": bottleneck,
            "queue_settings": {
                "queueing_enabled": queueing,
                "max_active_downloads": max_active_dl,
                "max_active_torrents": max_active,
            },
            "by_subject": dict(qb_by_subject),
            "global_stats": {
                "dl_speed": srv.get("dl_info_speed", 0),
                "up_speed": srv.get("up_info_speed", 0),
                "free_space": srv.get("free_space_on_disk", 0),
                "dht_nodes": srv.get("dht_nodes", 0),
            },
            "top_queued": [
                {"name": t.get("name", "")[:80], "size": format_size(t.get("size", 0)), "save_path": t.get("save_path", "")}
                for t in queued[:20]
            ],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ========== library / storage (enhanced) ==========
def _dir_stats(path: str) -> dict:
    if not os.path.isdir(path):
        return {"path": path, "dirs": 0, "size_bytes": 0, "missing": True}
    try:
        out = subprocess.run(
            ["du", "-sb", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        size = int(out.stdout.split()[0]) if out.returncode == 0 and out.stdout.strip() else 0
        dirs = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
        files = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
        return {
            "path": path,
            "dirs": len(dirs),
            "files": len(files),
            "size_bytes": size,
            "missing": False,
        }
    except Exception:
        return {"path": path, "dirs": 0, "files": 0, "size_bytes": 0, "missing": False, "error": True}


def get_library_stats() -> dict:
    return {
        "downloads": _dir_stats("/data/torrents/complete/anime"),
        "staging": _dir_stats("/data/library/Anime"),
        "guangya_webdav": _dir_stats("/data/library/Anime"),
    }


def scan_anime_dirs(root: str) -> dict:
    result = {}
    if not os.path.isdir(root):
        return result
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        m = re.search(r"bgm-(\d+)", name)
        sid = m.group(1) if m else None
        file_count = 0
        size = 0
        try:
            for dp, _, files in os.walk(p):
                for f in files:
                    file_count += 1
                    try:
                        size += os.path.getsize(os.path.join(dp, f))
                    except Exception:
                        pass
        except Exception:
            pass
        result[name] = {"subject_id": sid, "files": file_count, "size": size, "path": p}
    return result


# ========== GuangYa Disk ==========
def get_guangya_stats() -> dict:
    try:
        out = subprocess.run(
            ["bash", "-lc", "rclone ls gdrive:Media/Anime --fast-list 2>/dev/null | wc -l"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        file_count = int(out.stdout.strip()) if out.returncode == 0 else 0

        size_out = subprocess.run(
            ["rclone", "size", "gdrive:Media/Anime", "--fast-list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        total_size = "未知"
        total_bytes = 0
        if size_out.returncode == 0:
            m = re.search(r"Total size:\s+([\d.]+\s+\w+)", size_out.stdout)
            if m:
                total_size = m.group(1)
            mb = re.search(r"\(([\d]+)\)", size_out.stdout)
            if mb:
                total_bytes = int(mb.group(1))

        return {
            "ok": True,
            "file_count": file_count,
            "total_size": total_size,
            "total_bytes": total_bytes,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ========== show matrix ==========
def get_show_matrix() -> dict:
    catalog = get_catalog()
    state = get_state()
    subjects = catalog.get("subjects", {})
    subject_to_mikan = state.get("subject_to_mikan", {})

    qb_full = get_qb_full_data()
    qb_by_subject = qb_full.get("by_subject", {}) if qb_full.get("ok") else {}

    local_dirs = scan_anime_dirs("/data/torrents/complete/anime")
    staging_dirs = scan_anime_dirs("/data/library/Anime")
    guangya_dirs = scan_anime_dirs("/data/library/Anime")

    local_by_sid = {v["subject_id"]: v for v in local_dirs.values() if v["subject_id"]}
    staging_by_sid = {v["subject_id"]: v for v in staging_dirs.values() if v["subject_id"]}
    guangya_by_sid = {v["subject_id"]: v for v in guangya_dirs.values() if v["subject_id"]}

    rows = []
    for sid, s in subjects.items():
        coll = s.get("collection_label", "")
        title = s.get("title", s.get("name_cn", s.get("name", "")))
        mapped = sid in subject_to_mikan
        qb_list = qb_by_subject.get(sid, [])
        qb_count = len(qb_list)
        local = local_by_sid.get(sid)
        staging = staging_by_sid.get(sid)
        guangya = guangya_by_sid.get(sid)

        if guangya:
            overall = "cloud"
            overall_label = "已在云端"
        elif staging:
            overall = "staging"
            overall_label = "暂存待上传"
        elif local:
            overall = "local"
            overall_label = "本地已完成"
        elif qb_count > 0:
            overall = "qb"
            overall_label = "qBittorrent"
        elif mapped:
            overall = "mapped"
            overall_label = "已映射未下载"
        else:
            overall = "unmapped"
            overall_label = "未映射"

        rows.append({
            "subject_id": sid,
            "title": title,
            "collection_label": coll,
            "collection_type": s.get("collection_type", 0),
            "mikan_mapped": mapped,
            "qb_count": qb_count,
            "qb_states": list(set(t["state"] for t in qb_list)) if qb_list else [],
            "local_files": local["files"] if local else 0,
            "local_size": local["size"] if local else 0,
            "staging_files": staging["files"] if staging else 0,
            "guangya_files": guangya["files"] if guangya else 0,
            "overall": overall,
            "overall_label": overall_label,
        })

    priority = {"cloud": 0, "staging": 1, "local": 2, "qb": 3, "mapped": 4, "unmapped": 5}
    rows.sort(key=lambda r: (priority.get(r["overall"], 99), r["title"]))

    stats = Counter(r["overall"] for r in rows)
    return {
        "rows": rows,
        "stats": dict(stats),
        "total": len(rows),
        "mapped_count": sum(1 for r in rows if r["mikan_mapped"]),
        "unmapped_count": sum(1 for r in rows if not r["mikan_mapped"]),
        "has_qb_count": sum(1 for r in rows if r["qb_count"] > 0),
        "has_local_count": sum(1 for r in rows if r["local_files"] > 0),
        "has_guangya_count": sum(1 for r in rows if r["guangya_files"] > 0),
    }


# ========== pipeline logs / events ==========
def get_pipeline_log(lines: int = 100) -> str:
    log = f"{STATE_DIR}/anime-pipeline.log"
    if not os.path.isfile(log):
        return "暂无日志"
    try:
        with open(log, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:]).rstrip() or "日志为空"
    except Exception as e:
        return f"读取日志失败: {e}"


def get_pipeline_events(limit: int = 30) -> list:
    log = f"{STATE_DIR}/anime-pipeline.log"
    if not os.path.isfile(log):
        return []
    events = []
    try:
        with open(log, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
        for line in reversed(all_lines):
            line = line.rstrip()
            if not line:
                continue
            m = re.match(r"\[(\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\]\s*(.+)", line)
            if m:
                ts, msg = m.group(1), m.group(2)
                level = "info"
                if "error" in msg.lower() or "失败" in msg:
                    level = "error"
                elif "warn" in msg.lower():
                    level = "warn"
                elif "completed" in msg.lower() or "done" in msg.lower() or "ok" in msg.lower():
                    level = "success"
                events.append({"time": format_dt(ts), "raw": ts, "message": msg, "level": level})
                if len(events) >= limit:
                    break
            else:
                if "Transferred:" in line or "Elapsed time:" in line:
                    continue
                events.append({"time": "", "message": line, "level": "info"})
                if len(events) >= limit:
                    break
        return list(reversed(events))
    except Exception:
        return []


# ========== pipeline status ==========
def get_pipeline_status() -> dict:
    catalog = get_catalog()
    state = get_state()
    qb = get_qb_data()
    lib = get_library_stats()
    guangya = get_guangya_stats()

    def _qb_status():
        if not qb.get("ok"):
            return "error"
        dl = qb.get("downloading", [])
        if dl:
            return "active"
        return "healthy"

    def _guangya_status():
        if not guangya.get("ok"):
            return "error"
        if guangya.get("file_count", 0) == 0:
            return "warning"
        return "healthy"

    return {
        "bangumi": "healthy" if catalog.get("tracked_count", 0) > 0 else "warning",
        "mikan": "healthy" if state.get("subject_to_mikan") else "warning",
        "qbittorrent": _qb_status(),
        "library": "healthy" if lib.get("guangya_webdav", {}).get("dirs", 0) > 0 else "warning",
        "gdrive": _guangya_status(),
    }


def get_active_pipeline_processes() -> list[dict]:
    try:
        out = subprocess.run(
            [
                "bash",
                "-lc",
                "ps -eo pid=,args= | grep -E 'run_anime_pipeline|bangumi_mikan_qb_sync|import_completed_anime_to_library|prune_library_by_bangumi_state|sync_library_to_gdrive|cleanup_synced_anime_downloads' | grep -v grep",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        rows = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid, cmd = parts
            stage = "pipeline"
            label = "总流程"
            if "bangumi_cloud_download.py" in cmd:
                stage, label = "sync", "Bangumi → Mikan → GuangYa 离线下载"
            elif "bangumi_mikan_qb_sync.py" in cmd:
                stage, label = "sync", "Bangumi → Mikan → qB 入队"
            elif "import_completed_anime_to_library.py" in cmd:
                stage, label = "import", "整理 / 改名 / 入库暂存"
            elif "prune_library_by_bangumi_state.py" in cmd:
                stage, label = "prune", "按 Bangumi 状态清理库"
            elif "sync_library_to_guangya.sh" in cmd:
                stage, label = "upload", "上传到光鸭云盘"
            elif "cleanup_synced_anime_downloads.py" in cmd:
                stage, label = "cleanup", "删除已同步本地原始下载"
            rows.append({"pid": pid, "cmd": cmd, "stage": stage, "label": label})
        return rows
    except Exception:
        return []


def get_current_run_lines() -> list[str]:
    log = f"{STATE_DIR}/anime-pipeline.log"
    if not os.path.isfile(log):
        return []
    try:
        with open(log, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.read().splitlines()
        last_start = 0
        for i, line in enumerate(all_lines):
            if "[anime-pipeline] start" in line:
                last_start = i
        return all_lines[last_start:]
    except Exception:
        return []


def get_live_pipeline_status() -> dict:
    lines = get_current_run_lines()
    procs = get_active_pipeline_processes()
    running = bool(procs)

    tracked_total = 0
    processed_subjects = 0
    mapped_count = 0
    unresolved_count = 0
    added_count = 0
    latest_message = ""
    stage = "idle"
    stage_label = "空闲"

    for line in lines:
        if "Bangumi tracked anime count:" in line:
            m = re.search(r"Bangumi tracked anime count:\s*(\d+)", line)
            if m:
                tracked_total = int(m.group(1))
        if "Processing:" in line:
            processed_subjects += 1
            latest_message = line
        elif "Mapped to Mikan" in line:
            mapped_count += 1
            latest_message = line
        elif "Unresolved on Mikan" in line:
            unresolved_count += 1
            latest_message = line
        elif "Submitted GuangYa offline task:" in line:
            added_count += 1
            latest_message = line
        elif "Added to qBittorrent:" in line:
            added_count += 1
            latest_message = line
        elif line.strip():
            latest_message = line

    if procs:
        priority = {"sync": 5, "import": 4, "prune": 3, "upload": 2, "cleanup": 1, "pipeline": 0}
        active = max(procs, key=lambda x: priority.get(x["stage"], -1))
        stage = active["stage"]
        stage_label = active["label"]
    elif lines and any("[anime-pipeline] done" in line for line in lines[-5:]):
        stage = "done"
        stage_label = "本轮已完成"
    elif lines:
        stage = "waiting"
        stage_label = "等待下一轮"

    stage_progress = 0
    if stage == "sync" and tracked_total > 0:
        stage_progress = round(processed_subjects / tracked_total * 100, 1)
    elif stage == "done":
        stage_progress = 100

    latest_subject = ""
    for line in reversed(lines):
        m = re.search(r"Processing:\s*(.+?)\s*\(subject_id=", line)
        if m:
            latest_subject = m.group(1)
            break

    tail = lines[-25:]
    events = get_pipeline_events(20)
    return {
        "running": running,
        "stage": stage,
        "stage_label": stage_label,
        "stage_progress": stage_progress,
        "tracked_total": tracked_total,
        "processed_subjects": processed_subjects,
        "mapped_count": mapped_count,
        "unresolved_count": unresolved_count,
        "added_count": added_count,
        "latest_subject": latest_subject,
        "latest_message": latest_message,
        "active_processes": procs,
        "tail_lines": tail,
        "events": events,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_guangya_download_progress() -> dict:
    global _gy_progress_cache
    if _gy_progress_cache.get("data") and time.time() - float(_gy_progress_cache.get("ts") or 0) < 60:
        data = dict(_gy_progress_cache["data"])
        data["cached"] = True
        return data
    state = get_state()
    config = get_config()
    tasks = state.get("cloud_tasks", {}) or {}
    refreshed = False
    refresh_error = ""
    if tasks:
        try:
            pending = {
                guid: task for guid, task in tasks.items()
                if isinstance(task, dict) and task.get("task_id") and task.get("status") in {"submitted", "running"}
            }
            if pending:
                child_code = """
import json, sys
sys.path.insert(0, '/opt/guangya-webdav')
from app.guangya_client import GuangYaClient
cfg=json.load(open('/opt/guangya-webdav/config/config.json', encoding='utf-8'))
client=GuangYaClient(
    access_token=cfg.get('access_token'),
    refresh_token=cfg.get('refresh_token'),
    client_id=cfg.get('client_id'),
    device_id=cfg.get('device_id'),
)
payload=json.load(sys.stdin)
resp=client._request('POST', 'https://api.guangyapan.com/cloudcollection/v1/list_task', data={'taskIds': payload.get('taskIds', [])}, timeout=12)
print(json.dumps(resp, ensure_ascii=False))
"""
                proc = subprocess.run(
                    ["/opt/guangya-webdav/venv/bin/python", "-c", child_code],
                    input=json.dumps({"taskIds": [task["task_id"] for task in pending.values()]}),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or proc.stdout or "refresh failed")[-160:])
                detail_resp = json.loads(proc.stdout or "{}")
                raw_items = detail_resp.get("data") or detail_resp.get("list") or detail_resp.get("items") or []
                if isinstance(raw_items, dict):
                    raw_items = raw_items.get("list") or raw_items.get("items") or raw_items.get("records") or []
                by_id = {str(item.get("taskId") or item.get("task_id") or item.get("id")): item for item in raw_items if isinstance(item, dict)}
                for task in pending.values():
                    detail = by_id.get(str(task.get("task_id")))
                    if not detail:
                        continue
                    status = int(detail.get("status") or 0)
                    task["remote_status"] = status
                    task["remote_file_name"] = detail.get("fileName") or detail.get("file_name")
                    task["exist"] = bool(detail.get("exist"))
                    if status == 1:
                        task["status"] = "submitted"
                    elif status == 2:
                        task["status"] = "completed"
                    elif status == 3:
                        task["status"] = "failed"
                    elif status == 5:
                        task["status"] = "running"
                    elif status:
                        task["status"] = f"status_{status}"
                refreshed = True
        except Exception as e:
            refresh_error = str(e).replace("\n", " ")[:160]
    now_cn = datetime.now().strftime("%Y-%m-%d")
    daily_limit = int(config.get("max_new_cloud_tasks_per_run", 0) or 0)
    status_counts = Counter()
    collection_counts = Counter()
    show_map = {}
    today_submitted = 0
    specials = 0
    episodes = 0

    for guid, task in tasks.items():
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "unknown")
        status_counts[status] += 1
        coll = task.get("collection") or task.get("category_dir") or "-"
        collection_counts[coll] += 1
        submitted_at = str(task.get("submitted_at") or "")
        if submitted_at.startswith(now_cn):
            today_submitted += 1
        target_name = str(task.get("target_name") or "")
        if re.match(r"^(SP|OVA|OAD|NCOP|NCED)", target_name, re.I):
            specials += 1
        elif re.match(r"^\d{2}", target_name):
            episodes += 1
        show_key = task.get("target_dir") or f"{task.get('category_dir','')}/{task.get('show','')}"
        show = show_map.setdefault(show_key, {
            "target_dir": show_key,
            "show": task.get("show") or show_key.split("/")[-1],
            "collection": coll,
            "total": 0,
            "completed": 0,
            "running": 0,
            "submitted": 0,
            "failed": 0,
            "specials": 0,
            "episodes": 0,
            "latest": "",
        })
        show["total"] += 1
        if status == "completed":
            show["completed"] += 1
        elif status == "running":
            show["running"] += 1
        elif status == "failed" or status.startswith("status_"):
            show["failed"] += 1
        else:
            show["submitted"] += 1
        if re.match(r"^(SP|OVA|OAD|NCOP|NCED)", target_name, re.I):
            show["specials"] += 1
        else:
            show["episodes"] += 1
        if submitted_at and submitted_at > show.get("latest", ""):
            show["latest"] = submitted_at

    rows = list(show_map.values())
    for row in rows:
        row["percent"] = round(row["completed"] / row["total"] * 100, 1) if row["total"] else 0
    rows.sort(key=lambda r: (r["completed"] / r["total"] if r["total"] else 0, -r["total"], r["show"]))
    total = len(tasks)
    completed = status_counts.get("completed", 0)
    quota_percent = round(today_submitted / daily_limit * 100, 1) if daily_limit else 0
    data = {
        "total": total,
        "completed": completed,
        "running": status_counts.get("running", 0),
        "submitted": status_counts.get("submitted", 0),
        "failed": status_counts.get("failed", 0) + sum(v for k, v in status_counts.items() if str(k).startswith("status_")),
        "percent": round(completed / total * 100, 1) if total else 0,
        "today": today_submitted,
        "daily_limit": daily_limit,
        "quota_percent": quota_percent,
        "episodes": episodes,
        "specials": specials,
        "collections": dict(collection_counts),
        "status_counts": dict(status_counts),
        "rows": rows[:80],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refreshed": refreshed,
        "refresh_error": refresh_error,
        "cached": False,
    }
    _gy_progress_cache = {"ts": time.time(), "data": data}
    return data


# ========== HTML TEMPLATE ==========
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anime Stack Monitor</title>
<style>
:root {
  --bg:#0b0d10; --card:#13161b; --text:#e2e4e9; --muted:#8b929d;
  --ok:#3ddc84; --warn:#ffb224; --err:#ff4d4f; --accent:#3399ff;
  --border:#1f232b; --hover:#1a1d24; --info:#66b3ff; --purple:#a78bfa; --orange:#fb923c;
}
*{box-sizing:border-box}
body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif; line-height:1.5; }
.container { max-width:1400px; margin:0 auto; padding:24px; }
header { border-bottom:1px solid var(--border); padding-bottom:16px; margin-bottom:28px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }
h1 { margin:0; font-size:1.7rem; color:var(--accent); letter-spacing:-0.3px; }
.refresh { color:var(--muted); font-size:0.88rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:18px; margin-bottom:28px; }
.card { background:var(--card); border-radius:14px; padding:20px; border:1px solid var(--border); transition:border-color .2s, transform .2s; }
.card:hover { border-color:#2a303c; transform:translateY(-1px); }
.card h2 { margin:0 0 12px; font-size:0.95rem; color:var(--accent); text-transform:uppercase; letter-spacing:0.6px; font-weight:600; display:flex; align-items:center; gap:8px; }
.metric { font-size:2.1rem; font-weight:800; margin:4px 0; letter-spacing:-0.5px; }
.metric.small { font-size:1.35rem; }
.label { color:var(--muted); font-size:0.82rem; margin-top:6px; }
.status { display:inline-flex; align-items:center; gap:6px; padding:4px 12px; border-radius:20px; font-size:0.82rem; font-weight:700; }
.status.ok { background:rgba(61,220,132,0.1); color:var(--ok); }
.status.warn { background:rgba(255,178,36,0.1); color:var(--warn); }
.status.err { background:rgba(255,77,79,0.1); color:var(--err); }
.status.info { background:rgba(51,153,255,0.1); color:var(--info); }
.dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
.dot.ok { background:var(--ok); box-shadow:0 0 8px var(--ok); }
.dot.warn { background:var(--warn); box-shadow:0 0 8px var(--warn); }
.dot.err { background:var(--err); box-shadow:0 0 8px var(--err); }
.dot.active { background:var(--accent); box-shadow:0 0 8px var(--accent); animation:pulse 1.5s infinite; }
table { width:100%; border-collapse:collapse; font-size:0.9rem; }
th,td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--border); }
th { color:var(--muted); font-weight:500; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.4px; }
tr:last-child td { border-bottom:none; }
pre.log { background:#0a0c10; padding:18px; border-radius:10px; overflow-x:auto; font-size:0.82rem; line-height:1.5; color:#a0a6b1; max-height:420px; overflow-y:auto; border:1px solid var(--border); }
.badge { display:inline-block; padding:3px 10px; border-radius:6px; font-size:0.78rem; margin-right:6px; font-weight:600; }
.badge.wish { background:rgba(51,153,255,0.12); color:#66b3ff; }
.badge.collect { background:rgba(61,220,132,0.12); color:#5ce69c; }
.badge.doing { background:rgba(255,178,36,0.12); color:#ffc44d; }
.badge.cloud { background:rgba(167,139,250,0.12); color:#a78bfa; }
.badge.staging { background:rgba(102,179,255,0.12); color:#66b3ff; }
.badge.local { background:rgba(61,220,132,0.12); color:#5ce69c; }
.badge.qb { background:rgba(251,146,60,0.12); color:#fb923c; }
.badge.mapped { background:rgba(139,146,157,0.12); color:#8b929d; }
.badge.unmapped { background:rgba(255,77,79,0.12); color:#ff6b6b; }
.flex { display:flex; gap:16px; flex-wrap:wrap; }
.flex > div { flex:1; min-width:110px; }
.muted { color:var(--muted); }
.progress-bar { background:#1a1d24; border-radius:6px; height:8px; overflow:hidden; margin-top:6px; }
.progress-fill { background:linear-gradient(90deg,var(--accent),#66b3ff); height:100%; border-radius:6px; transition:width .4s ease; }
.timeline { position:relative; padding-left:18px; }
.timeline::before { content:""; position:absolute; left:5px; top:6px; bottom:6px; width:2px; background:var(--border); }
.timeline-item { position:relative; margin-bottom:14px; }
.timeline-item::before { content:""; position:absolute; left:-13px; top:6px; width:8px; height:8px; border-radius:50%; background:var(--muted); }
.timeline-item.success::before { background:var(--ok); box-shadow:0 0 6px var(--ok); }
.timeline-item.error::before { background:var(--err); box-shadow:0 0 6px var(--err); }
.timeline-item.warn::before { background:var(--warn); box-shadow:0 0 6px var(--warn); }
.timeline-time { font-size:0.75rem; color:var(--muted); margin-bottom:2px; }
.timeline-msg { font-size:0.88rem; word-break:break-word; }
.pipeline-wrap { overflow-x:auto; margin-bottom:28px; }
.pipeline-svg { min-width:900px; display:block; margin:0 auto; }
.live-grid { display:block; margin-bottom:28px; }
.live-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-top:16px; }
.live-metric { background:linear-gradient(180deg, rgba(27,33,42,0.95), rgba(14,18,24,0.95)); border:1px solid rgba(255,255,255,0.06); border-radius:12px; padding:14px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.03); }
.live-metric-value { font-size:1.3rem; font-weight:800; margin-bottom:4px; }
.live-stage-title { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:10px; }
.live-subject { font-size:1rem; font-weight:700; margin:8px 0 4px; }
.live-message { color:var(--muted); font-size:0.88rem; word-break:break-word; }
.live-shell { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(300px,0.9fr); gap:18px; align-items:stretch; }
.live-hero { background:radial-gradient(circle at top left, rgba(51,153,255,0.18), transparent 40%), linear-gradient(180deg, rgba(18,22,29,0.98), rgba(10,13,17,0.98)); border:1px solid rgba(255,255,255,0.06); border-radius:18px; padding:18px; position:relative; overflow:hidden; }
.live-hero::after { content:""; position:absolute; inset:auto -80px -80px auto; width:180px; height:180px; border-radius:50%; background:radial-gradient(circle, rgba(61,220,132,0.18), transparent 70%); pointer-events:none; }
.workflow-stage-copy { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:14px; }
.workflow-kicker { color:#66b3ff; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.18em; font-weight:700; }
.workflow-summary { font-size:0.95rem; color:var(--muted); max-width:560px; }
.live-stage-badge { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:999px; font-size:0.8rem; font-weight:700; background:rgba(51,153,255,0.12); color:#7dc4ff; }
.live-stage-badge::before { content:""; width:8px; height:8px; border-radius:50%; background:currentColor; box-shadow:0 0 12px currentColor; }
.live-workflow-board { background:linear-gradient(180deg, rgba(15,19,25,0.92), rgba(10,12,16,0.92)); border:1px solid rgba(255,255,255,0.05); border-radius:18px; padding:16px; }
.live-workflow-svg-wrap { position:relative; margin-bottom:16px; }
.live-workflow-svg { width:100%; height:auto; display:block; }
.workflow-stage-row { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:12px; }
.workflow-stage-card { position:relative; background:rgba(16,20,27,0.82); border:1px solid rgba(255,255,255,0.05); border-radius:14px; padding:14px; min-height:112px; transition:all .25s ease; }
.workflow-stage-card::before { content:""; position:absolute; inset:0; border-radius:inherit; border:1px solid transparent; pointer-events:none; }
.workflow-stage-card.done { border-color:rgba(61,220,132,0.18); background:linear-gradient(180deg, rgba(21,33,27,0.72), rgba(15,20,18,0.82)); }
.workflow-stage-card.active { border-color:rgba(51,153,255,0.45); box-shadow:0 0 0 1px rgba(51,153,255,0.12), 0 18px 50px rgba(51,153,255,0.16); transform:translateY(-2px); }
.workflow-stage-card.active::before { border-color:rgba(122,189,255,0.28); }
.workflow-stage-card.pending { opacity:0.7; }
.workflow-stage-dot { width:10px; height:10px; border-radius:50%; background:#4b5563; box-shadow:none; display:inline-block; margin-bottom:12px; }
.workflow-stage-card.done .workflow-stage-dot { background:var(--ok); box-shadow:0 0 12px var(--ok); }
.workflow-stage-card.active .workflow-stage-dot { background:var(--accent); box-shadow:0 0 14px var(--accent); animation:pulse 1.2s infinite; }
.workflow-stage-name { font-size:0.88rem; font-weight:700; margin-bottom:6px; }
.workflow-stage-desc { color:var(--muted); font-size:0.78rem; line-height:1.45; }
.workflow-ship-dot { fill:#7dc4ff; filter:drop-shadow(0 0 8px rgba(125,196,255,.9)); }
.workflow-ship-dot.warn { fill:#ffb224; filter:drop-shadow(0 0 8px rgba(255,178,36,.85)); }
.workflow-ship-dot.done { fill:#3ddc84; filter:drop-shadow(0 0 8px rgba(61,220,132,.85)); }
.live-sidebar { display:flex; flex-direction:column; gap:18px; }
.process-list { margin:12px 0 0; padding-left:18px; color:var(--muted); font-size:0.85rem; }
.process-list li { margin-bottom:6px; }
.log.compact { max-height:260px; }
.live-mini-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }
.live-mini-card { background:rgba(15,19,25,0.92); border:1px solid rgba(255,255,255,0.05); border-radius:14px; padding:14px; }
.live-mini-label { color:var(--muted); font-size:0.76rem; text-transform:uppercase; letter-spacing:.08em; }
.live-mini-value { font-size:1.35rem; font-weight:800; margin-top:6px; }
.panel-section { margin-bottom:28px; }
.panel-title { font-size:1.1rem; font-weight:700; color:var(--text); margin-bottom:14px; display:flex; align-items:center; gap:10px; }
.panel-title svg { width:20px; height:20px; color:var(--accent); }
.skeleton { background:linear-gradient(90deg, #1a1d24 25%, #252a35 50%, #1a1d24 75%); background-size:200% 100%; animation:skeleton 1.5s infinite; border-radius:8px; height:20px; }
@keyframes skeleton { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
.alert-banner { background:linear-gradient(90deg, rgba(255,77,79,0.15), rgba(255,178,36,0.1)); border:1px solid rgba(255,77,79,0.3); border-radius:12px; padding:14px 18px; margin-bottom:18px; display:flex; align-items:center; gap:12px; color:#ff8588; font-weight:600; }
.alert-banner.warn { background:linear-gradient(90deg, rgba(255,178,36,0.15), rgba(255,178,36,0.05)); border-color:rgba(255,178,36,0.3); color:#ffc44d; }
.bar-chart { display:flex; align-items:flex-end; gap:6px; height:120px; padding:10px 0; }
.bar-item { flex:1; display:flex; flex-direction:column; align-items:center; gap:6px; min-width:40px; }
.bar-fill { width:100%; border-radius:4px 4px 0 0; min-height:4px; transition:height .4s ease; }
.bar-label { font-size:0.7rem; color:var(--muted); text-align:center; white-space:nowrap; }
.bar-value { font-size:0.78rem; font-weight:700; }
.flow-grid { display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; }
.flow-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px; text-align:center; position:relative; }
.flow-arrow { position:absolute; right:-10px; top:50%; transform:translateY(-50%); color:var(--muted); font-size:1.2rem; z-index:2; }
.flow-card:last-child .flow-arrow { display:none; }
.flow-count { font-size:1.6rem; font-weight:800; margin:6px 0; }
.flow-label { font-size:0.78rem; color:var(--muted); }
.flow-delta { font-size:0.72rem; margin-top:4px; }
.matrix-toolbar { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
.matrix-toolbar input { background:#0a0c10; border:1px solid var(--border); border-radius:8px; padding:8px 12px; color:var(--text); font-size:0.88rem; min-width:240px; }
.matrix-toolbar input:focus { outline:none; border-color:var(--accent); }
.matrix-toolbar select { background:#0a0c10; border:1px solid var(--border); border-radius:8px; padding:8px 12px; color:var(--text); font-size:0.88rem; }
.matrix-table-wrap { overflow-x:auto; border-radius:12px; border:1px solid var(--border); }
.matrix-table { font-size:0.84rem; }
.matrix-table th { white-space:nowrap; }
.matrix-table td { white-space:nowrap; }
.matrix-table .td-title { max-width:260px; overflow:hidden; text-overflow:ellipsis; }
.pagination { display:flex; gap:8px; justify-content:center; margin-top:14px; }
.pagination button { background:var(--card); border:1px solid var(--border); color:var(--text); padding:6px 14px; border-radius:8px; cursor:pointer; font-size:0.82rem; }
.pagination button:hover { border-color:var(--accent); }
.pagination button:disabled { opacity:0.4; cursor:not-allowed; }
.pagination .page-info { color:var(--muted); font-size:0.82rem; padding:6px 10px; }
@keyframes pulse { 0%{opacity:1} 50%{opacity:.4} 100%{opacity:1} }
.packet { animation:movePacket 3s linear infinite; }
.packet:nth-child(2){animation-delay:0.6s}
.packet:nth-child(3){animation-delay:1.2s}
.packet:nth-child(4){animation-delay:1.8s}
.packet:nth-child(5){animation-delay:2.4s}
@keyframes movePacket { 0%{transform:translate(0,0);opacity:0} 10%{opacity:1} 90%{opacity:1} 100%{transform:translate(var(--dx), var(--dy));opacity:0} }

.gy-panel { background:linear-gradient(180deg, rgba(19,22,27,0.98), rgba(11,13,16,0.98)); border:1px solid var(--border); border-radius:16px; padding:16px; margin-bottom:28px; }
.gy-head { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; }
.gy-title { color:var(--accent); font-weight:800; letter-spacing:.08em; font-size:.9rem; }
.gy-kpis { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-bottom:14px; }
.gy-kpi { background:#0a0c10; border:1px solid var(--border); border-radius:10px; padding:10px; min-width:0; }
.gy-kpi b { display:block; font-size:1.25rem; line-height:1.1; }
.gy-kpi span { color:var(--muted); font-size:.68rem; letter-spacing:.08em; }
.gy-bars { display:grid; grid-template-columns:2fr 1fr; gap:12px; margin-bottom:14px; }
.gy-table-wrap { max-height:360px; overflow:auto; border:1px solid var(--border); border-radius:12px; }
.gy-table { font-size:.82rem; }
.gy-table td,.gy-table th { padding:8px; }
.gy-show { max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.gy-mini-bar { width:120px; height:7px; background:#1a1d24; border-radius:99px; overflow:hidden; }
.gy-mini-fill { height:100%; background:linear-gradient(90deg,var(--accent),var(--ok)); }
@media (max-width:980px){ .gy-kpis{grid-template-columns:repeat(3,minmax(0,1fr))} .gy-bars{grid-template-columns:1fr} }
@media (max-width:640px){ .gy-kpis{grid-template-columns:repeat(2,minmax(0,1fr))} }

@media (max-width:640px){ .container{padding:16px} h1{font-size:1.4rem} .metric{font-size:1.6rem} .flow-grid{grid-template-columns:repeat(3, minmax(0,1fr))} .flow-arrow{display:none} }
@media (max-width:980px){ .live-grid{grid-template-columns:1fr} .live-shell{grid-template-columns:1fr} .live-metrics{grid-template-columns:repeat(2,minmax(0,1fr))} .workflow-stage-row{grid-template-columns:repeat(2, minmax(0,1fr))} .flow-grid{grid-template-columns:repeat(3, minmax(0,1fr))} }
@media (max-width:640px){ .workflow-stage-row{grid-template-columns:1fr} .live-mini-grid{grid-template-columns:1fr} .flow-grid{grid-template-columns:repeat(2, minmax(0,1fr))} }
</style>
<meta http-equiv="refresh" content="60">
</head>
<body>
<div class="container">
<header>
  <h1>Anime Stack Monitor</h1>
  <div class="refresh">实时轮询: 3s | 整页刷新: 60s | {{ now }}</div>
</header>


<!-- GuangYa Progress -->
<section class="gy-panel">
  <div class="gy-head">
    <div class="gy-title">GUANGYA</div>
    <span id="gy-updated" class="muted">—</span>
  </div>
  <div class="gy-kpis">
    <div class="gy-kpi"><b id="gy-total">0</b><span>TASKS</span></div>
    <div class="gy-kpi"><b id="gy-done">0</b><span>DONE</span></div>
    <div class="gy-kpi"><b id="gy-run">0</b><span>RUN</span></div>
    <div class="gy-kpi"><b id="gy-wait">0</b><span>WAIT</span></div>
    <div class="gy-kpi"><b id="gy-fail">0</b><span>FAIL</span></div>
    <div class="gy-kpi"><b id="gy-today">0/0</b><span>TODAY</span></div>
  </div>
  <div class="gy-bars">
    <div><div class="label">TOTAL <span id="gy-pct">0%</span></div><div class="progress-bar"><div id="gy-fill" class="progress-fill" style="width:0%"></div></div></div>
    <div><div class="label">QUOTA <span id="gy-quota-pct">0%</span></div><div class="progress-bar"><div id="gy-quota-fill" class="progress-fill" style="width:0%"></div></div></div>
  </div>
  <div class="gy-table-wrap">
    <table class="gy-table">
      <thead><tr><th>DIR</th><th>ALL</th><th>DONE</th><th>RUN</th><th>WAIT</th><th>FAIL</th><th>SP</th><th>%</th></tr></thead>
      <tbody id="gy-rows"><tr><td colspan="8" class="muted">—</td></tr></tbody>
    </table>
  </div>
</section>

<!-- Live Progress -->
<div class="live-grid">
  <div class="live-shell">
    <div class="live-hero">
      <div class="workflow-stage-copy">
        <div>
          <div class="workflow-kicker">Live Workflow</div>
          <div class="live-stage-title" style="margin:6px 0 8px;">
            <span id="live-running-badge" class="status {% if live.running %}ok{% else %}warn{% endif %}">{% if live.running %}RUNNING{% else %}IDLE{% endif %}</span>
            <span id="live-stage-label" class="metric small">{{ live.stage_label }}</span>
          </div>
        </div>
        <div class="live-stage-badge">更新时间 <span id="live-updated-at">{{ live.updated_at }}</span></div>
      </div>

      <div class="live-workflow-board">
        <div class="live-workflow-svg-wrap">
          <svg id="live-workflow-svg" class="live-workflow-svg" viewBox="0 0 960 240" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="workflowGlow" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stop-color="#14324d"/><stop offset="45%" stop-color="#3399ff"/><stop offset="100%" stop-color="#63d1ff"/>
              </linearGradient>
              <linearGradient id="workflowBase" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stop-color="#1b2330"/><stop offset="100%" stop-color="#101722"/>
              </linearGradient>
            </defs>
            <path id="workflow-path-main" d="M100 80 C 180 80, 200 80, 280 80 S 380 80, 460 80 S 560 80, 640 80 S 740 80, 820 80" fill="none" stroke="url(#workflowBase)" stroke-width="16" stroke-linecap="round"/>
            <path d="M100 80 C 180 80, 200 80, 280 80 S 380 80, 460 80 S 560 80, 640 80 S 740 80, 820 80" fill="none" stroke="url(#workflowGlow)" stroke-width="4" stroke-linecap="round" stroke-dasharray="8 12" opacity="0.7">
              <animate attributeName="stroke-dashoffset" from="0" to="-40" dur="2.2s" repeatCount="indefinite"/>
            </path>
            <path d="M640 80 C 700 80, 710 130, 760 150 S 820 170, 860 170" fill="none" stroke="#ffb224" stroke-width="3" stroke-dasharray="6 10" opacity="0.5">
              <animate attributeName="stroke-dashoffset" from="0" to="24" dur="1.6s" repeatCount="indefinite"/>
            </path>
            <circle class="workflow-ship-dot" r="7">
              <animateMotion dur="3.2s" repeatCount="indefinite" rotate="auto">
                <mpath href="#workflow-path-main" />
              </animateMotion>
            </circle>
            <circle class="workflow-ship-dot" r="4.5" opacity="0.75">
              <animateMotion dur="3.2s" begin="1.0s" repeatCount="indefinite" rotate="auto">
                <mpath href="#workflow-path-main" />
              </animateMotion>
            </circle>
            <circle class="workflow-ship-dot warn" r="5.5" opacity="0.8">
              <animateMotion dur="2.4s" begin="0.8s" repeatCount="indefinite" rotate="auto">
                <mpath href="#workflow-path-main" />
              </animateMotion>
            </circle>
            <g fill="#8b929d" font-size="13" font-weight="700">
              <text x="72" y="46">Bangumi</text>
              <text x="245" y="46">Mikan</text>
              <text x="432" y="46">qB</text>
              <text x="586" y="46">Staging</text>
              <text x="783" y="46">光鸭云盘</text>
              <text x="820" y="194" fill="#ffb224">Cleanup</text>
            </g>
            <g>
              <circle cx="100" cy="80" r="11" fill="{% if live.stage in ['sync','import','prune','upload','cleanup','done'] %}#3ddc84{% else %}#4b5563{% endif %}"/>
              <circle cx="280" cy="80" r="11" fill="{% if live.stage in ['sync','import','prune','upload','cleanup','done'] %}{% if live.stage == 'sync' %}#3399ff{% else %}#3ddc84{% endif %}{% else %}#4b5563{% endif %}"/>
              <circle cx="460" cy="80" r="11" fill="{% if live.stage in ['sync','import','prune','upload','cleanup','done'] %}{% if live.stage == 'sync' %}#3399ff{% else %}#3ddc84{% endif %}{% else %}#4b5563{% endif %}"/>
              <circle cx="640" cy="80" r="11" fill="{% if live.stage in ['import','prune','upload','cleanup','done'] %}{% if live.stage == 'import' %}#3399ff{% else %}#3ddc84{% endif %}{% else %}#4b5563{% endif %}"/>
              <circle cx="820" cy="80" r="11" fill="{% if live.stage in ['upload','cleanup','done'] %}{% if live.stage == 'upload' %}#3399ff{% else %}#3ddc84{% endif %}{% else %}#4b5563{% endif %}"/>
              <circle cx="860" cy="170" r="11" fill="{% if live.stage in ['cleanup','done'] %}{% if live.stage == 'cleanup' %}#3399ff{% else %}#3ddc84{% endif %}{% else %}#4b5563{% endif %}"/>
            </g>
          </svg>
        </div>

        <div class="workflow-stage-row" id="workflow-stage-row">
          <div id="stage-sync" data-stage="sync" class="workflow-stage-card {% if live.stage == 'sync' %}active{% elif live.stage in ['import','prune','upload','cleanup','done'] %}done{% else %}pending{% endif %}">
            <span class="workflow-stage-dot"></span>
            <div class="workflow-stage-name">01 入队</div>
          </div>
          <div id="stage-import" data-stage="import" class="workflow-stage-card {% if live.stage == 'import' %}active{% elif live.stage in ['prune','upload','cleanup','done'] %}done{% else %}pending{% endif %}">
            <span class="workflow-stage-dot"></span>
            <div class="workflow-stage-name">02 整理</div>
          </div>
          <div id="stage-prune" data-stage="prune" class="workflow-stage-card {% if live.stage == 'prune' %}active{% elif live.stage in ['upload','cleanup','done'] %}done{% else %}pending{% endif %}">
            <span class="workflow-stage-dot"></span>
            <div class="workflow-stage-name">03 清理</div>
          </div>
          <div id="stage-upload" data-stage="upload" class="workflow-stage-card {% if live.stage == 'upload' %}active{% elif live.stage in ['cleanup','done'] %}done{% else %}pending{% endif %}">
            <span class="workflow-stage-dot"></span>
            <div class="workflow-stage-name">04 上传</div>
          </div>
          <div id="stage-cleanup" data-stage="cleanup" class="workflow-stage-card {% if live.stage == 'cleanup' %}active{% elif live.stage == 'done' %}done{% else %}pending{% endif %}">
            <span class="workflow-stage-dot"></span>
            <div class="workflow-stage-name">05 回收</div>
          </div>
        </div>
      </div>

      <div class="live-subject">当前条目：<span id="live-latest-subject">{{ live.latest_subject or '—' }}</span></div>
      <div id="live-latest-message" class="live-message">{{ live.latest_message or '暂无实时消息' }}</div>
      <div class="live-metrics">
        <div class="live-metric">
          <div id="live-processed" class="live-metric-value">{{ live.processed_subjects }}</div>
          <div class="label">已处理条目</div>
        </div>
        <div class="live-metric">
          <div id="live-total" class="live-metric-value">{{ live.tracked_total }}</div>
          <div class="label">本轮总条目</div>
        </div>
        <div class="live-metric">
          <div id="live-mapped" class="live-metric-value">{{ live.mapped_count }}</div>
          <div class="label">已匹配 Mikan</div>
        </div>
        <div class="live-metric">
          <div id="live-added" class="live-metric-value">{{ live.added_count }}</div>
          <div class="label">已入队 qB</div>
        </div>
      </div>
    </div>

    <div class="live-sidebar">
      <div class="card">
        <h2>活跃进程</h2>
        <ul id="live-process-list" class="process-list">
          {% for proc in live.active_processes %}
          <li><strong>{{ proc.label }}</strong> · PID {{ proc.pid }}</li>
          {% else %}
          <li>当前没有运行中的流程</li>
          {% endfor %}
        </ul>
      </div>
      <div class="live-mini-grid">
        <div class="live-mini-card">
          <div class="live-mini-label">阶段进度</div>
          <div id="live-stage-progress-text" class="live-mini-value">{{ live.stage_progress }}%</div>
          <div class="progress-bar"><div id="live-stage-progress-fill" class="progress-fill" style="width:{{ live.stage_progress }}%"></div></div>
        </div>
        <div class="live-mini-card">
          <div class="live-mini-label">本轮未匹配</div>
          <div id="live-unresolved" class="live-mini-value">{{ live.unresolved_count }}</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin-bottom:28px;">
  <div class="card">
    <h2>本轮事件流</h2>
    <div id="live-events" class="timeline">
      {% for ev in live.events %}
      <div class="timeline-item {{ ev.level }}">
        {% if ev.time %}<div class="timeline-time">{{ ev.time }}</div>{% endif %}
        <div class="timeline-msg">{{ ev.message }}</div>
      </div>
      {% else %}
      <div class="label">暂无事件</div>
      {% endfor %}
    </div>
  </div>
  <div class="card">
    <h2>本轮实时日志尾部</h2>
    <pre id="live-tail" class="log compact">{{ live.tail_lines|join('\\n') }}</pre>
  </div>
</div>

<!-- Global Overview -->
<div class="panel-section">
  <div class="panel-title">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
    全局概览
  </div>
  <div class="grid">
    <div class="card">
      <h2>Bangumi 跟踪</h2>
      <div class="metric">{{ tracked_count }}</div>
      <div class="label">
        <span class="badge wish">想看 {{ wish }}</span>
        <span class="badge doing">在看 {{ doing }}</span>
        <span class="badge collect">看过 {{ collect }}</span>
      </div>
    </div>
    <div class="card">
      <h2>Mikan 映射</h2>
      <div class="metric">{{ matrix.mapped_count if matrix else '—' }}</div>
      <div class="label">未映射: {{ matrix.unmapped_count if matrix else '—' }} | 映射率: {{ '%.1f'|format((matrix.mapped_count/matrix.total*100) if matrix and matrix.total else 0) }}%</div>
    </div>
    <div class="card">
      <h2>qBittorrent</h2>
      <div class="metric small">
        {% if qb.ok %}
          <span class="status ok"><span class="dot ok"></span> 在线</span>
        {% else %}
          <span class="status err"><span class="dot err"></span> 离线</span>
        {% endif %}
      </div>
      <div class="label">{{ qb.version or qb.error or "未知" }} | 种子数: {{ qb.torrent_count or 0 }}</div>
      {% if qb.ok and qb.global_stats %}
      <div class="label" style="margin-top:6px;">
        ↓ {{ format_speed(qb.global_stats.dl_speed) }} · ↑ {{ format_speed(qb.global_stats.up_speed) }}
      </div>
      {% endif %}
    </div>
    <div class="card">
      <h2>媒体库</h2>
      <div class="flex">
        <div>
          <div class="metric small">{{ lib.guangya_webdav.dirs }}</div>
          <div class="label">光鸭云盘目录</div>
        </div>
        <div>
          <div class="metric small">{{ lib.staging.dirs }}</div>
          <div class="label">暂存目录</div>
        </div>
        <div>
          <div class="metric small">{{ lib.downloads.dirs }}</div>
          <div class="label">下载目录</div>
        </div>
      </div>
      <div class="label" style="margin-top:10px;">挂载库大小: {{ lib_size }}</div>
    </div>
    <div class="card">
      <h2>光鸭云盘</h2>
      <div class="metric small">
        {% if guangya.ok %}
          <span class="status ok">已配置</span>
        {% else %}
          <span class="status err">未就绪</span>
        {% endif %}
      </div>
      <div class="label">文件数: {{ guangya.file_count or 0 }} | 总大小: {{ guangya.total_size or "未知" }}</div>
    </div>
    <div class="card">
      <h2>未匹配</h2>
      <div class="metric small">{{ unresolved_count }}</div>
      <div class="label">Mikan 未解析条目</div>
    </div>
    <div class="card">
      <h2>运行模式</h2>
      <div class="metric small">
        {% if dry_run %}
          <span class="status warn">DRY RUN</span>
        {% else %}
          <span class="status ok">LIVE</span>
        {% endif %}
      </div>
      <div class="label">上次运行: {{ last_run or "从未" }}</div>
    </div>
    <div class="card">
      <h2>整体健康</h2>
      <div class="flex" style="gap:8px;">
        <span class="status {{ 'ok' if pipeline.bangumi=='healthy' else 'warn' }}">BGM</span>
        <span class="status {{ 'ok' if pipeline.mikan=='healthy' else 'warn' }}">Mikan</span>
        <span class="status {{ 'ok' if pipeline.qbittorrent=='healthy' else ('err' if pipeline.qbittorrent=='error' else 'warn') }}">qB</span>
        <span class="status {{ 'ok' if pipeline.guangya=='healthy' else ('err' if pipeline.guangya=='error' else 'warn') }}">光鸭云盘</span>
      </div>
      <div class="label" style="margin-top:8px;">各模块状态一览</div>
    </div>
  </div>
</div>

<!-- qBittorrent Detail (async loaded) -->
<div class="panel-section">
  <div class="panel-title">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
    qBittorrent 详情
  </div>
  <div id="qb-detail-panel">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));">
      <div class="card"><div class="skeleton" style="height:60px;"></div></div>
      <div class="card"><div class="skeleton" style="height:60px;"></div></div>
      <div class="card"><div class="skeleton" style="height:60px;"></div></div>
    </div>
  </div>
</div>

<!-- Storage Flow (async loaded) -->
<div class="panel-section">
  <div class="panel-title">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
    存储流转
  </div>
  <div id="storage-flow-panel">
    <div class="card"><div class="skeleton" style="height:100px;"></div></div>
  </div>
</div>

<!-- Full Matrix (async loaded) -->
<div class="panel-section">
  <div class="panel-title">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
    全链路状态矩阵 <span class="muted">(每个番剧在整个流水线中的位置)</span>
  </div>
  <div id="matrix-panel">
    <div class="card"><div class="skeleton" style="height:200px;"></div></div>
  </div>
</div>

<!-- Issues (async loaded) -->
<div class="panel-section">
  <div class="panel-title">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    问题与异常
  </div>
  <div id="issues-panel">
    <div class="card"><div class="skeleton" style="height:100px;"></div></div>
  </div>
</div>

<!-- Downloading Torrents -->
{% if qb.ok and qb.downloading %}
<div class="card" style="margin-bottom:28px;">
  <h2>正在下载 ({{ qb.downloading|length }})</h2>
  <table>
    <tr><th>名称</th><th>进度</th><th>大小</th><th>下载速度</th><th>状态</th></tr>
    {% for t in qb.downloading %}
    <tr>
      <td style="max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{{ t.name }}">{{ t.name }}</td>
      <td style="min-width:120px;">
        <div style="display:flex; align-items:center; gap:8px;">
          <div class="progress-bar" style="flex:1;"><div class="progress-fill" style="width:{{ t.progress }}%"></div></div>
          <span style="font-size:0.78rem; color:var(--muted); min-width:36px;">{{ t.progress }}%</span>
        </div>
      </td>
      <td>{{ t.size }}</td>
      <td>{{ format_speed(t.dlspeed) }}</td>
      <td><span class="badge doing">{{ t.state }}</span></td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

<!-- Completed Torrents -->
{% if qb.ok and qb.completed %}
<div class="card" style="margin-bottom:28px;">
  <h2>最近完成 ({{ qb.completed|length }})</h2>
  <table>
    <tr><th>名称</th><th>大小</th><th>状态</th></tr>
    {% for t in qb.completed %}
    <tr>
      <td style="max-width:500px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="{{ t.name }}">{{ t.name }}</td>
      <td>{{ t.size }}</td>
      <td><span class="badge collect">{{ t.state }}</span></td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

{% if unresolved %}
<div class="card" style="margin-bottom:28px;">
  <h2>未匹配番剧列表</h2>
  <table>
    <tr><th>Subject ID</th><th>标题</th><th>收藏状态</th></tr>
    {% for u in unresolved %}
    <tr>
      <td><span class="muted">{{ u.subject_id }}</span></td>
      <td>{{ u.title }}</td>
      <td><span class="badge {{ u.collection }}">{{ u.collection }}</span></td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

</div>

<script>

async function pollGuangYa() {
  try {
    const r = await fetch('/api/guangya_progress');
    if (!r.ok) return;
    const d = await r.json();
    const set = (id,v)=>{ const el=document.getElementById(id); if(el) el.textContent=v; };
    set('gy-total', d.total||0); set('gy-done', d.completed||0); set('gy-run', d.running||0);
    set('gy-wait', d.submitted||0); set('gy-fail', d.failed||0); set('gy-today', `${d.today||0}/${d.daily_limit||0}`);
    set('gy-pct', `${d.percent||0}%`); set('gy-quota-pct', `${d.quota_percent||0}%`); set('gy-updated', d.updated_at||'—');
    const fill=document.getElementById('gy-fill'); if(fill) fill.style.width=`${Math.min(100,d.percent||0)}%`;
    const qfill=document.getElementById('gy-quota-fill'); if(qfill) qfill.style.width=`${Math.min(100,d.quota_percent||0)}%`;
    const rows=document.getElementById('gy-rows');
    if(rows) {
      const data=(d.rows||[]).slice(0,60);
      rows.innerHTML = data.length ? data.map(x=>`<tr><td class="gy-show" title="${x.target_dir||''}">${x.target_dir||'-'}</td><td>${x.total||0}</td><td>${x.completed||0}</td><td>${x.running||0}</td><td>${x.submitted||0}</td><td>${x.failed||0}</td><td>${x.specials||0}</td><td><div class="gy-mini-bar"><div class="gy-mini-fill" style="width:${Math.min(100,x.percent||0)}%"></div></div></td></tr>`).join('') : '<tr><td colspan="8" class="muted">—</td></tr>';
    }
  } catch(e) { console.error('pollGuangYa error', e); }
}

// ===== Live polling =====
async function pollLive() {
  try {
    const r = await fetch('/api/live_status');
    if(!r.ok) return;
    const d = await r.json();
    const badge = document.getElementById('live-running-badge');
    if(badge) { badge.className = 'status ' + (d.running ? 'ok' : 'warn'); badge.textContent = d.running ? 'RUNNING' : 'IDLE'; }
    const lbl = document.getElementById('live-stage-label');
    if(lbl) lbl.textContent = d.stage_label;
    const procList = document.getElementById('live-process-list');
    if(procList) {
      if(d.active_processes && d.active_processes.length) {
        procList.innerHTML = d.active_processes.map(p => `<li><strong>${p.label}</strong> · PID ${p.pid}</li>`).join('');
      } else { procList.innerHTML = '<li>当前没有运行中的流程</li>'; }
    }
    const stages = ['sync','import','prune','upload','cleanup'];
    stages.forEach(s => {
      const el = document.getElementById('stage-'+s);
      if(!el) return;
      el.classList.remove('active','done','pending');
      const idx = stages.indexOf(s);
      const activeIdx = stages.indexOf(d.stage);
      if(s === d.stage) el.classList.add('active');
      else if(activeIdx !== -1 && idx < activeIdx) el.classList.add('done');
      else el.classList.add('pending');
    });
    const subj = document.getElementById('live-latest-subject');
    if(subj) subj.textContent = d.latest_subject || '—';
    const msg = document.getElementById('live-latest-message');
    if(msg) msg.textContent = d.latest_message || '暂无实时消息';
    const upd = document.getElementById('live-updated-at');
    if(upd) upd.textContent = d.updated_at;
    const ptext = document.getElementById('live-stage-progress-text');
    if(ptext) ptext.textContent = d.stage_progress + '%';
    const pfill = document.getElementById('live-stage-progress-fill');
    if(pfill) pfill.style.width = d.stage_progress + '%';
    [['live-processed',d.processed_subjects],['live-total',d.tracked_total],['live-mapped',d.mapped_count],['live-added',d.added_count],['live-unresolved',d.unresolved_count]].forEach(([id,val])=>{
      const el=document.getElementById(id); if(el&&val!==undefined) el.textContent=val;
    });
    const evWrap = document.getElementById('live-events');
    if(evWrap && d.events) {
      evWrap.innerHTML = d.events.map(ev => `<div class="timeline-item ${ev.level}">${ev.time?`<div class="timeline-time">${ev.time}</div>`:''}<div class="timeline-msg">${ev.message}</div></div>`).join('');
    }
    const tail = document.getElementById('live-tail');
    if(tail && d.tail_lines) tail.textContent = d.tail_lines.join('\\n');
  } catch(e) {}
}
pollGuangYa();
setInterval(pollGuangYa, 10000);
setInterval(pollLive, 3000);

// ===== Full Status loading =====
let matrixData = [];
let matrixFilter = '';
let matrixFilterOverall = 'all';
let matrixPage = 1;
const matrixPageSize = 50;

function formatSize(b) {
  if (!b) return '0 B';
  const units = ['B','KB','MB','GB','TB'];
  for (const u of units) { if (Math.abs(b) < 1024) return b.toFixed(2) + ' ' + u; b /= 1024; }
  return b.toFixed(2) + ' PB';
}

function renderQBDetail(data) {
  const panel = document.getElementById('qb-detail-panel');
  if (!panel) return;
  if (!data.ok) {
    panel.innerHTML = `<div class="alert-banner">❌ qBittorrent 获取失败: ${data.error||'unknown'}</div>`;
    return;
  }
  let html = '';
  if (data.bottleneck) {
    html += `<div class="alert-banner">&#x26A0; 队列瓶颈警告：有 ${data.queued_downloads} 个种子排队中，超过了活跃下载数上限的两倍以上，建议调高 qBittorrent 的「最大活跃下载数」或关闭队列管理</div>`;
  }
  const states = data.states || {};
  const total = data.torrent_count || 0;
  const stateColors = {
    'downloading':'#3ddc84','stalledDL':'#66b3ff','queuedDL':'#fb923c','metaDL':'#a78bfa',
    'forcedDL':'#3ddc84','uploading':'#3ddc84','stalledUP':'#66b3ff','queuedUP':'#fb923c',
    'checkingUP':'#a78bfa','forcedUP':'#3ddc84','pausedDL':'#8b929d','pausedUP':'#8b929d',
    'checkingDL':'#a78bfa','moving':'#3399ff','missingFiles':'#ff4d4f','error':'#ff4d4f','unknown':'#8b929d'
  };
  html += `<div class="card" style="margin-bottom:18px;"><h2>种子状态分布 (共 ${total} 个)</h2>`;
  html += `<div class="bar-chart">`;
  for (const [k, v] of Object.entries(states)) {
    const pct = total ? (v.count / total * 100) : 0;
    const h = Math.max(4, pct * 1.2);
    html += `<div class="bar-item" title="${v.label}: ${v.count}"><div class="bar-value" style="color:${stateColors[k]||'#8b929d'}">${v.count}</div><div class="bar-fill" style="height:${h}px; background:${stateColors[k]||'#8b929d'}"></div><div class="bar-label">${v.label}</div></div>`;
  }
  html += `</div></div>`;

  html += `<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-bottom:18px;">`;
  html += `<div class="card"><div class="metric small" style="color:var(--ok)">${data.active_downloads}</div><div class="label">活跃下载中</div></div>`;
  html += `<div class="card"><div class="metric small" style="color:var(--warn)">${data.queued_downloads}</div><div class="label">排队下载</div></div>`;
  html += `<div class="card"><div class="metric small" style="color:var(--accent)">${data.seeding}</div><div class="label">做种中</div></div>`;
  html += `<div class="card"><div class="metric small">${formatSize(data.global_stats.dl_speed)}/s</div><div class="label">全局下载速度</div></div>`;
  html += `<div class="card"><div class="metric small">${formatSize(data.global_stats.up_speed)}/s</div><div class="label">全局上传速度</div></div>`;
  html += `<div class="card"><div class="metric small">${formatSize(data.global_stats.free_space)}</div><div class="label">磁盘剩余空间</div></div>`;
  html += `</div>`;

  html += `<div class="card" style="margin-bottom:18px;"><h2>队列设置</h2>`;
  html += `<div class="flex"><div><strong>队列管理:</strong> ${data.queue_settings.queueing_enabled ? '已启用' : '已关闭'}</div>`;
  html += `<div><strong>最大活跃下载数:</strong> ${data.queue_settings.max_active_downloads}</div>`;
  html += `<div><strong>最大活跃种子数:</strong> ${data.queue_settings.max_active_torrents}</div></div></div>`;

  if (data.top_queued && data.top_queued.length) {
    html += `<div class="card" style="margin-bottom:18px;"><h2>排队中的种子 (前 ${data.top_queued.length} 个)</h2>`;
    html += `<table><tr><th>名称</th><th>大小</th><th>保存路径</th></tr>`;
    for (const t of data.top_queued) {
      html += `<tr><td style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${t.name}">${t.name}</td><td>${t.size}</td><td class="muted">${t.save_path}</td></tr>`;
    }
    html += `</table></div>`;
  }
  panel.innerHTML = html;
}

function renderStorageFlow(data) {
  const panel = document.getElementById('storage-flow-panel');
  if (!panel || !data.matrix) return;
  const s = data.matrix.stats || {};
  const total = data.matrix.total || 0;
  const mapped = data.matrix.mapped_count || 0;
  const hasQb = data.matrix.has_qb_count || 0;
  const hasLocal = data.matrix.has_local_count || 0;
  const hasGuangya = data.matrix.has_guangya_count || 0;
  const steps = [
    {label:'Bangumi跟踪', count:total, color:'#3399ff'},
    {label:'Mikan映射', count:mapped, color:'#66b3ff'},
    {label:'qB种子', count:hasQb, color:'#fb923c'},
    {label:'本地完成', count:hasLocal, color:'#3ddc84'},
    {label:'暂存', count:data.staging_count||0, color:'#a78bfa'},
    {label:'光鸭云盘', count:hasGuangya, color:'#c084fc'},
  ];
  let html = `<div class="flow-grid">`;
  steps.forEach((step, i) => {
    const pct = total ? Math.round(step.count / total * 100) : 0;
    html += `<div class="flow-card"><div style="color:${step.color}; font-size:0.75rem; font-weight:700;">${step.label}</div><div class="flow-count" style="color:${step.color}">${step.count}</div><div class="flow-delta muted">${pct}% 占比</div>${i < steps.length-1 ? '<div class="flow-arrow">&#x2192;</div>' : ''}</div>`;
  });
  html += `</div>`;
  panel.innerHTML = html;
}

function renderMatrix(data) {
  const panel = document.getElementById('matrix-panel');
  if (!panel || !data.matrix) return;
  matrixData = data.matrix.rows || [];
  let html = `<div class="matrix-toolbar">`;
  html += `<input type="text" id="matrix-search" placeholder="搜索番剧标题..." oninput="onMatrixSearch(this.value)">`;
  html += `<select id="matrix-filter" onchange="onMatrixFilter(this.value)"><option value="all">全部状态</option><option value="cloud">已在云端</option><option value="staging">暂存待上传</option><option value="local">本地已完成</option><option value="qb">qBittorrent</option><option value="mapped">已映射未下载</option><option value="unmapped">未映射</option></select>`;
  html += `<span class="muted" style="padding:8px 0;">共 ${matrixData.length} 条目</span></div>`;
  html += `<div class="matrix-table-wrap"><table class="matrix-table"><thead><tr><th>番剧</th><th>Bangumi</th><th>Mikan</th><th>qB</th><th>本地完成</th><th>暂存</th><th>光鸭云盘</th><th>整体状态</th></tr></thead><tbody id="matrix-tbody"></tbody></table></div>`;
  html += `<div class="pagination" id="matrix-pagination"></div>`;
  panel.innerHTML = html;
  renderMatrixPage();
}

function renderMatrixPage() {
  const tbody = document.getElementById('matrix-tbody');
  const pag = document.getElementById('matrix-pagination');
  if (!tbody) return;
  let rows = matrixData;
  if (matrixFilter) { const f = matrixFilter.toLowerCase(); rows = rows.filter(r => (r.title||'').toLowerCase().includes(f)); }
  if (matrixFilterOverall && matrixFilterOverall !== 'all') { rows = rows.filter(r => r.overall === matrixFilterOverall); }
  const totalPages = Math.max(1, Math.ceil(rows.length / matrixPageSize));
  if (matrixPage > totalPages) matrixPage = totalPages;
  const start = (matrixPage - 1) * matrixPageSize;
  const pageRows = rows.slice(start, start + matrixPageSize);
  const overallClass = {cloud:'cloud', staging:'staging', local:'local', qb:'qb', mapped:'mapped', unmapped:'unmapped'};
  const collClass = {wish:'wish', collect:'collect', doing:'doing'};
  tbody.innerHTML = pageRows.map(r => {
    const qbBadge = r.qb_count > 0 ? `<span class="badge qb">${r.qb_count}种子</span>` : '<span class="muted">—</span>';
    const localBadge = r.local_files > 0 ? `<span class="badge local">${r.local_files}文件 ${formatSize(r.local_size)}</span>` : '<span class="muted">—</span>';
    const stBadge = r.staging_files > 0 ? `<span class="badge staging">${r.staging_files}文件</span>` : '<span class="muted">—</span>';
    const gdBadge = r.guangya_files > 0 ? `<span class="badge cloud">${r.guangya_files}文件</span>` : '<span class="muted">—</span>';
    return `<tr><td class="td-title" title="${r.title}">${r.title}</td><td><span class="badge ${collClass[r.collection_label]||''}">${r.collection_label||'?'}</span></td><td>${r.mikan_mapped ? '<span class="badge collect">已映射</span>' : '<span class="badge unmapped">未映射</span>'}</td><td>${qbBadge}</td><td>${localBadge}</td><td>${stBadge}</td><td>${gdBadge}</td><td><span class="badge ${overallClass[r.overall]||''}">${r.overall_label}</span></td></tr>`;
  }).join('');
  if (pag) {
    let phtml = `<button ${matrixPage<=1?'disabled':''} onclick="setMatrixPage(${matrixPage-1})">← 上一页</button>`;
    phtml += `<span class="page-info">第 ${matrixPage} / ${totalPages} 页 (${rows.length} 条)</span>`;
    phtml += `<button ${matrixPage>=totalPages?'disabled':''} onclick="setMatrixPage(${matrixPage+1})">下一页 →</button>`;
    pag.innerHTML = phtml;
  }
}
function onMatrixSearch(v) { matrixFilter = v; matrixPage = 1; renderMatrixPage(); }
function onMatrixFilter(v) { matrixFilterOverall = v; matrixPage = 1; renderMatrixPage(); }
function setMatrixPage(p) { matrixPage = p; renderMatrixPage(); }

function renderIssues(data) {
  const panel = document.getElementById('issues-panel');
  if (!panel || !data.matrix) return;
  const rows = data.matrix.rows || [];
  const unmapped = rows.filter(r => !r.mikan_mapped);
  const undownloaded = rows.filter(r => r.mikan_mapped && r.qb_count === 0 && r.guangya_files === 0);
  let html = `<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(380px,1fr));">`;

  html += `<div class="card"><h2>未映射到 Mikan (${unmapped.length})</h2>`;
  if (unmapped.length === 0) html += `<div class="label">所有跟踪的番剧都已映射！</div>`;
  else {
    html += `<div style="max-height:300px; overflow-y:auto;"><table class="matrix-table"><tr><th>番剧</th><th>收藏状态</th></tr>`;
    html += unmapped.slice(0, 50).map(r => `<tr><td class="td-title" title="${r.title}">${r.title}</td><td><span class="badge ${r.collection_label}">${r.collection_label}</span></td></tr>`).join('');
    if (unmapped.length > 50) html += `<tr><td colspan="2" class="muted">还有 ${unmapped.length - 50} 个未显示...</td></tr>`;
    html += `</table></div>`;
  }
  html += `</div>`;

  html += `<div class="card"><h2>已映射但未下载 (${undownloaded.length})</h2>`;
  if (undownloaded.length === 0) html += `<div class="label">所有已映射的番剧都已有种子或已在云端</div>`;
  else {
    html += `<div style="max-height:300px; overflow-y:auto;"><table class="matrix-table"><tr><th>番剧</th><th>收藏状态</th></tr>`;
    html += undownloaded.slice(0, 50).map(r => `<tr><td class="td-title" title="${r.title}">${r.title}</td><td><span class="badge ${r.collection_label}">${r.collection_label}</span></td></tr>`).join('');
    if (undownloaded.length > 50) html += `<tr><td colspan="2" class="muted">还有 ${undownloaded.length - 50} 个未显示...</td></tr>`;
    html += `</table></div>`;
  }
  html += `</div>`;

  if (data.qb && data.qb.bottleneck) {
    html += `<div class="card"><h2>qBittorrent 队列瓶颈</h2>`;
    html += `<div class="alert-banner">有 ${data.qb.queued_downloads} 个种子排队中，而最大活跃下载数只设为 ${data.qb.queue_settings.max_active_downloads}。</div></div>`;
  }
  html += `</div>`;
  panel.innerHTML = html;
}

async function loadFullStatus() {
  try {
    const r = await fetch('/api/full_status');
    if (!r.ok) return;
    const d = await r.json();
    renderQBDetail(d.qb || {});
    renderStorageFlow(d);
    renderMatrix(d);
    renderIssues(d);
  } catch(e) { console.error('loadFullStatus error', e); }
}
loadFullStatus();
setInterval(loadFullStatus, 15000);
</script>
</body>
</html>
"""


# ========== Routes ==========
@app.route("/")
def index():
    catalog = get_catalog()
    config = get_config()
    state = get_state()
    lib = get_library_stats()
    guangya = get_guangya_stats()
    qb = get_qb_data()
    pipeline = get_pipeline_status()
    live = get_live_pipeline_status()

    subjects = catalog.get("subjects", {})
    wish = sum(1 for s in subjects.values() if s.get("collection_type") == 1)
    doing = sum(1 for s in subjects.values() if s.get("collection_type") == 3)
    collect = sum(1 for s in subjects.values() if s.get("collection_type") == 2)
    tracked_count = catalog.get("tracked_count", len(subjects))
    unresolved = [
        {"subject_id": sid, "title": s.get("title", s.get("name_cn", s.get("name", ""))), "collection": {1:"wish",2:"collect",3:"doing"}.get(s.get("collection_type",0),"unknown")}
        for sid, s in subjects.items()
        if sid not in state.get("subject_to_mikan", {})
    ]
    unresolved_count = len(unresolved)

    matrix = None
    try:
        matrix = get_show_matrix()
    except Exception:
        pass

    last_run = format_dt(state.get("last_run", "") or catalog.get("fetched_at", ""))
    lib_size = "未知"
    try:
        lib_size = format_size(lib["guangya_webdav"]["size_bytes"])
    except Exception:
        pass

    return render_template_string(
        HTML_TEMPLATE,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        catalog=catalog,
        config=config,
        state=state,
        lib=lib,
        guangya=guangya,
        qb=qb,
        pipeline=pipeline,
        live=live,
        wish=wish,
        doing=doing,
        collect=collect,
        tracked_count=tracked_count,
        unresolved=unresolved,
        unresolved_count=unresolved_count,
        last_run=last_run,
        dry_run=config.get("dry_run", True),
        lib_size=lib_size,
        format_size=format_size,
        format_speed=format_speed,
        matrix=matrix,
    )


@app.route("/api/status")
def api_status():
    catalog = get_catalog()
    state = get_state()
    config = get_config()
    lib = get_library_stats()
    guangya = get_guangya_stats()
    qb = get_qb_data()
    pipeline = get_pipeline_status()
    live = get_live_pipeline_status()
    return jsonify(
        {
            "status": "ok",
            "catalog": {
                "fetched_at": state.get("last_run") or catalog.get("fetched_at"),
                "tracked_count": catalog.get("tracked_count", 0),
            },
            "state": {
                "last_sync": state.get("last_sync"),
                "mapped": len(state.get("subject_to_mikan", {})),
            },
            "qbittorrent": {
                "online": qb.get("ok", False),
                "version": qb.get("version", ""),
                "downloading": len(qb.get("downloading", [])),
                "completed": len(qb.get("completed", [])),
                "global_stats": qb.get("global_stats", {}),
            },
            "library": lib,
            "guangya": guangya,
            "pipeline": pipeline,
            "live": live,
            "config": {"dry_run": config.get("dry_run", True)},
        }
    )


@app.route("/api/live_status")
def api_live_status():
    return jsonify(get_live_pipeline_status())


@app.route("/api/guangya_progress")
def api_guangya_progress():
    return jsonify(get_guangya_download_progress())


@app.route("/api/full_status")
def api_full_status():
    return jsonify({
        "qb": get_qb_full_data(),
        "matrix": get_show_matrix(),
        "staging_count": len(scan_anime_dirs("/data/library/Anime")),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
