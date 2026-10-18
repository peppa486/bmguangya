import sys
import tempfile
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import subtitle_sidecars as sidecars
import sync_library_strm as sync
import guangya_inventory


def raw_config(root: Path) -> dict:
    return {
        "alist": {
            "public_url": "https://alist.example.test",
            "root": "/guangya/Anime",
            "stream_mount": "guangya",
            "username": "reader",
            "password": "must-not-leak",
        },
        "media_root": str(root / "media"),
        "archive_root": str(root / "archive"),
        "report_path": str(root / "report.json"),
        "network_timeout_seconds": 3,
        "guangya": {"root_dir": "Anime"},
        "jellyfin": {"base_url": "http://127.0.0.1:8096", "token": "test-token"},
    }


def inventory(entries: list[dict]) -> dict:
    root = {"name": "Anime", "type": "directory", "path": "", "children": []}

    def add(parent: dict, name: str, kind: str, path: str, *, file_id: str | None = None,
            size: int | None = None) -> dict:
        row = {"name": name, "type": kind, "path": path}
        if kind == "directory":
            row["children"] = []
        else:
            row.update({"file_id": file_id, "size": size})
        parent["children"].append(row)
        return row

    for entry in entries:
        parts = entry["path"].split("/")
        parent = root
        prefix = []
        for part in parts[:-1]:
            prefix.append(part)
            current_path = "/".join(prefix)
            matches = [row for row in parent["children"]
                       if row.get("name") == part and row.get("type") == "directory"]
            if matches:
                parent = matches[0]
            else:
                parent = add(parent, part, "directory", current_path)
        prefix.append(parts[-1])
        add(parent, parts[-1], entry.get("type", "file"), "/".join(prefix),
            file_id=entry.get("file_id"), size=entry.get("size"))
    return {"inventory_root": root}


def planned_episode(source: str = "在看/Foo/Disc 01/01.mkv", *,
                    local_path: str = "在看/Foo/EP01.strm") -> list[dict]:
    return [{
        "category": "在看",
        "source_path": "在看/Foo",
        "local_show": "在看/Foo",
        "subject_id": "12345",
        "skip_apply": False,
        "episodes": [{"episode_key": "EP01", "relative_path": source, "local_path": local_path}],
    }]


def make_entries(*subtitle_rows: tuple[str, str, bytes], video_name: str = "01.mkv",
                 extra_videos: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    rows = [{"path": "在看/Foo/Disc 01/" + video_name,
             "file_id": "video-1", "size": 100}]
    rows.extend({"path": "在看/Foo/Disc 01/" + name, "file_id": "video-extra-{}".format(i), "size": 100}
                for i, name in enumerate(extra_videos, start=1))
    fresh = []
    for index, (name, file_id, content) in enumerate(subtitle_rows, start=1):
        rows.append({"path": "在看/Foo/Disc 01/" + name, "file_id": file_id, "size": len(content)})
        fresh.append({"name": name, "file_id": file_id, "is_dir": False, "size": len(content)})
    return inventory(rows), fresh


class FakeResponse:
    def __init__(self, body: bytes, *, status_code: int = 200,
                 content_length: str | None = None, chunks: list[bytes] | None = None,
                 headers: dict | None = None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.body = body
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int):
        if self.chunks is not None:
            yield from self.chunks
        else:
            for index in range(0, len(self.body), chunk_size):
                yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class SubtitleSidecarTests(unittest.TestCase):
    def setup_plan(self, root: Path, inv: dict, *, planned: list[dict] | None = None):
        raw = raw_config(root)
        cfg = sync.validate_config(raw)
        entries = planned if planned is not None else planned_episode()
        target = entries[0]["episodes"][0]["local_path"] if entries and entries[0].get("episodes") else ""
        plan = sidecars.plan_subtitle_sidecars(
            inv, entries, cfg, generated_strm_paths={target} if target else set(),
        )
        return raw, cfg, plan

    def test_two_language_tracks_get_distinct_chinese_sidecar_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sc = "简体 track".encode()
            tc = "繁體 track".encode()
            inv, rows = make_entries(("01.scjp.ass", "sub-sc", sc), ("01.tcjp.ass", "sub-tc", tc))
            raw, cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["planned_count"], 2)
            self.assertEqual(
                {row["target_relative_path"] for row in plan["actions"]},
                {"在看/Foo/EP01.zh.scjp.ass", "在看/Foo/EP01.zh.tcjp.ass"},
            )

            session = FakeSession([
                FakeResponse(sc, content_length=str(len(sc))),
                FakeResponse(tc, content_length=str(len(tc))),
            ])
            listed = []

            def parent_lister(raw_config, config_data, parent):
                listed.append(parent)
                return rows

            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=parent_lister, session=session,
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["created_count"], 2)
            self.assertEqual(listed, ["在看/Foo/Disc 01"])
            self.assertEqual((root / "media/在看/Foo/EP01.zh.scjp.ass").read_bytes(), sc)
            self.assertEqual((root / "media/在看/Foo/EP01.zh.tcjp.ass").read_bytes(), tc)
            self.assertEqual(len(session.calls), 2)
            for url, kwargs in session.calls:
                self.assertNotIn("reader", url)
                self.assertNotIn("must-not-leak", url)
                self.assertEqual(kwargs["auth"], ("reader", "must-not-leak"))
                self.assertFalse(kwargs["allow_redirects"])

    def test_exact_stem_takes_priority_and_does_not_invent_language_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _rows = make_entries(("01.scjp.ass", "sub", b"x"), video_name="01.scjp.mkv",
                                      extra_videos=("01.mkv",))
            planned = planned_episode(source="在看/Foo/Disc 01/01.scjp.mkv")
            raw, cfg, plan = self.setup_plan(root, inv, planned=planned)
            self.assertEqual(plan["planned_count"], 1)
            action = plan["actions"][0]
            self.assertIsNone(action["language_tag"])
            self.assertEqual(action["target_relative_path"], "在看/Foo/EP01.ass")

    def test_unknown_multiple_tags_and_video_ambiguity_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _rows = make_entries(
                ("01.ru.ass", "sub-ru", b"a"),
                ("01.scjp.tcjp.ass", "sub-multi", b"b"),
                ("01.sc.ass", "sub-ambiguous", b"c"),
                extra_videos=("01.mp4",),
            )
            raw, cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["planned_count"], 0)
            self.assertEqual(plan["skipped_by_reason"].get("unknown_or_missing_language_tag"), 1)
            self.assertEqual(plan["skipped_by_reason"].get("multiple_language_tags"), 1)
            self.assertEqual(plan["skipped_by_reason"].get("ambiguous_video_companion"), 1)

    def test_missing_id_nonpositive_size_and_over_limit_are_not_planned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _ = make_entries(("01.scjp.ass", "sub-1", b"x"))
            subtitle = inv["inventory_root"]["children"][0]["children"][0]["children"][0]["children"][1]
            subtitle.pop("file_id")
            raw, cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["planned_count"], 0)
            self.assertEqual(plan["skipped_by_reason"].get("missing_file_id"), 1)

            inv, _ = make_entries(("01.scjp.ass", "sub-1", b"x"))
            subtitle = inv["inventory_root"]["children"][0]["children"][0]["children"][0]["children"][1]
            subtitle["size"] = 0
            _raw, _cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["skipped_by_reason"].get("invalid_size"), 1)

            inv, _ = make_entries(("01.scjp.ass", "sub-1", b"x"))
            subtitle = inv["inventory_root"]["children"][0]["children"][0]["children"][0]["children"][1]
            subtitle["size"] = sidecars.MAX_SUBTITLE_BYTES + 1
            _raw, _cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["skipped_by_reason"].get("size_over_limit"), 1)

    def test_fresh_listing_must_match_id_name_type_and_size_before_get(self):
        for mismatch in (
            {"name": "01.scjp.ass", "file_id": "wrong-id", "is_dir": False, "size": 1},
            {"name": "01.scjp.ass", "file_id": "sub", "is_dir": True, "size": 1},
            {"name": "01.scjp.ass", "file_id": "sub", "is_dir": False, "size": 99},
        ):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inv, _rows = make_entries(("01.scjp.ass", "sub", b"x"))
                raw, cfg, plan = self.setup_plan(root, inv)
                session = FakeSession([])
                result = sidecars.apply_subtitle_plan(
                    plan, raw, cfg, parent_lister=lambda *_args, row=mismatch: [row], session=session,
                )
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(result["errors"][0]["code"], "source_identity_or_size_mismatch")
                self.assertEqual(session.calls, [])
                self.assertFalse((root / "media/在看/Foo/EP01.zh.scjp.ass").exists())

    def test_real_native_adapter_lists_root_and_each_ancestor_before_fresh_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = raw_config(root)
            cfg = sync.validate_config(raw)

            class FakeNative:
                def __init__(self):
                    self.credentials = type("Credentials", (), {"get": lambda _self: {"root_path": "storage-root"}})()
                    self.calls = []
                    self.rows = {
                        "storage-root": [{"fileId": "anime-id", "fileName": "Anime", "resType": 2}],
                        "anime-id": [{"fileId": "watch-id", "fileName": "在看", "resType": 2}],
                        "watch-id": [{"fileId": "foo-id", "fileName": "Foo", "resType": 2}],
                        "foo-id": [{"fileId": "disc-id", "fileName": "Disc 01", "resType": 2}],
                        "disc-id": [
                            {"fileId": "video-id", "fileName": "01.mkv", "resType": 1, "fileSize": 100},
                            {"fileId": "sub-id", "fileName": "01.scjp.ass", "resType": 1, "fileSize": 4},
                        ],
                    }

                def get_file_list(self, *, parent_id, page_size, page, **_kwargs):
                    self.calls.append(parent_id)
                    rows = self.rows[parent_id] if page == 0 else []
                    return {"data": {"total": len(self.rows[parent_id]), "list": rows}}

            fake = FakeNative()
            result = sidecars._native_parent_listing(
                raw, cfg, "在看/Foo/Disc 01", client_factory=lambda _config: fake,
            )
            self.assertEqual([row["name"] for row in result], ["01.mkv", "01.scjp.ass"])
            self.assertEqual(fake.calls, [
                "storage-root", "anime-id", "watch-id", "foo-id", "disc-id",
            ])

    def test_truncated_or_oversized_http_body_never_writes(self):
        for body, chunks in ((b"", [b"ab"]), (b"", [b"abcdx"])):
            with self.subTest(chunks=chunks), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inv, rows = make_entries(("01.scjp.ass", "sub", b"abcd"))
                raw, cfg, plan = self.setup_plan(root, inv)
                session = FakeSession([FakeResponse(body, content_length="4", chunks=chunks)])
                result = sidecars.apply_subtitle_plan(
                    plan, raw, cfg, parent_lister=lambda *_args: rows, session=session,
                )
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(result["errors"][0]["code"], "bounded_download_failed")
                self.assertFalse((root / "media/在看/Foo/EP01.zh.scjp.ass").exists())

    def test_compressed_transfer_length_does_not_replace_decoded_size_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"decoded subtitle bytes"
            inv, rows = make_entries(("01.scjp.ass", "sub", payload))
            raw, cfg, plan = self.setup_plan(root, inv)
            # A compressed HTTP Content-Length can differ from the decoded file size.
            response = FakeResponse(payload, content_length="3")
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows,
                session=FakeSession([response]),
            )
            self.assertEqual(result["created_count"], 1)
            self.assertEqual(
                (root / "media/在看/Foo/EP01.zh.scjp.ass").read_bytes(), payload,
            )

    def test_redirect_to_cdn_drops_credentials_but_accepts_verified_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"subtitle bytes"
            inv, rows = make_entries(("01.scjp.ass", "sub", payload))
            raw, cfg, plan = self.setup_plan(root, inv)
            redirect = FakeResponse(b"", status_code=302, headers={
                "Location": "https://cdn.example.test/subtitle?sig=private-signature",
            })
            session = FakeSession([redirect, FakeResponse(payload)])
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows, session=session,
            )
            self.assertEqual(result["created_count"], 1)
            self.assertEqual(len(session.calls), 2)
            _first_url, first_kwargs = session.calls[0]
            second_url, second_kwargs = session.calls[1]
            self.assertEqual(first_kwargs["auth"], ("reader", "must-not-leak"))
            self.assertIn("sig=private-signature", second_url)
            self.assertIsInstance(second_kwargs["auth"], requests.auth.AuthBase)
            self.assertIsNone(second_kwargs["headers"]["Authorization"])
            self.assertNotIn("must-not-leak", second_url)
            self.assertNotIn("reader", second_url)
            self.assertTrue(redirect.closed)

    def test_cross_origin_request_overrides_session_auth_and_authorization_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"verified"
            inv, rows = make_entries(("01.scjp.ass", "sub", payload))
            raw, cfg, plan = self.setup_plan(root, inv)

            class PreparingSession:
                def __init__(self):
                    self.inner = requests.Session()
                    self.inner.auth = ("reader", "must-not-leak")
                    self.inner.headers["Authorization"] = "Bearer session-secret"
                    self.inner.trust_env = True
                    self.responses = [
                        FakeResponse(b"", status_code=302, headers={
                            "Location": "https://cdn.example.test/subtitle?sig=private-signature",
                        }),
                        FakeResponse(payload),
                    ]
                    self.prepared = []

                def get(self, url, **kwargs):
                    request = requests.Request(
                        "GET", url, auth=kwargs.get("auth"), headers=kwargs.get("headers"),
                    )
                    self.prepared.append(self.inner.prepare_request(request))
                    return self.responses.pop(0)

            session = PreparingSession()
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows, session=session,
            )
            self.assertEqual(result["created_count"], 1)
            self.assertTrue(session.prepared[0].headers["Authorization"].startswith("Basic "))
            self.assertNotIn("Authorization", session.prepared[1].headers)

    def test_redirect_limit_invalid_scheme_userinfo_and_https_downgrade_are_blocked(self):
        cases = [
            ([FakeResponse(b"", status_code=302, headers={"Location": "file:///etc/passwd"})], 1),
            ([FakeResponse(b"", status_code=302, headers={
                "Location": "https://user:pass@cdn.example.test/subtitle",
            })], 1),
            ([FakeResponse(b"", status_code=302, headers={
                "Location": "http://cdn.example.test/subtitle",
            })], 1),
            ([FakeResponse(b"", status_code=302, headers={
                "Location": "https://cdn.example.test/secure",
            }), FakeResponse(b"", status_code=302, headers={
                "Location": "http://cdn.example.test/insecure",
            })], 2),
            ([FakeResponse(b"", status_code=302, headers={"Location": "https://cdn.example.test/again"})
              for _ in range(sidecars.MAX_SUBTITLE_REDIRECTS + 1)], sidecars.MAX_SUBTITLE_REDIRECTS + 1),
        ]
        for responses, expected_calls in cases:
            with self.subTest(redirect_count=len(responses)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inv, rows = make_entries(("01.scjp.ass", "sub", b"x"))
                raw, cfg, plan = self.setup_plan(root, inv)
                session = FakeSession(responses)
                result = sidecars.apply_subtitle_plan(
                    plan, raw, cfg, parent_lister=lambda *_args: rows, session=session,
                )
                self.assertEqual(result["failed_count"], 1)
                self.assertEqual(result["errors"][0]["code"], "bounded_download_failed")
                self.assertFalse((root / "media/在看/Foo/EP01.zh.scjp.ass").exists())
                self.assertEqual(len(session.calls), expected_calls)

    def test_existing_identical_file_is_unchanged_and_conflict_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"verified subtitles"
            inv, rows = make_entries(("01.scjp.ass", "sub", payload))
            raw, cfg, plan = self.setup_plan(root, inv)
            target = Path(cfg["media_root"]) / "在看/Foo/EP01.zh.scjp.ass"
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows,
                session=FakeSession([FakeResponse(payload, content_length=str(len(payload)))]),
            )
            self.assertEqual(result["unchanged_count"], 1)
            self.assertEqual(target.read_bytes(), payload)

            target.unlink()
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows,
                session=FakeSession([FakeResponse(payload, content_length=str(len(payload)))]),
            )
            self.assertEqual(result["created_count"], 1)
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows,
                session=FakeSession([FakeResponse(payload, content_length=str(len(payload)))]),
            )
            self.assertEqual(result["unchanged_count"], 1)
            self.assertEqual(target.read_bytes(), payload)

            target.write_bytes(b"local user's subtitle")
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows,
                session=FakeSession([FakeResponse(payload, content_length=str(len(payload)))]),
            )
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["errors"][0]["code"], "target_conflict")
            self.assertEqual(target.read_bytes(), b"local user's subtitle")

    def test_failed_parent_listing_is_reported_without_leaking_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _rows = make_entries(("01.scjp.ass", "sub", b"x"))
            raw, cfg, plan = self.setup_plan(root, inv)
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg,
                parent_lister=lambda *_args: (_ for _ in ()).throw(RuntimeError("https://secret/token")),
                session=FakeSession([]),
            )
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["errors"][0]["code"], "fresh_parent_listing_failed")
            self.assertNotIn("secret", str(result))

    def test_symlink_target_is_rejected_without_touching_outside_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, rows = make_entries(("01.scjp.ass", "sub", b"x"))
            raw, cfg, plan = self.setup_plan(root, inv)
            media = Path(cfg["media_root"])
            outside = root / "outside"
            outside.mkdir()
            (media / "在看").mkdir(parents=True)
            try:
                (media / "在看/Foo").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest("directory symlinks are unavailable: {}".format(type(exc).__name__))
            session = FakeSession([FakeResponse(b"x", content_length="1")])
            result = sidecars.apply_subtitle_plan(
                plan, raw, cfg, parent_lister=lambda *_args: rows, session=session,
            )
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["errors"][0]["code"], "unsafe_local_target")
            self.assertEqual(session.calls, [])
            self.assertEqual(list(outside.iterdir()), [])

    def test_dry_plan_has_paths_and_counts_but_never_calls_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _rows = make_entries(("01.scjp.ass", "sub", b"x"))
            raw, cfg, plan = self.setup_plan(root, inv)
            self.assertEqual(plan["planned_count"], 1)
            self.assertEqual(plan["actions"][0]["target_relative_path"], "在看/Foo/EP01.zh.scjp.ass")
            self.assertFalse((Path(cfg["media_root"]) / "在看/Foo/EP01.zh.scjp.ass").exists())
            self.assertNotIn("url", str({
                "planned": [{"source": row["source_relative_path"],
                             "target": row["target_relative_path"]} for row in plan["actions"]],
                "counts": plan["planned_count"],
            }).casefold())
            self.assertNotIn(raw["alist"]["password"], str(plan["actions"]))

    def test_unsafe_inventory_and_local_paths_do_not_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv, _rows = make_entries(("01.scjp.ass", "sub", b"x"))
            unsafe_plan = planned_episode(local_path="../../outside/EP01.strm")
            raw, cfg, plan = self.setup_plan(root, inv, planned=unsafe_plan)
            self.assertEqual(plan["planned_count"], 0)

            bad_inventory = inventory([{"path": "../outside.ass", "file_id": "sub", "size": 1}])
            with self.assertRaises(Exception):
                sidecars.plan_subtitle_sidecars(bad_inventory, [], cfg)


if __name__ == "__main__":
    unittest.main()
