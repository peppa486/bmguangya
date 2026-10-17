import io
import json
import importlib
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts import library_inventory as inv


def config():
    return inv.normalize_config({
        "alist_public_url": "http://alist.test:5244",
        "alist_username": "tester",
        "alist_password": "secret",
        "alist_root": "/guangya/Anime",
        "bangumi": {"base_url": "https://bgm.test", "username": "user", "access_token": "token"},
        "inventory": {"output": "inventory.json"},
    })


def file_node(name, path, *, file_id=None, size=1):
    return {"name": name, "type": "file", "path": path, "file_id": file_id,
            "size": size, "modified": "2026-10-16T00:00:00Z"}


def dir_node(name, path, children=None):
    children = list(children or [])
    return {"name": name, "type": "directory", "path": path, "children": children,
            "size": None, "modified": None, "empty": not children}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise inv.requests.HTTPError("status {}".format(self.status_code))

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, get_handler=None, post_handler=None):
        self.headers = {}
        self.get_handler = get_handler
        self.post_handler = post_handler
        self.get_calls = []
        self.post_calls = []

    def get(self, url, *, params=None, timeout=None, **kwargs):
        self.get_calls.append((url, params, timeout))
        return self.get_handler(url, params or {})

    def post(self, url, *, json=None, headers=None, timeout=None, **kwargs):
        self.post_calls.append((url, json, headers, timeout))
        return self.post_handler(url, json or {}, headers or {})


class FakeAlistTree:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def list_all(self, path, page_size):
        self.calls.append((path, page_size))
        return self.entries.get(path, [])


class FakeBangumi:
    def __init__(self, rows, episodes):
        self.rows = rows
        self.episode_rows = episodes
        self.calls = []

    def collections(self):
        return [dict(row) for row in self.rows]

    def episodes(self, subject_id):
        self.calls.append(subject_id)
        return [dict(row) for row in self.episode_rows.get(subject_id, [])]


def standard_subject(subject_id, title, collection_type=3):
    return {"subject_id": str(subject_id), "collection_type": collection_type,
            "subject": {"id": int(subject_id), "name": title, "name_cn": title}}


def make_alist_tree():
    root = "/guangya/Anime"
    return FakeAlistTree({
        root: [
            {"name": "在看", "is_dir": True, "id": "cat-watching"},
            {"name": "想看", "is_dir": True},
            {"name": "看过", "is_dir": True},
            {"name": "未标记", "is_dir": True},
            {"name": "loose.mkv", "is_dir": False},
        ],
        root + "/在看": [{"name": "青空故事 第2季", "is_dir": True, "id": "show-20"}],
        root + "/在看/青空故事 第2季": [
            {"name": "Disc 1", "is_dir": True, "id": "disc-1"},
            {"name": "empty-sub", "is_dir": True},
            {"name": "SP01.mkv", "is_dir": False, "size": 99, "id": "f-sp"},
            {"name": "OVA2.mkv", "is_dir": False, "size": 98, "id": "f-ova"},
            {"name": "99.mkv", "is_dir": False, "size": 90, "id": "f-extra",
             "gcid": "0123456789abcdef0123456789abcdef01234567"},
            {"name": "98.mkv", "is_dir": False, "size": 89, "id": "f-extra-invalid",
             "gcid": "not-a-40-hex-hash"},
        ],
        root + "/在看/青空故事 第2季/Disc 1": [
            {"name": "01.mkv", "is_dir": False, "size": 100, "id": "f-01",
             "gcid": "ABCDEF0123456789ABCDEF0123456789ABCDEF01"},
        ],
        root + "/在看/青空故事 第2季/empty-sub": [],
        root + "/想看": [],
        root + "/看过": [],
        root + "/未标记": [{"name": "旧番", "is_dir": True}],
        root + "/未标记/旧番": [
            {"name": "01.mp4", "is_dir": False, "size": 50, "id": "f-old-01"},
        ],
    })


class ConfigTests(unittest.TestCase):
    def test_flat_config_normalizes_root_and_caps_concurrency(self):
        cfg = config()
        self.assertEqual(cfg["alist"]["root"], "/guangya/Anime")
        self.assertEqual(cfg["alist"]["max_workers"], 4)
        self.assertEqual(cfg["bangumi"]["username"], "user")
        self.assertEqual(cfg["inventory"]["progress_interval_seconds"], 15)

    def test_nested_config_is_supported(self):
        cfg = inv.normalize_config({
            "alist": {"api_base": "http://alist/api/v3", "root": "Anime", "token": "a"},
            "bangumi": {"username": "u", "access_token": "b"},
        })
        self.assertEqual(cfg["alist"]["api_base"], "http://alist/api/v3")
        self.assertEqual(cfg["alist"]["root"], "/Anime")

    def test_direct_guangya_source_is_opt_in_and_validated(self):
        cfg = inv.normalize_config({
            "alist": {"api_base": "http://alist", "token": "a", "root": "/guangya/Anime"},
            "bangumi": {"username": "u", "access_token": "b"},
            "guangya": {"api_base": "https://cloud.example"},
            "inventory": {"cloud_source": "guangya_direct"},
        })
        self.assertEqual(cfg["inventory"]["cloud_source"], "guangya_direct")
        self.assertEqual(cfg["inventory"]["guangya_api_base"], "https://cloud.example")
        with self.assertRaises(inv.InventoryError):
            inv.normalize_config({
                "alist": {"api_base": "http://alist", "token": "a"},
                "bangumi": {"username": "u", "access_token": "b"},
                "inventory": {"cloud_source": "unknown"},
            })


class PaginationTests(unittest.TestCase):
    def test_alist_paginates_all_pages_refreshing_only_the_first_page(self):
        cfg = config()
        cfg["alist"]["token"] = "test-token"
        session = FakeSession(post_handler=lambda url, body, headers: FakeResponse({
            "code": 200,
            "data": {
                "content": ([{"name": "a", "is_dir": False}, {"name": "b", "is_dir": False}]
                            if body["page"] == 1 else [{"name": "c", "is_dir": False}]),
                "total": 3,
            },
        }))
        client = inv.AlistClient(cfg, session_factory=lambda: session)
        rows = client.list_all("/guangya/Anime", page_size=2)
        self.assertEqual([row["name"] for row in rows], ["a", "b", "c"])
        self.assertEqual([call[1]["page"] for call in session.post_calls], [1, 2])
        self.assertEqual([call[1]["refresh"] for call in session.post_calls], [True, False])

    def test_cloud_scan_writes_live_path_heartbeat_and_typed_safe_errors(self):
        class RemoteError(RuntimeError):
            code = 354

        class SlowBrokenTree:
            def list_all(self, path, page_size):
                if path == "/guangya/Anime":
                    time.sleep(1.1)
                    return [{"name": "慢目录", "is_dir": True, "id": "slow-dir"}]
                raise RemoteError("raw body must never appear in progress: api token=secret")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config()
            cfg["inventory"]["progress_interval_seconds"] = 1
            progress = inv.InventoryProgress(root / "progress.json", source="guangya_direct",
                                             interval_seconds=1)
            scanner = inv.CloudScanner(cfg, SlowBrokenTree(), workers=1, progress=progress)
            output = io.StringIO()
            with redirect_stdout(output):
                tree = scanner.scan()
            written = json.loads((root / "progress.json").read_text(encoding="utf-8"))
            logs = output.getvalue()

        self.assertIn('"active_paths":["/guangya/Anime"]', logs)
        self.assertNotIn("api token=secret", logs)
        self.assertNotIn("raw body", logs)
        self.assertEqual(written["status"], "incomplete")
        self.assertEqual(written["directories_succeeded"], 1)
        self.assertEqual(written["directories_failed"], 1)
        self.assertEqual(written["directories_pending"], 0)
        self.assertEqual(written["recent_errors"][0]["path"], "/guangya/Anime/慢目录")
        self.assertEqual(written["recent_errors"][0]["error_type"], "RemoteError")
        self.assertEqual(written["recent_errors"][0]["source_error_code"], 354)
        self.assertTrue(tree["children"][0]["incomplete"])

    def test_alist_rejects_business_error_and_truncated_pages(self):
        cfg = config()
        cfg["alist"]["token"] = "test-token"
        denied = FakeSession(post_handler=lambda *args: FakeResponse({"code": 403, "message": "denied", "data": None}))
        with self.assertRaises(inv.InventoryError) as caught:
            inv.AlistClient(cfg, session_factory=lambda: denied).list_all("/root", 2)
        self.assertEqual(caught.exception.issue["code"], "business_error")

        short = FakeSession(post_handler=lambda *args: FakeResponse({
            "code": 200, "data": {"content": [], "total": 1},
        }))
        with self.assertRaises(inv.InventoryError) as caught:
            inv.AlistClient(cfg, session_factory=lambda: short).list_all("/root", 2)
        self.assertEqual(caught.exception.issue["code"], "truncated_pagination")

    def test_alist_still_rejects_unstable_totals_and_duplicate_names(self):
        cfg = config()
        cfg["alist"]["token"] = "test-token"
        unstable = FakeSession(post_handler=lambda url, body, headers: FakeResponse({
            "code": 200,
            "data": {"content": ([{"name": "a"}, {"name": "b"}] if body["page"] == 1
                                  else [{"name": "c"}, {"name": "d"}]),
                     "total": 3 if body["page"] == 1 else 4},
        }))
        with self.assertRaises(inv.InventoryError) as caught:
            inv.AlistClient(cfg, session_factory=lambda: unstable).list_all("/root", 2)
        self.assertEqual(caught.exception.issue["code"], "unstable_pagination")

        duplicate = FakeSession(post_handler=lambda url, body, headers: FakeResponse({
            "code": 200,
            "data": {"content": [{"name": "same"}], "total": 2},
        }))
        with self.assertRaises(inv.InventoryError) as caught:
            inv.AlistClient(cfg, session_factory=lambda: duplicate).list_all("/root", 1)
        self.assertEqual(caught.exception.issue["code"], "duplicate_entry")

    def test_transient_http_failures_are_retried_and_runtime_deadline_is_enforced(self):
        cfg = config()
        alist_cfg = cfg["alist"]
        alist_cfg.update({"max_attempts": 2, "retry_backoff_seconds": 0,
                          "runtime_deadline_monotonic": None})
        attempts = []

        def request(url, *, timeout, **kwargs):
            attempts.append(timeout)
            if len(attempts) == 1:
                return FakeResponse({"code": 500}, status=500)
            return FakeResponse({"code": 200})

        with mock.patch.object(inv.time, "sleep"):
            response = inv._request_with_retries(request, "http://alist.test", cfg=alist_cfg,
                                                 stage="alist_scan")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(attempts), 2)

        alist_cfg["runtime_deadline_monotonic"] = inv.time.monotonic() - 1
        with self.assertRaises(inv.InventoryError) as caught:
            inv._request_with_retries(request, "http://alist.test", cfg=alist_cfg,
                                      stage="alist_scan")
        self.assertEqual(caught.exception.issue["code"], "runtime_limit")

    def test_bangumi_collection_pagination_is_strict_and_includes_types_4_and_5(self):
        def handler(url, params):
            ctype = int(params["type"])
            offset = int(params["offset"])
            totals = {1: 3, 2: 0, 3: 0, 4: 1, 5: 1}
            rows = {
                1: [standard_subject(1, "一"), standard_subject(2, "二"), standard_subject(3, "三")],
                4: [standard_subject(4, "暂停番", 4)],
                5: [standard_subject(5, "弃番", 5)],
            }.get(ctype, [])
            return FakeResponse({"total": totals[ctype], "data": rows[offset:offset + 2]})

        session = FakeSession(get_handler=handler)
        cfg = config()
        cfg["bangumi"]["page_size"] = 2
        client = inv.BangumiClient(cfg, session_factory=lambda: session)
        rows = client.collections()
        self.assertEqual([row["collection_type"] for row in rows], [1, 1, 1, 4, 5])
        self.assertEqual(len(session.get_calls), 6)

        broken = FakeSession(get_handler=lambda url, params: FakeResponse({"data": []}))
        with self.assertRaises(inv.InventoryError):
            inv.BangumiClient(cfg, session_factory=lambda: broken).collections()

    def test_bangumi_episode_pages_are_strict_and_type_zero_only(self):
        def handler(url, params):
            self.assertEqual(params["type"], 0)
            offset = int(params["offset"])
            rows = [
                {"id": 10, "subject_id": 7, "type": 0, "sort": "1"},
                {"id": 11, "subject_id": 7, "type": 0, "sort": "2"},
                {"id": 12, "subject_id": 7, "type": 0, "sort": "3"},
            ]
            return FakeResponse({"total": 3, "data": rows[offset:offset + 2]})

        cfg = config()
        cfg["bangumi"]["page_size"] = 2
        session = FakeSession(get_handler=handler)
        rows = inv.BangumiClient(cfg, session_factory=lambda: session).episodes("7")
        self.assertEqual([row["id"] for row in rows], [10, 11, 12])
        self.assertEqual([call[1]["offset"] for call in session.get_calls], [0, 2])

        short = FakeSession(get_handler=lambda url, params: FakeResponse({"data": [], "total": 1}))
        with self.assertRaises(inv.InventoryError) as caught:
            inv.BangumiClient(cfg, session_factory=lambda: short).episodes("7")
        self.assertEqual(caught.exception.issue["code"], "truncated_pagination")


class CacheTests(unittest.TestCase):
    def test_episode_metadata_cache_uses_source_time_and_refetches_after_ttl(self):
        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        row = standard_subject("77", "缓存番")
        raw_episode = {"id": 77, "type": 0, "sort": "1", "airdate": "2020-01-01"}
        cached_at = (now - timedelta(hours=1)).isoformat()
        cache = {"updated_at": cached_at, "episodes": {"77": {
            "fetched_at": cached_at, "source": "https://api.bgm.tv/v0/episodes?type=0", "data": [raw_episode],
        }}}

        class CountingClient:
            cfg = {"base_url": "https://api.bgm.tv"}

            def __init__(self):
                self.calls = 0

            def episodes(self, subject_id):
                self.calls += 1
                return [raw_episode]

        client = CountingClient()
        result, updated, errors = inv.fetch_episode_metadata([row], client, cache, 86400, now=now)
        self.assertEqual(client.calls, 0)
        self.assertTrue(result["77"]["cache_hit"])
        self.assertEqual(result["77"]["fetched_at"], cached_at)
        self.assertEqual(errors, [])

        stale_at = (now - timedelta(hours=25)).isoformat()
        stale_cache = {"episodes": {"77": {"fetched_at": stale_at, "data": [raw_episode]}}}
        result, updated, errors = inv.fetch_episode_metadata([row], client, stale_cache, 86400, now=now)
        self.assertEqual(client.calls, 1)
        self.assertFalse(result["77"]["cache_hit"])
        self.assertEqual(updated["episodes"]["77"]["fetched_at"], now.isoformat())
        self.assertEqual(errors, [])


class EpisodeSemanticsTests(unittest.TestCase):
    def test_filename_parser_avoids_resolution_and_season_numbers(self):
        cases = {
            "01.mkv": "1",
            "Show S02E06 [1080p].mkv": "6",
            "[Group] 测试番 第12话.mkv": "12",
            "Show 1080p.mkv": None,
            "SP01.mkv": None,
            "01-12.mkv": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(inv.parse_video_episode(name), expected)
        self.assertEqual(inv.ep_key(0), "0")
        self.assertEqual(inv.episode_key({"sort": 0, "ep": 1}), "0")
        self.assertEqual(inv.parse_video_episode("00.mkv"), "0")
        zero_record = inv.episode_records([{
            "id": 1347308, "subject_id": 495562, "type": 0, "sort": 0, "ep": 1,
            "name": "第0話", "airdate": "2024-05-24",
        }], "495562", today=date(2026, 10, 17))[0]
        self.assertEqual(zero_record["key"], "0")
        self.assertEqual(inv._episode_label(zero_record["key"]), "EP00")
        self.assertFalse(zero_record["coverage_unknown"])
        self.assertEqual(inv.parse_episode_label("OVA2.mkv"), ("OVA02", True))
        self.assertEqual(inv.parse_episode_label("NCOP.mkv"), ("NCOP", True))

    def test_unknown_episode_sort_is_retained_and_disables_only_that_shows_coverage(self):
        subject = standard_subject("51", "未编号番", 3)
        episodes = [{"id": 501, "subject_id": 51, "type": 0, "sort": None, "ep": None,
                     "name": "特别篇", "airdate": "2020-01-01"}]
        rows = inv.episode_records(episodes, "51", today=date(2026, 10, 17))
        self.assertEqual(rows[0]["episode_id"], 501)
        self.assertIsNone(rows[0]["sort"])
        self.assertTrue(rows[0]["coverage_unknown"])
        tree, _ = inv.build_gui_tree(
            [subject], {"51": {"data": episodes, "fetched_at": "2026-10-17T00:00:00+00:00"}},
            dir_node("root", "", []), today=date(2026, 10, 17),
        )
        show = next(show for section in tree for show in section["shows"] if show.get("subject_id") == "51")
        self.assertTrue(show["coverage_unknown"])
        self.assertIsNone(show["total_eps"])
        self.assertIsNone(show["expected_eps_count"])
        self.assertEqual(show["missing_eps"], [])
        self.assertEqual(show["episodes"][0]["source_episode"]["name"], "特别篇")

    def test_exact_matching_keeps_seasons_separate_and_marks_duplicate_titles_ambiguous(self):
        subjects = [standard_subject("1", "青空故事"), standard_subject("2", "青空故事 第2季")]
        cloud = [dir_node("青空故事 第2季", "在看/青空故事 第2季")]
        selected, details = inv.match_subjects(subjects, cloud)
        self.assertEqual(set(selected), {"2"})
        self.assertEqual(details["ambiguous_subject_ids"], set())

        duplicate = [standard_subject("3", "相同标题"), standard_subject("4", "相同标题")]
        selected, details = inv.match_subjects(duplicate, [dir_node("相同标题", "在看/相同标题")])
        self.assertEqual(selected, {})
        self.assertEqual(details["ambiguous_subject_ids"], {"3", "4"})

    def test_inventory_tree_keeps_empty_dirs_future_eps_and_exact_missing_eps(self):
        cfg = config()
        scanner = inv.CloudScanner(cfg, make_alist_tree(), workers=3)
        cloud_root = scanner.scan()
        subjects = [
            standard_subject("10", "青空故事", 3),
            standard_subject("20", "青空故事 第2季", 3),
            standard_subject("30", "暂停番", 4),
        ]
        episode_map = {
            "10": [{"id": 100, "sort": "1", "airdate": "2020-01-01", "type": 0}],
            "20": [
                {"id": 201, "sort": "1", "airdate": "2020-01-01", "type": 0},
                {"id": 202, "sort": "2", "airdate": "2030-01-01", "type": 0},
                {"id": 203, "sort": "3", "airdate": "2020-01-01", "type": 0},
            ],
            "30": [],
        }
        episode_data = {sid: {"data": rows, "fetched_at": "2026-10-17T00:00:00+00:00",
                              "source": "test", "cache_hit": False}
                        for sid, rows in episode_map.items()}
        tree, matches = inv.build_gui_tree(subjects, episode_data, cloud_root, today=date(2026, 10, 17))
        doing = next(section for section in tree if section["category"] == "在看")
        absent = next(show for show in doing["shows"] if show["subject_id"] == "10")
        present = next(show for show in doing["shows"] if show["subject_id"] == "20")
        self.assertTrue(absent["unmatched"])
        self.assertEqual(absent["missing_eps"], ["EP01"])
        self.assertFalse(absent["matched"])
        self.assertTrue(absent["unmatched"])
        self.assertEqual(present["source_path"], "在看/青空故事 第2季")
        cloud_show = next(child for child in doing["shows"] if child["name"] == "青空故事 第2季")
        self.assertEqual(cloud_show["children"][0]["file_id"], "disc-1")
        self.assertEqual(present["missing_eps"], ["EP03"])
        self.assertEqual(present["future_eps"], ["EP02"])
        ep_one = next(ep for ep in present["episodes"] if ep["episode_key"] == "EP01")
        self.assertTrue(ep_one["present"])
        self.assertEqual(ep_one["file_id"], "f-01")
        self.assertEqual(ep_one["present_files"][0]["gcid"], "abcdef0123456789abcdef0123456789abcdef01")
        self.assertTrue(ep_one["relative_path"].startswith(present["source_path"] + "/"))
        self.assertEqual(present["extra_eps_count"], 4)
        extra_99 = next(row for row in present["extra_eps"] if row["name"] == "99.mkv")
        self.assertEqual(extra_99["gcid"], "0123456789abcdef0123456789abcdef01234567")
        extra_98 = next(row for row in present["extra_eps"] if row["name"] == "98.mkv")
        self.assertNotIn("gcid", extra_98)
        self.assertIn("OVA02", [ep["episode_key"] for ep in present["episodes"]])
        self.assertIn("未标记", [section["category"] for section in tree])
        unmarked = next(section for section in tree if section["category"] == "未标记")
        self.assertTrue(any(show["subject_id"] == "30" and show["name"] == "暂停番" for show in unmarked["shows"]))
        old_show = next(show for show in unmarked["shows"] if show["name"] == "旧番")
        self.assertEqual(len(old_show["episodes"]), 1)
        self.assertEqual(old_show["episodes"][0]["episode_key"], "EP01")
        self.assertTrue(old_show["episodes"][0]["present"])
        self.assertIsNone(old_show["episodes"][0]["episode_id"])
        empty = next(child for child in present["children"] if child["name"] == "empty-sub")
        self.assertEqual(empty["children"], [])
        self.assertEqual(matches["unmatched_subject_ids"], ["10", "30"])


class PersistenceTests(unittest.TestCase):
    def test_failed_refresh_preserves_last_success_and_writes_sidecar(self):
        class BrokenBangumi:
            def collections(self):
                raise inv.InventoryError("offline", stage="bangumi", code="http_error")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.json"
            out = root / "library.json"
            old_bytes = b'{"complete":true,"generated_at":"2026-10-16T00:00:00+00:00"}\n'
            cfg_path.write_text(json.dumps({
                "alist_public_url": "http://alist.test", "alist_username": "u", "alist_password": "p",
                "bangumi": {"username": "u", "access_token": "t"},
            }), encoding="utf-8")
            out.write_bytes(old_bytes)
            result, code = inv.run_inventory(cfg_path, out, bangumi_client=BrokenBangumi())
            self.assertEqual(code, 2)
            self.assertEqual(out.read_bytes(), old_bytes)
            self.assertTrue(Path(result["failure"]).exists())
            failure = json.loads(Path(result["failure"]).read_text(encoding="utf-8"))
            self.assertFalse(failure["complete"])
            self.assertEqual(failure["last_success_at"], "2026-10-16T00:00:00+00:00")

    def test_successful_offline_build_writes_compatible_gui_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.json"
            output = root / "library.json"
            cache = root / "episode-cache.json"
            cfg_path.write_text(json.dumps({
                "alist": {"public_url": "http://alist.test", "token": "a", "root": "/guangya/Anime"},
                "bangumi": {"username": "u", "access_token": "b"},
                "inventory": {"output": str(output)},
            }), encoding="utf-8")
            subjects = [standard_subject("1", "青空故事 第2季", 3)]
            episodes = {"1": [{"id": 1, "sort": "1", "airdate": "2020-01-01", "type": 0}]}
            result, code = inv.run_inventory(
                cfg_path, output, cache,
                bangumi_client=FakeBangumi(subjects, episodes),
                alist_client=make_alist_tree(),
                now=datetime(2026, 10, 17, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["complete"])
            self.assertEqual(payload["updated_at"], payload["generated_at"])
            self.assertEqual(payload["generated_at"], payload["freshness"]["checked_at"])
            self.assertEqual(payload["freshness"]["age_seconds"], 0)
            self.assertIn("oldest_source_age_seconds", payload["freshness"])
            self.assertEqual(payload["scan_started_at"], payload["started_at"])
            self.assertEqual(payload["counts"]["bangumi_targets"], 1)
            self.assertGreater(payload["counts"]["cloud_directories"], 0)
            self.assertTrue(any(section["category"] == "未标记" for section in payload["tree"]))
            show = next(show for section in payload["tree"] for show in section["shows"]
                        if show.get("subject_id") == "1")
            self.assertEqual(show["vids"][0]["name"], "01.mkv")
            self.assertIn("episode_key", show["episodes"][0])
            self.assertTrue(cache.exists())
            self.assertIsNone(result["failure"])
            progress = json.loads(Path(result["progress"]).read_text(encoding="utf-8"))
            self.assertEqual(progress["status"], "complete")
            self.assertEqual(progress["phase"], "inventory_finished")

    def test_direct_guangya_adapter_is_lazy_selected_and_identified_in_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.json"
            output = root / "library.json"
            cfg_path.write_text(json.dumps({
                "alist": {"public_url": "http://alist.test", "token": "a", "root": "/guangya/Anime"},
                "bangumi": {"username": "u", "access_token": "b"},
                "guangya": {"api_base": "https://cloud.example"},
                "inventory": {"output": str(output), "cloud_source": "guangya_direct"},
            }), encoding="utf-8")
            adapter = make_alist_tree()
            scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
            with mock.patch("sys.path", [scripts_dir, *sys.path]):
                adapter_module = importlib.import_module("scripts.guangya_inventory")
            with mock.patch.object(adapter_module, "GuangYaInventoryClient", return_value=adapter) as factory:
                result, code = inv.run_inventory(
                    cfg_path, output, root / "cache.json",
                    bangumi_client=FakeBangumi([], {}),
                    now=datetime(2026, 10, 17, tzinfo=timezone.utc),
                )
            self.assertEqual(code, 0)
            factory.assert_called_once()
            self.assertEqual(factory.call_args.args[0]["guangya"]["api_base"], "https://cloud.example")
            source = result["report"]["source"]
            self.assertEqual(source["cloud_kind"], "guangya_direct")
            self.assertEqual(source["cloud"], "https://cloud.example")
            self.assertTrue(source["cloud_refresh"])
            self.assertEqual(result["report"]["source_timestamps"]["alist"]["kind"], "guangya_direct")

    def test_unknown_episode_mapping_is_a_show_warning_not_a_failed_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.json"
            output = root / "library.json"
            cfg_path.write_text(json.dumps({
                "alist": {"public_url": "http://alist.test", "token": "a", "root": "/guangya/Anime"},
                "bangumi": {"username": "u", "access_token": "b"},
                "inventory": {"output": str(output)},
            }), encoding="utf-8")
            raw_episode = {"id": 19, "subject_id": 19, "type": 0, "sort": None, "ep": None,
                           "name": "未编号集", "airdate": "2020-01-01"}
            result, code = inv.run_inventory(
                cfg_path, output, root / "cache.json",
                bangumi_client=FakeBangumi([standard_subject("19", "未编号番")], {"19": [raw_episode]}),
                alist_client=make_alist_tree(),
                now=datetime(2026, 10, 17, tzinfo=timezone.utc),
            )
            self.assertEqual(code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["complete"])
            self.assertTrue(payload["episode_coverage_complete"] is False)
            self.assertEqual(payload["counts"]["coverage_unknown_shows"], 1)
            self.assertEqual(len(payload["warnings"]), 1)
            self.assertTrue(result["report"]["complete"])

    def test_metadata_cache_is_saved_even_when_cloud_scan_is_incomplete(self):
        class BrokenAlistTree:
            def list_all(self, path, page_size):
                raise inv.InventoryError("transient cloud failure", stage="alist_scan", code="http_error")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_path = root / "config.json"
            output = root / "library.json"
            cache = root / "episode-cache.json"
            cfg_path.write_text(json.dumps({
                "alist": {"public_url": "http://alist.test", "token": "a", "root": "/guangya/Anime"},
                "bangumi": {"username": "u", "access_token": "b"},
                "inventory": {"output": str(output)},
            }), encoding="utf-8")
            subject = standard_subject("9", "缓存不丢")
            bgm = FakeBangumi([subject], {"9": [{"id": 91, "sort": "1", "type": 0,
                                                 "airdate": "2020-01-01"}]})
            result, code = inv.run_inventory(cfg_path, output, cache,
                                             bangumi_client=bgm, alist_client=BrokenAlistTree())
            self.assertEqual(code, 2)
            cache_payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertIn("9", cache_payload["episodes"])
            self.assertFalse(output.exists())
            self.assertTrue(Path(result["failure"]).exists())
            progress = json.loads(Path(result["progress"]).read_text(encoding="utf-8"))
            self.assertEqual(progress["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
