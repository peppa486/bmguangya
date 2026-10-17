import datetime as dt
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitor"))
import server as monitor


class MonitorServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state_dir = Path(self.temp.name)
        inventory_path = state_dir / "library.json"
        self.inventory_path = inventory_path
        self.progress_path = state_dir / "sync-progress.json"
        self.inventory_progress_path = state_dir / "library.progress.json"
        self.failure_path = state_dir / "library.json.failure.json"
        self.index_path = Path(monitor.__file__).with_name("index.html")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            monitor.make_handler(
                inventory_path=inventory_path,
                progress_path=self.progress_path,
                inventory_progress_path=self.inventory_progress_path,
                failure_path=self.failure_path,
                index_path=self.index_path,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def write_inventory(self, *, age_seconds=10, complete=True):
        generated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)).isoformat()
        self.inventory_path.write_text(json.dumps({
            "complete": complete,
            "generated_at": generated,
            "last_success_at": generated,
            "updated_at": generated,
            "freshness": {"state": "fresh", "ttl_seconds": 900},
            "stats": {"bangumi_target_shows": 3, "cloud_directories": 2, "total_vids": 4, "missing_episodes": 1},
            "counts": {"bangumi_targets": 3},
            "tree": [],
            "errors": [],
        }), encoding="utf-8")

    def write_inventory_progress(self, *, age_seconds=10, status="running", **updates):
        updated = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)).isoformat()
        payload = {
            "status": status,
            "phase": "cloud_walk",
            "elapsed_seconds": 321,
            "directories_discovered": 90,
            "directories_started": 80,
            "directories_completed": 75,
            "directories_succeeded": 73,
            "directories_failed": 2,
            "directories_pending": 15,
            "files_seen": 240,
            "active_paths": ["/Anime/在看/示例番"],
            "error_counts": {"timeout": 2},
            "recent_errors": [{"code": "timeout", "message": "read failed", "path": "/Anime/在看/示例番"}],
            "updated_at": updated,
        }
        payload.update(updates)
        self.inventory_progress_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_api_returns_live_inventory_progress_and_no_store(self):
        self.write_inventory()
        self.progress_path.write_text(json.dumps({"status": "running", "submitted_count": 2}), encoding="utf-8")
        request = urllib.request.Request(self.base + "/api/data")
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate")
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["freshness"]["state"], "fresh")
        self.assertEqual(payload["progress"]["submitted_count"], 2)

    def test_scan_progress_reports_real_counts_and_no_percentage(self):
        self.write_inventory()
        self.write_inventory_progress(age_seconds=30, elapsed_seconds=170.7)
        payload, status = monitor.build_api_data(
            self.inventory_path, self.progress_path, self.failure_path,
            inventory_progress_path=self.inventory_progress_path,
        )
        self.assertEqual(status, 200)
        scan = payload["inventory_progress"]
        self.assertEqual(scan["status"], "running")
        self.assertEqual(scan["phase"], "cloud_walk")
        self.assertEqual(scan["directories_completed"], 75)
        self.assertEqual(scan["directories_pending"], 15)
        self.assertEqual(scan["directories_failed"], 2)
        self.assertEqual(scan["files_seen"], 240)
        self.assertEqual(scan["elapsed_seconds"], 170.7)
        self.assertNotIn("percent", scan)

    def test_scan_elapsed_time_accepts_only_nonnegative_finite_numbers(self):
        for value in (0, 170.7):
            with self.subTest(value=value):
                self.assertEqual(monitor._safe_duration(value), value)
        for value in (True, -1, -0.1, float("nan"), float("inf"), "170.7"):
            with self.subTest(value=value):
                self.assertIsNone(monitor._safe_duration(value))

    def test_stale_scan_heartbeat_and_secret_redaction_survive_inventory_unavailable(self):
        self.write_inventory_progress(
            age_seconds=121,
            recent_errors=[{
                "code": "auth", "message": "access_token=super-secret-value", "path": "/Anime/在看/示例番",
            }],
        )
        payload, status = monitor.build_api_data(
            self.inventory_path, self.progress_path, self.failure_path,
            inventory_progress_path=self.inventory_progress_path,
        )
        self.assertEqual(status, 503)
        scan = payload["inventory_progress"]
        self.assertEqual(scan["status"], "stale")
        self.assertEqual(scan["reported_status"], "running")
        self.assertEqual(scan["directories_completed"], 75)
        self.assertEqual(scan["directories_pending"], 15)
        self.assertNotIn("super-secret-value", json.dumps(payload))

    def test_stale_or_incomplete_inventory_is_not_reported_healthy(self):
        self.write_inventory(age_seconds=901)
        with urllib.request.urlopen(self.base + "/api/health", timeout=2) as response:
            stale = json.load(response)
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["freshness"]["state"], "stale")

        self.write_inventory(complete=False)
        with urllib.request.urlopen(self.base + "/api/health", timeout=2) as response:
            incomplete = json.load(response)
        self.assertFalse(incomplete["ok"])
        self.assertEqual(incomplete["freshness"]["state"], "incomplete")

    def test_only_fixed_get_routes_are_exposed(self):
        self.write_inventory()
        with urllib.request.urlopen(self.base + "/", timeout=2) as response:
            self.assertIn("Bangumi × 光鸭媒体库", response.read().decode("utf-8"))
        for path in ("/api/../library.json", "/private", "/%2e%2e/library.json"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=2)
            self.assertEqual(caught.exception.code, 404)
        request = urllib.request.Request(self.base + "/api/data", method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 405)

    def test_future_inventory_timestamp_is_unknown(self):
        now = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        result = monitor.enrich_freshness({
            "complete": True,
            "generated_at": (now + dt.timedelta(minutes=5)).isoformat(),
            "freshness": {"ttl_seconds": 900},
        }, now=now)
        self.assertEqual(result["freshness"]["state"], "unknown")
        self.assertIsNone(result["freshness"]["age_seconds"])

    def test_unavailable_inventory_ui_uses_safe_copy_and_empty_state(self):
        html = self.index_path.read_text(encoding="utf-8")
        self.assertIn("尚无可用的完整云盘清单，请稍后刷新", html)
        self.assertIn("当前没有可展示的完整清单", html)
        self.assertIn("目标 - · 实存目录 -", html)
        self.assertIn("if(!data){for(const id of ['targetCount','cloudDirCount','videoCount','missingCount'])", html)
        self.assertIn('id="progressSection" aria-label="下载流水线进度" hidden', html)
        self.assertIn("$('progressSection').hidden=!p", html)
        self.assertIn("function unavailableCopy(result,status)", html)
        self.assertNotIn("读取库存失败：${error.message}", html)
        self.assertNotIn('class="metric-icon">◎', html)
        self.assertEqual(html.count('<svg viewBox="0 0 24 24">'), 4)
        self.assertIn('id="inventoryProgressSection" aria-label="库存目录扫描进度" hidden', html)
        self.assertIn("function renderInventoryProgress()", html)
        self.assertIn("待处理目录", html)
        self.assertNotIn('id="inventoryProgressBar"', html)

    def test_scan_progress_uses_a_two_minute_running_heartbeat_window(self):
        now = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        fresh = monitor.enrich_inventory_progress({
            "status": "running",
            "updated_at": (now - dt.timedelta(seconds=120)).isoformat(),
        }, now=now)
        stale = monitor.enrich_inventory_progress({
            "status": "running",
            "updated_at": (now - dt.timedelta(seconds=121)).isoformat(),
        }, now=now)
        missing = monitor.enrich_inventory_progress({"status": "running"}, now=now)
        self.assertEqual(fresh["status"], "running")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(missing["status"], "stale")


if __name__ == "__main__":
    unittest.main()
