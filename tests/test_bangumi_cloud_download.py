import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from scripts import library_inventory as inventory_mod


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


class SubjectSyncRecoveryTests(unittest.TestCase):
    def _action(self, subject_id, folder_id, *, op='move', from_category='在看',
                to_category='看过', old_name='旧名称', new_name=None):
        new_name = old_name if new_name is None else new_name
        if op == 'move':
            return {
                'action': 'move', 'subject_id': str(subject_id), 'folder_id': folder_id,
                'title': new_name, 'from_category': from_category,
                'to_category': to_category, 'show_dir': new_name,
                'collection_type': 2,
            }
        return {
            'action': 'rename', 'subject_id': str(subject_id), 'folder_id': folder_id,
            'title': new_name, 'category_dir': to_category,
            'old_show_dir': old_name, 'show_dir': new_name,
            'collection_type': 2,
        }

    def _client(self, *, folders=None, fail_move_call=None, move_error_after=False,
                hidden_ids=None, hide_after_error_ids=None, missing_categories=None):
        class FakeClient:
            def __init__(self):
                missing_category_names = set(missing_categories or [])
                category_rows = [
                    ('cat-watching', '在看'), ('cat-watched', '看过'),
                    ('cat-want', '想看'), ('cat-unmarked', '未标记'),
                ]
                self.parents = {
                    'root': [
                        {'fileId': file_id, 'fileName': name, 'resType': 2}
                        for file_id, name in category_rows if name not in missing_category_names
                    ],
                    'cat-watching': [], 'cat-watched': [],
                    'cat-want': [], 'cat-unmarked': [],
                }
                for parent_id, rows in (folders or {}).items():
                    self.parents[parent_id].extend(dict(row) for row in rows)
                self.move_calls = []
                self.rename_calls = []
                self.fail_move_call = fail_move_call
                self.move_error_after = move_error_after
                self.hidden_ids = set(hidden_ids or [])
                self.hide_after_error_ids = set(hide_after_error_ids or [])
                self.create_calls = []

            def get_file_list(self, parent_id, page_size, page):
                rows = [row for row in self.parents.get(parent_id, [])
                        if str(row.get('fileId')) not in self.hidden_ids]
                return {'data': {'list': [dict(row) for row in rows], 'total': len(rows)}}

            def create_dir(self, parent_id, name, fail_if_exist=False):
                self.create_calls.append((parent_id, name, fail_if_exist))
                file_id = f'created-{len(self.create_calls)}'
                self.parents[parent_id].append({
                    'fileId': file_id, 'fileName': name, 'resType': 2,
                })
                self.parents[file_id] = []
                return {'data': {'fileId': file_id}}

            def move_file(self, file_ids, target_parent_id):
                self.move_calls.append((list(file_ids), target_parent_id))
                call_number = len(self.move_calls)
                if self.fail_move_call == call_number and not self.move_error_after:
                    self.hidden_ids.update(self.hide_after_error_ids)
                    raise RuntimeError('simulated move timeout before apply')
                for file_id in file_ids:
                    source_parent = next((parent for parent, rows in self.parents.items()
                                          if any(str(row.get('fileId')) == str(file_id) for row in rows)), None)
                    if source_parent is None:
                        raise RuntimeError('folder not found')
                    row = next(row for row in self.parents[source_parent]
                               if str(row.get('fileId')) == str(file_id))
                    self.parents[source_parent].remove(row)
                    self.parents[target_parent_id].append(row)
                if self.fail_move_call == call_number and self.move_error_after:
                    raise RuntimeError('simulated move timeout after apply')

            def rename(self, file_id, name):
                self.rename_calls.append((str(file_id), name))
                row = next(row for rows in self.parents.values() for row in rows
                           if str(row.get('fileId')) == str(file_id))
                row['fileName'] = name

        return FakeClient()

    def _state(self, locations):
        return {'subject_locations': locations, 'tracked_subjects': {}, 'cloud_tasks': {}}

    def test_partial_success_checkpoints_first_action_before_second_failure(self):
        client = self._client(folders={
            'cat-watching': [
                {'fileId': 'folder-1', 'fileName': '一号', 'resType': 2},
                {'fileId': 'folder-2', 'fileName': '二号', 'resType': 2},
            ],
        }, fail_move_call=2)
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '一号', 'category_dir': '在看', 'show_dir': '一号'},
            '2': {'folder_id': 'folder-2', 'title': '二号', 'category_dir': '在看', 'show_dir': '二号'},
        })
        handle = tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8')
        checkpoint_path = handle.name
        handle.close()
        try:
            with self.assertRaises(mod.SubjectSyncSafetyError):
                mod.apply_subject_sync_actions(
                    client, 'root', [self._action('1', 'folder-1', old_name='一号'),
                                     self._action('2', 'folder-2', old_name='二号')],
                    state, dry_run=False,
                    checkpoint=lambda: mod.write_json(checkpoint_path, state),
                )

            persisted = mod.read_json(checkpoint_path)
            self.assertEqual(len(client.move_calls), 2)
            journal = persisted['subject_sync']['journal']
            self.assertEqual([item['status'] for item in journal], ['done', 'pending'])
            self.assertEqual(persisted['subject_locations']['1']['category_dir'], '看过')
        finally:
            os.unlink(checkpoint_path)

    def test_unknown_pending_move_is_read_only_on_next_run(self):
        client = self._client(
            folders={'cat-watching': [
                {'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2},
            ]},
            fail_move_call=1, hide_after_error_ids={'folder-1'},
        )
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        persisted = {}
        action = self._action('1', 'folder-1')
        checkpoint = lambda: persisted.update(json.loads(json.dumps(state)))

        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [action], state, False, checkpoint=checkpoint)
        self.assertEqual(persisted['subject_sync']['journal'][0]['status'], 'pending')

        recovered_state = json.loads(json.dumps(persisted))
        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [action], recovered_state, False)

        self.assertEqual(len(client.move_calls), 1)
        self.assertEqual(recovered_state['subject_sync']['journal'][0]['status'], 'pending')

    def test_pending_recovery_rejects_changed_category_folder_id(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        })
        client.parents['root'] = [row for row in client.parents['root']
                                  if row['fileId'] != 'cat-watched']
        client.parents['root'].append({
            'fileId': 'cat-watched-replaced', 'fileName': '看过', 'resType': 2,
        })
        client.parents['cat-watched-replaced'] = []
        spec = {
            'action': 'move', 'subject_id': '1', 'folder_id': 'folder-1',
            'source_category': '在看', 'target_category': '看过',
            'source_parent_id': 'cat-watching', 'target_parent_id': 'cat-watched',
            'source_name': '旧名称', 'target_name': '旧名称',
            'title': '旧名称', 'collection_type': 2, 'status': 'pending',
        }
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        state['subject_sync'] = {'journal': [spec]}

        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(
                client, 'root', [self._action('1', 'folder-1')], state, False,
            )

        self.assertEqual(client.move_calls, [])
        self.assertEqual(state['subject_sync']['journal'][0]['status'], 'pending')

    def test_source_readback_does_not_authorize_replaying_pending_move(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        }, fail_move_call=1)
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        persisted = {}
        action = self._action('1', 'folder-1')
        checkpoint = lambda: persisted.update(json.loads(json.dumps(state)))

        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [action], state, False, checkpoint=checkpoint)
        self.assertEqual(state['subject_sync']['journal'][0]['status'], 'pending')

        recovered_state = json.loads(json.dumps(persisted))
        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [action], recovered_state, False)

        self.assertEqual(len(client.move_calls), 1)
        self.assertEqual(recovered_state['subject_sync']['journal'][0]['status'], 'pending')

    def test_write_error_with_target_readback_recovers_as_done(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        }, fail_move_call=1, move_error_after=True)
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        checkpoints = []

        applied = mod.apply_subject_sync_actions(
            client, 'root', [self._action('1', 'folder-1')], state, False,
            checkpoint=lambda: checkpoints.append(json.loads(json.dumps(state))),
        )

        self.assertEqual(len(client.move_calls), 1)
        self.assertEqual(state['subject_sync']['journal'][0]['status'], 'done')
        self.assertEqual(state['subject_locations']['1']['category_dir'], '看过')
        self.assertEqual(applied[0]['sync_status'], 'done')
        self.assertGreaterEqual(len(checkpoints), 2)

    def test_folder_already_at_target_updates_location_without_cloud_write(self):
        client = self._client(folders={
            'cat-watched': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        })
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        checkpoints = []

        applied = mod.apply_subject_sync_actions(
            client, 'root', [self._action('1', 'folder-1')], state, False,
            checkpoint=lambda: checkpoints.append(True),
        )

        self.assertEqual(client.move_calls, [])
        self.assertEqual(state['subject_locations']['1']['category_dir'], '看过')
        self.assertEqual(applied[0]['sync_status'], 'already_at_target')
        self.assertEqual(state.get('subject_sync', {}).get('journal', []), [])

    def test_dry_run_does_not_create_sync_journal_or_write_cloud(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        }, missing_categories={'未标记'})
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })

        mod.apply_subject_sync_actions(
            client, 'root', [self._action('1', 'folder-1', to_category='未标记')], state, True,
        )

        self.assertNotIn('subject_sync', state)
        self.assertEqual(client.create_calls, [])
        self.assertEqual(client.move_calls, [])

    def test_missing_target_category_is_created_fail_closed_before_move(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        }, missing_categories={'未标记'})
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        action = self._action('1', 'folder-1', to_category='未标记')
        action['collection_type'] = 0

        mod.apply_subject_sync_actions(client, 'root', [action], state, False)

        self.assertEqual(client.create_calls, [('root', '未标记', True)])
        self.assertEqual(client.move_calls, [(['folder-1'], 'created-1')])
        self.assertEqual(state['subject_locations']['1']['category_dir'], '未标记')

    def test_persisted_pending_rename_recovers_from_target_without_resending(self):
        client = self._client(folders={
            'cat-watched': [{'fileId': 'folder-1', 'fileName': '新名称', 'resType': 2}],
        })
        entry = {
            'action': 'rename', 'subject_id': '1', 'folder_id': 'folder-1',
            'source_category': '看过', 'target_category': '看过',
            'source_parent_id': 'cat-watched', 'target_parent_id': 'cat-watched',
            'source_name': '旧名称', 'target_name': '新名称',
            'title': '新名称', 'collection_type': 2, 'status': 'pending',
        }
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '看过', 'show_dir': '旧名称'},
        })
        state['subject_sync'] = {'journal': [entry]}

        mod.apply_subject_sync_actions(client, 'root', [], state, False)

        self.assertEqual(client.rename_calls, [])
        self.assertEqual(state['subject_sync']['journal'][0]['status'], 'done')
        self.assertEqual(state['subject_locations']['1']['show_dir'], '新名称')

    def test_name_conflict_refuses_write(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
            'cat-watched': [{'fileId': 'other-folder', 'fileName': '旧名称', 'resType': 2}],
        })
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })

        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [self._action('1', 'folder-1')], state, False)

        self.assertEqual(client.move_calls, [])
        self.assertEqual(state.get('subject_sync', {}).get('journal', []), [])

    def test_duplicate_target_name_with_expected_id_is_still_conflict(self):
        client = self._client(folders={
            'cat-watched': [
                {'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2},
                {'fileId': 'other-folder', 'fileName': '旧名称', 'resType': 2},
            ],
        })
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })

        with self.assertRaises(mod.SubjectSyncSafetyError):
            mod.apply_subject_sync_actions(client, 'root', [self._action('1', 'folder-1')], state, False)

        self.assertEqual(client.move_calls, [])

    def test_move_preserves_source_name_until_verified_rename(self):
        client = self._client(folders={
            'cat-watching': [{'fileId': 'folder-1', 'fileName': '旧名称', 'resType': 2}],
        })
        state = self._state({
            '1': {'folder_id': 'folder-1', 'title': '旧名称', 'category_dir': '在看', 'show_dir': '旧名称'},
        })
        actions = [
            self._action('1', 'folder-1', old_name='旧名称', new_name='新名称'),
            self._action('1', 'folder-1', op='rename', to_category='看过',
                         old_name='旧名称', new_name='新名称'),
        ]

        mod.apply_subject_sync_actions(client, 'root', actions, state, False)

        self.assertEqual(client.move_calls, [(['folder-1'], 'cat-watched')])
        self.assertEqual(client.rename_calls, [('folder-1', '新名称')])
        self.assertEqual(state['subject_locations']['1']['category_dir'], '看过')
        self.assertEqual(state['subject_locations']['1']['show_dir'], '新名称')
        self.assertEqual([item['status'] for item in state['subject_sync']['journal']], ['done', 'done'])


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

    def test_candidate_episode_numbering_maps_local_episode_to_bangumi_sort(self):
        show = {'episode_numbering': {
            'mode': 'season_relative',
            'ep_to_sort': {'1': '15', '2': '16', '3': '17'},
            'sort_to_ep': {'15': '1', '16': '2', '17': '3'},
            'episode_ids': {'15': 1501, '16': 1502, '17': 1503},
            'evidence': [], 'reason': None,
        }}
        item = {'title': '[Group] 测试番 第二季 [03] [简中]'}

        self.assertEqual(mod.candidate_inventory_episode_key(item, show), 'EP17')

    def test_candidate_episode_numbering_keeps_global_sort_episode(self):
        show = {'episode_numbering': {
            'mode': 'global_sort',
            'ep_to_sort': {'1': '15', '2': '16', '3': '17'},
            'sort_to_ep': {'15': '1', '16': '2', '17': '3'},
            'episode_ids': {'15': 1501, '16': 1502, '17': 1503},
            'evidence': [], 'reason': None,
        }}

        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [17] [简中]'}, show,
        ), 'EP17')

    def test_real_first_season_inventory_keeps_ep01_and_ep00_candidates(self):
        subject = {
            'subject_id': '1', 'collection_type': 3,
            'subject': {'id': 1, 'name': 'Test Show', 'name_cn': '测试番', 'eps': 13},
        }
        raw_episodes = [{
            'id': 1000 + number, 'subject_id': 1, 'type': 0,
            'sort': number, 'ep': number, 'name': f'Episode {number}',
            'airdate': '2000-01-01',
        } for number in range(13)]
        rows = inventory_mod.episode_records(raw_episodes, '1')
        cloud = {
            'name': '测试番', 'path': '在看/测试番', 'type': 'directory',
            'empty': False, 'children': [{
                'name': 'EP01.mkv', 'path': '在看/测试番/EP01.mkv',
                'type': 'file', 'file_id': 'file-01', 'size': 10,
            }],
        }

        show = inventory_mod.show_node_from_cloud(
            cloud, subject=subject, episodes=rows, expected_known=True,
        )

        self.assertEqual(show['episode_numbering']['mode'], 'global_sort')
        self.assertEqual(show['episode_numbering']['ep_to_sort'], {})
        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [01] [简中]'}, show,
        ), 'EP01')
        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [00] [简中]'}, show,
        ), 'EP00')

    def test_ambiguous_numbering_does_not_make_a_download_candidate(self):
        item = {'title': '[Group] 测试番 [03] [简中]'}
        ambiguous = {'subject_id': '1', 'matched': True, 'unmatched': False,
                     'source_path': '在看/测试番', 'episodes': [{
                         'episode_id': 17, 'episode_key': 'EP17',
                         'present': False, 'missing': True,
                     }],
                     'episode_numbering': {
                         'mode': 'blocked', 'ep_to_sort': {}, 'sort_to_ep': {},
                         'episode_ids': {}, 'evidence': [], 'reason': 'numbering_overlap',
                     }}

        self.assertEqual(mod.candidate_inventory_episode_key(item, ambiguous), '')
        self.assertFalse(mod.is_safe_inventory_subject(ambiguous))

    def test_non_global_numbering_without_alias_map_fails_closed(self):
        show = {'episode_numbering': {
            'mode': 'metadata_alias', 'ep_to_sort': {}, 'sort_to_ep': {},
            'episode_ids': {}, 'evidence': [], 'reason': 'no_alias_map',
        }}

        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [01] [简中]'}, show,
        ), '')
        self.assertFalse(mod.is_safe_inventory_subject(show))

    def test_overlapping_episode_and_sort_domains_are_rejected(self):
        show = {'episode_numbering': {
            'mode': 'metadata_alias',
            'ep_to_sort': {'3': '17', '5': '3'},
            'sort_to_ep': {'17': '3', '3': '5'},
            'episode_ids': {'17': 1703, '3': 1705},
            'evidence': [], 'reason': None,
        }}

        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [03] [简中]'}, show,
        ), '')

    def test_legacy_inventory_without_numbering_metadata_keeps_existing_key(self):
        self.assertEqual(mod.candidate_inventory_episode_key(
            {'title': '[Group] 测试番 [03] [简中]'}, {'episodes': []},
        ), 'EP03')

    def test_fresh_remote_local_episode_name_matches_its_sort_episode(self):
        class FakeClient:
            def get_file_list(self, parent_id, page_size, page):
                return {'data': {'list': [
                    {'fileId': 'file-ep03', 'fileName': 'EP03.mp4', 'resType': 1},
                ], 'total': 1}}

        show = {'episode_numbering': {
            'mode': 'season_relative',
            'ep_to_sort': {'1': '15', '2': '16', '3': '17'},
            'sort_to_ep': {'15': '1', '16': '2', '17': '3'},
            'episode_ids': {'15': 1501, '16': 1502, '17': 1503},
            'evidence': [], 'reason': None,
        }}

        existing = mod.get_remote_episode_file_index(
            FakeClient(), 'folder-1', ['EP17'], inventory_show=show,
        )

        self.assertEqual(existing, {'EP17': ['EP03.mp4']})

    def test_fresh_global_sort_listing_also_checks_new_local_episode_alias(self):
        class FakeClient:
            def get_file_list(self, parent_id, page_size, page):
                return {'data': {'list': [
                    {'fileId': 'file-ep03', 'fileName': 'EP03.mp4', 'resType': 1},
                ], 'total': 1}}

        show = {'episode_numbering': {
            'mode': 'global_sort',
            'ep_to_sort': {'1': '15', '2': '16', '3': '17'},
            'sort_to_ep': {'15': '1', '16': '2', '17': '3'},
            'episode_ids': {'15': 1501, '16': 1502, '17': 1503},
            'evidence': [], 'reason': None,
        }}

        existing = mod.get_remote_episode_file_index(
            FakeClient(), 'folder-1', ['EP03', 'EP17'], inventory_show=show,
        )

        self.assertEqual(existing, {'EP03': ['EP03.mp4'], 'EP17': ['EP03.mp4']})

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
                 rss_items=None, single_video=False, subtitle_sidecar=False,
                 remote_target_items=None, single_video_subfiles=None):
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
            def __init__(self, single_video=False, subtitle_sidecar=False,
                         target_items=None, single_subfiles=None):
                self.created = []
                self.submitted = []
                self.single_video = single_video
                self.subtitle_sidecar = subtitle_sidecar
                self.target_items = list(target_items or [])
                self.single_subfiles = (None if single_subfiles is None
                                        else list(single_subfiles))
                self.file_list_calls = []
                self.resolved_torrents = 0

            def list_all_tasks(self):
                return []

            def get_file_list(self, parent_id, page_size, page):
                self.file_list_calls.append((parent_id, page_size, page))
                items = self.target_items if parent_id == 'dir-3' else []
                return {'data': {'list': list(items), 'total': len(items)}}

            def create_dir(self, parent_id, name, fail_if_exist=False):
                folder_id = f'dir-{len(self.created) + 1}'
                self.created.append((parent_id, name, folder_id))
                return {'data': {'fileId': folder_id}}

            def resolve_torrent(self, torrent_url):
                if self.single_video:
                    self.resolved_torrents += 1
                    subfiles = (list(self.single_subfiles) if self.single_subfiles is not None
                                else [{'fileIndex': 0, 'fileName': 'Movie.mkv'}])
                    if self.subtitle_sidecar and self.single_subfiles is None:
                        subfiles.append({'fileIndex': 1, 'fileName': 'Movie.ass'})
                    info_hash = ('{:040x}'.format(self.resolved_torrents)
                                 if self.single_subfiles is not None else 'b' * 40)
                    return {'data': {'btResInfo': {
                        'infoHash': info_hash,
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
            target_items=remote_target_items,
            single_subfiles=single_video_subfiles,
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

    def _set_season_relative_episode_numbering(self, fixture, missing_sort_numbers=None):
        missing_sort_numbers = set(missing_sort_numbers or {17})
        with open(fixture[3], 'r', encoding='utf-8') as handle:
            snapshot = json.load(handle)
        show = snapshot['tree'][0]['shows'][0]
        ep_to_sort = {}
        sort_to_ep = {}
        for episode in show['episodes']:
            if episode.get('episode_id') is None:
                continue
            local_number = int(episode['episode_key'][2:])
            sort_number = local_number + 14
            ep_to_sort[str(local_number)] = str(sort_number)
            sort_to_ep[str(sort_number)] = str(local_number)
            episode['sort'] = sort_number
            episode['episode_key'] = f'EP{sort_number:02d}'
            episode['present'] = sort_number not in missing_sort_numbers
            episode['missing'] = sort_number in missing_sort_numbers
            episode['relative_path'] = (
                None if sort_number in missing_sort_numbers else f'在看/测试番/EP{sort_number:02d}.mkv'
            )
            episode['file_id'] = None if sort_number in missing_sort_numbers else f'file-{sort_number}'
            episode['file_name'] = None if sort_number in missing_sort_numbers else f'EP{sort_number:02d}.mkv'
        show['episode_numbering'] = {
            'mode': 'season_relative', 'ep_to_sort': ep_to_sort,
            'sort_to_ep': sort_to_ep,
            'episode_ids': {str(i + 14): i for i in range(1, 13)},
            'evidence': [], 'reason': None,
        }
        with open(fixture[3], 'w', encoding='utf-8') as handle:
            json.dump(snapshot, handle, ensure_ascii=False)

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

    def test_fresh_target_mp4_blocks_single_mkv_after_inventory_snapshot(self):
        item = {
            'guid': 'single-ep01',
            'title': '[Group] 测试番 [01] [简中] [1080p]',
            'torrent_url': 'https://example.com/test-show-01.torrent',
        }
        fixture = self._fixture(
            dry_run=False, episode_count=1, missing_numbers={1},
            rss_items=[item], single_video=True,
            remote_target_items=[{
                'fileId': 'new-file-after-scan', 'fileName': 'EP01.mp4', 'resType': 1,
            }],
        )
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(fixture[1].submitted, [])
            self.assertEqual(result['skipped'][0]['reason'], 'target_episode_already_present')
            self.assertEqual(result['skipped'][0]['existing_episode_keys'], ['EP01'])
            self.assertEqual(result['skipped'][0]['existing_target_files'], {'EP01': ['EP01.mp4']})
            self.assertIn(('dir-3', 100, 0), fixture[1].file_list_calls)
        finally:
            self._restore(fixture)

    def test_season_relative_source_ep03_selects_local_file_but_submits_sort_ep17(self):
        item = {
            'guid': 'season-two-ep03',
            'title': '[Group] 测试番 第二季 [03] [简中] [1080p]',
            'torrent_url': 'https://example.com/test-show-s02e03.torrent',
        }
        fixture = self._fixture(
            dry_run=False, episode_count=12, missing_numbers={3},
            rss_items=[item], single_video=True,
            single_video_subfiles=[
                {'fileIndex': 2, 'fileName': '[Group] 测试番 第二季 [03].mkv'},
                {'fileIndex': 16, 'fileName': '[Group] 测试番 [17].mkv'},
            ],
        )
        self._set_season_relative_episode_numbering(fixture)
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 1)
            self.assertEqual(fixture[1].submitted[0]['fileIndexes'], [2])
            self.assertEqual(fixture[1].submitted[0]['newName'], 'EP17.mkv')
        finally:
            self._restore(fixture)

    def test_multiple_season_relative_sources_keep_their_own_episode_labels(self):
        items = [
            {
                'guid': 'season-two-ep03',
                'title': '[Group] 测试番 第二季 [03] [简中] [1080p]',
                'torrent_url': 'https://example.com/test-show-s02e03.torrent',
            },
            {
                'guid': 'season-two-ep04',
                'title': '[Group] 测试番 第二季 [04] [简中] [1080p]',
                'torrent_url': 'https://example.com/test-show-s02e04.torrent',
            },
        ]
        fixture = self._fixture(
            dry_run=False, episode_count=12, missing_numbers={3, 4},
            rss_items=items, single_video=True,
            single_video_subfiles=[
                {'fileIndex': 2, 'fileName': '[Group] 测试番 第二季 [03].mkv'},
                {'fileIndex': 3, 'fileName': '[Group] 测试番 第二季 [04].mkv'},
                {'fileIndex': 16, 'fileName': '[Group] 测试番 [17].mkv'},
                {'fileIndex': 17, 'fileName': '[Group] 测试番 [18].mkv'},
            ],
        )
        self._set_season_relative_episode_numbering(fixture, missing_sort_numbers={17, 18})
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 2)
            self.assertEqual([task['fileIndexes'] for task in fixture[1].submitted], [[2], [3]])
            self.assertEqual([task['newName'] for task in fixture[1].submitted],
                             ['EP17.mkv', 'EP18.mkv'])
        finally:
            self._restore(fixture)

    def test_fresh_season_relative_ep03_blocks_sort_ep17_submission(self):
        item = {
            'guid': 'season-two-ep03',
            'title': '[Group] 测试番 第二季 [03] [简中] [1080p]',
            'torrent_url': 'https://example.com/test-show-s02e03.torrent',
        }
        fixture = self._fixture(
            dry_run=False, episode_count=12, missing_numbers={3},
            rss_items=[item], single_video=True,
            remote_target_items=[{
                'fileId': 'new-local-ep03', 'fileName': 'EP03.mp4', 'resType': 1,
            }],
        )
        self._set_season_relative_episode_numbering(fixture)
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(fixture[1].submitted, [])
            self.assertEqual(result['skipped'][0]['reason'], 'target_episode_already_present')
            self.assertEqual(result['skipped'][0]['existing_episode_keys'], ['EP17'])
            self.assertEqual(result['skipped'][0]['existing_target_files'], {'EP17': ['EP03.mp4']})
        finally:
            self._restore(fixture)

    def test_season_relative_batch_is_skipped_with_explicit_reason(self):
        fixture = self._fixture(dry_run=True)
        self._set_season_relative_episode_numbering(fixture)
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=True)

            self.assertEqual(result['planned_count'], 0)
            self.assertTrue(any(
                item.get('reason') == 'batch_episode_numbering_translation_unsupported'
                for item in result['skipped']
            ))
        finally:
            self._restore(fixture)

    def test_directory_named_like_episode_video_does_not_block_single_submission(self):
        item = {
            'guid': 'single-ep01',
            'title': '[Group] 测试番 [01] [简中] [1080p]',
            'torrent_url': 'https://example.com/test-show-01.torrent',
        }
        fixture = self._fixture(
            dry_run=False, episode_count=1, missing_numbers={1},
            rss_items=[item], single_video=True,
            remote_target_items=[{
                'fileId': 'directory-named-like-video', 'fileName': 'EP01.mkv', 'resType': 2,
            }],
        )
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 1)
            self.assertEqual(len(fixture[1].submitted), 1)
            self.assertEqual(fixture[1].submitted[0]['newName'], 'EP01.mkv')
        finally:
            self._restore(fixture)

    def test_fresh_target_episode_inside_batch_skips_the_entire_batch(self):
        fixture = self._fixture(
            dry_run=False, episode_count=12, missing_numbers=set(range(6, 13)),
            remote_target_items=[{
                'fileId': 'new-ep08-after-scan', 'fileName': 'EP08.mp4', 'resType': 1,
            }],
        )
        try:
            result = mod.process_collections(fixture[0], fixture[1], dry_run=False)

            self.assertEqual(result['submitted_count'], 0)
            self.assertEqual(fixture[1].submitted, [])
            self.assertEqual(result['skipped'][0]['reason'], 'target_episode_already_present')
            self.assertEqual(result['skipped'][0]['existing_episode_keys'], ['EP08'])
            self.assertEqual(result['skipped'][0]['existing_target_files'], {'EP08': ['EP08.mp4']})
            self.assertIn(('dir-3', 100, 0), fixture[1].file_list_calls)
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
