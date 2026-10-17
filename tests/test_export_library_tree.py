import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_library_tree.py"
FIXTURE = ROOT / "tests" / "fixtures" / "library_tree_sample.json"


class ExportLibraryTreeSmokeTests(unittest.TestCase):
    def test_cli_exports_actual_tree_and_keeps_missing_as_status_only(self):
        before = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            inventory_path = temp_root / "library.json"
            fixture = json.loads(before.decode("utf-8"))
            fixture["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            inventory_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            output = temp_root / "reports" / "cloud-tree.md"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--inventory", str(inventory_path), "--output", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = output.read_text(encoding="utf-8")

        self.assertEqual(FIXTURE.read_bytes(), before)
        self.assertIn("Bangumi 目标番数：2", report)
        self.assertIn("云盘目录：**云盘尚无目录**", report)
        self.assertIn("缺集（仅库存差集，不代表云盘文件）：`EP01`", report)
        self.assertNotIn("└── `EP01`", report)
        self.assertNotIn("EP02.strm", report)
        self.assertIn("`Disc 01`/", report)
        self.assertIn("`01.mkv`", report)
        self.assertIn("`empty-subdir`/", report)
        self.assertIn("重复集号（1）：`EP01`", report)
        self.assertIn("`OVA01.mkv`", report)
        self.assertIn("未核对原因：", report)
        self.assertIn("`readme.txt`", report)
        self.assertNotIn("must-not-appear-in-report", report)

    def test_cli_refuses_to_overwrite_the_inventory(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--inventory", str(FIXTURE), "--output", str(FIXTURE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("output path must differ", completed.stderr)
        self.assertIsInstance(json.loads(FIXTURE.read_text(encoding="utf-8")), dict)

    def test_stale_inventory_marks_missing_comparison_unchecked(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            inventory_path = temp_root / "stale.json"
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload["generated_at"] = "2020-01-01T00:00:00+00:00"
            inventory_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            output = temp_root / "stale.md"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--inventory", str(inventory_path), "--output", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = output.read_text(encoding="utf-8")
        self.assertIn("缺集状态：未核对", report)
        self.assertIn("库存快照新鲜度为 stale", report)
        self.assertNotIn("缺集（仅库存差集，不代表云盘文件）：`EP01`", report)


if __name__ == "__main__":
    unittest.main()
