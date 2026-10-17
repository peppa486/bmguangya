import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import reconcile_cloud_library as rec


ROOT = "/guangya/Anime"
NOW = dt.datetime.now(dt.timezone.utc)
SHOW_REL = "在看/Foo"


def fnode(name, file_id):
    return {"name": name, "type": "file", "file_id": file_id}


def dnode(name, children=None, file_id=None):
    children = list(children or [])
    node = {"name": name, "type": "directory", "children": children, "empty": not children}
    if file_id:
        node["file_id"] = file_id
    return node


def make_unassigned_inventory():
    orphan = dnode("孤儿番", [fnode("loose-episode.mkv", "orphan-episode")], file_id="orphan-dir")
    watching = dnode("在看", [
        orphan,
        fnode("stray.mkv", "watching-stray"),
    ], file_id="watching-dir")
    review = dnode("待核对-在看散落", [fnode("stray.mkv", "existing-stray")])
    unmarked = dnode("未标记", [dnode("孤儿番", [], file_id="existing-orphan-dir"), review], file_id="unmarked-dir")
    root = dnode("Anime", [watching, unmarked, fnode("root-stray.mkv", "root-stray")])
    now = NOW.isoformat()
    return {
        "complete": True, "generated_at": now, "updated_at": now,
        "freshness": {"state": "fresh", "age_seconds": 0, "ttl_seconds": 3600},
        "errors": [], "source": {"cloud_root": ROOT, "cloud_refresh": True},
        "tree": [
            {"category": "在看", "shows": [{"name": "孤儿番", "subject_id": None,
                "matched": False, "ambiguous": False, "unmatched": True,
                "source_path": "在看/孤儿番", "episodes": []}],
             "loose_files": [{"name": "stray.mkv", "path": "在看/stray.mkv"}]},
            {"category": "未标记", "shows": [], "unmarked_root_files": [
                {"name": "root-stray.mkv", "relative_path": "root-stray.mkv"}]},
        ],
        "inventory_root": root,
    }


def add_numbered_duplicate_packs(inventory, *, include_ambiguous=True):
    base = "[Sakurato] Yuru Camp S3 [01-12END][HEVC-10bit 1080p AAC][CHS&CHT]"
    pack_rows = [
        dnode(base + " (1)", [fnode("01.mkv", "pack-1-ep1")], file_id="pack-dir-1"),
        dnode(base + " (94)", [fnode("01.mkv", "pack-94-ep1")], file_id="pack-dir-94"),
    ]
    category = dnode("想看", pack_rows, file_id="want-dir")
    inventory["inventory_root"]["children"].append(category)
    inventory["tree"].append({
        "category": "想看",
        "shows": [{"name": row["name"], "subject_id": None, "matched": False,
                   "ambiguous": False, "unmatched": True, "source_path": "想看/" + row["name"],
                   "episodes": []} for row in pack_rows],
    })
    if include_ambiguous:
        name = "Conflicting Bgm Title"
        for index in (1, 2):
            source = "想看/{} ({})".format(name, index)
            node = dnode("{} ({})".format(name, index), [], file_id="ambiguous-{}".format(index))
            category["children"].append(node)
            inventory["tree"][-1]["shows"].append({
                "name": node["name"], "subject_id": None, "matched": False,
                "ambiguous": True, "unmatched": False, "source_path": source, "episodes": [],
            })
    return base


def make_inventory(*, nested=True, with_subtitle=True, extra=True, target=None):
    files = [fnode("01.mkv", "video-1")]
    if with_subtitle:
        files.append(fnode("01.ass", "subtitle-1"))
    if extra:
        files.extend([fnode("trailer.mp4", "extra-1"), fnode("orphan.srt", "subtitle-extra")])
    show_children = [dnode("Disc 01", files)] if nested else list(files)
    show_children.append(dnode("Empty", []))
    if target:
        show_children.append(fnode(target, "existing-target"))
    show_node = dnode("Foo", show_children)
    ep_rel = SHOW_REL + ("/Disc 01" if nested else "") + "/01.mkv"
    sub_rel = SHOW_REL + ("/Disc 01" if nested else "") + "/01.ass"
    episode = {
        "episode_key": "EP01", "episode_id": 101, "sort": 1, "present": True, "missing": False,
        "file_id": "video-1", "relative_path": ep_rel, "file_name": "01.mkv",
        "present_files": [{"relative_path": ep_rel, "name": "01.mkv", "file_id": "video-1"}],
    }
    episodes = [episode]
    if with_subtitle:
        # Subtitle companions are discovered from the complete inventory tree.
        assert sub_rel
    tree_node = dnode("Anime", [dnode("在看", [show_node])])
    return {
        "complete": True,
        "generated_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "freshness": {"state": "fresh", "age_seconds": 0, "ttl_seconds": 3600},
        "errors": [],
        "source": {"cloud_root": ROOT, "cloud_refresh": True},
        "tree": [{"category": "在看", "shows": [{
            "name": "准确标题", "subject_id": "123", "matched": True, "ambiguous": False,
            "unmatched": False, "coverage_unknown": False, "source_path": SHOW_REL,
            "episodes": episodes, "duplicate_eps": [],
        }]}],
        "inventory_root": tree_node,
    }


def add_verified_duplicate_episode(inventory, *, second_gcid=None, second_size=100,
                                   first_gcid="a" * 40, first_size=100):
    show = inventory["tree"][0]["shows"][0]
    show["expected_eps_count"] = 1
    show["duplicate_eps"] = ["EP01"]
    episode = show["episodes"][0]
    show_node = inventory["inventory_root"]["children"][0]["children"][0]
    nested = next(child for child in show_node["children"] if child["name"] == "Disc 01")
    nested_video = next(child for child in nested["children"] if child["name"] == "01.mkv")
    nested_video.update({"size": first_size, "gcid": first_gcid})
    root_video = fnode("EP01.mkv", "video-root")
    root_video.update({"size": second_size, "gcid": second_gcid or first_gcid})
    show_node["children"].append(root_video)
    first = {"relative_path": SHOW_REL + "/Disc 01/01.mkv", "name": "01.mkv",
             "file_id": "video-1", "size": first_size, "gcid": first_gcid}
    second = {"relative_path": SHOW_REL + "/EP01.mkv", "name": "EP01.mkv",
              "file_id": "video-root", "size": second_size, "gcid": second_gcid or first_gcid}
    episode.update({"file_id": first["file_id"], "relative_path": first["relative_path"],
                    "file_name": first["name"], "present": True, "missing": False,
                    "present_files": [first, second]})
    return inventory


def add_content_variant_episode(inventory, clusters, *, shared_loser_directory=False):
    """Add ordinary EP01 copies grouped by GCID/size for variant tests.

    Each cluster is a dict with ``gcid``, ``size`` and ``copies``. A copy is a
    ``(relative_path, name, file_id)`` tuple. The base inventory's Disc 01
    video is reused when its path appears in a cluster.
    """
    show = inventory["tree"][0]["shows"][0]
    show.update({"expected_eps_count": 1, "duplicate_eps": ["EP01"]})
    episode = show["episodes"][0]
    show_node = inventory["inventory_root"]["children"][0]["children"][0]
    disc_one = next(child for child in show_node["children"] if child["name"] == "Disc 01")
    base_path = SHOW_REL + "/Disc 01/01.mkv"
    disc_two = None
    evidence = []

    for cluster in clusters:
        for relative_path, name, file_id in cluster["copies"]:
            if relative_path == base_path:
                node = next(child for child in disc_one["children"] if child["name"] == name)
            else:
                parts = relative_path.split("/")
                parent_parts = parts[:-1]
                if "/".join(parent_parts) == SHOW_REL:
                    parent = show_node
                elif "/".join(parent_parts) == SHOW_REL + "/Disc 01":
                    parent = disc_one
                elif "/".join(parent_parts) == SHOW_REL + "/Disc 02":
                    if disc_two is None:
                        disc_two = dnode("Disc 02", [], file_id="disc-two")
                        show_node["children"].append(disc_two)
                    parent = disc_two
                else:
                    raise AssertionError("unsupported test path: " + relative_path)
                node = fnode(name, file_id)
                parent["children"].append(node)
            node.update({"file_id": file_id, "size": cluster["size"], "gcid": cluster["gcid"]})
            evidence.append({"relative_path": relative_path, "path": relative_path,
                             "name": name, "file_id": file_id,
                             "size": cluster["size"], "gcid": cluster["gcid"]})

    if shared_loser_directory:
        # The caller places all non-keeper videos under Disc 01; its existing
        # 01.ass remains an exact companion for the base 01.mkv copy.
        disc_one["file_id"] = "disc-one"
    first = evidence[0]
    episode.update({"file_id": first["file_id"], "relative_path": first["relative_path"],
                    "file_name": first["name"], "present": True, "missing": False,
                    "present_files": evidence})
    return inventory


def make_verified_duplicate_pack_inventory(*, second_episode=False):
    inventory = add_verified_duplicate_episode(make_inventory(extra=False))
    show = inventory["tree"][0]["shows"][0]
    show_node = inventory["inventory_root"]["children"][0]["children"][0]
    pack = next(child for child in show_node["children"] if child["name"] == "Disc 01")
    pack["file_id"] = "disc-pack-id"
    next(child for child in pack["children"] if child["name"] == "01.ass")["size"] = 42
    if second_episode:
        pack_video = fnode("02.mkv", "video-2-copy")
        pack_video.update({"size": 200, "gcid": "b" * 40})
        pack_subtitle = fnode("02.ass", "subtitle-2-copy")
        pack_subtitle["size"] = 43
        root_video = fnode("EP02.mkv", "video-2-keeper")
        root_video.update({"size": 200, "gcid": "b" * 40})
        pack["children"].extend([pack_video, pack_subtitle])
        show_node["children"].append(root_video)
        show["duplicate_eps"].append("EP02")
        show["episodes"].append({
            "episode_key": "EP02", "episode_id": 102, "sort": 2,
            "present": True, "missing": False,
            "file_id": "video-2-copy", "relative_path": SHOW_REL + "/Disc 01/02.mkv",
            "file_name": "02.mkv",
            "present_files": [
                {"relative_path": SHOW_REL + "/Disc 01/02.mkv", "name": "02.mkv",
                 "file_id": "video-2-copy", "size": 200, "gcid": "b" * 40},
                {"relative_path": SHOW_REL + "/EP02.mkv", "name": "EP02.mkv",
                 "file_id": "video-2-keeper", "size": 200, "gcid": "b" * 40},
            ],
        })
    return inventory


def make_whole_pack_duplicate_inventory():
    """Two numbered sibling packs with fully identical, hash-backed manifests."""
    inventory = make_verified_duplicate_pack_inventory()
    show_node = inventory["inventory_root"]["children"][0]["children"][0]
    source_pack = next(child for child in show_node["children"] if child["name"] == "Disc 01")
    source_pack["name"] = "Pack (2)"
    source_video = next(child for child in source_pack["children"] if child["name"] == "01.mkv")
    source_subtitle = next(child for child in source_pack["children"] if child["name"] == "01.ass")
    source_subtitle.update({"size": 42, "gcid": "c" * 40})
    source_extra = fnode("bonus.mp4", "source-bonus")
    source_extra.update({"size": 300, "gcid": "d" * 40})
    source_pack["children"].append(source_extra)

    root_video = next(child for child in show_node["children"] if child["name"] == "EP01.mkv")
    show_node["children"].remove(root_video)
    root_video.update({"name": "01.mkv", "file_id": "peer-video", "size": 100, "gcid": "a" * 40})
    peer_subtitle = fnode("01.ass", "peer-subtitle")
    peer_subtitle.update({"size": 42, "gcid": "c" * 40})
    peer_extra = fnode("bonus.mp4", "peer-bonus")
    peer_extra.update({"size": 300, "gcid": "d" * 40})
    peer_pack = dnode("Pack (1)", [root_video, peer_subtitle, peer_extra], file_id="peer-pack-id")
    show_node["children"].append(peer_pack)

    episode = inventory["tree"][0]["shows"][0]["episodes"][0]
    source_path = SHOW_REL + "/Pack (2)/01.mkv"
    peer_path = SHOW_REL + "/Pack (1)/01.mkv"
    episode.update({
        "file_id": source_video["file_id"],
        "relative_path": source_path,
        "file_name": "01.mkv",
        "present_files": [
            {"relative_path": source_path, "name": "01.mkv", "file_id": source_video["file_id"],
             "size": 100, "gcid": "a" * 40},
            {"relative_path": peer_path, "name": "01.mkv", "file_id": "peer-video",
             "size": 100, "gcid": "a" * 40},
        ],
    })
    return inventory


def add_duplicate_review_name_collision(inventory):
    title_directory = dnode("准确标题", [fnode("Disc 01", "existing-review-item")],
                            file_id="review-title-dir")
    duplicate_review = dnode("待核对-重复内容", [title_directory], file_id="duplicate-review-dir")
    unmarked = dnode("未标记", [duplicate_review], file_id="unmarked-review-dir")
    inventory["inventory_root"]["children"].append(unmarked)
    return inventory


def config(tmp, *, nested=True):
    return rec.normalize_config({
        "alist": {"api_base": "http://alist.test:5244", "root": ROOT, "token": "secret"},
        "inventory": {"output": str(Path(tmp) / "library.json"), "freshness_ttl_seconds": 3600},
        "reconcile": {"report": str(Path(tmp) / "reconcile.json")},
    })


def abs_path(relative):
    return ROOT + "/" + relative


class FakeAList:
    def __init__(self, *, moved=False, fail_list=False):
        self.fail_list = fail_list
        self.refresh_values = []
        self.writes = []
        self.dirs = {
            abs_path("在看"): [dnode("Foo")],
            abs_path(SHOW_REL): [],
            abs_path(SHOW_REL + "/Disc 01"): [],
            abs_path(SHOW_REL + "/Empty"): [],
        }
        self.dirs[abs_path(SHOW_REL)][0:0] = []
        if moved:
            self.dirs[abs_path(SHOW_REL)].append(fnode("01.mkv", "video-1"))
        else:
            self.dirs[abs_path(SHOW_REL + "/Disc 01")].append(fnode("01.mkv", "video-1"))
        self.dirs[abs_path(SHOW_REL + "/Disc 01")].append(fnode("01.ass", "subtitle-1"))

    def list_all(self, path, *, refresh=True):
        self.refresh_values.append(refresh)
        if self.fail_list:
            raise rec.ReconcileError("simulated list timeout", code="alist_timeout")
        if path not in self.dirs:
            raise rec.ReconcileError("directory missing: " + path, code="missing_dir")
        return copy.deepcopy(self.dirs[path])

    def move_file(self, source_dir, target_dir, name):
        self.writes.append(("move", source_dir, target_dir, name))
        matches = [row for row in self.dirs[source_dir] if row["name"] == name]
        if len(matches) != 1:
            raise rec.ReconcileError("source missing", code="source_missing")
        row = matches[0]
        self.dirs[source_dir].remove(row)
        self.dirs[target_dir].append(row)
        return {"code": 200}

    def rename_file(self, source_path, new_name):
        self.writes.append(("rename", source_path, new_name))
        parent, old_name = source_path.rsplit("/", 1)
        matches = [row for row in self.dirs[parent] if row["name"] == old_name]
        if len(matches) != 1:
            raise rec.ReconcileError("source missing", code="source_missing")
        if any(row["name"].casefold() == new_name.casefold() for row in self.dirs[parent]):
            raise rec.ReconcileError("target conflict", code="target_conflict")
        matches[0]["name"] = new_name
        return {"code": 200}


class MemoryAList:
    """In-memory directory tree for exercising opt-in organization writes."""
    def __init__(self, root_node):
        self.dirs = {}
        self.writes = []
        self.refresh_values = []

        def index(node, path):
            rows = copy.deepcopy(node.get("children", []))
            self.dirs[path] = rows
            for child in rows:
                if child.get("type") == "directory":
                    index(child, path + "/" + child["name"])

        index(root_node, ROOT)

    def list_all(self, path, *, refresh=True):
        self.refresh_values.append(refresh)
        if path not in self.dirs:
            raise rec.ReconcileError("directory missing: " + path, code="missing_dir")
        return copy.deepcopy(self.dirs[path])

    def make_dir(self, path):
        self.writes.append(("mkdir", path))
        parent, name = path.rsplit("/", 1)
        if parent not in self.dirs or any(row["name"].casefold() == name.casefold() for row in self.dirs[parent]):
            raise rec.ReconcileError("mkdir target conflict", code="mkdir_conflict")
        self.dirs[parent].append(dnode(name, file_id="dir-" + str(len(self.dirs))))
        self.dirs[path] = []
        return {"code": 200}

    def rename_file(self, source_path, new_name):
        self.writes.append(("rename", source_path, new_name))
        parent, old_name = source_path.rsplit("/", 1)
        rows = self.dirs[parent]
        matches = [row for row in rows if row["name"] == old_name]
        if len(matches) != 1 or any(row["name"].casefold() == new_name.casefold() for row in rows):
            raise rec.ReconcileError("rename source/target conflict", code="rename_conflict")
        row = matches[0]
        row["name"] = new_name
        if row.get("type") == "directory":
            old_prefix = source_path
            new_prefix = parent + "/" + new_name
            for old_path in list(self.dirs):
                if old_path == old_prefix or old_path.startswith(old_prefix + "/"):
                    self.dirs[new_prefix + old_path[len(old_prefix):]] = self.dirs.pop(old_path)
        return {"code": 200}

    def move_file(self, source_dir, target_dir, name):
        self.writes.append(("move", source_dir, target_dir, name))
        if source_dir not in self.dirs or target_dir not in self.dirs:
            raise rec.ReconcileError("move directory missing", code="move_dir_missing")
        matches = [row for row in self.dirs[source_dir] if row["name"] == name]
        if len(matches) != 1 or any(row["name"].casefold() == name.casefold() for row in self.dirs[target_dir]):
            raise rec.ReconcileError("move source/target conflict", code="move_conflict")
        row = matches[0]
        self.dirs[source_dir].remove(row)
        self.dirs[target_dir].append(row)
        if row.get("type") == "directory":
            old_prefix = source_dir + "/" + name
            new_prefix = target_dir + "/" + name
            for old_path in list(self.dirs):
                if old_path == old_prefix or old_path.startswith(old_prefix + "/"):
                    self.dirs[new_prefix + old_path[len(old_prefix):]] = self.dirs.pop(old_path)
        return {"code": 200}


class ReconcilePlanTests(unittest.TestCase):
    def test_builds_safe_video_and_exact_subtitle_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory()
            cfg = config(tmp)
            plan = rec.build_plan(inventory, cfg)
        video = next(row for row in plan["actions"] if row["role"] == "video")
        subtitle = next(row for row in plan["actions"] if row["role"] == "subtitle")
        self.assertEqual(video["target_relative_path"], SHOW_REL + "/EP01.mkv")
        self.assertEqual(video["source_relative_path"], SHOW_REL + "/Disc 01/01.mkv")
        self.assertEqual(subtitle["target_relative_path"], SHOW_REL + "/EP01.ass")
        self.assertEqual(subtitle["requires_verified_video"], video["action_id"])
        self.assertTrue(video["move_required"])
        self.assertTrue(video["rename_required"])
        self.assertEqual({row["name"] for row in plan["unassigned"]}, {"trailer.mp4"})
        self.assertIn("unpaired_subtitle", {row["code"] for row in plan["unresolved"]})
        self.assertEqual(plan["empty_directories"][0]["relative_path"], SHOW_REL + "/Empty")
        self.assertTrue(all(row["action"] == "none" for row in plan["empty_directories"]))

    def test_conflicts_and_multiple_file_evidence_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            inventory = make_inventory(target="EP01.mkv")
            plan = rec.build_plan(inventory, cfg)
            video = next(row for row in plan["actions"] if row["role"] == "video")
            self.assertEqual(video["state"], "blocked")
            self.assertEqual(video["blocked"]["code"], "target_conflict")

            inventory = make_inventory(with_subtitle=False, extra=False)
            episode = inventory["tree"][0]["shows"][0]["episodes"][0]
            episode["present_files"].append(dict(episode["present_files"][0], file_id="duplicate"))
            plan = rec.build_plan(inventory, cfg)
            self.assertEqual(plan["actions"], [])
            self.assertIn("not_single_file", {row["code"] for row in plan["unresolved"]})

    def test_same_gcid_and_size_quarantines_duplicate_episode_and_exact_subtitle(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_verified_duplicate_episode(make_inventory(extra=False))
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)

        duplicate_video = next(row for row in plan["actions"]
                               if row.get("role") == "duplicate_content_video")
        duplicate_subtitle = next(row for row in plan["actions"]
                                  if row.get("role") == "duplicate_content_subtitle")
        self.assertEqual(duplicate_video["source_relative_path"], SHOW_REL + "/Disc 01/01.mkv")
        self.assertEqual(duplicate_video["kept_relative_path"], SHOW_REL + "/EP01.mkv")
        self.assertEqual(duplicate_video["target_relative_path"],
                         "未标记/待核对-重复内容/准确标题/01.mkv")
        self.assertEqual(duplicate_video["content_gcid"], "a" * 40)
        self.assertEqual(duplicate_video["content_size"], 100)
        self.assertEqual(duplicate_subtitle["requires_verified_video"], duplicate_video["action_id"])
        self.assertEqual(duplicate_subtitle["source_relative_path"], SHOW_REL + "/Disc 01/01.ass")
        self.assertEqual(duplicate_subtitle["target_relative_path"],
                         "未标记/待核对-重复内容/准确标题/01.ass")
        self.assertFalse(any(row.get("code") in {"not_single_file", "duplicate_episode", "unpaired_subtitle"}
                             and row.get("episode_key") == "EP01" for row in plan["unresolved"]))

    def test_duplicate_content_is_deduplicated_per_verified_content_cluster(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_content_variant_episode(make_inventory(extra=False), [
                {"gcid": "a" * 40, "size": 100, "copies": [
                    (SHOW_REL + "/Disc 01/01.mkv", "01.mkv", "video-1"),
                    (SHOW_REL + "/EP01.mkv", "EP01.mkv", "variant-a-root"),
                ]},
                {"gcid": "b" * 40, "size": 200, "copies": [
                    (SHOW_REL + "/Disc 02/01-b.mkv", "01-b.mkv", "variant-b-copy"),
                    (SHOW_REL + "/EP01-b.mkv", "EP01-b.mkv", "variant-b-root"),
                ]},
            ])
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)

        duplicate_videos = [row for row in plan["actions"]
                            if row.get("role") == "duplicate_content_video"]
        self.assertEqual(len(duplicate_videos), 2)
        by_gcid = {row["content_gcid"]: row for row in duplicate_videos}
        self.assertEqual(set(by_gcid), {"a" * 40, "b" * 40})
        self.assertEqual(by_gcid["a" * 40]["content_size"], 100)
        self.assertEqual(by_gcid["a" * 40]["kept_relative_path"], SHOW_REL + "/EP01.mkv")
        self.assertEqual(by_gcid["a" * 40]["source_relative_path"], SHOW_REL + "/Disc 01/01.mkv")
        self.assertEqual(by_gcid["b" * 40]["content_size"], 200)
        self.assertEqual(by_gcid["b" * 40]["kept_relative_path"], SHOW_REL + "/EP01-b.mkv")
        self.assertEqual(by_gcid["b" * 40]["source_relative_path"], SHOW_REL + "/Disc 02/01-b.mkv")
        variants = next(row for row in plan["unresolved"]
                        if row.get("code") == "duplicate_content_variants")
        self.assertEqual([row["copy_count"] for row in variants["content_variants"]], [2, 2])
        self.assertFalse(any(row.get("role") == "video" and row.get("episode_key") == "EP01"
                             for row in plan["actions"]))

    def test_duplicate_content_variant_with_2_plus_1_only_moves_the_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_content_variant_episode(make_inventory(extra=False), [
                {"gcid": "a" * 40, "size": 100, "copies": [
                    (SHOW_REL + "/Disc 01/01.mkv", "01.mkv", "video-1"),
                    (SHOW_REL + "/EP01.mkv", "EP01.mkv", "variant-a-root"),
                ]},
                {"gcid": "b" * 40, "size": 200, "copies": [
                    (SHOW_REL + "/EP01-b.mkv", "EP01-b.mkv", "variant-b-only"),
                ]},
            ])
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)

        duplicate_videos = [row for row in plan["actions"]
                            if row.get("role") == "duplicate_content_video"]
        self.assertEqual(len(duplicate_videos), 1)
        self.assertEqual(duplicate_videos[0]["file_id"], "video-1")
        self.assertEqual(duplicate_videos[0]["content_gcid"], "a" * 40)
        self.assertEqual(duplicate_videos[0]["content_size"], 100)
        self.assertEqual(duplicate_videos[0]["kept_relative_path"], SHOW_REL + "/EP01.mkv")
        self.assertFalse(any(row.get("file_id") == "variant-b-only" for row in plan["actions"]))
        variants = next(row for row in plan["unresolved"]
                        if row.get("code") == "duplicate_content_variants")
        self.assertEqual([row["copy_count"] for row in variants["content_variants"]], [2, 1])

    def test_distinct_hash_singletons_stay_blocked_and_skip_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_content_variant_episode(make_inventory(extra=False), [
                {"gcid": "a" * 40, "size": 100, "copies": [
                    (SHOW_REL + "/Disc 01/01.mkv", "01.mkv", "video-1"),
                ]},
                {"gcid": "b" * 40, "size": 200, "copies": [
                    (SHOW_REL + "/EP01.mkv", "EP01.mkv", "variant-b-only"),
                ]},
            ])
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)

        variants = next(row for row in plan["unresolved"]
                        if row.get("code") == "duplicate_content_variants")
        self.assertEqual(variants["state"], "blocked")
        self.assertEqual([row["copy_count"] for row in variants["content_variants"]], [1, 1])
        self.assertFalse(any(row.get("subject_id") == "123" and row.get("episode_key") == "EP01"
                             for row in plan["actions"]))

    def test_duplicate_content_ambiguous_path_or_invalid_hash_fails_entire_episode(self):
        cases = ("invalid_hash", "ambiguous_path")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                inventory = add_content_variant_episode(make_inventory(extra=False), [
                    {"gcid": "a" * 40, "size": 100, "copies": [
                        (SHOW_REL + "/Disc 01/01.mkv", "01.mkv", "video-1"),
                        (SHOW_REL + "/EP01.mkv", "EP01.mkv", "variant-a-root"),
                    ]},
                    {"gcid": "b" * 40, "size": 200, "copies": [
                        (SHOW_REL + "/Disc 02/01-b.mkv", "01-b.mkv", "variant-b-copy"),
                        (SHOW_REL + "/EP01-b.mkv", "EP01-b.mkv", "variant-b-root"),
                    ]},
                ])
                episode = inventory["tree"][0]["shows"][0]["episodes"][0]
                if case == "invalid_hash":
                    evidence = next(row for row in episode["present_files"]
                                    if row["file_id"] == "variant-b-copy")
                    evidence["gcid"] = "invalid"
                else:
                    # The same path component resolves to a file and directory,
                    # so the nested video path cannot be uniquely verified.
                    inventory["inventory_root"]["children"][0]["children"][0]["children"].append(
                        fnode("Disc 01", "same-name-file"))
                cfg = config(tmp)
                cfg["organize_unassigned"] = True
                plan = rec.build_plan(inventory, cfg)

                self.assertFalse(any(row.get("role", "").startswith("duplicate_content_")
                                     for row in plan["actions"]))
                self.assertIn("duplicate_content_unverified", {row.get("code") for row in plan["unresolved"]})

    def test_directory_collapse_accepts_distinct_clusters_with_all_keepers_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_content_variant_episode(make_inventory(extra=False), [
                {"gcid": "a" * 40, "size": 100, "copies": [
                    (SHOW_REL + "/Disc 01/01.mkv", "01.mkv", "video-1"),
                    (SHOW_REL + "/EP01.mkv", "EP01.mkv", "variant-a-root"),
                ]},
                {"gcid": "b" * 40, "size": 200, "copies": [
                    (SHOW_REL + "/Disc 01/01-b.mkv", "01-b.mkv", "variant-b-copy"),
                    (SHOW_REL + "/EP01-b.mkv", "EP01-b.mkv", "variant-b-root"),
                ]},
            ], shared_loser_directory=True)
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            cfg["collapse_verified_duplicate_directories"] = True
            plan = rec.build_plan(inventory, cfg)

        directory = next(row for row in plan["actions"]
                         if row.get("role") == "duplicate_content_directory")
        self.assertEqual(directory["source_relative_path"], SHOW_REL + "/Disc 01")
        video_evidence = [row for row in directory["covered_duplicate_evidence"]
                          if row.get("role") == "duplicate_content_video"]
        self.assertEqual({row["content_gcid"] for row in video_evidence}, {"a" * 40, "b" * 40})
        self.assertEqual({row["kept_relative_path"] for row in video_evidence},
                         {SHOW_REL + "/EP01.mkv", SHOW_REL + "/EP01-b.mkv"})
        self.assertTrue(all(not row["kept_relative_path"].startswith(directory["source_relative_path"] + "/")
                            for row in video_evidence))
        self.assertEqual(len(directory["covered_action_ids"]), 3)

    def test_verified_directory_collapse_is_opt_in_and_keeps_full_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_verified_duplicate_pack_inventory(second_episode=True)
            cfg = config(tmp)
            self.assertFalse(cfg["collapse_verified_duplicate_directories"])
            cfg["collapse_verified_duplicate_directories"] = True
            disabled = rec.build_plan(inventory, cfg)
            self.assertFalse(any(row.get("role") == "duplicate_content_directory"
                                 for row in disabled["actions"]))

            cfg["organize_unassigned"] = True
            cfg["collapse_verified_duplicate_directories"] = False
            per_file = rec.build_plan(inventory, cfg)
            self.assertFalse(any(row.get("role") == "duplicate_content_directory"
                                 for row in per_file["actions"]))
            cfg["collapse_verified_duplicate_directories"] = True
            collapsed = rec.build_plan(inventory, cfg)

        directory_actions = [row for row in collapsed["actions"]
                             if row.get("role") == "duplicate_content_directory"]
        self.assertEqual(len(directory_actions), 1)
        action = directory_actions[0]
        self.assertEqual(action["source_kind"], "directory")
        self.assertEqual(action["source_relative_path"], SHOW_REL + "/Disc 01")
        self.assertEqual(action["target_relative_path"], "未标记/待核对-重复内容/准确标题/Disc 01")
        self.assertEqual(len(action["covered_action_ids"]), 4)
        self.assertEqual(len(action["covered_duplicate_evidence"]), 4)
        self.assertEqual({row["relative_path"] for row in action["directory_manifest"]},
                         {"01.mkv", "01.ass", "02.mkv", "02.ass"})
        self.assertEqual({row["file_id"] for row in action["directory_manifest"]},
                         {"video-1", "subtitle-1", "video-2-copy", "subtitle-2-copy"})
        self.assertFalse(any(row.get("role") in {"duplicate_content_video", "duplicate_content_subtitle"}
                             for row in collapsed["actions"]))
        duplicate_rows = [row for row in collapsed["unassigned"]
                          if row.get("relative_path") in {SHOW_REL + "/Disc 01/01.mkv",
                                                          SHOW_REL + "/Disc 01/02.mkv"}]
        self.assertTrue(all(row["state"] == "planned" for row in duplicate_rows))
        self.assertTrue(all(row["covered_by_action_id"] == action["action_id"] for row in duplicate_rows))

    def test_identical_numbered_pack_manifest_proves_uncovered_extras_safe_to_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_whole_pack_duplicate_inventory()
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            cfg["collapse_verified_duplicate_directories"] = True
            plan = rec.build_plan(inventory, cfg)

        actions = [row for row in plan["actions"]
                   if row.get("role") == "duplicate_content_directory"]
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["source_relative_path"], SHOW_REL + "/Pack (2)")
        self.assertEqual(action["kept_directory_relative_path"], SHOW_REL + "/Pack (1)")
        self.assertEqual(action["kept_directory_id"], "peer-pack-id")
        self.assertEqual(action["whole_pack_duplicate_proof"]["method"],
                         "sibling_complete_gcid_size_manifest")
        self.assertEqual(action["whole_pack_duplicate_proof"]["additional_video_count"], 1)
        self.assertEqual(action["whole_pack_duplicate_proof"]["additional_file_count"], 1)
        self.assertEqual(action["whole_pack_duplicate_proof"]["additional_video_relative_paths"],
                         ["bonus.mp4"])
        self.assertEqual({row["relative_path"] for row in action["kept_directory_manifest"]},
                         {"01.mkv", "01.ass", "bonus.mp4"})
        self.assertFalse(any(row.get("source_relative_path") == SHOW_REL + "/Pack (1)"
                             and row.get("role") == "duplicate_content_directory"
                             for row in plan["actions"]))

    def test_whole_pack_proof_rejects_changed_shape_hash_or_missing_evidence(self):
        mutations = ("extra_hash", "extra_size", "extra_shape", "extra_missing_gcid", "extra_missing_size")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                inventory = make_whole_pack_duplicate_inventory()
                show_node = inventory["inventory_root"]["children"][0]["children"][0]
                peer = next(child for child in show_node["children"] if child["name"] == "Pack (1)")
                peer_extra = next(child for child in peer["children"] if child["name"] == "bonus.mp4")
                if mutation == "extra_hash":
                    peer_extra["gcid"] = "e" * 40
                elif mutation == "extra_size":
                    peer_extra["size"] = 301
                elif mutation == "extra_shape":
                    peer["children"].remove(peer_extra)
                elif mutation == "extra_missing_gcid":
                    peer_extra.pop("gcid")
                else:
                    peer_extra["size"] = 0
                cfg = config(tmp)
                cfg["organize_unassigned"] = True
                cfg["collapse_verified_duplicate_directories"] = True
                plan = rec.build_plan(inventory, cfg)

                self.assertFalse(any(row.get("role") == "duplicate_content_directory"
                                     for row in plan["actions"]))
                self.assertTrue(any(row.get("role") == "duplicate_content_video"
                                    for row in plan["actions"]))

    def test_keeper_unique_video_or_missing_identity_prevents_directory_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            keeper_inside = make_verified_duplicate_pack_inventory()
            show_node = keeper_inside["inventory_root"]["children"][0]["children"][0]
            pack = next(child for child in show_node["children"] if child["name"] == "Disc 01")
            keeper = next(child for child in show_node["children"] if child["name"] == "EP01.mkv")
            show_node["children"].remove(keeper)
            pack["children"].append(keeper)
            keeper_evidence = keeper_inside["tree"][0]["shows"][0]["episodes"][0]["present_files"][1]
            keeper_evidence["relative_path"] = SHOW_REL + "/Disc 01/EP01.mkv"
            keeper_inside["tree"][0]["shows"][0]["episodes"][0]["file_name"] = "01.mkv"

            unique_video = make_verified_duplicate_pack_inventory()
            unique_pack = next(child for child in unique_video["inventory_root"]["children"][0]["children"][0]["children"]
                               if child["name"] == "Disc 01")
            unique_pack["children"].append(fnode("bonus.mp4", "unique-video"))

            unknown_hash = add_verified_duplicate_episode(make_inventory(extra=False), second_gcid="b" * 40)
            unknown_pack = next(child for child in unknown_hash["inventory_root"]["children"][0]["children"][0]["children"]
                                if child["name"] == "Disc 01")
            unknown_pack["file_id"] = "unknown-hash-pack"

            unknown_aux_id = make_verified_duplicate_pack_inventory()
            aux_pack = next(child for child in unknown_aux_id["inventory_root"]["children"][0]["children"][0]["children"]
                            if child["name"] == "Disc 01")
            aux_pack["children"].append(fnode("cover.jpg", None))

            for inventory in (keeper_inside, unique_video, unknown_hash, unknown_aux_id):
                cfg = config(tmp)
                cfg["organize_unassigned"] = True
                cfg["collapse_verified_duplicate_directories"] = True
                plan = rec.build_plan(inventory, cfg)
                self.assertFalse(any(row.get("role") == "duplicate_content_directory"
                                     for row in plan["actions"]))

    def test_different_or_missing_content_evidence_keeps_every_duplicate_in_place(self):
        cases = (
            ({"second_gcid": "b" * 40}, "duplicate_content_variants"),
            ({"second_size": 101}, "duplicate_content_variants"),
            ({"first_size": 0}, "duplicate_content_unverified"),
            ({"first_gcid": None}, "duplicate_content_unverified"),
        )
        for kwargs, expected_issue in cases:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                inventory = add_verified_duplicate_episode(make_inventory(extra=False), **kwargs)
                cfg = config(tmp)
                cfg["organize_unassigned"] = True
                plan = rec.build_plan(inventory, cfg)
                self.assertFalse(any(row.get("role", "").startswith("duplicate_content_")
                                     for row in plan["actions"]))
                self.assertFalse(any(row.get("target_relative_path", "").startswith(
                    "未标记/待核对-重复内容/") for row in plan["actions"]))
                self.assertIn(expected_issue, {row["code"] for row in plan["unresolved"]})
                if expected_issue == "duplicate_content_unverified":
                    self.assertIn("not_single_file", {row["code"] for row in plan["unresolved"]})

    def test_unmatched_and_unknown_files_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            inventory = make_inventory(with_subtitle=False, extra=False)
            inventory["tree"][0]["shows"][0]["episodes"][0]["episode_key"] = "unknown"
            inventory["tree"][0]["shows"][0]["episodes"][0]["episode_id"] = None
            plan = rec.build_plan(inventory, cfg)
            self.assertEqual(plan["actions"], [])
            self.assertIn("unknown_episode_key", {row["code"] for row in plan["unresolved"]})

    def test_ep00_and_special_labels_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show = inventory["tree"][0]["shows"][0]
            ep = show["episodes"][0]
            ep.update({"episode_key": "EP00", "sort": 0, "relative_path": SHOW_REL + "/01.mkv"})
            ep["present_files"][0]["relative_path"] = SHOW_REL + "/01.mkv"
            special_node = fnode("special-source.mp4", "special-1")
            inventory["inventory_root"]["children"][0]["children"][0]["children"].append(special_node)
            show["episodes"].append({
                "episode_key": "SP01", "episode_id": None, "is_special": True, "present": True,
                "missing": False, "file_id": "special-1", "relative_path": SHOW_REL + "/special-source.mp4",
                "file_name": "special-source.mp4", "present_files": [{
                    "relative_path": SHOW_REL + "/special-source.mp4", "name": "special-source.mp4",
                    "file_id": "special-1",
                }],
            })
            plan = rec.build_plan(inventory, cfg)
            names = {row["episode_key"]: row["target_name"] for row in plan["actions"]}
            self.assertEqual(names, {"EP00": "EP00.mkv", "SP01": "SP01.mp4"})

    def test_unknown_extension_is_blocked_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show_node = inventory["inventory_root"]["children"][0]["children"][0]
            show_node["children"][0]["name"] = "01.zip"
            episode = inventory["tree"][0]["shows"][0]["episodes"][0]
            episode["relative_path"] = SHOW_REL + "/01.zip"
            episode["file_name"] = "01.zip"
            episode["present_files"][0].update({"relative_path": SHOW_REL + "/01.zip", "name": "01.zip"})
            plan = rec.build_plan(inventory, cfg)
            self.assertEqual(plan["actions"], [])
            self.assertIn("unsupported_file", {row["code"] for row in plan["unresolved"]})

    def test_unassigned_organization_is_opt_in_and_preserves_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_unassigned_inventory()
            cfg = config(tmp)
            disabled = rec.build_plan(inventory, cfg)
            self.assertFalse(disabled["organization_enabled"])
            self.assertFalse(any(row.get("kind", "").startswith("organize_") for row in disabled["actions"]))

            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)
        self.assertTrue(plan["organization_enabled"])
        mkdirs = [row for row in plan["actions"] if row.get("kind") == "organize_mkdir"]
        moves = [row for row in plan["actions"] if row.get("kind") == "organize_move"]
        self.assertEqual([row["target_relative_path"] for row in mkdirs], ["未标记/待核对-根目录散落"])
        by_source = {row["source_relative_path"]: row for row in moves}
        self.assertEqual(by_source["在看/孤儿番"]["target_relative_path"], "未标记/孤儿番 [from-在看]")
        self.assertEqual(by_source["在看/stray.mkv"]["target_relative_path"],
                         "未标记/待核对-在看散落/stray [from-在看].mkv")
        self.assertEqual(by_source["root-stray.mkv"]["target_relative_path"],
                         "未标记/待核对-根目录散落/root-stray.mkv")
        self.assertEqual(plan["unresolved"], [])

    def test_numbered_duplicate_unclaimed_directories_share_review_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_unassigned_inventory()
            base = add_numbered_duplicate_packs(inventory)
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)

        grouped = [row for row in plan["actions"]
                   if row.get("organization_group") == "duplicate-numbered-directory"]
        self.assertEqual(len(grouped), 2)
        self.assertEqual({row["organization_group_base"] for row in grouped}, {base})
        self.assertEqual({row["target_dir_relative"] for row in grouped}, {
            "未标记/待核对-重复资源/" + base,
        })
        self.assertEqual({row["target_name"] for row in grouped}, {base + " (1)", base + " (94)"})
        self.assertFalse(any("Conflicting Bgm Title" in row.get("source_relative_path", "")
                             for row in plan["actions"]))
        self.assertEqual({row["relative_path"] for row in plan["unresolved"]
                          if row.get("code") == "ambiguous_unclaimed_directory"}, {
            "想看/Conflicting Bgm Title (1)", "想看/Conflicting Bgm Title (2)",
        })

    def test_reliable_out_of_bangumi_episode_and_exact_subtitle_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show = inventory["tree"][0]["shows"][0]
            show.update({"matched": True, "ambiguous": False, "unmatched": False,
                         "coverage_unknown": False, "expected_eps_count": 12, "total_eps": 12})
            show["extra_eps"] = [{"name": "22.mkv", "relative_path": "在看/Foo/22.mkv",
                                  "file_id": "range-video", "parsed_episode": "EP22",
                                  "reason": "episode_not_in_bangumi", "is_special": False}]
            foo = inventory["inventory_root"]["children"][0]["children"][0]
            foo["children"].extend([fnode("22.mkv", "range-video"), fnode("22.ass", "range-subtitle"),
                                    fnode("SP01.mp4", "special-video")])
            show["extra_eps"].append({"name": "SP01.mp4", "relative_path": "在看/Foo/SP01.mp4",
                                      "file_id": "special-video", "parsed_episode": None,
                                      "reason": "episode_not_in_bangumi", "is_special": True})
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)
        range_actions = [row for row in plan["actions"] if row.get("reason") == "out_of_bangumi_range"]
        by_role = {row["role"]: row for row in range_actions}
        self.assertEqual(set(by_role), {"out_of_range_video", "out_of_range_subtitle"})
        self.assertEqual(by_role["out_of_range_subtitle"]["requires_verified_video"],
                         by_role["out_of_range_video"]["action_id"])
        self.assertEqual(by_role["out_of_range_video"]["target_relative_path"],
                         "未标记/待核对-准确标题/22.mkv")
        self.assertEqual(by_role["out_of_range_subtitle"]["target_relative_path"],
                         "未标记/待核对-准确标题/22.ass")
        self.assertEqual(plan["out_of_range"][0]["reason"], "out_of_bangumi_range")
        self.assertNotIn("unpaired_subtitle", {row.get("code") for row in plan["unresolved"]})
        self.assertNotIn("special-video", {row.get("file_id") for row in range_actions})

    def test_out_of_range_unknown_coverage_and_duplicate_rows_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show = inventory["tree"][0]["shows"][0]
            show.update({"matched": True, "ambiguous": False, "unmatched": False,
                         "coverage_unknown": True, "expected_eps_count": None})
            show["extra_eps"] = [{"name": "22.mkv", "relative_path": "在看/Foo/22.mkv",
                                  "file_id": "range-video", "parsed_episode": "EP22",
                                  "reason": "episode_not_in_bangumi", "is_special": False}]
            foo = inventory["inventory_root"]["children"][0]["children"][0]
            foo["children"].append(fnode("22.mkv", "range-video"))
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            plan = rec.build_plan(inventory, cfg)
            self.assertFalse(any(row.get("file_id") == "range-video" for row in plan["actions"]))
            self.assertIn("out_of_range_not_confident", {row["code"] for row in plan["unresolved"]})

            show["coverage_unknown"] = False
            show["expected_eps_count"] = 12
            foo["children"].append(fnode("EP22-alt.mkv", "range-video-2"))
            show["extra_eps"].append({"name": "EP22-alt.mkv", "relative_path": "在看/Foo/EP22-alt.mkv",
                                      "file_id": "range-video-2", "parsed_episode": "EP22",
                                      "reason": "episode_not_in_bangumi", "is_special": False})
            plan = rec.build_plan(inventory, cfg)
        self.assertFalse(any(row.get("file_id") in {"range-video", "range-video-2"} for row in plan["actions"]))
        self.assertIn("duplicate_out_of_range_episode", {row["code"] for row in plan["unresolved"]})


class ReconcileExecutionTests(unittest.TestCase):
    def _run(self, tmp, inventory, client=None, *, apply=False, report=None,
             organize_unassigned=False, collapse_duplicate_directories=False):
        cfg_path = Path(tmp) / "runtime.json"
        inv_path = Path(tmp) / "library.json"
        report_path = report or Path(tmp) / "reconcile.json"
        cfg_path.write_text(json.dumps({
            "alist": {"api_base": "http://alist.test:5244", "root": ROOT, "token": "secret"},
            "inventory": {"output": str(inv_path), "freshness_ttl_seconds": 3600},
            "reconcile": {"report": str(report_path), "organize_unassigned": organize_unassigned,
                          "collapse_verified_duplicate_directories": collapse_duplicate_directories},
        }), encoding="utf-8")
        inv_path.write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
        return rec.run_reconcile(cfg_path, inv_path, report_path, apply=apply, client=client, now=NOW)

    def test_dry_run_writes_plan_without_cloud_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeAList()
            report, code = self._run(tmp, make_inventory(), client)
            self.assertEqual(code, 0)
            self.assertEqual(client.writes, [])
            self.assertEqual(report["mode"], "dry-run")
            self.assertTrue(Path(report["report_path"]).exists())
            self.assertTrue(report["complete"])

    def test_apply_moves_and_renames_video_and_companion_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeAList()
            report, code = self._run(tmp, make_inventory(extra=False), client, apply=True)
            self.assertEqual(code, 0)
            self.assertEqual([row[0] for row in client.writes], ["move", "rename", "move", "rename"])
            self.assertEqual([row for row in client.dirs[abs_path(SHOW_REL)] if row["type"] == "file"],
                             [fnode("EP01.mkv", "video-1"), fnode("EP01.ass", "subtitle-1")])
            self.assertEqual(client.dirs[abs_path(SHOW_REL + "/Disc 01")], [])
            self.assertTrue(all(refresh is True for refresh in client.refresh_values))
            self.assertEqual(report["action_counts"]["done"], 2)
            self.assertEqual(report["emptied_source_directories"][0]["action"], "none")

    def test_live_target_conflict_never_calls_a_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(with_subtitle=False, extra=False, target="EP01.mkv")
            client = FakeAList()
            report, code = self._run(tmp, inventory, client, apply=True)
            self.assertEqual(code, 0)
            self.assertEqual(client.writes, [])
            self.assertTrue(report["blocked"])

    def test_pending_move_is_reconciled_without_blind_move_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(with_subtitle=False, extra=False)
            client = FakeAList(moved=True)
            cfg = config(tmp)
            plan = rec.build_plan(inventory, cfg)
            action = plan["actions"][0]
            report_path = Path(tmp) / "reconcile.json"
            report_path.write_text(json.dumps({"actions": [{
                "action_id": action["action_id"], "state": "pending", "pending_step": "move", "steps": [],
            }]}), encoding="utf-8")
            report, code = self._run(tmp, inventory, client, apply=True, report=report_path)
            self.assertEqual(code, 0)
            self.assertEqual(client.writes, [("rename", abs_path(SHOW_REL + "/01.mkv"), "EP01.mkv")])
            self.assertEqual(report["actions"][0]["state"], "done")
            self.assertTrue(report["actions"][0]["recovered_from_pending"] if report["actions"][0].get("recovered_from_pending") else
                            any(event["state"] == "verified_after_restart" for event in report["actions"][0]["steps"]))

    def test_pending_journal_survives_unavailable_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(with_subtitle=False, extra=False)
            client = FakeAList(fail_list=True)
            cfg = config(tmp)
            action = rec.build_plan(inventory, cfg)["actions"][0]
            report_path = Path(tmp) / "reconcile.json"
            report_path.write_text(json.dumps({"actions": [{
                "action_id": action["action_id"], "state": "pending", "pending_step": "move", "steps": [],
            }]}), encoding="utf-8")
            report, code = self._run(tmp, inventory, client, apply=True, report=report_path)
            self.assertEqual(code, 2)
            self.assertEqual(report["actions"][0]["state"], "pending")
            self.assertEqual(report["actions"][0]["pending_step"], "move")
            self.assertEqual(client.writes, [])

    def test_pending_evidence_survives_interrupt_after_first_replan_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(with_subtitle=False, extra=False)
            cfg = config(tmp)
            action = rec.build_plan(inventory, cfg)["actions"][0]
            report_path = Path(tmp) / "reconcile.json"
            steps = [{"step": "move", "state": "request_started", "at": "2026-10-17T00:00:00Z"}]
            prior_action = dict(action, state="pending", pending_step="move",
                                pending_since="2026-10-17T00:00:00Z",
                                pending_error={"code": "response_lost", "message": "unconfirmed"},
                                steps=steps)
            report_path.write_text(json.dumps({"actions": [prior_action]}), encoding="utf-8")

            original_write = rec._write_journal
            writes = 0

            def write_then_interrupt(path, report):
                nonlocal writes
                original_write(path, report)
                writes += 1
                if writes == 1:
                    raise KeyboardInterrupt("simulated process stop after initial plan journal")

            with mock.patch.object(rec, "_write_journal", side_effect=write_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self._run(tmp, inventory, FakeAList(), apply=True, report=report_path)

            interrupted = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(interrupted["actions"][0]["state"], "pending")
            self.assertEqual(interrupted["actions"][0]["pending_step"], "move")
            self.assertEqual(interrupted["actions"][0]["steps"], steps)
            self.assertEqual(interrupted["actions"][0]["pending_error"]["code"], "response_lost")

            client = FakeAList(fail_list=True)
            report, code = self._run(tmp, inventory, client, apply=True, report=report_path)
            self.assertEqual(code, 2)
            self.assertEqual(report["actions"][0]["state"], "pending")
            self.assertEqual(report["actions"][0]["pending_step"], "move")
            self.assertEqual(report["actions"][0]["steps"], steps)
            self.assertEqual(client.writes, [])

    def test_unmatched_pending_is_retained_and_blocks_new_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_inventory = make_inventory(with_subtitle=False, extra=False)
            cfg = config(tmp)
            old_action = rec.build_plan(original_inventory, cfg)["actions"][0]
            report_path = Path(tmp) / "reconcile.json"
            steps = [{"step": "move", "state": "request_started", "at": "2026-10-17T00:00:00Z"}]
            prior_action = dict(old_action, state="pending", pending_step="move", steps=steps)
            report_path.write_text(json.dumps({"actions": [prior_action]}), encoding="utf-8")

            inventory = copy.deepcopy(original_inventory)
            inventory["tree"][0]["shows"][0]["episodes"][0]["file_id"] = "replacement-video"
            inventory["tree"][0]["shows"][0]["episodes"][0]["present_files"][0]["file_id"] = "replacement-video"
            disc = inventory["inventory_root"]["children"][0]["children"][0]["children"][0]
            disc["children"][0]["file_id"] = "replacement-video"
            client = FakeAList()

            report, code = self._run(tmp, inventory, client, apply=True, report=report_path)

            self.assertEqual(code, 2)
            self.assertEqual(client.writes, [])
            carried = next(row for row in report["actions"] if row["action_id"] == old_action["action_id"])
            self.assertEqual(carried["state"], "pending")
            self.assertEqual(carried["pending_step"], "move")
            self.assertEqual(carried["steps"], steps)
            self.assertEqual(carried["pending_recovery_status"], "action_not_in_new_plan")
            replacement = next(row for row in report["actions"] if row["file_id"] == "replacement-video")
            self.assertEqual(replacement["state"], "blocked")
            self.assertEqual(replacement["blocked"]["code"], "unmatched_pending_journal")
            self.assertIn("pending_action_not_in_new_plan", {row["code"] for row in report["errors"]})

    def test_organization_apply_journals_and_verifies_moves_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_unassigned_inventory()
            client = MemoryAList(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True, organize_unassigned=True)
            self.assertEqual(code, 0)
            self.assertTrue(report["organization_enabled"])
            self.assertFalse(report["errors"])
            self.assertEqual(report["action_counts"]["done"], 4)
            self.assertTrue(any(write[0] == "mkdir" for write in client.writes))
            self.assertTrue(any(write[0] == "rename" for write in client.writes))
            self.assertIn("/guangya/Anime/未标记/孤儿番 [from-在看]", client.dirs)
            self.assertIn("/guangya/Anime/未标记/待核对-根目录散落", client.dirs)
            moved_files = {row["name"] for row in client.dirs["/guangya/Anime/未标记/待核对-在看散落"]}
            self.assertEqual(moved_files, {"stray [from-在看].mkv", "stray.mkv"})
            self.assertEqual(client.dirs["/guangya/Anime/未标记/待核对-根目录散落"][0]["name"],
                             "root-stray.mkv")
            self.assertFalse(any(write[0] == "delete" for write in client.writes))

    def test_collapsed_duplicate_directory_uses_stable_collision_name_and_preserves_existing_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_duplicate_review_name_collision(
                make_verified_duplicate_pack_inventory(second_episode=True))
            client = MemoryAList(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 0)
        action = next(row for row in report["actions"]
                      if row.get("role") == "duplicate_content_directory")
        self.assertTrue(action["target_name"].startswith("Disc 01 [dir-"))
        self.assertEqual(action["state"], "done")
        target = abs_path(action["target_relative_path"].rsplit("/", 1)[0])
        self.assertEqual({row["name"] for row in client.dirs[target]}, {"Disc 01", action["target_name"]})
        self.assertEqual(next(row for row in client.dirs[target] if row["name"] == "Disc 01")["file_id"],
                         "existing-review-item")
        self.assertIn(abs_path(action["target_relative_path"]), client.dirs)
        self.assertFalse(any(row[0] == "delete" for row in client.writes))

    def test_live_added_subtree_entry_blocks_collapsed_directory_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_verified_duplicate_pack_inventory(second_episode=True)
            client = MemoryAList(inventory["inventory_root"])
            source_subtree = abs_path(SHOW_REL + "/Disc 01")
            client.dirs[source_subtree].append(fnode("new-unique.mp4", "live-added-video"))
            report, code = self._run(tmp, inventory, client, apply=True,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 2)
        action = next(row for row in report["actions"]
                      if row.get("role") == "duplicate_content_directory")
        self.assertEqual(action["state"], "blocked")
        self.assertEqual(action["blocked"]["code"], "directory_manifest_changed")
        self.assertIn("live subtree entry count", action["blocked"]["message"])
        self.assertFalse(any(write[0] == "move" and write[1] == abs_path(SHOW_REL)
                             and write[3] == "Disc 01" for write in client.writes))
        self.assertIn("new-unique.mp4", {row["name"] for row in client.dirs[source_subtree]})

    def test_live_gcid_change_blocks_collapsed_directory_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_verified_duplicate_pack_inventory()
            client = MemoryAList(inventory["inventory_root"])
            source_subtree = abs_path(SHOW_REL + "/Disc 01")
            next(row for row in client.dirs[source_subtree] if row["name"] == "01.mkv")["gcid"] = "c" * 40
            report, code = self._run(tmp, inventory, client, apply=True,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 2)
        action = next(row for row in report["actions"]
                      if row.get("role") == "duplicate_content_directory")
        self.assertEqual(action["state"], "blocked")
        self.assertEqual(action["blocked"]["code"], "directory_manifest_changed")
        self.assertIn("GCID changed", action["blocked"]["message"])
        self.assertFalse(any(write[0] == "move" and write[1] == abs_path(SHOW_REL)
                             and write[3] == "Disc 01" for write in client.writes))

    def test_live_kept_pack_change_blocks_whole_pack_directory_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_whole_pack_duplicate_inventory()
            client = MemoryAList(inventory["inventory_root"])
            peer_path = abs_path(SHOW_REL + "/Pack (1)")
            next(row for row in client.dirs[peer_path] if row["name"] == "bonus.mp4")["gcid"] = "e" * 40
            report, code = self._run(tmp, inventory, client, apply=True,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 2)
        action = next(row for row in report["actions"]
                      if row.get("role") == "duplicate_content_directory")
        self.assertEqual(action["state"], "blocked")
        self.assertEqual(action["blocked"]["code"], "directory_manifest_changed")
        self.assertIn("live kept-peer subtree GCID changed", action["blocked"]["message"])
        self.assertFalse(any(write[0] == "move" and write[1] == abs_path(SHOW_REL)
                             and write[3] == "Pack (2)" for write in client.writes))

    def test_pending_whole_pack_move_recovers_before_reading_peer_or_source_subtree(self):
        class SourceAndPeerSubtreeUnavailable(MemoryAList):
            unavailable_paths = set()

            def list_all(self, path, *, refresh=True):
                if path in self.unavailable_paths:
                    raise rec.ReconcileError("subtree is no longer readable", code="missing_dir")
                return super().list_all(path, refresh=refresh)

        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_whole_pack_duplicate_inventory()
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            cfg["collapse_verified_duplicate_directories"] = True
            action = next(row for row in rec.build_plan(inventory, cfg)["actions"]
                          if row.get("role") == "duplicate_content_directory")
            client = SourceAndPeerSubtreeUnavailable(inventory["inventory_root"])
            client.make_dir(abs_path("未标记"))
            client.make_dir(abs_path("未标记/待核对-重复内容"))
            client.make_dir(abs_path("未标记/待核对-重复内容/准确标题"))
            client.move_file(action["source_dir"], action["target_dir"], action["original_name"])
            client.unavailable_paths.update({action["source_path"], action["kept_directory_path"]})
            writes_before_recovery = list(client.writes)
            report_path = Path(tmp) / "reconcile.json"
            pending = dict(action, state="pending", pending_step="organize_move",
                           current_name=action["original_name"], pending_since=NOW.isoformat(), steps=[])
            report_path.write_text(json.dumps({"actions": [pending]}), encoding="utf-8")

            report, code = self._run(tmp, inventory, client, apply=True, report=report_path,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 0)
        recovered = next(row for row in report["actions"]
                         if row.get("role") == "duplicate_content_directory")
        self.assertEqual(recovered["state"], "done")
        self.assertTrue(recovered["recovered_from_pending"])
        self.assertEqual(client.writes, writes_before_recovery)

    def test_pending_collapsed_move_recovers_from_target_id_without_source_manifest(self):
        class SourceSubtreeUnavailable(MemoryAList):
            unavailable_paths = set()

            def list_all(self, path, *, refresh=True):
                if path in self.unavailable_paths:
                    raise rec.ReconcileError("source subtree is no longer readable", code="missing_dir")
                return super().list_all(path, refresh=refresh)

        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_verified_duplicate_pack_inventory(second_episode=True)
            cfg = config(tmp)
            cfg["organize_unassigned"] = True
            cfg["collapse_verified_duplicate_directories"] = True
            action = next(row for row in rec.build_plan(inventory, cfg)["actions"]
                          if row.get("role") == "duplicate_content_directory")
            client = SourceSubtreeUnavailable(inventory["inventory_root"])
            client.make_dir(abs_path("未标记"))
            client.make_dir(abs_path("未标记/待核对-重复内容"))
            client.make_dir(abs_path("未标记/待核对-重复内容/准确标题"))
            client.move_file(action["source_dir"], action["target_dir"], action["original_name"])
            client.unavailable_paths.add(action["source_path"])
            writes_before_recovery = list(client.writes)
            report_path = Path(tmp) / "reconcile.json"
            pending = dict(action, state="pending", pending_step="organize_move",
                           current_name=action["original_name"], pending_since=NOW.isoformat(), steps=[])
            report_path.write_text(json.dumps({"actions": [pending]}), encoding="utf-8")

            report, code = self._run(tmp, inventory, client, apply=True, report=report_path,
                                     organize_unassigned=True, collapse_duplicate_directories=True)

        self.assertEqual(code, 0)
        recovered = next(row for row in report["actions"]
                         if row.get("role") == "duplicate_content_directory")
        self.assertEqual(recovered["state"], "done")
        self.assertTrue(recovered["recovered_from_pending"])
        self.assertEqual(client.writes, writes_before_recovery)

    def test_numbered_duplicate_packs_apply_as_preserved_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_unassigned_inventory()
            base = add_numbered_duplicate_packs(inventory, include_ambiguous=False)
            client = MemoryAList(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True, organize_unassigned=True)

        self.assertEqual(code, 0)
        grouped = [row for row in report["actions"]
                   if row.get("organization_group") == "duplicate-numbered-directory"]
        self.assertEqual(len(grouped), 2)
        self.assertTrue(all(row["state"] == "done" for row in grouped))
        parent = "/guangya/Anime/未标记/待核对-重复资源/" + base
        self.assertEqual({row["name"] for row in client.dirs[parent]}, {base + " (1)", base + " (94)"})
        self.assertEqual(client.dirs[parent + "/" + base + " (1)"][0]["name"], "01.mkv")
        self.assertEqual(client.dirs[parent + "/" + base + " (94)"][0]["name"], "01.mkv")
        self.assertFalse(any(write[0] == "delete" for write in client.writes))

    def test_out_of_range_video_and_subtitle_apply_to_review_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show = inventory["tree"][0]["shows"][0]
            show.update({"matched": True, "ambiguous": False, "unmatched": False,
                         "coverage_unknown": False, "expected_eps_count": 12, "total_eps": 12})
            show["extra_eps"] = [{"name": "22.mkv", "relative_path": "在看/Foo/22.mkv",
                                  "file_id": "range-video", "parsed_episode": "EP22",
                                  "reason": "episode_not_in_bangumi", "is_special": False}]
            foo = inventory["inventory_root"]["children"][0]["children"][0]
            foo["children"].extend([fnode("22.mkv", "range-video"), fnode("22.ass", "range-subtitle")])
            client = MemoryAList(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True, organize_unassigned=True)
            self.assertEqual(code, 0)
            self.assertEqual(report["out_of_range"][0]["target_relative_path"],
                             "未标记/待核对-准确标题/22.mkv")
            self.assertEqual(report["out_of_range"][0]["state"], "done")
            target = "/guangya/Anime/未标记/待核对-准确标题"
            self.assertEqual({row["name"] for row in client.dirs[target]}, {"22.mkv", "22.ass"})
            self.assertIn("EP01.mkv", {row["name"] for row in client.dirs["/guangya/Anime/在看/Foo"]})

    def test_out_of_range_subtitle_stays_put_when_video_move_is_not_verified(self):
        class FailedOutOfRangeVideo(MemoryAList):
            def move_file(self, source_dir, target_dir, name):
                if name == "22.mkv":
                    self.writes.append(("move_failed", source_dir, target_dir, name))
                    raise rec.ReconcileError("simulated rejected video move", code="quota_rejected")
                return super().move_file(source_dir, target_dir, name)

        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory(nested=False, with_subtitle=False, extra=False)
            show = inventory["tree"][0]["shows"][0]
            show.update({"matched": True, "ambiguous": False, "unmatched": False,
                         "coverage_unknown": False, "expected_eps_count": 12, "total_eps": 12})
            show["extra_eps"] = [{"name": "22.mkv", "relative_path": "在看/Foo/22.mkv",
                                  "file_id": "range-video", "parsed_episode": "EP22",
                                  "reason": "episode_not_in_bangumi", "is_special": False}]
            foo = inventory["inventory_root"]["children"][0]["children"][0]
            foo["children"].extend([fnode("22.mkv", "range-video"), fnode("22.ass", "range-subtitle")])
            client = FailedOutOfRangeVideo(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True, organize_unassigned=True)

        self.assertEqual(code, 2)
        self.assertIn("22.ass", {row["name"] for row in client.dirs["/guangya/Anime/在看/Foo"]})
        self.assertNotIn("22.ass", {row["name"] for row in client.dirs["/guangya/Anime/未标记/待核对-准确标题"]})
        subtitle = next(row for row in report["actions"] if row.get("role") == "out_of_range_subtitle")
        self.assertEqual(subtitle["state"], "blocked")
        self.assertEqual(subtitle["blocked"]["code"], "video_not_verified")

    def test_duplicate_content_subtitle_stays_put_when_video_move_is_not_verified(self):
        class FailedDuplicateVideo(MemoryAList):
            def move_file(self, source_dir, target_dir, name):
                if name == "01.mkv":
                    self.writes.append(("move_failed", source_dir, target_dir, name))
                    raise rec.ReconcileError("simulated rejected duplicate move", code="quota_rejected")
                return super().move_file(source_dir, target_dir, name)

        with tempfile.TemporaryDirectory() as tmp:
            inventory = add_verified_duplicate_episode(make_inventory(extra=False))
            client = FailedDuplicateVideo(inventory["inventory_root"])
            report, code = self._run(tmp, inventory, client, apply=True, organize_unassigned=True)

        self.assertEqual(code, 2)
        source = "/guangya/Anime/在看/Foo/Disc 01"
        target = "/guangya/Anime/未标记/待核对-重复内容/准确标题"
        self.assertIn("01.ass", {row["name"] for row in client.dirs[source]})
        self.assertNotIn("01.ass", {row["name"] for row in client.dirs[target]})
        subtitle = next(row for row in report["actions"]
                        if row.get("role") == "duplicate_content_subtitle")
        self.assertEqual(subtitle["state"], "blocked")
        self.assertEqual(subtitle["blocked"]["code"], "video_not_verified")

    def test_incomplete_or_stale_inventory_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory()
            inventory["complete"] = False
            report, code = self._run(tmp, inventory)
            self.assertEqual(code, 1)
            self.assertFalse(report["complete"])
            inventory["complete"] = True
            inventory["generated_at"] = (NOW - dt.timedelta(hours=2)).isoformat()
            report, code = self._run(tmp, inventory)
            self.assertEqual(code, 1)
            self.assertEqual(report["errors"][0]["code"], "inventory_stale")

    def test_dry_run_does_not_overwrite_pending_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = make_inventory()
            cfg = config(tmp)
            action = rec.build_plan(inventory, cfg)["actions"][0]
            report_path = Path(tmp) / "reconcile.json"
            prior = {"actions": [{"action_id": action["action_id"], "state": "pending", "pending_step": "move"}]}
            report_path.write_text(json.dumps(prior), encoding="utf-8")
            report, code = self._run(tmp, inventory, report=report_path)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), prior)
            self.assertTrue(Path(str(report_path) + ".failure.json").exists())


class AListClientTests(unittest.TestCase):
    class Response:
        status_code = 200
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self.payload

    class Session:
        def __init__(self, payloads):
            self.payloads = list(payloads)
            self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return AListClientTests.Response(self.payloads.pop(0))

    def test_list_all_pages_and_business_code_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            cfg["alist"]["page_size"] = 2
            session = self.Session([
                {"code": 200, "data": {"total": 3, "content": [fnode("a.mkv", "a"), fnode("b.mkv", "b")]}},
                {"code": 200, "data": {"total": 3, "content": [fnode("c.mkv", "c")]}},
            ])
            rows = rec.AListClient(cfg, session=session).list_all(ROOT, refresh=True)
            self.assertEqual(len(rows), 3)
            self.assertEqual([call[1]["json"]["page"] for call in session.calls], [1, 2])
            self.assertEqual([call[1]["json"]["refresh"] for call in session.calls], [True, False])

            bad = self.Session([{"code": 500, "message": "denied"}])
            with self.assertRaisesRegex(rec.ReconcileError, "business code was not 200"):
                rec.AListClient(cfg, session=bad).list_all(ROOT)


if __name__ == "__main__":
    unittest.main()
