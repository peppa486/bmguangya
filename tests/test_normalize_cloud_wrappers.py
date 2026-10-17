import copy
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import normalize_cloud_wrappers as wrapper
from scripts import reconcile_cloud_library as rec


ROOT = "/guangya/Anime"
SHOW_REL = "在看/Foo"
WRAPPER_NAME = "EP01.mkv"
WRAPPER_REL = SHOW_REL + "/" + WRAPPER_NAME
TARGET_NAME = WRAPPER_NAME + " [资源包]"
TARGET_REL = SHOW_REL + "/" + TARGET_NAME


def file_node(name, file_id, *, size=10):
    return {"name": name, "type": "file", "file_id": file_id, "size": size}


def dir_node(name, file_id, children=None, *, path=None):
    return {"name": name, "type": "directory", "file_id": file_id,
            "path": path, "children": list(children or []), "empty": not children}


def make_inventory(*, wrapper_name=WRAPPER_NAME, live_extra=False):
    wrapped_file = file_node("EP01.mkv", "wrapped-video", size=101)
    inner = dir_node(wrapper_name, "wrapper-id", [wrapped_file], path=WRAPPER_REL)
    nested_wrapper = dir_node("EP02.mkv", "nested-wrapper-id",
                              [file_node("EP02.mkv", "nested-video", size=102)],
                              path=SHOW_REL + "/Disc 01/EP02.mkv")
    disc = dir_node("Disc 01", "disc-id", [nested_wrapper], path=SHOW_REL + "/Disc 01")
    ordinary_video = file_node("EP10.mp4", "ordinary-video", size=202)
    if live_extra:
        inner["children"].append(file_node("new-live.mkv", "live-new", size=303))
    show_node = dir_node("Foo", "show-id", [inner, disc, ordinary_video], path=SHOW_REL)
    category_node = dir_node("在看", "category-id", [show_node], path="在看")
    root_node = dir_node("Anime", "root-id", [category_node], path="")
    show = {
        "name": "Bangumi Foo",
        "cloud_name": "Foo",
        "type": "directory",
        "subject_id": "123",
        "matched": True,
        "ambiguous": False,
        "unmatched": False,
        "coverage_unknown": False,
        "source_path": SHOW_REL,
        "path": SHOW_REL,
        "dirs": [wrapper_name, "Disc 01"],
        "children": copy.deepcopy(show_node["children"]),
        "vids": [{"name": "EP01.mkv", "path": WRAPPER_REL + "/EP01.mkv",
                  "relative_path": WRAPPER_REL + "/EP01.mkv"}],
        "episodes": [{
            "episode_key": "EP01", "present": True, "missing": False,
            "relative_path": WRAPPER_REL + "/EP01.mkv", "file_name": "EP01.mkv",
            "file_id": "wrapped-video",
            "present_files": [{"name": "EP01.mkv", "file_id": "wrapped-video",
                               "relative_path": WRAPPER_REL + "/EP01.mkv"}],
        }],
        "expected_eps_count": 2,
        "present_eps_count": 1,
        "missing_eps_count": 1,
        "count": 2,
    }
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "generated_at": now,
        "updated_at": now,
        "last_success_at": now,
        "source_timestamps": {"alist": {"completed_at": now}, "bangumi": {"completed_at": now}},
        "freshness": {"state": "fresh", "age_seconds": 0, "ttl_seconds": 3600,
                      "checked_at": now, "source_ages_seconds": {"alist": 0}},
        "complete": True,
        "cloud_complete": True,
        "collection_complete": True,
        "errors": [],
        "source": {"cloud_root": ROOT, "cloud_refresh": True},
        "tree": [{"category": "在看", "path": "在看", "dirs": ["Foo"], "shows": [show]}],
        "inventory_root": root_node,
        "counts": {"cloud_files": 2, "cloud_video_files": 2, "cloud_directories": 3},
        "stats": {"total_vids": 2, "missing_episodes": 1},
    }


def config_payload(tmp, *, enabled=True):
    return {
        "alist": {"api_base": "http://alist.test:5244", "root": ROOT, "token": "test-token"},
        "inventory": {"path": str(Path(tmp) / "library.json"), "freshness_ttl_seconds": 3600},
        "reconcile": {"report_path": str(Path(tmp) / "wrappers.json"),
                      "normalize_video_named_directories": enabled},
    }


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class FakeAList:
    def __init__(self, root_node, *, fail_readback_once=False):
        self.dirs = {}
        self.writes = []
        self.reads = []
        self.renamed = False
        self.fail_readback_once = fail_readback_once

        def index(node, path):
            children = copy.deepcopy(node.get("children", []))
            self.dirs[path] = children
            for child in children:
                if child.get("type") == "directory":
                    index(child, path.rstrip("/") + "/" + child["name"])

        index(root_node, ROOT)

    def list_all(self, path, *, refresh=True):
        self.reads.append((path, refresh))
        if self.renamed and self.fail_readback_once and path == ROOT + "/" + SHOW_REL:
            self.fail_readback_once = False
            raise rec.ReconcileError("simulated readback timeout", code="alist_timeout")
        if path not in self.dirs:
            raise rec.ReconcileError("missing directory", code="missing_directory")
        return copy.deepcopy(self.dirs[path])

    def rename_file(self, source_path, new_name):
        self.writes.append((source_path, new_name))
        parent, old_name = source_path.rsplit("/", 1)
        matches = [row for row in self.dirs[parent] if row.get("name") == old_name]
        if len(matches) != 1 or any(row.get("name", "").casefold() == new_name.casefold()
                                    for row in self.dirs[parent]):
            raise rec.ReconcileError("rename conflict", code="rename_conflict")
        matches[0]["name"] = new_name
        old_prefix = source_path
        new_prefix = parent + "/" + new_name
        for old_path in list(self.dirs):
            if old_path == old_prefix or old_path.startswith(old_prefix + "/"):
                self.dirs[new_prefix + old_path[len(old_prefix):]] = self.dirs.pop(old_path)
        self.renamed = True
        raise rec.ReconcileError("simulated lost rename response", code="alist_request_uncertain")

    def add_file(self, parent, row):
        self.dirs[parent].append(copy.deepcopy(row))


class NormalizeCloudWrappersTests(unittest.TestCase):
    def _setup_files(self, tmp, inventory=None, *, enabled=True):
        inventory_path = Path(tmp) / "library.json"
        report_path = Path(tmp) / "wrappers.json"
        config_path = Path(tmp) / "runtime.json"
        write_json(inventory_path, inventory or make_inventory())
        write_json(config_path, config_payload(tmp, enabled=enabled))
        return config_path, inventory_path, report_path

    def test_dry_run_is_local_only_and_ordinary_video_file_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            before = inventory_path.read_bytes()
            client_factory = mock.Mock(side_effect=AssertionError("dry-run must not create a cloud client"))
            report = wrapper.run(config_path, inventory_path, report_path,
                                 apply=False, client_factory=client_factory)
            client_factory.assert_not_called()
            self.assertEqual(inventory_path.read_bytes(), before)
            self.assertEqual(len(report["actions"]), 1)
            self.assertEqual(report["actions"][0]["source_name"], WRAPPER_NAME)
            self.assertEqual(report["actions"][0]["state"], "planned")

    def test_numbered_video_suffix_is_supported_only_for_directories(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            inventory = make_inventory()
            numbered_name = "EP05.mkv (3)"
            numbered_path = SHOW_REL + "/" + numbered_name
            numbered = dir_node(numbered_name, "numbered-wrapper-id",
                                [file_node("EP05.mkv", "numbered-video")], path=numbered_path)
            cloud_show = rec._node_by_relative_path(inventory["inventory_root"], SHOW_REL)
            cloud_show["children"].append(numbered)
            inventory["tree"][0]["shows"][0]["children"].append(copy.deepcopy(numbered))
            inventory["tree"][0]["shows"][0]["dirs"].append(numbered_name)
            config_path, inventory_path, report_path = self._setup_files(tmp, inventory)
            report = wrapper.run(config_path, inventory_path, report_path)
            self.assertEqual({row["source_name"] for row in report["actions"]},
                             {WRAPPER_NAME, numbered_name})
            self.assertIn(numbered_name + wrapper.RESOURCE_SUFFIX,
                          {row["target_name"] for row in report["actions"]})

    def test_apply_renames_only_direct_child_directory_and_relocates_all_paths(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            original = json.loads(inventory_path.read_text(encoding="utf-8"))
            timestamp_fields = {key: copy.deepcopy(original.get(key)) for key in
                                ("generated_at", "updated_at", "last_success_at", "source_timestamps", "freshness")}
            before_show = copy.deepcopy(original["tree"][0]["shows"][0])
            client = FakeAList(original["inventory_root"])
            report = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                 client_factory=lambda _cfg: client)
            updated = json.loads(inventory_path.read_text(encoding="utf-8"))
            action = report["actions"][0]

            self.assertEqual(action["state"], "done")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(client.writes[0], (ROOT + "/" + WRAPPER_REL, TARGET_NAME))
            self.assertEqual(updated["generated_at"], timestamp_fields["generated_at"])
            self.assertEqual(updated["updated_at"], timestamp_fields["updated_at"])
            self.assertEqual(updated["last_success_at"], timestamp_fields["last_success_at"])
            self.assertEqual(updated["source_timestamps"], timestamp_fields["source_timestamps"])
            self.assertEqual(updated["freshness"], timestamp_fields["freshness"])
            self.assertEqual(updated["counts"], original["counts"])
            self.assertEqual(updated["stats"], original["stats"])

            show = updated["tree"][0]["shows"][0]
            self.assertEqual(show["name"], before_show["name"])
            self.assertEqual(show["dirs"], [TARGET_NAME, "Disc 01"])
            self.assertEqual(show["episodes"][0]["relative_path"], TARGET_REL + "/EP01.mkv")
            self.assertEqual(show["episodes"][0]["present_files"][0]["relative_path"],
                             TARGET_REL + "/EP01.mkv")
            self.assertEqual(show["vids"][0]["path"], TARGET_REL + "/EP01.mkv")
            self.assertEqual(show["vids"][0]["relative_path"], TARGET_REL + "/EP01.mkv")
            self.assertEqual(show["episodes"][0]["file_name"], "EP01.mkv")
            self.assertEqual(show["episodes"][0]["file_id"], "wrapped-video")
            inventory_root_show = rec._node_by_relative_path(updated["inventory_root"], SHOW_REL)
            self.assertEqual([row["name"] for row in inventory_root_show["children"]],
                             [TARGET_NAME, "Disc 01", "EP10.mp4"])
            relocated = rec._node_by_relative_path(updated["inventory_root"], TARGET_REL)
            self.assertEqual(relocated["file_id"], "wrapper-id")
            self.assertEqual(relocated["children"][0]["name"], "EP01.mkv")
            self.assertEqual(len(updated["derived_path_renames"]), 1)
            self.assertFalse(any("/" + WRAPPER_NAME + "/" in item.get("relative_path", "")
                                 for item in show["episodes"]))

    def test_new_live_child_makes_snapshot_mismatch_and_blocks_rename(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            snapshot = json.loads(inventory_path.read_text(encoding="utf-8"))
            client = FakeAList(snapshot["inventory_root"])
            client.add_file(ROOT + "/" + WRAPPER_REL,
                            file_node("new-live.mkv", "new-live-id", size=999))
            report = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                 client_factory=lambda _cfg: client)
            self.assertEqual(report["actions"][0]["state"], "blocked")
            self.assertEqual(report["actions"][0]["code"], "live_inventory_mismatch")
            self.assertEqual(client.writes, [])

    def test_destination_collision_is_blocked_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            inventory = make_inventory()
            show_node = rec._node_by_relative_path(inventory["inventory_root"], SHOW_REL)
            show_node["children"].append(file_node(TARGET_NAME, "preexisting-target"))
            show = inventory["tree"][0]["shows"][0]
            show["children"].append(file_node(TARGET_NAME, "preexisting-target"))
            config_path, inventory_path, report_path = self._setup_files(tmp, inventory)
            client = FakeAList(inventory["inventory_root"])
            report = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                 client_factory=lambda _cfg: client)
            self.assertEqual(report["actions"][0]["state"], "blocked")
            self.assertEqual(report["actions"][0]["code"], "target_name_conflict")
            self.assertEqual(client.writes, [])

    def test_same_source_name_file_and_directory_is_blocked_in_dry_run(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            inventory = make_inventory()
            show_node = rec._node_by_relative_path(inventory["inventory_root"], SHOW_REL)
            show_node["children"].append(file_node(WRAPPER_NAME, "same-name-file"))
            inventory["tree"][0]["shows"][0]["children"].append(file_node(WRAPPER_NAME, "same-name-file"))
            config_path, inventory_path, report_path = self._setup_files(tmp, inventory)
            report = wrapper.run(config_path, inventory_path, report_path)
            self.assertEqual(len(report["actions"]), 1)
            self.assertEqual(report["actions"][0]["state"], "blocked")
            self.assertEqual(report["actions"][0]["code"], "ambiguous_source_name")

    def test_timeout_unknown_stays_pending_then_recovers_by_unique_directory_id(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            snapshot = json.loads(inventory_path.read_text(encoding="utf-8"))
            client = FakeAList(snapshot["inventory_root"], fail_readback_once=True)
            first = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                client_factory=lambda _cfg: client)
            self.assertEqual(first["actions"][0]["state"], "pending")
            self.assertEqual(len(client.writes), 1)
            old_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(rec._node_by_relative_path(old_inventory["inventory_root"], WRAPPER_REL))

            recovered = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                    client_factory=lambda _cfg: client)
            self.assertEqual(recovered["actions"][0]["state"], "done")
            self.assertEqual(len(client.writes), 1)
            new_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            self.assertIsNone(rec._node_by_relative_path(new_inventory["inventory_root"], WRAPPER_REL))
            self.assertIsNotNone(rec._node_by_relative_path(new_inventory["inventory_root"], TARGET_REL))

    def test_pending_recovers_if_cloud_rename_happened_before_inventory_write(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            snapshot = json.loads(inventory_path.read_text(encoding="utf-8"))
            client = FakeAList(snapshot["inventory_root"])
            original_write = rec.atomic_write_json
            failed = {"done": False}

            def fail_inventory_once(path, payload):
                if Path(path) == inventory_path and not failed["done"]:
                    failed["done"] = True
                    raise OSError("simulated interruption before local inventory persistence")
                return original_write(path, payload)

            with mock.patch.object(wrapper.rec, "atomic_write_json", side_effect=fail_inventory_once):
                with self.assertRaises(OSError):
                    wrapper.run(config_path, inventory_path, report_path, apply=True,
                                client_factory=lambda _cfg: client)
            on_disk_journal = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk_journal["actions"][0]["state"], "pending")
            self.assertIsNotNone(rec._node_by_relative_path(
                json.loads(inventory_path.read_text(encoding="utf-8"))["inventory_root"], WRAPPER_REL))

            recovered = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                    client_factory=lambda _cfg: client)
            self.assertEqual(recovered["actions"][0]["state"], "done")
            self.assertEqual(len(client.writes), 1)
            self.assertIsNotNone(rec._node_by_relative_path(
                json.loads(inventory_path.read_text(encoding="utf-8"))["inventory_root"], TARGET_REL))

    def test_pending_recovers_if_inventory_was_saved_before_done_journal(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp)
            snapshot = json.loads(inventory_path.read_text(encoding="utf-8"))
            client = FakeAList(snapshot["inventory_root"])
            original_write = rec.atomic_write_json
            failed = {"done": False}

            def fail_done_journal_once(path, payload):
                if (Path(path) == report_path and not failed["done"]
                        and any(row.get("state") == "done" for row in payload.get("actions", []))):
                    failed["done"] = True
                    raise OSError("simulated interruption before done journal persistence")
                return original_write(path, payload)

            with mock.patch.object(wrapper.rec, "atomic_write_json", side_effect=fail_done_journal_once):
                with self.assertRaises(OSError):
                    wrapper.run(config_path, inventory_path, report_path, apply=True,
                                client_factory=lambda _cfg: client)
            on_disk_journal = json.loads(report_path.read_text(encoding="utf-8"))
            on_disk_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk_journal["actions"][0]["state"], "pending")
            self.assertIsNotNone(rec._node_by_relative_path(on_disk_inventory["inventory_root"], TARGET_REL))

            recovered = wrapper.run(config_path, inventory_path, report_path, apply=True,
                                    client_factory=lambda _cfg: client)
            self.assertEqual(recovered["actions"][0]["state"], "done")
            self.assertEqual(len(client.writes), 1)
            self.assertEqual(len(json.loads(inventory_path.read_text(encoding="utf-8"))["derived_path_renames"]), 1)

    def test_disabled_config_has_no_candidates_or_cloud_calls(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, report_path = self._setup_files(tmp, enabled=False)
            client_factory = mock.Mock(side_effect=AssertionError("disabled dry-run must remain local"))
            report = wrapper.run(config_path, inventory_path, report_path, apply=False,
                                 client_factory=client_factory)
            client_factory.assert_not_called()
            self.assertEqual(report["actions"], [])
            self.assertFalse(report["summary"]["candidate_count"])

    def test_default_report_is_separate_from_reconcile_report(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, configured_reconcile_report = self._setup_files(tmp)
            configured_reconcile_report.write_text("keep this reconcile report", encoding="utf-8")
            expected_report = inventory_path.parent / "wrapper-normalization-report.json"
            report = wrapper.run(config_path, inventory_path)
            self.assertEqual(Path(report["report_path"]), expected_report)
            self.assertEqual(configured_reconcile_report.read_text(encoding="utf-8"),
                             "keep this reconcile report")
            self.assertTrue(expected_report.exists())

    def test_direct_cli_help_and_local_dry_run(self):
        script_path = Path(wrapper.__file__).resolve()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            config_path, inventory_path, configured_reconcile_report = self._setup_files(tmp)
            configured_reconcile_report.write_text("preserve reconcile output", encoding="utf-8")
            local_only_config = json.loads(config_path.read_text(encoding="utf-8"))
            local_only_config["alist"]["api_base"] = "http://127.0.0.1:1"
            write_json(config_path, local_only_config)
            inventory_before = inventory_path.read_bytes()

            help_result = subprocess.run(
                [sys.executable, str(script_path), "--help"], cwd=tmp, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--dry-run", help_result.stdout)
            self.assertIn("--apply", help_result.stdout)

            result = subprocess.run(
                [sys.executable, str(script_path), "--config", str(config_path),
                 "--inventory", str(inventory_path), "--dry-run"],
                cwd=tmp, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            output = json.loads(result.stdout)
            self.assertEqual(output["state"], "planned")
            self.assertEqual(output["candidate_count"], 1)
            self.assertTrue((inventory_path.parent / "wrapper-normalization-report.json").exists())
            self.assertEqual(inventory_path.read_bytes(), inventory_before)
            self.assertEqual(configured_reconcile_report.read_text(encoding="utf-8"),
                             "preserve reconcile output")


if __name__ == "__main__":
    unittest.main()
