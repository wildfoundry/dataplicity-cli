from __future__ import annotations

import datetime as dt
import unittest

from dataplicity_cli.cli import (
    LOGGING_MAX_OUTPUT_ITEMS,
    _extract_log_device,
    _extract_objects,
    _extract_port_forwarding_ports,
    _extract_port_numbers,
    _find_allowed_ports,
    _format_log_ts,
    _friendly_response_message,
    _org_logs_params_from_developer_params,
    _parse_kv_pairs,
    _parse_log_time_expr,
    _resource_key,
    _resource_name,
    _resource_status,
    _sanitize_payload,
    _truncate_logging_payload,
)


class CliCoreHelpersTest(unittest.TestCase):
    def test_sanitize_payload_recursively_strips_sensitive_fields(self) -> None:
        payload = {
            "safe": "ok",
            "token": "secret",
            "nested": {"api_key": "hide", "keep": 1},
            "list": [{"refresh": "hide"}, {"value": 2}],
        }
        sanitized = _sanitize_payload(payload)
        self.assertEqual(sanitized, {"safe": "ok", "nested": {"keep": 1}, "list": [{}, {"value": 2}]})

    def test_friendly_response_message_uses_detail_and_invalid_auth_override(self) -> None:
        message = _friendly_response_message("fallback", {"detail": "Token has expired"}, "")
        self.assertIn("Saved login appears expired", message)

        message = _friendly_response_message("fallback", {"non_field_errors": ["bad request"]}, "")
        self.assertEqual(message, "bad request")

    def test_extract_port_numbers_supports_int_ranges_and_dict_ranges(self) -> None:
        self.assertEqual(_extract_port_numbers(443), {443})
        self.assertEqual(_extract_port_numbers("22, 80 443"), {22, 80, 443})
        self.assertEqual(_extract_port_numbers("1000-1002"), {1000, 1001, 1002})
        self.assertEqual(_extract_port_numbers({"start": 9000, "end": 9002}), {9000, 9001, 9002})
        self.assertEqual(_extract_port_numbers("70000"), set())

    def test_extract_port_forwarding_ports_handles_none_and_mixed_list(self) -> None:
        self.assertIsNone(_extract_port_forwarding_ports({"other": "value"}))
        ports = _extract_port_forwarding_ports({"port_forwarding_ports": [22, "80-81", {"start": 9000, "end": 9001}]})
        self.assertEqual(ports, [22, 80, 81, 9000, 9001])

    def test_find_allowed_ports_walks_nested_payload(self) -> None:
        payload = {
            "metadata": {
                "port_whitelist": ["22", "80-81"],
                "nested": [{"supported_ports": [443]}],
            }
        }
        self.assertEqual(_find_allowed_ports(payload), {22, 80, 81, 443})

    def test_org_logs_params_mapping_prefers_explicit_search(self) -> None:
        params = {
            "page_size": 55,
            "device": "abc",
            "path": "/devices/abc",
            "search": "timeout",
            "level": "error",
            "since": "2026-06-01T00:00:00Z",
            "until": "2026-06-01T01:00:00Z",
        }
        mapped = _org_logs_params_from_developer_params(params)
        self.assertEqual(
            mapped,
            {
                "limit": 55,
                "device_hash": "abc",
                "search": "timeout",
                "level": "error",
                "after": "2026-06-01T00:00:00Z",
                "before": "2026-06-01T01:00:00Z",
            },
        )

    def test_parse_log_time_expr_handles_relative_and_iso_inputs(self) -> None:
        now = dt.datetime(2026, 6, 29, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(_parse_log_time_expr("15m", now=now), "2026-06-28T23:45:00Z")
        self.assertEqual(_parse_log_time_expr("2026-06-01T00:00:00", now=now), "2026-06-01T00:00:00Z")
        with self.assertRaises(ValueError):
            _parse_log_time_expr("not-a-time", now=now)

    def test_truncate_logging_payload_for_large_result_sets(self) -> None:
        payload = {"results": [{"message": "x" * 3000}] * (LOGGING_MAX_OUTPUT_ITEMS + 5)}
        safe_payload, truncated, dropped = _truncate_logging_payload(payload)
        self.assertTrue(truncated)
        self.assertEqual(dropped, 5)
        self.assertIn("__cli_truncated__", safe_payload)
        first_message = safe_payload["results"][0]["message"]
        self.assertIn("[truncated", first_message)

    def test_format_log_timestamp_and_extract_device(self) -> None:
        self.assertEqual(_format_log_ts(0), "1970-01-01T00:00:00Z")
        self.assertEqual(_format_log_ts("bad"), "")
        self.assertEqual(_extract_log_device({"device_hash": "hash-a"}), "hash-a")
        self.assertEqual(_extract_log_device({"message": "connection device_hash=hash-b"}), "hash-b")

    def test_parse_and_extract_resource_helpers(self) -> None:
        self.assertEqual(_parse_kv_pairs(["a=1", "bad", "b=2"]), {"a": "1", "b": "2"})
        items = _extract_objects({"results": [{"id": 1}, "skip", {"id": 2}]})
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(_resource_key({"uuid": "abc"}), "abc")
        self.assertEqual(_resource_name({"title": "My Monitor"}), "My Monitor")
        self.assertEqual(_resource_status({"metadata": {"health": "healthy"}}), "healthy")


if __name__ == "__main__":
    unittest.main()
