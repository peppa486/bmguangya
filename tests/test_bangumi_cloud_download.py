import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest


sys.modules.setdefault('oss2', types.SimpleNamespace())

SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'bangumi_cloud_download.py'))
spec = importlib.util.spec_from_file_location(
    'bangumi_cloud_download', SCRIPT_PATH
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class BangumiCloudDownloadNamingTests(unittest.TestCase):
    def test_build_target_names_uses_numeric_episode_filename_only(self):
        folder, filename = mod.build_target_names(
            display_name='药屋少女的呢喃',
            source_title='[北宇治字幕组] 药屋少女的呢喃 [07][WebRip][1080p]',
            resolved_file_name='[北宇治字幕组] 药屋少女的呢喃 [07][WebRip][1080p].mkv',
        )
        self.assertEqual(folder, '药屋少女的呢喃')
        self.assertEqual(filename, '07.mkv')

    def test_build_target_names_keeps_bangumi_distinguishing_suffix_in_folder(self):
        folder, filename = mod.build_target_names(
            display_name='药屋少女的呢喃 第2期',
            source_title='[喵萌奶茶屋] 药屋少女的呢喃 第2期 [18][1080p]',
            resolved_file_name='[喵萌奶茶屋] 药屋少女的呢喃 第2期 [18][1080p].mp4',
        )
        self.assertEqual(folder, '药屋少女的呢喃 第2期')
        self.assertEqual(filename, '18.mp4')

    def test_build_target_names_parses_chinese_episode_marker(self):
        folder, filename = mod.build_target_names(
            display_name='宇宙兄弟',
            source_title='【恶魔岛字幕组】★4月新番【宇宙兄弟_Uchuu Kyoudai】【第99话】[GB][720P][MP4][完]',
            resolved_file_name='source.mkv',
        )
        self.assertEqual(folder, '宇宙兄弟')
        self.assertEqual(filename, '99.mkv')

    def test_build_target_names_keeps_chinese_season_suffix_in_folder(self):
        folder, filename = mod.build_target_names(
            display_name='吹响吧！上低音号 第三季',
            source_title='[北宇治字幕组] 吹响吧！上低音号 第三季 [01][1080p]',
            resolved_file_name='[北宇治字幕组] 吹响吧！上低音号 第三季 [01][1080p].mkv',
        )
        self.assertEqual(folder, '吹响吧！上低音号 第三季')
        self.assertEqual(filename, '01.mkv')


    def test_build_target_names_keeps_specials_out_of_main_episode_numbers(self):
        cases = [
            ('[字幕组] 测试番 NCOP [1080p]', '[字幕组] 测试番 NCOP [1080p].mkv', 'NCOP.mkv'),
            ('[字幕组] 测试番 NCED [1080p]', '[字幕组] 测试番 NCED [1080p].mkv', 'NCED.mkv'),
            ('[字幕组] 测试番 SP01 [1080p]', '[字幕组] 测试番 SP01 [1080p].mkv', 'SP01.mkv'),
            ('[字幕组] 测试番 OVA2 [1080p]', '[字幕组] 测试番 OVA2 [1080p].mkv', 'OVA02.mkv'),
            ('[字幕组] 测试番 OAD [1080p]', '[字幕组] 测试番 OAD [1080p].mkv', 'OAD01.mkv'),
        ]
        for source_title, resolved_file_name, expected in cases:
            with self.subTest(source_title=source_title):
                _, filename = mod.build_target_names('测试番', source_title, resolved_file_name)
                self.assertEqual(filename, expected)


class BangumiCloudDownloadOrganizationTests(unittest.TestCase):
    def test_build_subject_path_uses_chinese_collection_category(self):
        category_dir, show_dir = mod.build_subject_path('药屋少女的呢喃', 3)
        self.assertEqual(category_dir, '在看')
        self.assertEqual(show_dir, '药屋少女的呢喃')

    def test_plan_subject_sync_actions_moves_when_collection_changes(self):
        tracked_subjects = {
            '42': {
                'subject_id': '42',
                'title': '石纪元',
                'collection_type': 2,
            }
        }
        subject_locations = {
            '42': {
                'subject_id': '42',
                'collection_type': 3,
                'category_dir': '在看',
                'show_dir': '石纪元',
                'folder_id': 'folder-42',
            }
        }
        removed_subject_ids = []
        actions = mod.plan_subject_sync_actions(tracked_subjects, subject_locations, removed_subject_ids)
        self.assertEqual(
            actions,
            [
                {
                    'action': 'move',
                    'subject_id': '42',
                    'title': '石纪元',
                    'from_category': '在看',
                    'to_category': '看过',
                    'show_dir': '石纪元',
                    'folder_id': 'folder-42',
                    'collection_type': 2,
                }
            ],
        )

    def test_plan_subject_sync_actions_deletes_when_subject_removed(self):
        tracked_subjects = {}
        subject_locations = {
            '42': {
                'subject_id': '42',
                'collection_type': 3,
                'category_dir': '在看',
                'show_dir': '石纪元',
                'folder_id': 'folder-42',
            }
        }
        removed_subject_ids = ['42']
        actions = mod.plan_subject_sync_actions(tracked_subjects, subject_locations, removed_subject_ids)
        self.assertEqual(
            actions,
            [
                {
                    'action': 'delete',
                    'subject_id': '42',
                    'title': '石纪元',
                    'category_dir': '在看',
                    'show_dir': '石纪元',
                    'folder_id': 'folder-42',
                }
            ],
        )


class BangumiCloudDownloadPayloadTests(unittest.TestCase):
    def test_build_create_task_payload_uses_clean_name_and_parent(self):
        payload = mod.build_create_task_payload(
            resource_url='magnet:?xt=urn:btih:ABCDEF',
            parent_id='parent-123',
            target_name='药屋少女的呢喃 - S01E07.mkv',
        )
        self.assertEqual(
            payload,
            {
                'url': 'magnet:?xt=urn:btih:ABCDEF',
                'parentId': 'parent-123',
                'newName': '药屋少女的呢喃 - S01E07.mkv',
            },
        )


class BangumiCloudDownloadSelectionTests(unittest.TestCase):
    def test_is_single_episode_candidate_rejects_multi_episode_ranges_and_batches(self):
        self.assertTrue(mod.is_single_episode_candidate('[字幕组] 测试番 [07][1080p]'))
        self.assertFalse(mod.is_single_episode_candidate('[字幕组] 测试番 [96-97][1080p]'))
        self.assertFalse(mod.is_single_episode_candidate('【字幕组】测试番 第01-12话 合集'))
        self.assertFalse(mod.is_single_episode_candidate('【恶魔岛字幕组】★4月新番【宇宙兄弟_Uchuu Kyoudai】【第01-99话】[GB][720P][MP4][全]'))
        self.assertFalse(mod.is_single_episode_candidate('Test Show Complete Batch 01-12'))
        self.assertTrue(mod.is_single_episode_candidate('Movie Title BDRip 1080p'))


    def test_extract_episode_label_handles_season_episode_without_s01_false_positive(self):
        _, filename = mod.build_target_names(
            display_name='间谍过家家 第三季',
            source_title='[NEST] 间谍过家家 第三季 S01E06 [CR WEB-DL 1080p AVC AAC][简日内封]',
            resolved_file_name='source.mkv',
        )
        self.assertEqual(filename, '06.mkv')

    def test_pick_best_per_episode_prefers_highest_v_revision(self):
        items = [
            {
                'guid': 'v1',
                'title': '[字幕组] 测试番 - 05 [1080p][简中]',
                'torrent_url': 'https://example.com/05.torrent',
            },
            {
                'guid': 'v2',
                'title': '[字幕组] 测试番 - 05v2 [1080p][简中]',
                'torrent_url': 'https://example.com/05v2.torrent',
            },
        ]
        chosen = mod.pick_best_per_episode(
            items,
            {'require_keywords_any': ['简中'], 'exclude_keywords': []},
            locked_subgroup=None,
        )
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]['guid'], 'v2')
        self.assertEqual(chosen[0]['episode_key'], 'ep-005')
        self.assertEqual(chosen[0]['release_version'], 2)


class BangumiCloudDownloadDryRunTests(unittest.TestCase):
    def test_process_collections_dry_run_only_submits_best_item_per_episode(self):
        class FakeBangumiClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def get_combined_anime_collections(self, tracked_collection_types):
                return [
                    {
                        'subject_id': '1',
                        'collection_type': 3,
                        'ep_status': 0,
                        'subject': {'id': 1, 'name_cn': '测试番', 'name': 'Test Show'},
                    }
                ]

        class FakeMikanClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def search_bangumi(self, titles):
                return [{'bangumi_id': 'm1', 'title': '测试番'}]

            def choose_best_bangumi(self, row, candidates):
                return {'bangumi_id': 'm1', 'title': '测试番', 'score': 100}

            def fetch_rss_items(self, bangumi_id):
                return [
                    {
                        'guid': 'guid-1a',
                        'title': '[A字幕组] 测试番 [01][1080p]',
                        'torrent_url': 'https://example.com/test-show-01-a.torrent',
                    },
                    {
                        'guid': 'guid-1b',
                        'title': '[B字幕组] 测试番 [01][1080p]',
                        'torrent_url': 'https://example.com/test-show-01-b.torrent',
                    }
                ]

        original_bangumi_client = mod.BangumiClient
        original_mikan_client = mod.MikanClient
        original_apply_actions = mod.apply_subject_sync_actions
        original_plan_actions = mod.plan_subject_sync_actions
        original_partition_matcher = mod.item_matches_subject_partition
        try:
            mod.BangumiClient = FakeBangumiClient
            mod.MikanClient = FakeMikanClient
            mod.plan_subject_sync_actions = lambda *args, **kwargs: []
            mod.apply_subject_sync_actions = lambda *args, **kwargs: []
            mod.item_matches_subject_partition = lambda *args, **kwargs: True

            with tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                json.dump({}, tmp)
                tmp_path = tmp.name

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'bangumi': {},
                    'mikan': {},
                    'filters': {},
                    'tracked_collection_types': [3],
                    'download_collection_types': [3],
                },
                client=None,
                dry_run=True,
            )

            self.assertEqual(result['submitted_count'], 1)
            self.assertEqual(len(result['submitted']), 1)
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_process_collections_dry_run_does_not_persist_fake_tasks(self):
        class FakeBangumiClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def get_combined_anime_collections(self, tracked_collection_types):
                return [
                    {
                        'subject_id': '1',
                        'collection_type': 3,
                        'ep_status': 0,
                        'subject': {'id': 1, 'name_cn': '测试番', 'name': 'Test Show'},
                    }
                ]

        class FakeMikanClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def search_bangumi(self, titles):
                return [{'bangumi_id': 'm1', 'title': '测试番'}]

            def choose_best_bangumi(self, row, candidates):
                return {'bangumi_id': 'm1', 'title': '测试番', 'score': 100}

            def fetch_rss_items(self, bangumi_id):
                return [
                    {
                        'guid': 'guid-1',
                        'title': '[字幕组] 测试番 [01][1080p]',
                        'torrent_url': 'https://example.com/test-show-01.torrent',
                    }
                ]

        original_bangumi_client = mod.BangumiClient
        original_mikan_client = mod.MikanClient
        original_apply_actions = mod.apply_subject_sync_actions
        original_plan_actions = mod.plan_subject_sync_actions
        original_partition_matcher = mod.item_matches_subject_partition
        try:
            mod.BangumiClient = FakeBangumiClient
            mod.MikanClient = FakeMikanClient
            mod.plan_subject_sync_actions = lambda *args, **kwargs: []
            mod.apply_subject_sync_actions = lambda *args, **kwargs: []
            mod.item_matches_subject_partition = lambda *args, **kwargs: True

            with tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                json.dump({}, tmp)
                tmp_path = tmp.name

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'bangumi': {},
                    'mikan': {},
                    'tracked_collection_types': [3],
                    'download_collection_types': [3],
                },
                client=None,
                dry_run=True,
            )

            self.assertEqual(result['submitted_count'], 1)
            with open(tmp_path, 'r', encoding='utf-8') as fh:
                persisted = json.load(fh)
            self.assertEqual(persisted, {})
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)


    def test_dry_run_skips_existing_target_path_even_when_guid_differs(self):
        class FakeBangumiClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def get_combined_anime_collections(self, tracked_collection_types):
                return [
                    {
                        'subject_id': '1',
                        'collection_type': 3,
                        'ep_status': 0,
                        'subject': {'id': 1, 'name_cn': '测试番', 'name': 'Test Show'},
                    }
                ]

        class FakeMikanClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def search_bangumi(self, titles):
                return [{'bangumi_id': 'm1', 'title': '测试番'}]

            def choose_best_bangumi(self, row, candidates):
                return {'bangumi_id': 'm1', 'title': '测试番', 'score': 100}

            def fetch_rss_items(self, bangumi_id):
                return [
                    {
                        'guid': 'new-guid-same-episode',
                        'title': '[字幕组] 测试番 [01][1080p]',
                        'torrent_url': 'https://example.com/test-show-01-new.torrent',
                    }
                ]

        original_bangumi_client = mod.BangumiClient
        original_mikan_client = mod.MikanClient
        original_apply_actions = mod.apply_subject_sync_actions
        original_plan_actions = mod.plan_subject_sync_actions
        original_partition_matcher = mod.item_matches_subject_partition
        try:
            mod.BangumiClient = FakeBangumiClient
            mod.MikanClient = FakeMikanClient
            mod.plan_subject_sync_actions = lambda *args, **kwargs: []
            mod.apply_subject_sync_actions = lambda *args, **kwargs: []
            mod.item_matches_subject_partition = lambda *args, **kwargs: True

            initial_state = {
                'seen_guids': ['old-guid'],
                'cloud_tasks': {
                    'old-guid': {
                        'target_dir': '在看/测试番',
                        'target_name': '01.mkv',
                        'status': 'submitted',
                    }
                },
            }
            with tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                json.dump(initial_state, tmp, ensure_ascii=False)
                tmp_path = tmp.name

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'bangumi': {},
                    'mikan': {},
                    'filters': {},
                    'tracked_collection_types': [3],
                    'download_collection_types': [3],
                },
                client=None,
                dry_run=True,
            )

            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(result['skipped_existing_target_count'], 1)
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)


if __name__ == '__main__':
    unittest.main()
