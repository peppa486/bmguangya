import os
import sys
import unittest
from unittest import mock
from urllib.parse import quote


SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import bangumi_mikan_qb_sync as sync


class MikanBangumiMatchingTests(unittest.TestCase):
    def setUp(self):
        self.client = sync.MikanClient({"max_candidates_per_show": 6})

    def test_real_penguin_drum_theatrical_candidates_do_not_match_tv_subject(self):
        subject = {"subject": {
            "name_cn": "回转企鹅罐",
            "name": "輪るピングドラム",
            "eps": 24,
            "platform": "TV",
        }}
        actual_search_candidates = [
            {"bangumi_id": "2940", "title": "RE:cycle of the PENGUINDRUM [后篇] 我爱你"},
            {"bangumi_id": "2798", "title": "RE:cycle of the PENGUINDRUM [前篇] 你的列车是生存战略"},
        ]

        self.assertIsNone(self.client.choose_best_bangumi(subject, actual_search_candidates))

    def test_unique_exact_tv_title_wins_over_nearby_theatrical_candidates(self):
        subject = {"subject": {
            "name_cn": "回转企鹅罐",
            "name": "輪るピングドラム",
            "eps": 24,
            "platform": "TV",
        }}
        candidates = [
            {"bangumi_id": "2940", "title": "RE:cycle of the PENGUINDRUM [后篇] 我爱你"},
            {"bangumi_id": "2798", "title": "RE:cycle of the PENGUINDRUM [前篇] 你的列车是生存战略"},
            {"bangumi_id": "tv-1", "title": "回转企鹅罐"},
        ]

        chosen = self.client.choose_best_bangumi(subject, candidates)

        self.assertEqual(chosen["bangumi_id"], "tv-1")
        self.assertEqual(chosen["score"], 1.0)

    def test_title_similarity_and_conflicting_season_markers_do_not_select(self):
        subject = {"subject": {
            "name_cn": "测试番 第2季",
            "name": "Test Series Season 2",
            "eps": 12,
            "platform": "TV",
        }}
        candidates = [
            {"bangumi_id": "base", "title": "测试番"},
            {"bangumi_id": "s1", "title": "测试番 第1季"},
            {"bangumi_id": "movie", "title": "测试番 第2季 劇場版"},
        ]

        self.assertIsNone(self.client.choose_best_bangumi(subject, candidates))

    def test_chinese_second_season_marker_rejects_bare_first_season_alias(self):
        subject = {"subject": {
            "name_cn": "测试番 第二季",
            "name": "测试番",
            "eps": 12,
            "platform": "TV",
        }}
        candidates = [
            # Mikan's bare title is the first-season entry; the Bangumi row's
            # bare alias must not erase the explicit second-season marker.
            {"bangumi_id": "s1", "title": "测试番"},
            {"bangumi_id": "s2", "title": "测试番 第二季"},
        ]

        chosen = self.client.choose_best_bangumi(subject, candidates)

        self.assertEqual(chosen["bangumi_id"], "s2")

    def test_chinese_season_cour_and_part_numerals_are_parsed(self):
        for title, expected in (
            ("测试番 第一季", 1),
            ("测试番 第十二期", 12),
            ("测试番 第二部", 2),
            ("测试番 第三クール", 3),
        ):
            with self.subTest(title=title):
                self.assertEqual(sync._explicit_season_marker(title), expected)

    def test_explicit_override_title_is_an_exact_alias_but_keeps_season_guard(self):
        subject = {"subject": {
            "name_cn": "测试番 第二季",
            "name": "Test Series",
            "eps": 12,
            "platform": "TV",
        }}
        candidate = {"bangumi_id": "42", "title": "Mikan Alias 第二季"}

        self.assertIsNone(self.client.choose_best_bangumi(subject, [candidate]))
        selected = self.client.choose_best_bangumi(
            subject, [candidate], extra_exact_titles=["Mikan Alias 第二季"],
        )
        self.assertEqual(selected["bangumi_id"], "42")

        wrong_season = {"bangumi_id": "43", "title": "Mikan Alias 第一季"}
        self.assertIsNone(self.client.choose_best_bangumi(
            subject, [wrong_season], extra_exact_titles=["Mikan Alias 第一季"],
        ))

    def test_explicit_movie_alias_is_rejected_for_tv_even_when_title_is_exact(self):
        subject = {"subject": {
            "name_cn": "测试番 劇場版",
            "eps": 12,
            "platform": "TV",
        }}

        self.assertIsNone(self.client.choose_best_bangumi(subject, [
            {"bangumi_id": "movie", "title": "测试番 劇場版"},
        ]))

    def test_cached_old_movie_mapping_must_pass_the_same_exact_validation(self):
        subject = {"subject": {
            "name_cn": "回转企鹅罐",
            "name": "輪るピングドラム",
            "eps": 24,
            "platform": "TV",
        }}

        self.assertIsNone(sync.validate_cached_mikan_mapping(
            subject, "2940", "RE:cycle of the PENGUINDRUM [后篇] 我爱你",
        ))
        valid = sync.validate_cached_mikan_mapping(subject, "tv-1", "回转企鹅罐")
        self.assertEqual(valid["bangumi_id"], "tv-1")

    def test_search_checks_japanese_alias_and_prioritizes_its_exact_result(self):
        self.client.max_candidates = 1
        first_html = (
            '<a href="/Home/Bangumi/2940">RE:cycle of the PENGUINDRUM [后篇] 我爱你</a>'
            '<a href="/Home/Bangumi/2798">RE:cycle of the PENGUINDRUM [前篇] 你的列车是生存战略</a>'
        )
        second_html = '<a href="/Home/Bangumi/100">輪るピングドラム</a>'
        urls = []

        def fake_request(url, **kwargs):
            urls.append(url)
            if quote("回转企鹅罐") in url:
                return first_html.encode("utf-8")
            return second_html.encode("utf-8")

        subject = {"subject": {
            "name_cn": "回转企鹅罐",
            "name": "輪るピングドラム",
            "eps": 24,
            "platform": "TV",
        }}
        with mock.patch.object(sync, "request", side_effect=fake_request):
            candidates = self.client.search_bangumi(["回转企鹅罐", "輪るピングドラム"])

        self.assertEqual(len(urls), 2)
        self.assertEqual([row["bangumi_id"] for row in candidates], ["100"])
        self.assertEqual(self.client.choose_best_bangumi(subject, candidates)["bangumi_id"], "100")


if __name__ == "__main__":
    unittest.main()
