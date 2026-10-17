#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from typing import Dict, List, Optional, Tuple


COLLECTION_TYPE_LABELS = {
    1: "wish",
    2: "collect",
    3: "doing",
    4: "on_hold",
    5: "dropped",
}


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def log(*parts):
    print(f"[{now()}]", *parts, flush=True)


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def normalize_title(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def normalize_mikan_title(text: str) -> str:
    """Normalize punctuation and width while preserving kana and all scripts."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _explicit_season_marker(text: str) -> Optional[int]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    chinese_digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }

    def parse_number(value: str) -> Optional[int]:
        if value.isdigit():
            number = int(value)
        elif all(char in chinese_digits or char == "十" for char in value):
            if value.count("十") > 1:
                return None
            if "十" not in value:
                # Accept common written ordinals (二) and digit-by-digit forms (〇二).
                number = int("".join(str(chinese_digits[char]) for char in value))
            else:
                tens_text, ones_text = value.split("十")
                if (len(tens_text) > 1 or len(ones_text) > 1
                        or (tens_text and tens_text not in chinese_digits)
                        or (ones_text and ones_text not in chinese_digits)):
                    return None
                tens = chinese_digits[tens_text] if tens_text else 1
                ones = chinese_digits[ones_text] if ones_text else 0
                number = tens * 10 + ones
        else:
            return None
        return number if 1 <= number <= 99 else None

    chinese_or_arabic_number = r"(?:\d{1,2}|[零〇一二三四五六七八九十]{1,3})"
    patterns = (
        rf"第\s*({chinese_or_arabic_number})\s*(?:季|期|部|クール)",
        r"\bseason\s*(\d{1,2})\b",
        r"\bs(\d{1,2})\b",
        r"\b(\d{1,2})(?:st|nd|rd|th)\s*season\b",
        r"\bpart\s*(\d{1,2})\b",
        r"\bcour\s*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.I)
        if match:
            parsed = parse_number(match.group(1))
            if parsed is not None:
                return parsed
    return None


def _explicit_format(text: str) -> Optional[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    if re.search(r"剧场版|劇場版|movie|film|総集編|総集篇|总集篇|总集编", normalized):
        return "movie"
    if re.search(r"\bova\b|\boad\b|\bona\b", normalized):
        if re.search(r"\bova\b", normalized):
            return "ova"
        if re.search(r"\boad\b", normalized):
            return "oad"
        return "ona"
    if re.search(r"\bweb(?:アニメ|anime|series)?\b", normalized):
        return "web"
    if re.search(r"\btv(?:アニメ|版|series)?\b", normalized):
        return "tv"
    return None


def _subject_format(subject: dict) -> Optional[str]:
    value = unicodedata.normalize("NFKC", str(subject.get("platform") or "")).casefold()
    if not value:
        return None
    if any(token in value for token in ("剧场", "劇場", "movie", "film", "映画")):
        return "movie"
    if "ova" in value:
        return "ova"
    if "oad" in value:
        return "oad"
    if "ona" in value:
        return "ona"
    if "web" in value:
        return "web"
    if "tv" in value or "テレビ" in value:
        return "tv"
    return None


def _is_tv_subject(subject: dict) -> bool:
    fmt = _subject_format(subject)
    if fmt == "tv":
        return True
    episodes = subject.get("eps") or subject.get("total_episodes") or 0
    try:
        return int(episodes) > 1
    except (TypeError, ValueError):
        return False


def validate_cached_mikan_mapping(subject_row: dict, bangumi_id: str,
                                  title: str) -> Optional[dict]:
    """Revalidate persisted mappings against the current exact subject title."""
    if not bangumi_id or not title:
        return None
    return MikanClient({}).choose_best_bangumi(
        subject_row, [{"bangumi_id": str(bangumi_id), "title": str(title)}],
    )


def sanitize_path_component(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:120] or 'unknown'


def subject_savepath(base_savepath: str, show_title: str, subject_id: str) -> str:
    folder = f"{sanitize_path_component(show_title)} [bgm-{subject_id}]"
    return f"{base_savepath.rstrip('/')}/{folder}"


def request(url: str, *, method: str = "GET", headers: Optional[dict] = None, data: Optional[bytes] = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(0.4 * (attempt + 1))
    raise last_error


class IncompleteCollectionError(RuntimeError):
    """Bangumi collection pagination was malformed or stopped before total."""


class BangumiClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.username = cfg["username"]
        self.access_token = cfg.get("access_token", "")
        self.user_agent = cfg.get("user_agent", "anime-sync/0.1")

    def _headers(self) -> dict:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get_anime_collections(self, collection_type: int) -> List[dict]:
        items = []
        offset = 0
        limit = 30
        expected_total = None
        seen_subject_ids = set()
        while True:
            qs = urllib.parse.urlencode({
                "subject_type": 2,
                "type": collection_type,
                "limit": limit,
                "offset": offset,
            })
            url = f"{self.base_url}/v0/users/{urllib.parse.quote(self.username)}/collections?{qs}"
            try:
                payload = json.loads(request(url, headers=self._headers()).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IncompleteCollectionError("Bangumi returned invalid collection JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise IncompleteCollectionError("Bangumi collection response has no data list")
            if not isinstance(payload.get("total"), int) or payload["total"] < 0:
                raise IncompleteCollectionError("Bangumi collection response has no valid total")
            if expected_total is None:
                expected_total = payload["total"]
            elif payload["total"] != expected_total:
                raise IncompleteCollectionError("Bangumi collection total changed during pagination")
            chunk = payload["data"]
            if offset < expected_total and not chunk:
                raise IncompleteCollectionError("Bangumi collection ended before the reported total")
            if len(chunk) > limit:
                raise IncompleteCollectionError("Bangumi returned more rows than requested")
            for row in chunk:
                if not isinstance(row, dict):
                    raise IncompleteCollectionError("Bangumi collection contains a malformed row")
                subject = row.get("subject") if isinstance(row.get("subject"), dict) else {}
                subject_id = str(row.get("subject_id") or subject.get("id") or "")
                if not subject_id:
                    raise IncompleteCollectionError("Bangumi collection row has no subject id")
                if subject_id in seen_subject_ids:
                    raise IncompleteCollectionError("Bangumi collection pagination repeated a subject")
                seen_subject_ids.add(subject_id)
                row["collection_type"] = int(row.get("type") or collection_type)
            items.extend(chunk)
            if len(items) == expected_total:
                break
            if len(items) > expected_total:
                raise IncompleteCollectionError("Bangumi collection exceeded the reported total")
            offset += len(chunk)
            if offset <= 0:
                raise IncompleteCollectionError("Bangumi collection offset did not advance")
            time.sleep(0.3)
        return items

    def get_combined_anime_collections(self, collection_types: List[int]) -> List[dict]:
        merged = []
        seen = {}
        for collection_type in collection_types:
            for row in self.get_anime_collections(collection_type):
                subject = row.get("subject", {})
                subject_id = str(row.get("subject_id") or subject.get("id") or "")
                if not subject_id:
                    raise IncompleteCollectionError("Bangumi collection row has no subject id")
                if subject_id in seen:
                    raise IncompleteCollectionError(
                        "Bangumi subject appeared in multiple collection types"
                    )
                seen[subject_id] = collection_type
                merged.append(row)
        return merged


class MikanClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg.get("base_url", "https://mikanani.me").rstrip("/")
        self.user_agent = cfg.get("user_agent", "anime-sync/0.1")
        self.max_candidates = int(cfg.get("max_candidates_per_show", 6))

    def _headers(self) -> dict:
        return {"User-Agent": self.user_agent}

    def search_bangumi(self, title_candidates: List[str]) -> List[dict]:
        seen = {}
        for title in [t for t in title_candidates if t]:
            url = f"{self.base_url}/Home/Search?searchstr={urllib.parse.quote(title)}"
            page_html = request(url, headers=self._headers(), timeout=40).decode("utf-8", "ignore")
            matches = re.findall(r'<a[^>]+href="/Home/Bangumi/(\d+)"[^>]*>(.*?)</a>', page_html, flags=re.I | re.S)
            for bangumi_id, raw_name in matches:
                name = html.unescape(re.sub(r"<[^>]+>", "", raw_name)).strip()
                if not name:
                    continue
                seen[bangumi_id] = {"bangumi_id": bangumi_id, "title": name}
        # Search all Bangumi aliases. A Chinese title query can return only
        # similarly named theatrical entries while the Japanese alias contains
        # the television series. Keep exact title hits ahead of fuzzy noise
        # before applying the configured candidate cap.
        aliases = {normalize_mikan_title(title) for title in title_candidates if title}
        candidates = list(seen.values())
        exact = [row for row in candidates
                 if normalize_mikan_title(row.get("title")) in aliases]
        other = [row for row in candidates if row not in exact]
        return exact + other[:max(0, self.max_candidates - len(exact))]

    def choose_best_bangumi(self, subject: dict, candidates: List[dict], *,
                            extra_exact_titles: Optional[List[str]] = None) -> Optional[dict]:
        if not candidates:
            return None
        subj = subject.get("subject", {})
        if not isinstance(subj, dict):
            return None
        titles = [subj.get(key) for key in ("name_cn", "name") if subj.get(key)]
        aliases = {normalize_mikan_title(title) for title in titles if title}
        aliases.update(
            normalize_mikan_title(title)
            for title in (extra_exact_titles or []) if title
        )
        if not aliases:
            return None
        subject_seasons = {
            season for season in (_explicit_season_marker(title) for title in titles)
            if season is not None
        }
        subject_format = _subject_format(subj)
        is_tv = _is_tv_subject(subj)
        matched = {}
        for cand in candidates:
            title = str(cand.get("title") or "")
            cand_norm = normalize_mikan_title(title)
            if not cand_norm or cand_norm not in aliases:
                continue

            candidate_season = _explicit_season_marker(title)
            if subject_seasons:
                # Ambiguous Bangumi aliases with conflicting season markers do
                # not prove which Mikan season is intended.
                if candidate_season is None or candidate_season not in subject_seasons:
                    continue
            elif candidate_season is not None:
                continue

            candidate_format = _explicit_format(title)
            if is_tv and candidate_format == "movie":
                continue
            if (subject_format and candidate_format
                    and subject_format != candidate_format):
                continue

            bangumi_id = str(cand.get("bangumi_id") or "")
            if not bangumi_id:
                continue
            matched[bangumi_id] = cand

        # Even exact aliases can map to duplicate entries. Require one unique
        # Mikan Bangumi ID after title, season, and format checks.
        if len(matched) != 1:
            return None
        best = next(iter(matched.values()))
        best["score"] = 1.0
        return best

    def fetch_rss_items(self, bangumi_id: str) -> List[dict]:
        url = f"{self.base_url}/RSS/Bangumi?bangumiId={urllib.parse.quote(str(bangumi_id))}"
        xml_bytes = request(url, headers=self._headers(), timeout=40)
        root = ET.fromstring(xml_bytes)
        items = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            guid = (item.findtext("guid") or title).strip()
            link = (item.findtext("link") or "").strip()
            enclosure = item.find("enclosure")
            torrent_url = enclosure.get("url", "").strip() if enclosure is not None else ""
            desc = (item.findtext("description") or "").strip()
            items.append({
                "guid": guid,
                "title": title,
                "page_url": link,
                "torrent_url": torrent_url,
                "description": desc,
            })
        return items


class QBittorrentClient:
    def __init__(self, cfg: dict):
        self.base_url = cfg["base_url"].rstrip("/")
        self.username = cfg["username"]
        self.password = cfg["password"]
        self.category = cfg.get("category", "anime")
        self.savepath = cfg.get("savepath", "/downloads/complete/anime")
        self.paused = bool(cfg.get("paused", False))
        tags = cfg.get("tags", [])
        self.tags = ",".join(tags) if isinstance(tags, list) else str(tags)
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _request(self, path: str, *, method: str = "GET", form: Optional[dict] = None) -> bytes:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"User-Agent": "anime-sync/0.1"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        with self.opener.open(req, timeout=30) as r:
            return r.read()

    def login(self) -> None:
        resp = self._request("/api/v2/auth/login", method="POST", form={"username": self.username, "password": self.password})
        if resp.decode("utf-8", "ignore").strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login failed: {resp!r}")

    def add_torrent_url(self, torrent_url: str, savepath: Optional[str] = None) -> None:
        form = {
            "urls": torrent_url,
            "category": self.category,
            "savepath": savepath or self.savepath,
            "paused": "true" if self.paused else "false",
            "tags": self.tags,
        }
        self._request("/api/v2/torrents/add", method="POST", form=form)

    def torrents(self) -> List[dict]:
        return json.loads(self._request("/api/v2/torrents/info?limit=2000").decode("utf-8"))


def parse_episode_number(title: str) -> Optional[int]:
    patterns = [
        r"\[(\d{1,3})\](?!.*\[\d{1,3}\])",
        r"\b(?:ep|episode|第)\s*0*(\d{1,3})\b",
        r"(?:-|_)\s*0*(\d{1,3})(?:\D|$)",
    ]
    for pat in patterns:
        m = re.search(pat, title, flags=re.I)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def extract_episode_span(title: str) -> Optional[Tuple[int, int]]:
    range_patterns = [
        r"\[(\d{1,3})\s*[-~—–]\s*(\d{1,3})\]",
        r"\b(\d{1,3})\s*[-~—–]\s*(\d{1,3})\b",
    ]
    for pat in range_patterns:
        m = re.search(pat, title, flags=re.I)
        if not m:
            continue
        try:
            start = int(m.group(1))
            end = int(m.group(2))
        except ValueError:
            return None
        if start > end:
            start, end = end, start
        return start, end
    ep = parse_episode_number(title)
    if ep is None:
        return None
    return ep, ep


def extract_part_index(*texts: str) -> int:
    patterns = [
        r"第\s*([0-9]{1,2})\s*部(?:分|篇)?",
        r"第\s*([0-9]{1,2})\s*クール",
        r"\bpart\s*([0-9]{1,2})\b",
        r"\bcour\s*([0-9]{1,2})\b",
    ]
    for text in texts:
        if not text:
            continue
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if not m:
                continue
            try:
                value = int(m.group(1))
            except ValueError:
                continue
            if value > 0:
                return value
    return 1


def partition_series_key(*texts: str) -> str:
    text = ' '.join(t for t in texts if t).strip().lower()
    if not text:
        return ''
    text = re.sub(r"第\s*[0-9]{1,2}\s*部(?:分|篇)?", '', text, flags=re.I)
    text = re.sub(r"第\s*[0-9]{1,2}\s*クール", '', text, flags=re.I)
    text = re.sub(r"\bpart\s*[0-9]{1,2}\b", '', text, flags=re.I)
    text = re.sub(r"\bcour\s*[0-9]{1,2}\b", '', text, flags=re.I)
    text = re.sub(r"\s+", ' ', text).strip(' -_')
    return text


def _subject_part_info(subject_row: dict) -> Tuple[int, int, str]:
    subject = subject_row.get("subject", {})
    eps = subject.get("eps") or subject.get("total_episodes")
    try:
        eps = int(eps or 0)
    except (TypeError, ValueError):
        eps = 0
    part_index = extract_part_index(
        subject.get("name_cn") or "",
        subject.get("name") or "",
    )
    key = partition_series_key(subject.get("name_cn") or "", subject.get("name") or "")
    return eps, part_index, key


def allowed_episode_spans(subject_row: dict, related_subject_rows: Optional[List[dict]] = None) -> List[Tuple[int, int]]:
    eps, part_index, series_key = _subject_part_info(subject_row)
    if eps <= 0:
        return []

    spans = [(1, eps)]
    if part_index <= 1:
        return spans

    offset = 0
    if related_subject_rows:
        related_parts = []
        for row in related_subject_rows:
            row_eps, row_part_index, row_key = _subject_part_info(row)
            if row_eps <= 0 or row_key != series_key:
                continue
            related_parts.append((row_part_index, row_eps))
        if related_parts:
            by_part = {idx: row_eps for idx, row_eps in related_parts}
            offset = sum(by_part[idx] for idx in sorted(by_part) if idx < part_index)
    if offset <= 0:
        offset = (part_index - 1) * eps

    spans.append((offset + 1, offset + eps))
    return spans


def item_matches_subject_partition(subject_row: dict, title: str, related_subject_rows: Optional[List[dict]] = None) -> bool:
    spans = allowed_episode_spans(subject_row, related_subject_rows=related_subject_rows)
    if not spans:
        return True
    episode_span = extract_episode_span(title)
    if episode_span is None:
        return True
    start, end = episode_span
    for allowed_start, allowed_end in spans:
        if start >= allowed_start and end <= allowed_end:
            return True
    return False


def extract_episode_key(title: str) -> str:
    ep = parse_episode_number(title)
    if ep is not None:
        return f"ep-{ep:03d}"
    if any(x in title.lower() for x in ["合集", "全集", "batch", "complete", "box", "bdrip"]):
        return f"pack-{normalize_title(title)[:32]}"
    return f"misc-{normalize_title(title)[:40]}"


def extract_subgroup(title: str, filters: dict) -> Optional[str]:
    text = title.lower()
    for grp in filters.get("preferred_subgroups", []):
        if grp.lower() in text:
            return grp
    return None


def score_item(title: str, filters: dict, locked_subgroup: Optional[str] = None) -> Tuple[bool, int, List[str]]:
    reasons = []
    text = title.lower()
    score = 0

    for bad in filters.get("exclude_keywords", []):
        if bad.lower() in text:
            reasons.append(f"excluded:{bad}")
            return False, -999, reasons

    require_any = filters.get("require_keywords_any", [])
    if require_any:
        if any(k.lower() in text for k in require_any):
            score += 30
            reasons.append("matched_simplified")
        else:
            reasons.append("missing_required_keywords")
            return False, -999, reasons

    matched_subgroup = extract_subgroup(title, filters)
    if locked_subgroup:
        if matched_subgroup != locked_subgroup:
            reasons.append(f"subgroup_mismatch:{locked_subgroup}")
            return False, -999, reasons
        score += 200
        reasons.append(f"locked_subgroup:{locked_subgroup}+200")

    for idx, grp in enumerate(filters.get("preferred_subgroups", [])):
        if grp.lower() in text:
            bonus = 120 - idx * 10
            score += bonus
            reasons.append(f"subgroup:{grp}+{bonus}")
            break

    for idx, res in enumerate(filters.get("prefer_resolutions", [])):
        if res.lower() in text:
            bonus = 20 - idx * 3
            score += bonus
            reasons.append(f"resolution:{res}+{bonus}")
            break

    if any(x in text for x in ["2160p", "1080p", "720p"]):
        score += 5
    if any(x in text for x in ["hevc", "x265"]):
        score += 3
    return True, score, reasons


def choose_best_items(rss_items: List[dict], filters: dict, locked_subgroup: Optional[str] = None) -> List[dict]:
    grouped: Dict[str, dict] = {}
    for item in rss_items:
        ok, score, reasons = score_item(item["title"], filters, locked_subgroup=locked_subgroup)
        if not ok:
            continue
        key = extract_episode_key(item["title"])
        cand = dict(item)
        cand["score"] = score
        cand["reasons"] = reasons
        cand["matched_subgroup"] = extract_subgroup(item["title"], filters)
        prev = grouped.get(key)
        if prev is None or cand["score"] > prev["score"]:
            grouped[key] = cand

    if grouped or not locked_subgroup:
        return sorted(grouped.values(), key=lambda x: (x["score"], x["title"]), reverse=True)

    return choose_best_items(rss_items, filters, locked_subgroup=None)


def load_state(path: str) -> dict:
    if os.path.exists(path):
        return read_json(path)
    return {
        "seen_guids": [],
        "subject_to_mikan": {},
        "subject_to_mikan_title": {},
        "subject_subgroups": {},
        "tracked_subjects": {},
        "removed_subject_ids": [],
        "last_run": None,
    }


def host_download_path(qb_savepath: str, override: Optional[str] = None) -> str:
    if override:
        return override
    if qb_savepath.startswith("/downloads/"):
        return qb_savepath.replace("/downloads/", "/data/torrents/", 1)
    if qb_savepath == "/downloads":
        return "/data/torrents"
    return qb_savepath


def compute_brake_state(cfg: dict, qb: Optional[QBittorrentClient]) -> dict:
    throttle = cfg.get("throttle", {}) or {}
    state = {
        "active": False,
        "reasons": [],
        "free_bytes": None,
        "queued_download_count": None,
        "queued_download_bytes": None,
        "download_root_fs_path": None,
    }
    if qb is None:
        return state

    download_root = host_download_path(
        cfg.get("qbittorrent", {}).get("savepath", "/downloads/complete/anime"),
        throttle.get("download_root_fs_path"),
    )
    state["download_root_fs_path"] = download_root
    try:
        state["free_bytes"] = shutil.disk_usage(download_root).free
    except FileNotFoundError:
        state["reasons"].append(f"download_root_missing:{download_root}")
        state["active"] = True
        return state

    torrents = qb.torrents()
    download_states = {
        "downloading", "stalledDL", "metaDL", "forcedMetaDL", "forcedDL",
        "checkingDL", "queuedDL", "allocating", "moving",
    }
    queued = [item for item in torrents if str(item.get("state", "")) in download_states]
    state["queued_download_count"] = len(queued)
    state["queued_download_bytes"] = sum(int(item.get("amount_left") or 0) for item in queued)

    min_free_gib = float(throttle.get("min_free_gib", 8))
    max_queue_gib = float(throttle.get("max_queued_download_gib", 200))
    max_queue_count = int(throttle.get("max_queued_download_count", 300))
    min_free_bytes = int(min_free_gib * (1024 ** 3))
    max_queue_bytes = int(max_queue_gib * (1024 ** 3))

    if state["free_bytes"] < min_free_bytes:
        state["reasons"].append(
            f"low_free_space:{state['free_bytes']/(1024**3):.2f}GiB<{min_free_gib:.2f}GiB"
        )
    if state["queued_download_bytes"] > max_queue_bytes:
        state["reasons"].append(
            f"queue_backlog_bytes:{state['queued_download_bytes']/(1024**3):.2f}GiB>{max_queue_gib:.2f}GiB"
        )
    if state["queued_download_count"] > max_queue_count:
        state["reasons"].append(
            f"queue_backlog_count:{state['queued_download_count']}>{max_queue_count}"
        )

    state["active"] = bool(state["reasons"])
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Bangumi anime collections to qBittorrent using Mikan RSS")
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    parser.add_argument("--dry-run", action="store_true", help="Do not submit torrents to qBittorrent")
    args = parser.parse_args()

    cfg = read_json(args.config)
    state_path = cfg.get("state_path", "/opt/anime-stack/state/bangumi-sync-state.json")
    catalog_path = cfg.get("catalog_path", "/opt/anime-stack/state/bangumi-collections-catalog.json")
    state = load_state(state_path)
    state.setdefault("seen_guids", [])
    state.setdefault("subject_to_mikan", {})
    state.setdefault("subject_to_mikan_title", {})
    state.setdefault("subject_subgroups", {})
    state.setdefault("tracked_subjects", {})
    state.setdefault("removed_subject_ids", [])
    seen = set(state["seen_guids"])

    bangumi = BangumiClient(cfg["bangumi"])
    mikan = MikanClient(cfg.get("mikan", {}))
    dry_run = bool(cfg.get("dry_run", True) or args.dry_run)
    qb = None if dry_run else QBittorrentClient(cfg["qbittorrent"])

    brake_state = {"active": False, "reasons": []}
    if qb is not None:
        qb.login()
        log("qBittorrent login ok")
        brake_state = compute_brake_state(cfg, qb)
        log(
            "Brake state:",
            json.dumps({
                "active": brake_state.get("active"),
                "reasons": brake_state.get("reasons"),
                "free_gib": None if brake_state.get("free_bytes") is None else round(brake_state["free_bytes"] / (1024 ** 3), 2),
                "queued_download_count": brake_state.get("queued_download_count"),
                "queued_download_gib": None if brake_state.get("queued_download_bytes") is None else round(brake_state["queued_download_bytes"] / (1024 ** 3), 2),
                "download_root_fs_path": brake_state.get("download_root_fs_path"),
            }, ensure_ascii=False),
        )

    tracked_collection_types = [int(x) for x in cfg.get("tracked_collection_types", [1, 2, 3])]
    download_collection_types = set(int(x) for x in cfg.get("download_collection_types", [3]))
    collections = bangumi.get_combined_anime_collections(tracked_collection_types)

    type_counts: Dict[str, int] = {}
    for row in collections:
        label = COLLECTION_TYPE_LABELS.get(int(row.get("collection_type") or 0), str(row.get("collection_type") or "unknown"))
        type_counts[label] = type_counts.get(label, 0) + 1
    log(f"Bangumi tracked anime count: {len(collections)} type_counts={type_counts}")

    previous_tracked = set(str(x) for x in state.get("tracked_subjects", {}).keys())
    current_tracked = set()
    tracked_subjects = {}
    submitted = []
    unresolved = []

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
        log(f"Processing: {display} (subject_id={subject_id}, collection={collection_label})")

        if collection_type not in download_collection_types:
            log(f"Track only, skip download: {display} ({collection_label})")
            continue

        if brake_state.get("active"):
            reason_text = '; '.join(brake_state.get("reasons", []))
            log(f"Brake engaged, skip adding torrents for {display}: {reason_text}")
            continue

        mikan_id = state["subject_to_mikan"].get(subject_id)
        cached_title = state["subject_to_mikan_title"].get(subject_id, "")
        best = validate_cached_mikan_mapping(row, str(mikan_id or ""), cached_title)
        if not best:
            candidates = mikan.search_bangumi([title_cn, title_jp])
            best = mikan.choose_best_bangumi(row, candidates)
            if best and best.get("bangumi_id"):
                state["subject_to_mikan"][subject_id] = best["bangumi_id"]
                state["subject_to_mikan_title"][subject_id] = str(best.get("title") or "")
                log(f"Mapped to Mikan bangumiId={best['bangumi_id']} title={best['title']} score={best.get('score')}")

        if not best:
            unresolved.append({"subject_id": subject_id, "title": display, "collection": collection_label})
            log(f"Unresolved on Mikan: {display}")
            continue

        rss_items = mikan.fetch_rss_items(best["bangumi_id"])
        locked_subgroup = state["subject_subgroups"].get(subject_id)
        if locked_subgroup:
            log(f"Locked subgroup for {display}: {locked_subgroup}")
        chosen = choose_best_items(rss_items, cfg.get("filters", {}), locked_subgroup=locked_subgroup)
        if not chosen:
            log(f"No eligible RSS items after filters: {display}")
            continue

        watched_ep = int(row.get("ep_status") or 0)
        related_subject_rows = [candidate for candidate in collections if candidate is not row]
        for item in chosen:
            guid = item["guid"]
            if guid in seen:
                continue
            if not item_matches_subject_partition(row, item["title"], related_subject_rows):
                log(f"Skip partition mismatch: {display} :: {item['title']}")
                continue

            episode_span = extract_episode_span(item["title"])
            if episode_span is not None and watched_ep > 0:
                _, item_end_ep = episode_span
                if item_end_ep <= watched_ep:
                    log(f"Skip watched item: {display} watched_ep={watched_ep} :: {item['title']}")
                    seen.add(guid)
                    state["seen_guids"].append(guid)
                    continue

            savepath = subject_savepath(
                cfg.get("qbittorrent", {}).get("savepath", "/downloads/complete/anime"),
                display,
                subject_id,
            )

            if dry_run:
                log(f"DRY RUN add: {item['title']} {item['torrent_url']}")
            else:
                qb.add_torrent_url(item["torrent_url"], savepath=savepath)
                log(f"Added torrent: {item['title']} -> {savepath}")

            seen.add(guid)
            state["seen_guids"].append(guid)

            matched_subgroup = item.get("matched_subgroup")
            if matched_subgroup and not state["subject_subgroups"].get(subject_id):
                state["subject_subgroups"][subject_id] = matched_subgroup
                log(f"Locked subgroup for {display}: {matched_subgroup}")

            submitted.append({
                "subject_id": subject_id,
                "collection": collection_label,
                "show": display,
                "title": item["title"],
                "torrent_url": item["torrent_url"],
                "score": item.get("score"),
                "reasons": item.get("reasons", []),
                "savepath": savepath,
            })

    removed_subject_ids = sorted(previous_tracked - current_tracked)
    state["tracked_subjects"] = tracked_subjects
    state["removed_subject_ids"] = removed_subject_ids
    state["last_run"] = now()
    write_json(state_path, state)

    catalog = {
        "generated_at": now(),
        "download_collection_types": sorted(download_collection_types),
        "removed_subject_ids": removed_subject_ids,
        "subjects": tracked_subjects,
        "tracked_collection_types": tracked_collection_types,
        "tracked_count": len(collections),
        "type_counts": type_counts,
    }
    write_json(catalog_path, catalog)

    summary = {
        "submitted_count": len(submitted),
        "unresolved_count": len(unresolved),
        "dry_run": dry_run,
        "tracked_count": len(collections),
        "tracked_type_counts": type_counts,
        "removed_subject_ids": removed_subject_ids,
        "download_collection_types": sorted(download_collection_types),
        "submitted": submitted,
        "unresolved": unresolved,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
