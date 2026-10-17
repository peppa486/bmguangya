import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor


SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts'))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from guangya_inventory import GuangYaInventoryClient, GuangYaInventoryError
from cloud_api import GuangYaAPIError


class FakeBackend:
    def __init__(self, *, root_path='storage-root', entries=None, overrides=None):
        self.root_path = root_path
        self.entries = entries or {}
        self.overrides = overrides or {}
        self.calls = []
        self.lock = threading.Lock()

    def list_page(self, parent_id, page_size, page):
        with self.lock:
            self.calls.append((parent_id, page_size, page, threading.get_ident()))
        override = self.overrides.get((parent_id, page))
        if override is not None:
            return override
        entries = self.entries.get(parent_id, [])
        start = page * page_size
        return {
            'data': {
                'list': entries[start:start + page_size],
                'total': len(entries),
            }
        }


class FakeCredentials:
    def __init__(self, root_path):
        self.root_path = root_path

    def get(self):
        return {'root_path': self.root_path, 'access_token': 'secret-token', 'device_id': 'device'}


class FakeGuangYaClient:
    def __init__(self, backend):
        self.backend = backend
        self.credentials = FakeCredentials(backend.root_path)

    def get_file_list(self, *, parent_id, page_size, page):
        return self.backend.list_page(parent_id, page_size, page)


def adapter_for(backend, *, root='/guangya/Anime', mount='/guangya'):
    created_clients = []
    lock = threading.Lock()

    def factory(config):
        client = FakeGuangYaClient(backend)
        with lock:
            created_clients.append(client)
        return client

    raw = {
        'alist': {
            'database_path': '/not-opened-in-tests/alist.db',
            'guangya_mount_path': mount,
            'root': root,
        },
        'guangya': {'root_dir': 'Anime'},
    }
    normalized = {'alist': {'root': root}}
    return GuangYaInventoryClient(raw, normalized, client_factory=factory), created_clients


def directory(file_id, name, **extra):
    return {'fileId': file_id, 'fileName': name, 'resType': 2, **extra}


def file(file_id, name, **extra):
    return {'fileId': file_id, 'fileName': name, 'resType': 1, **extra}


class GuangYaInventoryRootTests(unittest.TestCase):
    def test_normalizer_retains_only_valid_gcid_as_lowercase(self):
        valid = 'ABCDEF0123456789ABCDEF0123456789ABCDEF01'
        normalized = GuangYaInventoryClient._normalize_entry(
            file('episode-id', 'EP01.mkv', gcid=valid, fileSize=123),
        )
        self.assertEqual(normalized['gcid'], valid.lower())
        self.assertEqual(normalized['size'], 123)

        for gcid in (None, 'a' * 39, 'g' * 40, f"{'a' * 40} ", 123):
            with self.subTest(gcid=gcid):
                entry = file('episode-id', 'EP01.mkv')
                if gcid is not None:
                    entry['gcid'] = gcid
                normalized = GuangYaInventoryClient._normalize_entry(entry)
                self.assertNotIn('gcid', normalized)

    def test_root_path_is_resolved_from_alist_storage_anchor_and_maps_contract(self):
        backend = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
            'anime-id': [
                directory('watching-id', '在看', utime=0),
                file('episode-id', 'EP01.mkv', fileSize=123, utime=1704164645),
                file('poster-id', 'poster.jpg', size='456'),
            ],
        })
        client, _ = adapter_for(backend)

        entries = client.list_all('/guangya/Anime', page_size=10)

        self.assertEqual([row['name'] for row in entries], ['在看', 'EP01.mkv', 'poster.jpg'])
        self.assertEqual(entries[0], {
            'name': '在看', 'is_dir': True, 'file_id': 'watching-id', 'id': 'watching-id',
            'size': None, 'modified': '1970-01-01T00:00:00Z',
        })
        self.assertEqual(entries[1]['size'], 123)
        self.assertEqual(entries[1]['modified'], '2024-01-02T03:04:05Z')
        self.assertEqual(entries[2]['size'], 456)
        self.assertEqual(entries[2]['modified'], None)
        self.assertEqual([call[0] for call in backend.calls], ['storage-root', 'anime-id'])

    def test_empty_or_slash_storage_root_maps_to_guangya_cloud_root(self):
        for root_path in ('', '/'):
            with self.subTest(root_path=root_path):
                backend = FakeBackend(root_path=root_path, entries={
                    '': [directory('anime-id', 'Anime')],
                    'anime-id': [],
                })
                client, _ = adapter_for(backend)
                self.assertEqual(client.list_all('/guangya/Anime'), [])
                self.assertEqual(backend.calls[0][0], '')

    def test_root_path_must_match_mount_and_configured_root_directory(self):
        with self.assertRaises(GuangYaInventoryError):
            adapter_for(FakeBackend(), root='/somewhere/Anime')
        with self.assertRaises(GuangYaInventoryError):
            adapter_for(FakeBackend(), root='/guangya/Other')


class GuangYaInventoryPaginationTests(unittest.TestCase):
    def test_short_pages_continue_until_stable_total(self):
        backend = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
            'anime-id': [
                directory('d1', '在看'), file('f1', '01.mkv'), directory('d2', '想看'),
            ],
        })
        client, _ = adapter_for(backend)

        rows = client.list_all('/guangya/Anime', page_size=2)

        self.assertEqual(len(rows), 3)
        self.assertEqual([(call[2]) for call in backend.calls if call[0] == 'anime-id'], [0, 1])

    def test_missing_total_and_duplicate_page_ids_fail_closed(self):
        missing_total = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
        }, overrides={
            ('anime-id', 0): {'data': {'list': [file('f1', '01.mkv')]}},
        })
        client, _ = adapter_for(missing_total)
        with self.assertRaisesRegex(GuangYaInventoryError, 'invalid total'):
            client.list_all('/guangya/Anime')

        duplicate_id = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
        }, overrides={
            ('anime-id', 0): {'data': {'list': [file('f1', '01.mkv')], 'total': 2}},
            ('anime-id', 1): {'data': {'list': [file('f1', '02.mkv')], 'total': 2}},
        })
        client, _ = adapter_for(duplicate_id)
        with self.assertRaisesRegex(GuangYaInventoryError, 'repeated a fileId'):
            client.list_all('/guangya/Anime', page_size=1)

    def test_missing_or_null_list_is_empty_only_when_total_is_zero(self):
        for directory_data in ({'total': 0}, {'total': 0, 'list': None},
                               {'total': 0, 'list': []}):
            with self.subTest(directory_data=directory_data):
                backend = FakeBackend(entries={
                    'storage-root': [directory('anime-id', 'Anime')],
                }, overrides={
                    ('anime-id', 0): {'data': directory_data},
                })
                client, _ = adapter_for(backend)
                self.assertEqual(client.list_all('/guangya/Anime'), [])

        for directory_data in ({}, {'total': 1}, {'total': 0, 'list': 'invalid'},
                               {'total': False}, {'total': '0'}):
            with self.subTest(directory_data=directory_data):
                backend = FakeBackend(entries={
                    'storage-root': [directory('anime-id', 'Anime')],
                }, overrides={
                    ('anime-id', 0): {'data': directory_data},
                })
                client, _ = adapter_for(backend)
                with self.assertRaises(GuangYaInventoryError):
                    client.list_all('/guangya/Anime')

    def test_unknown_entry_and_unstable_total_fail_closed(self):
        unknown_type = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
            'anime-id': [{'fileId': 'odd', 'fileName': 'odd', 'resType': 9}],
        })
        client, _ = adapter_for(unknown_type)
        with self.assertRaisesRegex(GuangYaInventoryError, 'unknown resType'):
            client.list_all('/guangya/Anime')

        changed_total = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
        }, overrides={
            ('anime-id', 0): {'data': {'list': [file('f1', '01.mkv')], 'total': 2}},
            ('anime-id', 1): {'data': {'list': [file('f2', '02.mkv')], 'total': 3}},
        })
        client, _ = adapter_for(changed_total)
        with self.assertRaisesRegex(GuangYaInventoryError, 'total changed'):
            client.list_all('/guangya/Anime', page_size=1)


class GuangYaInventoryPathCacheTests(unittest.TestCase):
    def test_only_scanned_parent_paths_resolve_and_concurrent_children_share_map(self):
        backend = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
            'anime-id': [directory('watch-id', '在看'), directory('want-id', '想看')],
            'watch-id': [file('watch-file', 'EP01.mkv')],
            'want-id': [file('want-file', 'EP02.mkv')],
        })
        client, created_clients = adapter_for(backend)
        with self.assertRaisesRegex(GuangYaInventoryError, 'not discovered'):
            client.list_all('/guangya/Anime/在看')
        self.assertEqual(client.list_all('/guangya/Anime'), [
            {'name': '在看', 'is_dir': True, 'file_id': 'watch-id', 'id': 'watch-id',
             'size': None, 'modified': None},
            {'name': '想看', 'is_dir': True, 'file_id': 'want-id', 'id': 'want-id',
             'size': None, 'modified': None},
        ])
        barrier = threading.Barrier(2)

        def list_child(path):
            barrier.wait(timeout=5)
            return client.list_all(path)

        with ThreadPoolExecutor(max_workers=2) as pool:
            watch_future = pool.submit(list_child, '/guangya/Anime/在看')
            want_future = pool.submit(list_child, '/guangya/Anime/想看')
            self.assertEqual(watch_future.result(timeout=5)[0]['file_id'], 'watch-file')
            self.assertEqual(want_future.result(timeout=5)[0]['file_id'], 'want-file')
        self.assertEqual(len(created_clients), 3)
        self.assertEqual(len({id(value) for value in created_clients}), 3)
        self.assertEqual({call[0] for call in backend.calls},
                         {'storage-root', 'anime-id', 'watch-id', 'want-id'})

    def test_directory_path_cache_rejects_duplicate_names_and_conflicting_ids(self):
        duplicate_names = FakeBackend(entries={
            'storage-root': [directory('anime-id', 'Anime')],
            'anime-id': [directory('one', '在看'), directory('two', '在看')],
        })
        client, _ = adapter_for(duplicate_names)
        with self.assertRaisesRegex(GuangYaInventoryError, 'duplicate directory names'):
            client.list_all('/guangya/Anime')

        backend = FakeBackend()
        client, _ = adapter_for(backend)
        client._directory_ids['/guangya/Anime/在看'] = 'old-id'
        normalized = client._normalize_entry(directory('new-id', '在看'))
        with self.assertRaisesRegex(GuangYaInventoryError, 'conflicting fileIds'):
            client._cache_child_directories('/guangya/Anime', [normalized])

    def test_api_error_text_does_not_leak_credentials(self):
        class SecretFailureClient(FakeGuangYaClient):
            def get_file_list(self, *, parent_id, page_size, page):
                raise GuangYaAPIError('Authorization: secret-token')

        backend = FakeBackend()
        clients = []

        def factory(_config):
            if not clients:
                client = SecretFailureClient(backend)
            else:
                client = FakeGuangYaClient(backend)
            clients.append(client)
            return client

        adapter = GuangYaInventoryClient(
            {'alist': {'root': '/guangya/Anime'}, 'guangya': {'root_dir': 'Anime'}},
            {'alist': {'root': '/guangya/Anime'}}, client_factory=factory,
        )
        with self.assertRaises(GuangYaInventoryError) as raised:
            adapter.list_all('/guangya/Anime')
        self.assertNotIn('secret-token', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
