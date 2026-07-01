from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import app


class DevicesHistoryCliTest(unittest.TestCase):
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

    def test_history_list_returns_timeline_rows_in_json(self) -> None:
        timeline_payload = {
            "results": [
                {
                    "id": 321,
                    "timestamp": "2026-06-29T02:00:00Z",
                    "source": "operator",
                    "type": "service",
                    "author_display": "ops@example.com",
                    "message": "Power-cycled after maintenance window.",
                    "is_deleted": False,
                }
            ],
            "count": 1,
        }

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            self.assertEqual(path, f"/api/developer/devices/{self.device_hash}/timeline/")
            self.assertEqual((params or {}).get("limit"), 120)
            return ApiResponse(True, 200, timeline_payload, json.dumps(timeline_payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    ["--json", "--config", str(config_path), "devices", "history", "list", self.device_hash],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("count"), 1)
        self.assertEqual((payload.get("results") or [{}])[0].get("id"), 321)

    def test_history_show_returns_selected_row(self) -> None:
        timeline_payload = {
            "results": [
                {"id": 100, "message": "Older event"},
                {"id": 555, "message": "Target event", "type": "logistics"},
            ],
            "count": 2,
        }

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            self.assertEqual(path, f"/api/developer/devices/{self.device_hash}/timeline/")
            self.assertEqual((params or {}).get("limit"), 500)
            return ApiResponse(True, 200, timeline_payload, json.dumps(timeline_payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    [
                        "--json",
                        "--config",
                        str(config_path),
                        "devices",
                        "history",
                        "show",
                        self.device_hash,
                        "--event-id",
                        "555",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("id"), 555)
        self.assertEqual(payload.get("message"), "Target event")

    def test_history_comment_posts_message_and_type(self) -> None:
        created_payload = {
            "id": 991,
            "timestamp": "2026-06-29T03:15:00Z",
            "source": "operator",
            "type": "service",
            "author_username": "timeline-admin@example.com",
            "message": "Replaced sensor board and re-seated cable.",
        }

        def fake_post(_self, path: str, *, json_data=None, data=None):  # type: ignore[no-untyped-def]
            self.assertEqual(path, f"/api/developer/devices/{self.device_hash}/timeline/")
            self.assertIsNone(data)
            self.assertEqual((json_data or {}).get("message"), "Replaced sensor board and re-seated cable.")
            self.assertEqual((json_data or {}).get("type"), "service")
            return ApiResponse(True, 201, created_payload, json.dumps(created_payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.post", new=fake_post):
                result = self.runner.invoke(
                    app,
                    [
                        "--json",
                        "--config",
                        str(config_path),
                        "devices",
                        "history",
                        "comment",
                        self.device_hash,
                        "--message",
                        "Replaced sensor board and re-seated cable.",
                        "--type",
                        "service",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("id"), 991)
        self.assertEqual(payload.get("type"), "service")

    def test_history_comment_rejects_system_reserved_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            result = self.runner.invoke(
                app,
                [
                    "--json",
                    "--config",
                    str(config_path),
                    "devices",
                    "history",
                    "comment",
                    self.device_hash,
                    "--message",
                    "Trying reserved type",
                    "--type",
                    "lifecycle",
                ],
            )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("ok"), False)
        self.assertIn("reserved for automatic system entries", str(payload.get("detail")))

    def test_history_delete_soft_deletes_entry(self) -> None:
        deleted_payload = {
            "id": 777,
            "timestamp": "2026-06-29T04:40:00Z",
            "source": "operator",
            "type": "service",
            "is_deleted": True,
            "message": "[deleted]",
        }

        def fake_request(
            _self,
            method: str,
            path: str,
            *,
            params=None,
            json_data=None,
            data=None,
            headers=None,
            timeout=20,
            allow_refresh=True,
        ):  # type: ignore[no-untyped-def]
            _ = (params, json_data, data, headers, timeout, allow_refresh)
            self.assertEqual(method, "DELETE")
            self.assertEqual(path, f"/api/developer/devices/{self.device_hash}/timeline/777/")
            return ApiResponse(True, 200, deleted_payload, json.dumps(deleted_payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.request", new=fake_request):
                result = self.runner.invoke(
                    app,
                    [
                        "--json",
                        "--config",
                        str(config_path),
                        "devices",
                        "history",
                        "delete",
                        self.device_hash,
                        "--event-id",
                        "777",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("id"), 777)
        self.assertEqual(payload.get("is_deleted"), True)

    def test_history_list_debug_prints_endpoint(self) -> None:
        timeline_payload = {"results": [], "count": 0}

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            self.assertEqual(path, f"/api/developer/devices/{self.device_hash}/timeline/")
            return ApiResponse(True, 200, timeline_payload, json.dumps(timeline_payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    [
                        "--debug",
                        "--config",
                        str(config_path),
                        "devices",
                        "history",
                        "list",
                        self.device_hash,
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("/api/developer/devices/", result.output)
        self.assertIn("/timeline/", result.output)


if __name__ == "__main__":
    unittest.main()
