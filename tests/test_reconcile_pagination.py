"""Fresh AList reads refresh once, then paginate the refreshed listing."""
import unittest
from unittest.mock import Mock

from scripts.reconcile_cloud_library import AListClient


class FreshPaginationTests(unittest.TestCase):
    def client(self):
        client = AListClient.__new__(AListClient)
        client.cfg = {"page_size": 2}
        client._post = Mock(side_effect=[
            {"data": {"total": 3, "content": [{"name": "a"}, {"name": "b"}]}},
            {"data": {"total": 3, "content": [{"name": "c"}]}},
        ])
        return client

    def test_refresh_first_page_then_read_same_listing(self):
        client = self.client()
        self.assertEqual([row["name"] for row in client.list_all("/library", refresh=True)], ["a", "b", "c"])
        requests = [call.args[1] for call in client._post.call_args_list]
        self.assertEqual([(row["page"], row["refresh"]) for row in requests], [(1, True), (2, False)])

    def test_cached_read_never_forces_refresh(self):
        client = self.client()
        client.list_all("/library", refresh=False)
        self.assertEqual([call.args[1]["refresh"] for call in client._post.call_args_list], [False, False])


if __name__ == "__main__":
    unittest.main()
