from __future__ import annotations

import os
import unittest
from unittest.mock import Mock

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import (
    _connection_quality_points,
    _device_is_active,
    _device_is_online,
    _extract_devices,
    _render_latency_sparkline,
    _render_quality_status_bar,
    _resolve_m2m_url,
    _ssh_host_key_options,
    _sort_devices_for_display,
)


class DeviceCliHelpersTest(unittest.TestCase):
    def test_ssh_host_key_options_use_platform_null_device(self) -> None:
        self.assertEqual(_ssh_host_key_options(True), [])
        self.assertEqual(
            _ssh_host_key_options(False),
            ["-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}"],
        )

    def test_extract_devices_includes_limited_devices_bucket(self) -> None:
        payload = {
            "devices": [{"hash_id": "active-1"}],
            "limited_devices": [{"hash_id": "limited-1"}],
        }
        rows = _extract_devices(payload)
        hashes = [str(item.get("hash_id")) for item in rows]
        self.assertEqual(hashes, ["active-1", "limited-1"])

    def test_device_active_prefers_explicit_flags(self) -> None:
        self.assertTrue(_device_is_active({"is_active": True}))
        self.assertFalse(_device_is_active({"is_active": False}))
        self.assertTrue(_device_is_active({"enabled": True}))
        self.assertFalse(_device_is_active({"enabled": False}))
        self.assertTrue(_device_is_active({}))

    def test_sort_devices_prioritizes_active_then_online_then_name(self) -> None:
        devices = [
            {"hash_id": "z-limited-online", "name": "Zulu", "status": "online", "is_active": False},
            {"hash_id": "a-active-offline", "name": "Alpha", "status": "offline", "is_active": True},
            {"hash_id": "b-active-online", "name": "Beta", "status": "online", "is_active": True},
            {"hash_id": "m-limited-offline", "name": "Mike", "status": "offline", "is_active": False},
        ]
        ordered = _sort_devices_for_display(devices)
        hashes = [str(item.get("hash_id")) for item in ordered]
        self.assertEqual(
            hashes,
            [
                "b-active-online",
                "a-active-offline",
                "z-limited-online",
                "m-limited-offline",
            ],
        )

    def test_device_online_uses_boolean_fields(self) -> None:
        self.assertTrue(_device_is_online({"is_online": True}))
        self.assertFalse(_device_is_online({"is_online": False}))
        self.assertTrue(_device_is_online({"online": True}))
        self.assertFalse(_device_is_online({"online": False}))
        self.assertTrue(_device_is_online({"status": "online"}))

    def test_connection_quality_chart_helpers_render_colored_output(self) -> None:
        payload = {
            "points": [
                {"status": "good", "latency_ms": 40},
                {"status": "degraded", "latency_ms": 180},
                {"status": "poor", "latency_ms": 420},
                {"status": "unknown"},
            ]
        }
        points = _connection_quality_points(payload)
        self.assertEqual(len(points), 4)
        status_bar = _render_quality_status_bar(points, width=4)
        latency_line = _render_latency_sparkline(points, width=4)
        self.assertIn("[green]", status_bar)
        self.assertIn("[yellow]", status_bar)
        self.assertIn("[red]", status_bar)
        self.assertIn("[dim]", status_bar)
        self.assertTrue(any(glyph in latency_line for glyph in "▁▂▃▄▅▆▇█"))

    def test_latency_sparkline_handles_missing_data(self) -> None:
        points = [{"status": "unknown"}, {"status": "unknown"}]
        rendered = _render_latency_sparkline(points, width=4)
        self.assertIn("latency unavailable", rendered)


class ResolveM2MUrlTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_router_device_is_reported_as_offline(self) -> None:
        state = Mock()
        state.api.get.return_value = ApiResponse(
            False,
            404,
            {"detail": "unknown device"},
            '{"detail":"unknown device"}',
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Device is offline or unavailable for Remote Access",
        ):
            await _resolve_m2m_url(state, "offline-device")

    async def test_host_lookup_uses_requested_timeout(self) -> None:
        state = Mock()
        state.api.request.return_value = ApiResponse(
            False,
            0,
            None,
            "request timed out",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Timed out after 10s while resolving remote host",
        ):
            await _resolve_m2m_url(
                state,
                "offline-device",
                request_timeout=10,
            )

        state.api.request.assert_called_once_with(
            "GET",
            "/api/remote/devices/offline-device/host/",
            timeout=10,
        )


if __name__ == "__main__":
    unittest.main()
