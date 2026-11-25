import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magpie_usage.client import MagpieClient, MagpieError
from magpie_usage.metrics import build_snapshot, text
from magpie_usage.state import CollectionState
from magpie_usage.__main__ import save, main


AT = 1795172757


def payloads():
    return (
        {"data": [{"windows": [{"name": "weekly", "remaining": 69},
                                {"name": "daily", "remaining": 90}]}]},
        {"series": [{"calls": 7, "errors": 2, "input": 100, "output": 20,
                     "reasoning": 5, "cache_read": 30, "timed": 5, "ttft_ms": 6250}],
         "input": 100, "output": 20, "cache_read": 30, "reasoning": 5,
         "sessionCount": 3, "calls": 7, "cost": 0.25,
         "models": [{"name": "one", "calls": 2}, {"name": "two", "calls": 5}]},
        {"rows": [{"served": "gpt-6.1-sol", "status": 200, "ms": 3102,
                   "prompt": "PRIVATE", "api_key": "SECRET"}]},
    )


class MetricsTests(unittest.TestCase):
    def test_original_aggregation_and_timezone(self):
        value = build_snapshot(*payloads(), at=AT)
        self.assertEqual(value["success_permille"], 714)
        self.assertEqual(value["tokens_today"], 155)
        self.assertEqual(value["average_ttft_ms"], 1250)
        self.assertEqual(value["remaining_percent"], 69)
        self.assertEqual(value["limit_window_minutes"], 10080)
        self.assertEqual(value["top_model"], "two")
        self.assertEqual(value["cache_hit_permille"], 231)
        self.assertEqual(value["total_cost_micros"], 250000)
        self.assertEqual(value["updated_at"], "11-20 19:05:57")

    def test_missing_data_does_not_claim_zero_or_100_percent(self):
        value = build_snapshot({}, {}, {}, at=AT)
        for field in ("remaining_percent", "success_permille", "tokens_today",
                      "cache_hit_permille", "latest_model", "session_count", "total_cost_micros"):
            self.assertIsNone(value[field], field)

    def test_observed_zero_is_preserved_but_zero_calls_have_no_rate(self):
        value = build_snapshot({}, {"series": [{"calls": 0, "errors": 0, "input": 0}], "cost": 0}, {}, at=AT)
        self.assertEqual(value["calls_today"], 0)
        self.assertEqual(value["tokens_today"], 0)
        self.assertEqual(value["total_cost_micros"], 0)
        self.assertIsNone(value["success_permille"])

    def test_missing_errors_do_not_claim_all_calls_succeeded(self):
        value = build_snapshot({}, {"series": [{"calls": 3}]}, {}, at=AT)
        self.assertIsNone(value["errors_today"])
        self.assertIsNone(value["success_permille"])

    def test_unlimited_window_is_not_treated_as_lowest_quota(self):
        quotas, usage, requests = payloads()
        quotas["data"][0]["windows"].append({"remaining": 1, "unlimited": True})
        self.assertEqual(build_snapshot(quotas, usage, requests, at=AT)["remaining_percent"], 69)

    def test_booleans_negative_and_nonfinite_are_not_counts(self):
        value = build_snapshot({}, {"series": [{"calls": True, "errors": -1, "input": float("nan")}]}, {}, at=AT)
        self.assertIsNone(value["calls_today"])
        self.assertIsNone(value["errors_today"])
        self.assertIsNone(value["tokens_today"])
        json.dumps(value, allow_nan=False)

    def test_raw_requests_and_secrets_are_not_projected(self):
        encoded = json.dumps(build_snapshot(*payloads(), at=AT))
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("api_key", encoded)

    def test_utf8_model_name_budget_and_control_characters(self):
        value = text("模型" * 40 + "\x00\n")
        self.assertLessEqual(len(value.encode()), 63)
        self.assertNotIn("\x00", value)
        self.assertNotIn("\n", value)
        self.assertEqual(value.encode().decode(), value)


class TimeoutTests(unittest.TestCase):
    def test_request_timeout_preserves_last_success_and_is_not_a_new_sample(self):
        from unittest.mock import Mock
        state=CollectionState();state.succeed(payloads(),AT,10)
        opener=Mock();opener.open.side_effect=TimeoutError('deadline')
        with patch('magpie_usage.client.build_opener',return_value=opener):
            try:
                state.succeed(MagpieClient(timeout=.05).collect(),AT+1,11)
            except MagpieError:
                state.fail('timeout')
        self.assertEqual(state.view(70)['snapshot']['generated_at'],AT)
        self.assertEqual(state.view(70)['status'],'stale')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],.05)

    def test_partial_parallel_collection_is_rejected(self):
        def get(path):
            if path=='/':return {'name':'magpie'}
            if path.endswith('quotas'):raise MagpieError('timeout')
            return {}
        with patch.object(MagpieClient,'get',side_effect=get):
            with self.assertRaises(MagpieError):MagpieClient().collect()


class StateTests(unittest.TestCase):
    def test_waiting_has_no_fabricated_timestamp(self):
        state = CollectionState()
        state.fail("unavailable")
        self.assertIsNone(state.view(10)["snapshot"])
        self.assertEqual(state.view(10)["status"], "waiting")

    def test_failure_retains_success_time_then_stales_and_recovers(self):
        state = CollectionState()
        state.succeed(payloads(), AT, 10)
        state.fail("unavailable")
        self.assertEqual(state.view(30)["snapshot"]["generated_at"], AT)
        self.assertEqual(state.view(69.999)["status"], "ready")
        self.assertEqual(state.view(70)["status"], "stale")
        self.assertEqual(state.view(70)["snapshot"]["updated_at"], "11-20 19:05:57")
        state.succeed(payloads(), AT + 80, 90)
        self.assertEqual(state.view(90)["status"], "ready")
        self.assertIsNone(state.view(90)["error"])
        self.assertEqual(state.view(90)["snapshot"]["generated_at"], AT + 80)

    def test_rendering_does_not_change_snapshot_or_update_time(self):
        state = CollectionState()
        state.succeed(payloads(), AT, 10)
        view = state.view(20)
        view["snapshot"]["generated_at"] = 0
        self.assertEqual(state.view(21)["snapshot"]["generated_at"], AT)

    def test_new_observation_uses_completion_time_even_for_same_metrics(self):
        state = CollectionState()
        state.succeed(payloads(), AT, 10)
        state.succeed(payloads(), AT + 15, 25)
        self.assertEqual(state.view(26)["snapshot"]["updated_at"], "11-20 19:06:12")


class ClientTests(unittest.TestCase):
    def test_rejects_remote_hosts_credentials_and_nonroot_urls(self):
        for url in ("https://127.0.0.1:3425", "http://example.com", "http://user:pass@localhost:3425",
                    "http://127.0.0.1:3425/private", "http://127.0.0.1:3425/?key=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                MagpieClient(url)

    def test_health_is_checked_before_metrics(self):
        client = MagpieClient()
        with patch.object(client, "get", return_value={"name": "other"}) as get:
            with self.assertRaises(MagpieError):
                client.collect()
            get.assert_called_once_with("/")

    def test_reads_exactly_the_three_confirmed_metric_apis(self):
        client = MagpieClient()
        def get(path):
            return {"name": "magpie"} if path == "/" else {}
        with patch.object(client, "get", side_effect=get) as request:
            self.assertEqual(client.collect(), ({}, {}, {}))
            self.assertEqual({call.args[0] for call in request.call_args_list}, {
                "/", "/v1/magpie/quotas", "/v1/magpie/usage", "/v1/magpie/usage/requests?limit=1"})


class OutputTests(unittest.TestCase):
    def test_output_paths_cannot_replace_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            lock = base / '.collector.lock'
            lock.write_text('existing lock')
            for option in ('--cache-file', '--feed-file', '--output'):
                arguments = ['magpie_usage', '--once', '--cache-file', str(base/'cache.json')]
                if option == '--cache-file':
                    arguments[-1] = str(lock)
                else:
                    arguments += [option, str(lock)]
                with patch('sys.argv', arguments), contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main(), 1)
                self.assertIn('进程锁', error.getvalue())
                self.assertEqual(lock.read_text(), 'existing lock')

    def test_local_output_round_trip_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save(path, {"status": "waiting", "snapshot": None})
            self.assertEqual(json.loads(path.read_text())["status"], "waiting")
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                save(link, {})
            self.assertEqual(json.loads(path.read_text())["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
