from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import app


class DevicesConnectionQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.device_hash = "0bbaf1d7919ef6ffb9734b237d0a6477b8a4fe54e1c6705fc032e094a81d21f6"

    @staticmethod
    def _write_authed_config(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "base_url": "https://gateway.dataplicity.com",
                    "auth_method": "api_key",
                    "api_key": "test-key",
                }
            ),
            encoding="utf-8",
        )

    def test_prefers_remote_access_status_endpoint_for_quality(self) -> None:
        status_payload = {
            "device_hash": self.device_hash,
            "device_status": "online",
            "m2m_identity_cached": True,
            "router_host_lookup": {"ok": True, "status_code": 200, "detail": None},
            "connection_quality_24h": {
                "window_hours": 24,
                "bucket_minutes": 5,
                "points": [{"ts": "2026-06-28T09:00:00+00:00", "status": "good", "latency_ms": 42}],
            },
        }

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            if path == f"/api/developer/devices/{self.device_hash}/remote-access-status/":
                return ApiResponse(True, 200, status_payload, json.dumps(status_payload))
            raise AssertionError(f"unexpected GET path: {path}")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    ["--json", "--config", str(config_path), "devices", "connection-quality", self.device_hash],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("device_hash"), self.device_hash)
        self.assertEqual((payload.get("connection_quality_24h") or {}).get("window_hours"), 24)
        self.assertIsNone(payload.get("warning"))

    def test_falls_back_when_remote_access_status_endpoint_missing(self) -> None:
        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            if path == f"/api/developer/devices/{self.device_hash}/remote-access-status/":
                return ApiResponse(False, 404, {"detail": "Not found."}, '{"detail":"Not found."}')
            if path == f"/api/developer/devices/{self.device_hash}/":
                return ApiResponse(True, 200, {"status": "online", "last_heartbeat": 1719559200000}, "")
            if path == f"/api/remote/devices/{self.device_hash}/host/":
                return ApiResponse(True, 200, {"m2m_url": "wss://m2m.dataplicity.com/m2m/"}, "")
            raise AssertionError(f"unexpected GET path: {path}")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    ["--json", "--config", str(config_path), "devices", "connection-quality", self.device_hash],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertIsNone(payload.get("connection_quality_24h"))
        self.assertIn("unavailable on this API host", str(payload.get("warning")))


if __name__ == "__main__":
    unittest.main()
