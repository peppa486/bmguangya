import copy
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_library_strm as sync


NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
MEDIA_REL = "在看/Foo/Disc 01/01.mkv"


def config(media_root: Path, archive_root: Path) -> dict:
    return {
        "alist": {
            "public_url": "http://127.0.0.1:5244",
            "root": "/guangya/Anime",
            "stream_mount": "guangya",
            "username": "jellyfin-reader",
            "password": "s3cret",
        },
        "guangya": {"root_dir": "Anime"},
        "media_root": str(media_root),
        "archive_root": str(archive_root),
        "report_path": str(archive_root.parent / "report.json"),
        "network_timeout_seconds": 3,
        "jellyfin": {
            "base_url": "http://127.0.0.1:8096",
            "token": "test-token",
            "username": "root",
            "db_path": str(archive_root.parent / "jellyfin.db"),
        },
    }


def show(*, matched: bool = True, episode_key: str = "EP01") -> dict:
    return {
        "name": "Bangumi 准确标题" if matched else "Foo",
        "cloud_name": "Foo",
        "subject_id": "12345" if matched else None,
        "source_path": "在看/Foo",
        "path": "在看/Foo",
        "matched": matched,
        "ambiguous": False,
        "unmatched": not matched,
        "count": 1,
        "vids": [{"name": "01.mkv", "relative_path": MEDIA_REL, "path": MEDIA_REL}],
        "duplicate_eps": [],
        "episodes": [{
            "episode_key": episode_key,
            "episode_id": "9001",
            "present": True,
            "relative_path": MEDIA_REL,
            "file_name": "01.mkv",
            "present_files": [{"relative_path": MEDIA_REL, "name": "01.mkv", "file_id": "f-1"}],
        }],
    }


def inventory(*shows: dict) -> dict:
    return {
        "complete": True,
        "generated_at": NOW.isoformat(),
        "last_success_at": NOW.isoformat(),
        "freshness": {"state": "fresh", "age_seconds": 0, "ttl_seconds": 900},
        "errors": [],
        "source": {"cloud_root": "/guangya/Anime", "cloud_refresh": True},
        "tree": [
            {"category": category, "shows": list(shows) if category == "在看" else []}
            for category in sync.CATEGORIES
        ],
    }


def unmatched_show_at(source_path: str, title: str, file_paths: list[str], *, episode_key: str) -> dict:
    row = show(matched=False, episode_key=episode_key)
    row.update({
        "name": title,
        "cloud_name": title,
        "source_path": source_path,
        "path": source_path,
        "count": len(file_paths),
        "vids": [
            {"name": path.rsplit("/", 1)[-1], "relative_path": path, "path": path}
            for path in file_paths
        ],
        "episodes": [{
            "episode_key": episode_key,
            "episode_id": None,
            "present": True,
            "reason": "episode_not_in_bangumi",
            "relative_path": file_paths[0],
            "file_name": file_paths[0].rsplit("/", 1)[-1],
            "present_files": [{"relative_path": file_paths[0], "name": file_paths[0].rsplit("/", 1)[-1]}],
        }] if file_paths else [],
    })
    return row


class SyncLibraryStrmTests(unittest.TestCase):
    def test_inventory_must_be_complete_fresh_and_live_refreshed(self):
        inv = inventory(show())
        cfg = config(Path("/media"), Path("/archive"))
        sync.validate_inventory(inv, cfg, now=NOW)

        stale = copy.deepcopy(inv)
        stale["generated_at"] = (NOW - dt.timedelta(seconds=901)).isoformat()
        with self.assertRaisesRegex(sync.SyncError, "stale"):
            sync.validate_inventory(stale, cfg, now=NOW)

        incomplete = copy.deepcopy(inv)
        incomplete["complete"] = False
        with self.assertRaisesRegex(sync.SyncError, "incomplete"):
            sync.validate_inventory(incomplete, cfg, now=NOW)

        not_live = copy.deepcopy(inv)
        not_live["source"]["cloud_refresh"] = False
        with self.assertRaisesRegex(sync.SyncError, "live AList refresh"):
            sync.validate_inventory(not_live, cfg, now=NOW)

    def test_exact_episode_maps_to_webdav_url_preserving_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = sync.validate_config(config(root / "media", root / "archive"))
            planned, unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            self.assertEqual(unresolved, [])
            self.assertEqual(planned[0]["episodes"][0]["local_path"], "在看/Bangumi 准确标题/EP01.strm")
            url = sync.authenticated_strm_url(cfg, planned[0]["episodes"][0]["relative_path"])
            parsed = urllib.parse.urlsplit(url)
            self.assertEqual((parsed.username, parsed.password), ("jellyfin-reader", "s3cret"))
            self.assertEqual(
                urllib.parse.unquote(parsed.path),
                "/dav/guangya/Anime/在看/Foo/Disc 01/01.mkv",
            )

    def test_known_12_episode_match_does_not_claim_extra_ep22(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            matched = show()
            matched["total_eps"] = 12
            matched["expected_eps_count"] = 12
            matched["coverage_unknown"] = False
            matched["vids"].extend([
                {"name": "22.mkv", "relative_path": "在看/Foo/Disc 01/22.mkv"},
                {"name": "OVA01.mkv", "relative_path": "在看/Foo/Disc 01/OVA01.mkv"},
            ])
            matched["episodes"].extend([
                {
                    "episode_key": "EP22", "episode_id": None, "present": True,
                    "relative_path": "在看/Foo/Disc 01/22.mkv", "reason": "episode_not_in_bangumi",
                    "present_files": [{"relative_path": "在看/Foo/Disc 01/22.mkv", "name": "22.mkv"}],
                },
                {
                    "episode_key": "OVA01", "episode_id": None, "present": True,
                    "relative_path": "在看/Foo/Disc 01/OVA01.mkv", "reason": "episode_not_in_bangumi",
                    "is_special": True,
                    "present_files": [{"relative_path": "在看/Foo/Disc 01/OVA01.mkv", "name": "OVA01.mkv"}],
                },
            ])
            planned, unresolved, _ = sync.build_show_plan(inventory(matched), cfg)

            keys = {episode["episode_key"] for episode in planned[0]["episodes"]}
            self.assertEqual(keys, {"EP01", "OVA01"})
            self.assertTrue(any(
                row["episode"] == "EP22" and row["reason"] == "out_of_range_episode"
                for row in unresolved
            ))
            self.assertFalse(planned[0]["cleanup_safe"])

            current_show = media / "在看" / "Bangumi 准确标题"
            current_show.mkdir(parents=True)
            old_ep22 = current_show / "EP22.strm"
            old_ep22.write_text("http://old.invalid/22.mkv\n", encoding="utf-8")
            sync.apply_plan(planned, cfg)
            self.assertTrue(old_ep22.is_file())
            self.assertEqual(old_ep22.read_text(encoding="utf-8"), "http://old.invalid/22.mkv\n")

            unmatched = show(matched=False, episode_key="EP22")
            unmatched["vids"] = [{
                "name": "22.mkv", "relative_path": "在看/Foo/22.mkv", "path": "在看/Foo/22.mkv",
            }]
            unmatched["episodes"][0].update({
                "episode_id": None,
                "reason": "episode_not_in_bangumi",
                "relative_path": "在看/Foo/22.mkv",
                "present_files": [{"relative_path": "在看/Foo/22.mkv", "name": "22.mkv"}],
            })
            planned_unmatched, unresolved_unmatched, _ = sync.build_show_plan(inventory(unmatched), cfg)
            self.assertIsNone(planned_unmatched[0]["subject_id"])
            self.assertIsNone(planned_unmatched[0]["nfo_title"])
            self.assertEqual([ep["episode_key"] for ep in planned_unmatched[0]["episodes"]], ["EP22"])
            self.assertFalse(any(row["episode"] == "EP22" for row in unresolved_unmatched))

    def test_review_directories_are_excluded_and_old_indexes_archived_but_regular_unmatched_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            raw = config(media, archive)
            review_root = "未标记/待核对-Foo"
            review = unmatched_show_at(review_root, "待核对-Foo", [
                review_root + "/01.mkv", review_root + "/01-copy.mkv",
            ], episode_key="EP01")
            review_descendant = unmatched_show_at(
                review_root + "/Disc 2", "Disc 2", [review_root + "/Disc 2/02.mkv"], episode_key="EP02",
            )
            regular_unmatched = unmatched_show_at(
                "未标记/普通未匹配", "普通未匹配", ["未标记/普通未匹配/22.mkv"], episode_key="EP22",
            )
            inv = inventory()
            next(section for section in inv["tree"] if section["category"] == "未标记")["shows"] = [
                review, review_descendant, regular_unmatched,
            ]

            config_path = root / "config.json"
            inventory_path = root / "library.json"
            report_path = root / "report.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            inventory_path.write_text(json.dumps(inv), encoding="utf-8")
            old_review = media / "未标记" / "待核对-Foo"
            nested_old_review = old_review / "Disc 2"
            nested_old_review.mkdir(parents=True)
            old_review_strm = old_review / "EP01.strm"
            old_review_strm.write_text("http://old.invalid/review.mkv\n", encoding="utf-8")
            nested_old_strm = nested_old_review / "EP02.strm"
            nested_old_strm.write_text("http://old.invalid/review-2.mkv\n", encoding="utf-8")
            (old_review / "tvshow.nfo").write_bytes(sync.make_show_nfo("12345", "Old review title"))

            with patch.object(sync, "refresh_jellyfin", return_value={"requested": True, "status_code": 204, "verified": True}):
                report, code = sync.run(config_path, inventory_path, report_path, apply=True, now=NOW)

            self.assertEqual(code, 0)
            self.assertEqual(report["skipped_review_files"], 3)
            self.assertEqual(report["excluded_review_paths"], [{
                "path": review_root,
                "inventory_show_paths": [review_root, review_root + "/Disc 2"],
                "video_file_count": 3,
            }])
            self.assertEqual(report["planned_strm_count"], 1)
            self.assertFalse(old_review_strm.exists())
            self.assertFalse(nested_old_strm.exists())
            self.assertFalse(list(old_review.rglob("*.strm")) if old_review.exists() else [])
            self.assertEqual(report["apply_stats"]["archive_count"], 3)
            self.assertTrue((media / "未标记" / "普通未匹配" / "EP22.strm").is_file())

    def test_review_directories_can_be_included_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review_root = "未标记/待核对-Foo"
            review = unmatched_show_at(review_root, "待核对-Foo", [review_root + "/01.mkv"], episode_key="EP01")
            inv = inventory()
            next(section for section in inv["tree"] if section["category"] == "未标记")["shows"] = [review]
            raw = config(root / "media", root / "archive")
            raw["strm"] = {"include_review_directories": True}
            cfg = sync.validate_config(raw)

            planned, unresolved, _ = sync.build_show_plan(inv, cfg)

            self.assertEqual([item["episode_key"] for item in planned[0]["episodes"]], ["EP01"])
            self.assertFalse(planned[0].get("review_excluded", False))
            self.assertFalse(any(item["reason"] == "review_directory_excluded" for item in unresolved))

    def test_review_directory_option_requires_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = config(Path(tmp) / "media", Path(tmp) / "archive")
            raw["strm"] = {"include_review_directories": "true"}
            with self.assertRaisesRegex(sync.SyncError, "must be a boolean"):
                sync.validate_config(raw)

    def test_unmatched_unknown_and_duplicate_files_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = sync.validate_config(config(root / "media", root / "archive"))
            unmatched = show(matched=False, episode_key="UNKNOWN")
            planned, unresolved, _ = sync.build_show_plan(inventory(unmatched), cfg)
            self.assertIsNone(planned[0]["subject_id"])
            self.assertIsNone(planned[0]["nfo_title"])
            self.assertEqual(planned[0]["episodes"], [])
            self.assertTrue(any(row["reason"] == "unrecognized_episode_key" for row in unresolved))

            duplicate = show()
            duplicate["vids"].append({"name": "01-alt.mkv", "relative_path": "在看/Foo/01-alt.mkv"})
            duplicate["episodes"][0]["present_files"].append({
                "relative_path": "在看/Foo/01-alt.mkv", "name": "01-alt.mkv", "file_id": "f-2",
            })
            planned, unresolved, _ = sync.build_show_plan(inventory(duplicate), cfg)
            self.assertEqual(planned[0]["episodes"], [])
            self.assertTrue(any(row["reason"] == "duplicate_episode_mapping" for row in unresolved))

    def test_path_traversal_and_duplicate_show_targets_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = sync.validate_config(config(root / "media", root / "archive"))
            bad = show()
            bad["episodes"][0]["relative_path"] = "在看/Foo/../../secret.mkv"
            bad["episodes"][0]["present_files"][0]["relative_path"] = bad["episodes"][0]["relative_path"]
            with self.assertRaisesRegex(sync.SyncError, "traversal"):
                sync.build_show_plan(inventory(bad), cfg)

            mismatched_display_path = show()
            mismatched_display_path["path"] = "想看/Foo"
            with self.assertRaisesRegex(sync.SyncError, "disagree"):
                sync.build_show_plan(inventory(mismatched_display_path), cfg)

            first, second = show(), show()
            second["source_path"] = second["path"] = "在看/Other"
            second["cloud_name"] = "Other"
            with self.assertRaisesRegex(sync.SyncError, "duplicate source or local title"):
                sync.build_show_plan(inventory(first, second), cfg)

    def test_apply_archives_obsolete_indexes_preserves_other_nfo_and_posts_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            old_show = media / "看过" / "Old"
            old_show.mkdir(parents=True)
            (old_show / "EP01.strm").write_text("https://old.invalid/01.mkv\n", encoding="utf-8")
            (old_show / "tvshow.nfo").write_bytes(sync.make_show_nfo("55", "Old"))
            manual = media / "未标记" / "Manual"
            manual.mkdir(parents=True)
            manual_nfo = manual / "tvshow.nfo"
            manual_nfo.write_text(
                '<tvshow><title>Manual</title><uniqueid type="tmdb">7</uniqueid></tvshow>', encoding="utf-8"
            )

            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            with patch.object(sync, "refresh_jellyfin", return_value={"requested": True, "status_code": 204, "verified": True}) as refresh:
                stats = sync.apply_plan(planned, cfg)
                refresh(cfg)

            new_show = media / "在看" / "Bangumi 准确标题"
            strm_path = new_show / "EP01.strm"
            self.assertTrue(strm_path.is_file())
            self.assertIn(
                "/dav/guangya/Anime/%E5%9C%A8%E7%9C%8B/Foo/Disc%2001/01.mkv",
                strm_path.read_text(encoding="utf-8"),
            )
            nfo = ET.parse(new_show / "tvshow.nfo").getroot()
            self.assertEqual(nfo.findtext("title"), "Bangumi 准确标题")
            self.assertEqual(nfo.findtext("uniqueid"), "12345")
            self.assertFalse(old_show.exists())
            self.assertEqual(stats["archive_count"], 2)
            archived = list(archive.rglob("*"))
            self.assertEqual(sum(path.is_file() for path in archived), 2)
            self.assertTrue(manual_nfo.exists())
            self.assertEqual(refresh.call_count, 1)

    def test_apply_keeps_ambiguous_show_indexes_while_reconciling_safe_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            ambiguous_dir = media / "在看" / "Ambiguous"
            ambiguous_dir.mkdir(parents=True)
            ambiguous_strm = ambiguous_dir / "EP01.strm"
            ambiguous_strm.write_text("http://old.invalid/ambiguous.mkv\n", encoding="utf-8")
            ambiguous_nfo = ambiguous_dir / "tvshow.nfo"
            ambiguous_nfo.write_bytes(sync.make_show_nfo("55", "Ambiguous"))
            unknown_dir = media / "在看" / "Unknown"
            unknown_dir.mkdir(parents=True)
            unknown_strm = unknown_dir / "EP01.strm"
            unknown_strm.write_text("http://old.invalid/unknown.mkv\n", encoding="utf-8")

            safe_dir = media / "在看" / "Bangumi 准确标题"
            safe_dir.mkdir(parents=True)
            stale_strm = safe_dir / "EP99.strm"
            stale_strm.write_text("http://old.invalid/stale.mkv\n", encoding="utf-8")

            ambiguous_show = {
                "name": "Ambiguous",
                "cloud_name": "Ambiguous",
                "subject_id": None,
                "source_path": "在看/Ambiguous",
                "path": "在看/Ambiguous",
                "matched": False,
                "ambiguous": True,
                "unmatched": False,
                "count": 1,
                "vids": [{"name": "01.mkv", "relative_path": "在看/Ambiguous/01.mkv"}],
                "episodes": [],
            }
            unknown_show = {
                "name": "Unknown",
                "cloud_name": "Unknown",
                "source_path": "在看/Unknown",
                "path": "在看/Unknown",
                "matched": False,
                "ambiguous": False,
                "unmatched": True,
                "count": 1,
                "vids": [{"name": "ep-title.mkv", "relative_path": "在看/Unknown/ep-title.mkv"}],
                "episodes": [{
                    "episode_key": "UNKNOWN",
                    "present": True,
                    "relative_path": "在看/Unknown/ep-title.mkv",
                    "present_files": [{"relative_path": "在看/Unknown/ep-title.mkv"}],
                }],
            }
            planned, unresolved, _ = sync.build_show_plan(inventory(show(), ambiguous_show, unknown_show), cfg)
            self.assertTrue(any(row["reason"] == "ambiguous_show_preserved" for row in unresolved))
            self.assertTrue(any(row["reason"] == "unrecognized_episode_key" for row in unresolved))
            sync.apply_plan(planned, cfg)

            self.assertTrue(ambiguous_strm.is_file())
            self.assertTrue(ambiguous_nfo.is_file())
            self.assertTrue(unknown_strm.is_file())
            self.assertFalse(stale_strm.exists())
            self.assertTrue((safe_dir / "EP01.strm").is_file())

    def test_symlink_media_and_archive_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_parent = root / "media-parent"
            outside = root / "outside"
            media_parent.mkdir()
            outside.mkdir()
            media = media_parent / "media"
            try:
                media.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable on this platform")
            cfg = sync.validate_config(config(media, root / "archive"))
            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            with self.assertRaisesRegex(sync.SyncError, "symlink"):
                sync.apply_plan(planned, cfg)

            media.unlink()
            real_media = root / "real-media"
            real_media.mkdir()
            archive_target = root / "outside-archive"
            archive_target.mkdir()
            archive_link = root / "archive-link"
            try:
                archive_link.symlink_to(archive_target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable on this platform")
            cfg = sync.validate_config(config(real_media, archive_link))
            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            with self.assertRaisesRegex(sync.SyncError, "symlink"):
                sync.apply_plan(planned, cfg)

    def test_read_only_database_token_lookup_and_run_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "jellyfin.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE Users (Id TEXT, Username TEXT)")
            connection.execute("CREATE TABLE Devices (UserId TEXT, AccessToken TEXT, DateLastActivity TEXT)")
            connection.execute("INSERT INTO Users VALUES ('u1', 'root')")
            connection.execute("INSERT INTO Devices VALUES ('u1', 'do-not-print-me', '2026-10-17')")
            connection.commit()
            connection.close()
            raw = config(root / "media", root / "archive")
            raw["jellyfin"]["token"] = ""
            raw["jellyfin"]["db_path"] = str(database)
            cfg = sync.validate_config(raw)
            self.assertEqual(sync.jellyfin_token(cfg), "do-not-print-me")

            config_path = root / "config.json"
            inventory_path = root / "library.json"
            report_path = root / "report.json"
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            inventory_path.write_text(json.dumps(inventory(show())), encoding="utf-8")
            with patch.object(sync, "refresh_jellyfin", return_value={"requested": True, "status_code": 204, "verified": True}) as refresh:
                report, code = sync.run(config_path, inventory_path, report_path, apply=True, now=NOW)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["applied"])
            self.assertTrue(report["jellyfin_refresh"]["verified"])
            self.assertTrue(report_path.is_file())
            self.assertNotIn("do-not-print-me", report_path.read_text(encoding="utf-8"))
            refresh.assert_called_once()

    def test_archive_root_must_be_disjoint_from_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = config(root / "media", root)
            with self.assertRaisesRegex(sync.SyncError, "outside"):
                sync.validate_config(raw)

    def test_unmanaged_nfo_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            target = media / "在看" / "Bangumi 准确标题"
            target.mkdir(parents=True)
            nfo = target / "tvshow.nfo"
            original = b'<tvshow><title>Provider title</title><uniqueid type="tmdb">7</uniqueid></tvshow>'
            nfo.write_bytes(original)
            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            with self.assertRaisesRegex(sync.SyncError, "unmanaged tvshow.nfo"):
                sync.apply_plan(planned, cfg)
            self.assertEqual(nfo.read_bytes(), original)

    def test_safe_bangumi_identity_move_archives_only_the_old_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            old_show = media / "在看" / "Old title"
            old_show.mkdir(parents=True)
            old_strm = old_show / "EP01.strm"
            old_strm.write_text("http://old.invalid/01.mkv\n", encoding="utf-8")
            (old_show / "tvshow.nfo").write_bytes(sync.make_show_nfo("12345", "Old title"))

            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            stats = sync.apply_plan(planned, cfg)
            self.assertFalse(old_strm.exists())
            self.assertEqual(stats["archive_count"], 2)
            self.assertTrue((media / "在看" / "Bangumi 准确标题" / "EP01.strm").is_file())

    def test_nested_symlink_in_media_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            outside = root / "outside"
            media.mkdir()
            outside.mkdir()
            (media / "在看").symlink_to(outside, target_is_directory=True)
            cfg = sync.validate_config(config(media, root / "archive"))
            planned, _unresolved, _ = sync.build_show_plan(inventory(show()), cfg)
            with self.assertRaisesRegex(sync.SyncError, "symlink"):
                sync.apply_plan(planned, cfg)

    def test_unknown_episode_coverage_preserves_old_local_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            archive = root / "archive"
            cfg = sync.validate_config(config(media, archive))
            show_with_unknown_coverage = show()
            show_with_unknown_coverage["coverage_unknown"] = True
            current_dir = media / "在看" / "Bangumi 准确标题"
            current_dir.mkdir(parents=True)
            stale = current_dir / "EP99.strm"
            stale.write_text("http://old.invalid/99.mkv\n", encoding="utf-8")

            planned, unresolved, _ = sync.build_show_plan(inventory(show_with_unknown_coverage), cfg)
            self.assertTrue(any(row["reason"] == "bangumi_episode_coverage_unknown" for row in unresolved))
            sync.apply_plan(planned, cfg)
            self.assertTrue(stale.is_file())


if __name__ == "__main__":
    unittest.main()
