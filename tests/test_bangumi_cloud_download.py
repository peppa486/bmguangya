import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


sys.modules.setdefault('oss2', types.SimpleNamespace())

SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'bangumi_cloud_download.py'))
spec = importlib.util.spec_from_file_location(
    'bangumi_cloud_download', SCRIPT_PATH
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def write_inventory(path, *, subject_id='1', episode_key='EP01', present=False, missing=True,
                    matched=False, unmatched=True, ambiguous=False, coverage_unknown=False,
                    episodes=None):
    show = {
        'subject_id': subject_id,
        'name': '测试番',
        'matched': matched,
        'unmatched': unmatched,
        'ambiguous': ambiguous,
        'coverage_unknown': coverage_unknown,
        'source_path': '在看/测试番' if matched else None,
        'episodes': episodes or [{
            'episode_id': 101,
            'sort': 0 if episode_key == 'EP00' else 1,
            'episode_key': episode_key,
            'present': present,
            'missing': missing,
            'relative_path': None,
            'file_id': None,
            'file_name': None,
        }],
    }
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({
            'complete': True,
            'freshness': {'state': 'fresh', 'age_seconds': 0, 'ttl_seconds': 3600},
            'source': {'cloud_root': '/guangya/Anime'},
            'tree': [{'category': '在看', 'shows': [show]}],
        }, handle, ensure_ascii=False)


class BangumiCloudDownloadNamingTests(unittest.TestCase):
    def test_build_target_names_uses_numeric_episode_filename_only(self):
        folder, filename = mod.build_target_names(
            display_name='药屋少女的呢喃',
            source_title='[北宇治字幕组] 药屋少女的呢喃 [07][WebRip][1080p]',
            resolved_file_name='[北宇治字幕组] 药屋少女的呢喃 [07][WebRip][1080p].mkv',
        )
        self.assertEqual(folder, '药屋少女的呢喃')
        self.assertEqual(filename, 'EP07.mkv')

    def test_build_target_names_keeps_bangumi_distinguishing_suffix_in_folder(self):
        folder, filename = mod.build_target_names(
            display_name='药屋少女的呢喃 第2期',
            source_title='[喵萌奶茶屋] 药屋少女的呢喃 第2期 [18][1080p]',
            resolved_file_name='[喵萌奶茶屋] 药屋少女的呢喃 第2期 [18][1080p].mp4',
        )
        self.assertEqual(folder, '药屋少女的呢喃 第2期')
        self.assertEqual(filename, 'EP18.mp4')

    def test_build_target_names_parses_chinese_episode_marker(self):
        folder, filename = mod.build_target_names(
            display_name='宇宙兄弟',
            source_title='【恶魔岛字幕组】★4月新番【宇宙兄弟_Uchuu Kyoudai】【第99话】[GB][720P][MP4][完]',
            resolved_file_name='source.mkv',
        )
        self.assertEqual(folder, '宇宙兄弟')
        self.assertEqual(filename, 'EP99.mkv')

    def test_build_target_names_keeps_chinese_season_suffix_in_folder(self):
        folder, filename = mod.build_target_names(
            display_name='吹响吧！上低音号 第三季',
            source_title='[北宇治字幕组] 吹响吧！上低音号 第三季 [01][1080p]',
            resolved_file_name='[北宇治字幕组] 吹响吧！上低音号 第三季 [01][1080p].mkv',
        )
        self.assertEqual(folder, '吹响吧！上低音号 第三季')
        self.assertEqual(filename, 'EP01.mkv')

    def test_episode_zero_is_kept_as_ep00(self):
        folder, filename = mod.build_target_names(
            display_name='测试番',
            source_title='[字幕组] 测试番 第0话',
            resolved_file_name='00.mkv',
        )
        self.assertEqual(folder, '测试番')
        self.assertEqual(filename, 'EP00.mkv')


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

    def test_plan_subject_sync_actions_moves_removed_subject_to_unmarked(self):
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
                    'action': 'move',
                    'subject_id': '42',
                    'title': '石纪元',
                    'from_category': '在看',
                    'to_category': '未标记',
                    'show_dir': '石纪元',
                    'folder_id': 'folder-42',
                    'collection_type': 0,
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
        self.assertEqual(filename, 'EP06.mkv')

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

    def test_sidecar_subtitles_are_rejected_until_they_can_be_preserved(self):
        self.assertFalse(mod.is_single_episode_candidate('[Group] 测试番 [01][外挂字幕]'))
        self.assertTrue(mod.has_external_subtitle_files({
            'subfiles': [{'fileName': '01.ass'}, {'fileName': '01.mkv'}],
        }))
        self.assertFalse(mod.has_external_subtitle_files({
            'subfiles': [{'fileName': '01.mkv'}],
        }))

    def test_unnumbered_single_resolution_requires_an_explicit_file_list(self):
        self.assertTrue(mod.is_single_video_resolution({
            'subfiles': [{'fileIndex': 0, 'fileName': 'Movie.mkv'}],
        }))
        self.assertFalse(mod.is_single_video_resolution({'fileName': 'Movie.mkv'}))
        self.assertFalse(mod.is_single_video_resolution({
            'fileName': 'Movie.mkv', 'subfiles': [],
        }))

    def test_batch_candidate_can_cover_only_the_missing_tail_of_known_subject(self):
        episodes = [
            {'episode_id': i, 'episode_key': f'EP{i:02d}',
             'present': i <= 5, 'missing': i > 5}
            for i in range(1, 13)
        ]
        episodes.append({'episode_id': None, 'episode_key': 'SP01',
                         'present': True, 'missing': False})
        show = {'episodes': episodes, 'coverage_unknown': False}
        missing = {episode['episode_key']: episode for episode in episodes if episode['missing']}
        subject = {'subject_id': '1', 'collection_type': 3,
                   'subject': {'id': 1, 'name_cn': '测试番', 'name': 'Test Show', 'eps': 12}}
        items = [{
            'guid': 'batch-1',
            'title': '[Group] 测试番 [01-12] [简中][1080p]',
            'torrent_url': 'https://example.com/test-show-01-12.torrent',
        }]

        candidate = mod.choose_exact_batch_candidate(
            items, {}, missing, show, subject, [], max_episode_count=52,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['batch_episode_keys'], [f'EP{i:02d}' for i in range(6, 13)])
        self.assertIsNone(mod.choose_exact_batch_candidate(
            [{**items[0], 'title': '[Group] 测试番 [01-24] [简中]'}],
            {}, missing, show, subject, [], max_episode_count=52,
        ))
        self.assertIsNone(mod.choose_exact_batch_candidate(
            items, {}, missing, show, subject, [], max_episode_count=6,
        ))

    def test_batch_file_selection_returns_only_unique_indexes_for_missing_episodes(self):
        subfiles = [
            {'fileIndex': i - 1, 'fileName': f'[Group] 测试番 [{i:02d}].mkv'}
            for i in range(1, 13)
        ]
        expected = [f'EP{i:02d}' for i in range(6, 13)]

        self.assertEqual(mod.choose_batch_video_file_indexes(
            {'subfiles': subfiles}, expected,
        ), list(range(5, 12)))
        self.assertIsNone(mod.choose_batch_video_file_indexes(
            {'subfiles': subfiles[:-1]}, expected,
        ))
        self.assertIsNone(mod.choose_batch_video_file_indexes(
            {'subfiles': subfiles + [dict(subfiles[5], fileIndex=99)]}, expected,
        ))
        self.assertEqual(mod.choose_batch_video_file_indexes(
            {'subfiles': [{'fileIndex': 8, 'fileName': '09.mkv'}]}, ['EP09'],
        ), [8])
        self.assertIsNone(mod.choose_batch_video_file_indexes(
            {'subfiles': subfiles}, ['EP06', 'EP06'],
        ))

    def test_batch_selection_obeys_latest_episode_limit_for_airing_shows(self):
        from datetime import date

        missing = {
            f'EP{i:02d}': {'episode_key': f'EP{i:02d}'}
            for i in range(6, 13)
        }
        subject = {'subject': {'date': date.today().isoformat(), 'eps': 12}}
        limited = mod.limit_batch_missing_episodes(missing, subject, {})
        self.assertEqual(list(limited), ['EP12'])
        self.assertEqual(mod.limit_batch_missing_episodes(
            missing, subject, {'limit_airing_subjects_to_latest': False},
        ), missing)


class BangumiCloudDownloadMikanOverrideTests(unittest.TestCase):
    def test_override_config_validation_is_strict_and_per_subject(self):
        overrides, errors = mod.parse_mikan_subject_overrides({
            'subject_overrides': {
                '1': {'bangumi_id': '42', 'title': ' Mikan Alias '},
                '2': {'bangumi_id': 43, 'title': 'Numeric JSON ID'},
                '3': {'bangumi_id': 'not-numeric', 'title': 'Invalid ID'},
                '4': {'bangumi_id': '44', 'title': '   '},
                '5': {'bangumi_id': '45', 'title': 'Unexpected key', 'other': True},
            },
        })
        self.assertEqual(overrides, {
            '1': {'bangumi_id': '42', 'title': 'Mikan Alias'},
            '2': {'bangumi_id': '43', 'title': 'Numeric JSON ID'},
        })
        self.assertEqual(errors, {
            '3': 'mikan_override_invalid_id',
            '4': 'mikan_override_invalid_title',
            '5': 'mikan_override_invalid_structure',
        })

        for config in (
            {'subject_overrides': None},
            {'subject_overrides': []},
            {'subject_overrides': {'not-numeric': {'bangumi_id': '42', 'title': 'Alias'}}},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                mod.parse_mikan_subject_overrides(config)

    def test_override_search_must_verify_the_configured_id_and_exact_title(self):
        mikan = mod.MikanClient({})
        override = {'bangumi_id': '42', 'title': 'Mikan Alias'}
        row = {'subject': {'name_cn': 'Bangumi title', 'eps': 12, 'platform': 'TV'}}

        with mock.patch.object(mikan, 'search_bangumi', return_value=[
            {'bangumi_id': '43', 'title': 'Mikan Alias'},
            {'bangumi_id': '42', 'title': 'Different title'},
        ]):
            selected, reason = mod.resolve_mikan_subject_override(mikan, row, override)

        self.assertIsNone(selected)
        self.assertEqual(reason, 'mikan_override_id_title_unverified')

    def test_override_title_is_subject_scoped_and_season_conflicts_fail_closed(self):
        mikan = mod.MikanClient({})
        row = {'subject': {
            'name_cn': 'Bangumi title 第二季', 'name': 'Bangumi Original',
            'eps': 12, 'platform': 'TV',
        }}
        valid_override = {'bangumi_id': '42', 'title': 'Mikan Alias 第二季'}
        with mock.patch.object(mikan, 'search_bangumi', return_value=[
            {'bangumi_id': '42', 'title': 'Mikan Alias 第二季'},
        ]):
            selected, reason = mod.resolve_mikan_subject_override(mikan, row, valid_override)
        self.assertEqual(selected['bangumi_id'], '42')
        self.assertIsNone(reason)

        wrong_season = {'bangumi_id': '43', 'title': 'Mikan Alias 第一季'}
        with mock.patch.object(mikan, 'search_bangumi', return_value=[
            {'bangumi_id': '43', 'title': 'Mikan Alias 第一季'},
        ]):
            selected, reason = mod.resolve_mikan_subject_override(mikan, row, wrong_season)
        self.assertIsNone(selected)
        self.assertEqual(reason, 'mikan_override_conflicts_with_subject')


class BangumiCloudDownloadBatchIntegrationTests(unittest.TestCase):
    def _fixture(self, *, dry_run, episode_count=12, missing_numbers=None,
                 rss_items=None, single_video=False, subtitle_sidecar=False):
        class FakeBangumiClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def get_combined_anime_collections(self, tracked_collection_types):
                return [{
                    'subject_id': '1', 'collection_type': 3, 'ep_status': 5,
                    'subject': {'id': 1, 'name_cn': '测试番', 'name': 'Test Show', 'eps': episode_count},
                }]

        class FakeMikanClient:
            def __init__(self, cfg):
                self.cfg = cfg

            def search_bangumi(self, titles):
                return [{'bangumi_id': 'm1', 'title': '测试番'}]

            def choose_best_bangumi(self, row, candidates):
                return {'bangumi_id': 'm1', 'title': '测试番', 'score': 100}

            def fetch_rss_items(self, bangumi_id):
                return rss_items if rss_items is not None else [{
                    'guid': 'batch-1',
                    'title': '[Group] 测试番 [01-12] [简中][1080p]',
                    'torrent_url': 'https://example.com/test-show-01-12.torrent',
                }]

        class FakeGuangYaClient:
            def __init__(self, single_video=False, subtitle_sidecar=False):
                self.created = []
                self.submitted = []
                self.single_video = single_video
                self.subtitle_sidecar = subtitle_sidecar

            def list_all_tasks(self):
                return []

            def get_file_list(self, parent_id, page_size, page):
                return {'data': {'list': [], 'total': 0}}

            def create_dir(self, parent_id, name, fail_if_exist=False):
                folder_id = f'dir-{len(self.created) + 1}'
                self.created.append((parent_id, name, folder_id))
                return {'data': {'fileId': folder_id}}

            def resolve_torrent(self, torrent_url):
                if self.single_video:
                    subfiles = [{'fileIndex': 0, 'fileName': 'Movie.mkv'}]
                    if self.subtitle_sidecar:
                        subfiles.append({'fileIndex': 1, 'fileName': 'Movie.ass'})
                    return {'data': {'btResInfo': {
                        'infoHash': 'b' * 40,
                        'subfiles': subfiles,
                    }}}
                return {'data': {'btResInfo': {
                    'infoHash': 'a' * 40,
                    'subfiles': [
                        {'fileIndex': i - 1, 'fileName': f'[Group] 测试番 [{i:02d}].mkv'}
                        for i in range(1, 13)
                    ],
                }}}

            def create_task(self, payload):
                self.submitted.append(payload)
                return {'data': {'taskId': 'batch-task-1'}}

        original_bangumi_client = mod.BangumiClient
        original_mikan_client = mod.MikanClient
        original_apply_actions = mod.apply_subject_sync_actions
        original_plan_actions = mod.plan_subject_sync_actions
        original_partition_matcher = mod.item_matches_subject_partition
        mod.BangumiClient = FakeBangumiClient
        mod.MikanClient = FakeMikanClient
        mod.plan_subject_sync_actions = lambda *args, **kwargs: []
        mod.apply_subject_sync_actions = lambda *args, **kwargs: []
        mod.item_matches_subject_partition = lambda *args, **kwargs: True

        tmp = tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8')
        json.dump({}, tmp)
        tmp.close()
        state_path = tmp.name
        inventory_path = state_path + '.inventory.json'
        progress_path = state_path + '.progress.json'
        if missing_numbers is None:
            missing_numbers = set(range(6, episode_count + 1))
        else:
            missing_numbers = {int(value) for value in missing_numbers}
        episodes = [
            {'episode_id': i, 'sort': i, 'episode_key': f'EP{i:02d}',
             'present': i not in missing_numbers, 'missing': i in missing_numbers,
             'relative_path': f'在看/测试番/EP{i:02d}.mkv' if i not in missing_numbers else None,
             'file_id': f'file-{i}' if i not in missing_numbers else None,
             'file_name': f'EP{i:02d}.mkv' if i not in missing_numbers else None}
            for i in range(1, episode_count + 1)
        ]
        episodes.append({'episode_id': None, 'episode_key': 'SP01',
                         'present': True, 'missing': False})
        write_inventory(inventory_path, episodes=episodes, matched=True, unmatched=False)
        client = None if dry_run else FakeGuangYaClient(
            single_video=single_video, subtitle_sidecar=subtitle_sidecar,
        )
        cfg = {
            'state_path': state_path, 'inventory_path': inventory_path,
            'progress_path': progress_path, 'bangumi': {}, 'mikan': {},
            'filters': {}, 'tracked_collection_types': [3],
            'download_collection_types': [3], 'max_new_cloud_tasks_per_run': 10,
            'limit_airing_subjects_to_latest': False,
        }
        return (cfg, client, state_path, inventory_path, progress_path,
                original_bangumi_client, original_mikan_client,
                original_apply_actions, original_plan_actions,
                original_partition_matcher)

    def _restore(self, fixture):
        (cfg, client, state_path, inventory_path, progress_path,
         bangumi_client, mikan_client, apply_actions, plan_actions,
         partition_matcher) = fixture
        mod.BangumiClient = bangumi_client
        mod.MikanClient = mikan_client
        mod.apply_subject_sync_actions = apply_actions
        mod.plan_subject_sync_actions = plan_actions
        mod.item_matches_subject_partition = partition_matcher
        for path in (state_path, inventory_path, progress_path):
            if os.path.exists(path):
                os.unlink(path)

    def test_subject_override_precedes_cached_mapping_and_is_verified_before_planning(self):
        movie_item = {
            'guid': 'movie-override',
            'title': '[Group] 测试番 [01] [简中] [1080p]',
            'torrent_url': 'https://example.com/movie-override.torrent',
        }
        fixture = self._fixture(
            dry_run=True, episode_count=1, missing_numbers={1},
            rss_items=[movie_item],
        )
        cfg = fixture[0]
        cfg['mikan']['subject_overrides'] = {
            '1': {'bangumi_id': '42', 'title': 'Mikan Alias'},
        }
        with open(fixture[2], 'w', encoding='utf-8') as handle:
            json.dump({
                'subject_to_mikan': {'1': 'stale-cached-id'},
                'subject_to_mikan_title': {'1': '测试番'},
            }, handle)

        original_mikan_class = fixture[6]

        class RecordingMikanClient:
            def __init__(self, config):
                self.guard = original_mikan_class({})
                self.search_calls = []
                self.selection_calls = []
                self.rss_ids = []

            def search_bangumi(self, titles):
                self.search_calls.append(list(titles))
                return [
                    {'bangumi_id': '42', 'title': 'Mikan Alias'},
                    {'bangumi_id': '43', 'title': 'Mikan Alias'},
                ]

            def choose_best_bangumi(self, row, candidates, *, extra_exact_titles=None):
                self.selection_calls.append((
                    [dict(candidate) for candidate in candidates],
                    list(extra_exact_titles or []),
                ))
                return self.guard.choose_best_bangumi(
                    row, candidates, extra_exact_titles=extra_exact_titles,
                )

            def fetch_rss_items(self, bangumi_id):
                self.rss_ids.append(str(bangumi_id))
                return [movie_item]

        fake_mikan = None

        def make_mikan(config):
            nonlocal fake_mikan
            fake_mikan = RecordingMikanClient(config)
            return fake_mikan

        mod.MikanClient = make_mikan
        try:
            result = mod.process_collections(cfg, fixture[1], dry_run=True)
            self.assertEqual(result['planned_count'], 1)
            self.assertEqual(fake_mikan.search_calls, [['Mikan Alias']])
            self.assertEqual(fake_mikan.selection_calls, [(
                [{'bangumi_id': '42', 'title': 'Mikan Alias'}], ['Mikan Alias'],
            )])
            self.assertEqual(fake_mikan.rss_ids, ['42'])
        finally:
            self._restore(fixture)

    def test_invalid_subject_override_does_not_fall_back_to_cached_mapping_or_search(self):
        movie_item = {
            'guid': 'movie-invalid-override',
            'title': '[Group] 测试番 [01] [简中] [1080p]',
            'torrent_url': 'https://example.com/movie-invalid-override.torrent',
        }
        fixture = self._fixture(
            dry_run=True, episode_count=1, missing_numbers={1},
            rss_items=[movie_item],
        )
        cfg = fixture[0]
        cfg['mikan']['subject_overrides'] = {
            '1': {'bangumi_id': 'not-numeric', 'title': 'Mikan Alias'},
        }
        with open(fixture[2], 'w', encoding='utf-8') as handle:
            json.dump({
                'subject_to_mikan': {'1': 'cached-id'},
                'subject_to_mikan_title': {'1': '测试番'},
            }, handle)

        class NoSearchMikanClient:
            def __init__(self, config):
                pass

            def search_bangumi(self, titles):
                raise AssertionError('invalid override must not fall back to Mikan search')

        mod.MikanClient = NoSearchMikanClient
        try:
            result = mod.process_collections(cfg, fixture[1], dry_run=True)
            self.assertEqual(result['planned_count'], 0)
            self.assertEqual(result['unresolved'], [{
                'subject_id': '1', 'title': '测试番',
                'reason': 'mikan_override_invalid_id',
            }])
        finally:
            self._restore(fixture)

    def test_dry_run_plans_one_batch_for_only_unpresent_episodes(self):
        fixture = self._fixture(dry_run=True)
        cfg, client = fixture[0], fixture[1]
        try:
            result = mod.process_collections(cfg, client, dry_run=True)
            self.assertEqual(result['planned_count'], 1)
            self.assertTrue(result['planned'][0]['batch'])
            self.assertEqual(result['planned'][0]['episode_keys'],
                             [f'EP{i:02d}' for i in range(6, 13)])
            self.assertEqual(result['planned'][0]['target_dir'], '在看/测试番')
            self.assertFalse(os.path.exists(fixture[4]))
            with open(fixture[2], 'r', encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), {})
        finally:
            self._restore(fixture)

    def test_real_batch_submission_is_one_quota_task_with_exact_indexes(self):
        fixture = self._fixture(dry_run=False)
        cfg, client = fixture[0], fixture[1]
        try:
            result = mod.process_collections(cfg, client, dry_run=False)
            self.assertEqual(result['submitted_count'], 1)
            self.assertEqual(result['daily_quota_used'], 1)
            self.assertEqual(len(client.submitted), 1)
            payload = client.submitted[0]
            self.assertEqual(payload['parentId'], 'dir-3')
            self.assertEqual(payload['fileIndexes'], list(range(5, 12)))
            self.assertIn('BATCH-EP06-EP12', payload['newName'])
            with open(fixture[2], 'r', encoding='utf-8') as handle:
                state = json.load(handle)
            task = state['cloud_tasks']['batch-1']
            self.assertEqual(task['episode_keys'], [f'EP{i:02d}' for i in range(6, 13)])
            self.assertEqual(task['info_hash'], 'a' * 40)
            self.assertEqual(task['file_indexes'], list(range(5, 12)))
            self.assertTrue(task['batch'])
            self.assertEqual(task['task_id'], 'batch-task-1')
        finally:
            self._restore(fixture)

    def test_unlabelled_single_movie_maps_only_to_a_one_episode_subject(self):
        movie_item = {
            'guid': 'movie-1',
            'title': '[Group] 测试番 BDRip 1080p [简中]',
            'torrent_url': 'https://example.com/movie.torrent',
        }
        one_episode = self._fixture(
            dry_run=True, episode_count=1, missing_numbers={1},
            rss_items=[movie_item],
        )
        try:
            result = mod.process_collections(one_episode[0], one_episode[1], dry_run=True)
            self.assertEqual(result['planned_count'], 1)
            self.assertEqual(result['planned'][0]['episode_key'], 'EP01')
            self.assertFalse(result['planned'][0]['batch'])
        finally:
            self._restore(one_episode)

        twelve_episode_subject = self._fixture(
            dry_run=True, episode_count=12, missing_numbers={1},
            rss_items=[movie_item],
        )
        try:
            result = mod.process_collections(twelve_episode_subject[0],
                                             twelve_episode_subject[1], dry_run=True)
            self.assertEqual(result['planned_count'], 0)
        finally:
            self._restore(twelve_episode_subject)

    def test_unlabelled_single_movie_submission_requires_one_video_and_keeps_ep_name(self):
        movie_item = {
            'guid': 'movie-1',
            'title': '[Group] 测试番 BDRip 1080p [简中]',
            'torrent_url': 'https://example.com/movie.torrent',
        }
        fixture = self._fixture(
            dry_run=False, episode_count=1, missing_numbers={1},
            rss_items=[movie_item], single_video=True,
        )
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)
            self.assertEqual(result['submitted_count'], 1)
            self.assertEqual(fixture[1].submitted[0]['newName'], 'EP01.mkv')
            self.assertEqual(fixture[1].submitted[0]['fileIndexes'], [0])
        finally:
            self._restore(fixture)

        multi_video = self._fixture(
            dry_run=False, episode_count=1, missing_numbers={1},
            rss_items=[movie_item], single_video=False,
        )
        try:
            result = mod.process_collections(multi_video[0], multi_video[1], dry_run=False)
            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(result['skipped'][0]['reason'],
                             'unlabelled_single_source_is_not_one_video')
        finally:
            self._restore(multi_video)

        sidecar_subtitle = self._fixture(
            dry_run=False, episode_count=1, missing_numbers={1},
            rss_items=[movie_item], single_video=True, subtitle_sidecar=True,
        )
        try:
            result = mod.process_collections(sidecar_subtitle[0], sidecar_subtitle[1],
                                             dry_run=False)
            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(result['skipped'][0]['reason'],
                             'external_subtitles_not_supported')
        finally:
            self._restore(sidecar_subtitle)


class BangumiCloudDownloadInventoryTests(unittest.TestCase):
    def test_new_bangumi_target_can_download_only_exact_missing_ep00(self):
        show = {
            'subject_id': '495562', 'matched': False, 'unmatched': True,
            'ambiguous': False, 'coverage_unknown': False, 'source_path': None,
            'episodes': [{
                'episode_id': 1347308, 'sort': 0, 'episode_key': 'EP00',
                'present': False, 'missing': True,
            }],
        }
        self.assertTrue(mod.is_safe_inventory_subject(show))
        missing = mod.missing_episode_map(show)
        self.assertEqual(set(missing), {'EP00'})
        self.assertEqual(mod.canonical_episode_key('EP00'), 'EP00')

    def test_ambiguous_and_unknown_coverage_subjects_fail_closed(self):
        base = {
            'subject_id': '7', 'matched': False, 'unmatched': True,
            'ambiguous': False, 'coverage_unknown': False,
            'episodes': [{'episode_id': 71, 'episode_key': 'EP01',
                          'present': False, 'missing': True}],
        }
        self.assertTrue(mod.is_safe_inventory_subject(base))
        self.assertFalse(mod.is_safe_inventory_subject({**base, 'ambiguous': True}))
        self.assertFalse(mod.is_safe_inventory_subject({**base, 'coverage_unknown': True}))

    def test_rejected_guid_can_retry_but_unknown_submission_stays_active(self):
        rejected = {'cloud_tasks': {'g': {
            'subject_id': '7', 'episode_key': 'EP01', 'status': 'rejected',
        }}}
        unknown = {'cloud_tasks': {'g': {
            'subject_id': '7', 'episode_key': 'EP01', 'status': 'submission_unknown',
        }}}
        self.assertFalse(mod.has_active_episode_task(rejected, '7', 'EP01'))
        self.assertTrue(mod.has_active_episode_task(unknown, '7', 'EP01'))

    def test_legacy_remote_task_hash_blocks_duplicate_submission(self):
        info_hash = 'a' * 40
        remote = [{'status': 5, 'res': f'magnet:?xt=urn:btih:{info_hash}'}]
        self.assertTrue(mod.has_active_hash_task({}, remote, info_hash))


class GuangYaDirectoryPaginationTests(unittest.TestCase):
    def test_short_pages_continue_until_total_and_parent_cache_is_reused(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
                self.created = 0

            def get_file_list(self, parent_id, page_size, page):
                self.calls.append((parent_id, page_size, page))
                start, end = ((0, 80) if page == 0 else (80, 120))
                items = [
                    {'fileId': f'f{i}', 'fileName': f'File {i}', 'resType': 1}
                    for i in range(start, end)
                ]
                if page == 1:
                    items[-1] = {'fileId': 'dir-late', 'fileName': 'Late directory', 'resType': 2}
                return {'data': {'list': items, 'total': 120}}

            def create_dir(self, parent_id, name, fail_if_exist=False):
                self.created += 1
                return {'data': {'fileId': f'created-{self.created}'}}

        client = FakeClient()
        cache = {}
        found = mod.find_child_dir_id(client, 'category', 'Late directory', cache)
        self.assertEqual(found, 'dir-late')
        self.assertEqual(client.calls, [('category', 100, 0), ('category', 100, 1)])
        self.assertEqual(mod.ensure_child_dir(client, 'category', 'Created', cache), 'created-1')
        self.assertEqual(mod.ensure_child_dir(client, 'category', 'Created', cache), 'created-1')
        self.assertEqual(client.created, 1)
        self.assertEqual(len(client.calls), 2)

    def test_truncated_directory_page_is_an_error(self):
        class FakeClient:
            def get_file_list(self, parent_id, page_size, page):
                items = ([{'fileId': f'f{i}', 'fileName': str(i), 'resType': 1}
                          for i in range(80)] if page == 0 else [])
                return {'data': {'list': items, 'total': 120}}

        with self.assertRaises(RuntimeError):
            mod.list_all_children(FakeClient(), 'category')

    def test_empty_directory_accepts_missing_or_null_list_only_for_zero_total(self):
        for data in ({'total': 0}, {'total': 0, 'list': None},
                     {'total': 0, 'list': []}):
            class EmptyClient:
                def get_file_list(self, parent_id, page_size, page):
                    return {'data': dict(data)}

            with self.subTest(data=data):
                self.assertEqual(mod.list_all_children(EmptyClient(), 'category'), [])

        for data in ({}, {'total': 1}, {'total': 0, 'list': 'invalid'},
                     {'total': False}, {'total': '0'}):
            class MalformedClient:
                def get_file_list(self, parent_id, page_size, page):
                    return {'data': dict(data)}

            with self.subTest(data=data), self.assertRaises(RuntimeError):
                mod.list_all_children(MalformedClient(), 'category')


class BangumiCollectionPaginationTests(unittest.TestCase):
    def test_short_collection_page_advances_by_received_rows_and_checks_total(self):
        import bangumi_mikan_qb_sync as sync_module
        client = mod.BangumiClient({
            'base_url': 'https://api.bgm.tv', 'username': 'test', 'access_token': '',
        })
        offsets = []

        def fake_request(url, **kwargs):
            from urllib.parse import parse_qs, urlparse
            offset = int(parse_qs(urlparse(url).query)['offset'][0])
            offsets.append(offset)
            row_id = len(offsets)
            payload = {
                'total': 2,
                'data': [{'subject_id': row_id, 'subject': {'id': row_id}}],
            }
            return json.dumps(payload).encode('utf-8')

        with mock.patch.object(sync_module, 'request', side_effect=fake_request), \
             mock.patch.object(sync_module.time, 'sleep'):
            rows = client.get_anime_collections(1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(offsets, [0, 1])

    def test_duplicate_subject_across_collection_types_is_not_silently_ignored(self):
        client = mod.BangumiClient({
            'base_url': 'https://api.bgm.tv', 'username': 'test', 'access_token': '',
        })
        client.get_anime_collections = lambda collection_type: [{
            'subject_id': '1', 'subject': {'id': 1}, 'collection_type': collection_type,
        }]
        with self.assertRaises(RuntimeError):
            client.get_combined_anime_collections([1, 3])


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
            inventory_path = tmp_path + '.inventory.json'
            progress_path = tmp_path + '.progress.json'
            write_inventory(inventory_path)

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'inventory_path': inventory_path,
                    'progress_path': progress_path,
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
            self.assertEqual(result['planned_count'], 1)
            self.assertEqual(result['planned'][0]['episode_key'], 'EP01')
            self.assertFalse(os.path.exists(progress_path))
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if 'inventory_path' in locals() and os.path.exists(inventory_path):
                os.unlink(inventory_path)

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
            inventory_path = tmp_path + '.inventory.json'
            progress_path = tmp_path + '.progress.json'
            write_inventory(inventory_path)

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'inventory_path': inventory_path,
                    'progress_path': progress_path,
                    'bangumi': {},
                    'mikan': {},
                    'tracked_collection_types': [3],
                    'download_collection_types': [3],
                },
                client=None,
                dry_run=True,
            )

            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(result['planned_count'], 1)
            with open(tmp_path, 'r', encoding='utf-8') as fh:
                persisted = json.load(fh)
            self.assertEqual(persisted, {})
            self.assertFalse(os.path.exists(progress_path))
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if 'inventory_path' in locals() and os.path.exists(inventory_path):
                os.unlink(inventory_path)


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
                        'subject_id': '1',
                        'episode_key': 'EP01',
                        'target_dir': '在看/测试番',
                        'target_name': 'EP01.mkv',
                        'status': 'submitted',
                    }
                },
            }
            with tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                json.dump(initial_state, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            inventory_path = tmp_path + '.inventory.json'
            write_inventory(inventory_path)

            result = mod.process_collections(
                {
                    'state_path': tmp_path,
                    'inventory_path': inventory_path,
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
            self.assertEqual(result['planned_count'], 0)
            self.assertEqual(result['skipped_count'], 1)
        finally:
            mod.BangumiClient = original_bangumi_client
            mod.MikanClient = original_mikan_client
            mod.apply_subject_sync_actions = original_apply_actions
            mod.plan_subject_sync_actions = original_plan_actions
            mod.item_matches_subject_partition = original_partition_matcher
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            if 'inventory_path' in locals() and os.path.exists(inventory_path):
                os.unlink(inventory_path)


if __name__ == '__main__':
    unittest.main()
