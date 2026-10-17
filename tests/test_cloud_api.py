import os
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import requests


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from cloud_api import AListTokenProvider, GuangYaAPIError, GuangYaClient  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpResponse(FakeResponse):
    status_code = 200

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def request(self, *args, **kwargs):
        return FakeHttpResponse(self.payload)


class FakeCredentials:
    def get(self, refresh=False):
        return {"access_token": "token", "device_id": "device"}


class GuangYaEnvelopeTests(unittest.TestCase):
    def test_accepts_real_success_envelope_without_code(self):
        payload = GuangYaClient._decode(FakeResponse({"msg": "success", "data": {"list": []}}),
                                        expected_data="list")
        self.assertEqual(payload["data"]["list"], [])

    def test_explicit_failure_is_rejected_even_with_success_code_and_data(self):
        with self.assertRaises(GuangYaAPIError):
            GuangYaClient._decode(FakeResponse({"msg": "failed", "code": 200, "data": {"taskId": "x"}}))

    def test_error_text_does_not_echo_arbitrary_business_message(self):
        with self.assertRaises(GuangYaAPIError) as caught:
            GuangYaClient._decode(FakeResponse({
                "msg": "error Bearer private-token", "code": 403, "data": {},
            }))
        self.assertNotIn("private-token", str(caught.exception))

    def test_quota_rejection_retains_business_code(self):
        with self.assertRaises(GuangYaAPIError) as caught:
            GuangYaClient._decode(FakeResponse({"msg": "error", "code": 354, "data": {}}))
        self.assertEqual(caught.exception.code, 354)

    def test_unknown_message_without_known_success_code_fails_closed(self):
        with self.assertRaises(GuangYaAPIError):
            GuangYaClient._decode(FakeResponse({"msg": "maybe", "data": {"list": []}}),
                                  expected_data="list")

    def test_empty_directory_accepts_missing_or_null_list_only_with_integer_zero_total(self):
        for data in ({"total": 0}, {"total": 0, "list": None}):
            with self.subTest(data=data):
                payload = GuangYaClient._decode(
                    FakeResponse({"msg": "success", "data": data}),
                    expected_data="list", allow_empty_list_without_rows=True,
                )
                self.assertEqual(payload["data"]["list"], [])

    def test_file_list_normalizes_confirmed_success_with_empty_data_object(self):
        client = GuangYaClient(
            {}, session=FakeSession({"msg": "success", "data": {}}),
            credential_provider=FakeCredentials(),
        )

        payload = client.get_file_list(parent_id="1957716102180147216")

        self.assertEqual(payload["data"], {"total": 0, "list": []})

    def test_empty_directory_exception_does_not_hide_nonempty_or_malformed_pages(self):
        for data in ({"total": 1}, {"total": 0, "list": "bad"},
                     {"total": False}, {"total": "0"}, {"unexpected": True}):
            with self.subTest(data=data), self.assertRaises(GuangYaAPIError):
                GuangYaClient._decode(
                    FakeResponse({"msg": "success", "data": data}),
                    expected_data="list", allow_empty_list_without_rows=True,
                )

        for payload in (
            {"msg": "ok", "data": {}},
            {"msg": "success", "data": None},
            {"msg": "error", "data": {}},
        ):
            with self.subTest(payload=payload), self.assertRaises(GuangYaAPIError):
                GuangYaClient._decode(
                    FakeResponse(payload), expected_data="list",
                    allow_empty_list_without_rows=True,
                )

    def test_task_list_still_requires_list_even_when_total_is_zero(self):
        with self.assertRaises(GuangYaAPIError):
            GuangYaClient._decode(
                FakeResponse({"msg": "success", "data": {"total": 0}}),
                expected_data="list",
            )

    def test_file_list_sends_explicit_stable_sort_and_type_fields(self):
        client = GuangYaClient.__new__(GuangYaClient)
        calls = []

        def fake_request(self, method, path, *, data=None, **kwargs):
            calls.append((method, path, dict(data), kwargs))
            return {"data": {"total": 0, "list": []}}

        client._request = types.MethodType(fake_request, client)
        client.get_file_list(parent_id="folder", page_size=100, page=2)
        client.get_file_list(parent_id="folder", page_size=100, page=3,
                             deadline_monotonic=123.5)
        self.assertEqual(calls[0][:3], (
            "POST", "/nd.bizuserres.s/v1/file/get_file_list",
            {"parentId": "folder", "page": 2, "pageSize": 100,
             "orderBy": 3, "sortType": 1, "fileTypes": []},
        ))
        self.assertTrue(calls[0][3]["allow_empty_list_without_rows"])
        self.assertIsNone(calls[0][3]["deadline_monotonic"])
        self.assertEqual(calls[1][3]["deadline_monotonic"], 123.5)


class GuangYaRefreshCoordinationTests(unittest.TestCase):
    def test_concurrent_expired_workers_refresh_alist_once_and_reuse_new_token(self):
        state = {"token": "expired-token", "refreshes": 0}
        state_lock = threading.Lock()
        config = {"alist": {
            "database_path": os.path.join(os.path.abspath(os.getcwd()),
                                           "unique-refresh-coordination-test.db"),
            "guangya_mount_path": "/guangya",
        }}
        providers = [AListTokenProvider(config) for _ in range(4)]
        for provider in providers:
            def read_db(provider=provider):
                with state_lock:
                    return {"access_token": state["token"], "device_id": "device"}

            def refresh(provider=provider):
                time.sleep(0.03)
                with state_lock:
                    state["refreshes"] += 1
                    state["token"] = "fresh-token"

            provider._from_alist_db = read_db
            provider._refresh_through_alist = refresh

        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            results = list(pool.map(
                lambda provider: provider.get(refresh=True,
                                              expired_access_token="expired-token"),
                providers,
            ))

        self.assertEqual(state["refreshes"], 1)
        self.assertEqual({row["access_token"] for row in results}, {"fresh-token"})


class GuangYaTaskPaginationTests(unittest.TestCase):
    def test_uses_cursor_and_deduplicates_overlapping_task_rows(self):
        client = GuangYaClient.__new__(GuangYaClient)
        calls = []
        pages = [
            {"data": {"list": [{"taskId": "1"}, {"taskId": "2"}],
                      "hasMore": True, "cursor": "opaque-one", "total": 3}},
            {"data": {"list": [{"taskId": "2"}, {"taskId": "3"}],
                      "cursor": "opaque-two", "total": 3}},
        ]

        def fake_request(self, method, path, *, data=None, readonly=False,
                         expected_data=None, deadline_monotonic=None):
            calls.append(dict(data))
            return pages[len(calls) - 1]

        client._request = types.MethodType(fake_request, client)
        tasks = client.list_all_tasks(page_size=2)
        self.assertEqual([task["taskId"] for task in tasks], ["1", "2", "3"])
        self.assertEqual(calls[0], {"page": 0, "pageSize": 2})
        self.assertEqual(calls[1], {"page": 0, "pageSize": 2, "cursor": "opaque-one"})

    def test_truncated_cursor_sequence_fails_closed(self):
        client = GuangYaClient.__new__(GuangYaClient)
        page = {"data": {"list": [{"taskId": "1"}], "hasMore": False, "total": 2}}
        client._request = types.MethodType(lambda *args, **kwargs: page, client)
        with self.assertRaises(GuangYaAPIError):
            client.list_all_tasks()

    def test_boolean_total_is_not_treated_as_an_empty_task_scan(self):
        client = GuangYaClient.__new__(GuangYaClient)
        page = {"data": {"list": [], "hasMore": False, "total": False}}
        client._request = types.MethodType(lambda *args, **kwargs: page, client)
        with self.assertRaisesRegex(GuangYaAPIError, "no valid total"):
            client.list_all_tasks()

    def test_missing_has_more_is_rejected_before_cumulative_total(self):
        client = GuangYaClient.__new__(GuangYaClient)
        page = {"msg": "success", "data": {"list": [{"taskId": "1"}], "total": 2}}
        client._request = types.MethodType(lambda *args, **kwargs: page, client)
        with self.assertRaisesRegex(GuangYaAPIError, "omitted hasMore"):
            client.list_all_tasks()

    def test_explicit_non_boolean_has_more_is_rejected_even_at_total(self):
        client = GuangYaClient.__new__(GuangYaClient)
        page = {"msg": "success", "data": {
            "list": [{"taskId": "1"}], "total": 1, "hasMore": 0,
        }}
        client._request = types.MethodType(lambda *args, **kwargs: page, client)
        with self.assertRaisesRegex(GuangYaAPIError, "valid hasMore"):
            client.list_all_tasks()

    def test_true_has_more_conflicts_with_cumulative_total(self):
        client = GuangYaClient.__new__(GuangYaClient)
        page = {"msg": "success", "data": {
            "list": [{"taskId": "1"}], "total": 1, "hasMore": True,
        }}
        client._request = types.MethodType(lambda *args, **kwargs: page, client)
        with self.assertRaisesRegex(GuangYaAPIError, "hasMore conflicts"):
            client.list_all_tasks()

    def test_new_account_empty_success_data_object_is_a_valid_empty_scan(self):
        client = GuangYaClient(
            {}, session=FakeSession({"msg": "success", "data": {}}),
            credential_provider=FakeCredentials(),
        )
        self.assertEqual(client.list_all_tasks(), [])

    def test_error_envelope_with_empty_data_is_not_an_empty_task_scan(self):
        client = GuangYaClient(
            {}, session=FakeSession({"msg": "error", "data": {}}),
            credential_provider=FakeCredentials(),
        )
        with self.assertRaises(GuangYaAPIError):
            client.list_all_tasks()

    def test_empty_task_normalization_does_not_accept_other_incomplete_envelopes(self):
        for envelope in (
            {"msg": "success", "data": None},
            {"msg": "success", "data": {"total": 0}},
            {"msg": "success", "data": {"total": 0, "list": None}},
        ):
            client = GuangYaClient.__new__(GuangYaClient)
            client._request = types.MethodType(
                lambda *args, _payload=envelope, **kwargs: _payload, client,
            )
            with self.subTest(envelope=envelope), self.assertRaises(GuangYaAPIError):
                client.list_all_tasks()


class GuangYaReadonlyRequestTests(unittest.TestCase):
    class SequencedSession:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def test_readonly_request_retries_transient_timeouts_with_configured_timeout(self):
        session = self.SequencedSession([
            requests.Timeout("Bearer private-token"),
            requests.Timeout("Bearer private-token"),
            FakeHttpResponse({"msg": "success", "data": {"total": 0, "list": []}}),
        ])
        client = GuangYaClient(
            {"readonly_request_timeout_seconds": 20, "readonly_max_attempts": 3},
            session=session, credential_provider=FakeCredentials(),
        )

        with mock.patch("cloud_api.time.sleep"):
            result = client.get_file_list()

        self.assertEqual(result["data"]["total"], 0)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual([call[2]["timeout"] for call in session.calls], [(10.0, 20.0)] * 3)

    def test_write_request_is_never_retried_even_when_readonly_attempts_are_configured(self):
        session = self.SequencedSession([requests.Timeout("private")])
        client = GuangYaClient(
            {"readonly_request_timeout_seconds": 20, "readonly_max_attempts": 4},
            session=session, credential_provider=FakeCredentials(),
        )

        with self.assertRaises(GuangYaAPIError) as caught:
            client.create_task({"newName": "EP01.mkv"})

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["timeout"], 45)
        self.assertNotIn("private", str(caught.exception))

    def test_readonly_request_timeout_is_bounded_by_remaining_scan_deadline(self):
        session = self.SequencedSession([
            FakeHttpResponse({"msg": "success", "data": {"list": []}}),
        ])
        client = GuangYaClient(
            {"readonly_request_timeout_seconds": 20},
            session=session, credential_provider=FakeCredentials(),
        )

        with mock.patch("cloud_api.time.monotonic", side_effect=[100.0, 100.1]):
            client._request("POST", "/readonly", readonly=True,
                            expected_data="list", deadline_monotonic=110.0)

        self.assertEqual(session.calls[0][2]["timeout"], (10.0, 10.0))

    def test_expired_request_deadline_prevents_network_request(self):
        session = self.SequencedSession([])
        client = GuangYaClient({}, session=session, credential_provider=FakeCredentials())

        with mock.patch("cloud_api.time.monotonic", return_value=101.0):
            with self.assertRaises(GuangYaAPIError):
                client._request("POST", "/readonly", readonly=True,
                                deadline_monotonic=100.0)

        self.assertEqual(session.calls, [])

    def test_task_scan_deadline_rejects_partial_cursor_results(self):
        client = GuangYaClient.__new__(GuangYaClient)
        client.cloud_task_scan_timeout_seconds = 10.0
        clock = [0.0]
        calls = []
        pages = [
            {"data": {"list": [{"taskId": "1"}], "hasMore": True,
                      "cursor": "opaque-cursor-one", "total": 3}},
            {"data": {"list": [{"taskId": "2"}], "hasMore": True,
                      "cursor": "opaque-cursor-two", "total": 3}},
        ]

        def fake_request(self, method, path, *, data=None, deadline_monotonic=None,
                         **kwargs):
            calls.append((dict(data), deadline_monotonic))
            clock[0] += 6.0
            return pages[len(calls) - 1]

        client._request = types.MethodType(fake_request, client)
        with mock.patch("cloud_api.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(GuangYaAPIError, "scan exceeded"):
                client.list_all_tasks(page_size=1)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], 10.0)

    def test_task_scan_logs_aggregate_progress_without_cursor_or_url(self):
        client = GuangYaClient.__new__(GuangYaClient)
        client.cloud_task_scan_timeout_seconds = 900.0
        calls = []

        def fake_request(self, method, path, *, data=None, deadline_monotonic=None,
                         **kwargs):
            calls.append(dict(data))
            index = len(calls)
            return {"data": {
                "list": [{"taskId": str(index)}],
                "hasMore": index < 10,
                "cursor": f"private-cursor-{index}",
                "total": 10,
            }}

        client._request = types.MethodType(fake_request, client)
        with self.assertLogs("cloud_api", level="INFO") as logs:
            tasks = client.list_all_tasks(page_size=1)

        self.assertEqual(len(tasks), 10)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("pages=10", logs.output[0])
        self.assertIn("rows=10", logs.output[0])
        self.assertNotIn("private-cursor", logs.output[0])
        self.assertNotIn("http", logs.output[0].lower())

    def test_readonly_settings_are_bounded(self):
        for config in (
            {"readonly_request_timeout_seconds": 0},
            {"readonly_request_timeout_seconds": 121},
            {"readonly_request_timeout_seconds": True},
            {"readonly_max_attempts": 0},
            {"readonly_max_attempts": 5},
            {"readonly_max_attempts": 2.0},
            {"cloud_task_scan_timeout_seconds": 3601},
        ):
            with self.subTest(config=config), self.assertRaises(GuangYaAPIError):
                GuangYaClient(config)


if __name__ == "__main__":
    unittest.main()
