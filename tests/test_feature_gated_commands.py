from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import app


class FeatureGatedCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.org_hash = "org-feature-off"

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

    def _devices_payload(self) -> list[dict[str, str]]:
        return [{"hash_id": "dev-1", "organisation_hash": self.org_hash, "name": "device-1"}]

    def test_fleet_jobs_list_reports_feature_unavailable_for_org(self) -> None:
        rules_endpoint = f"/api/organisations/{self.org_hash}/incident-automation/rules/"

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            if path == "/api/developer/devices/":
                return ApiResponse(True, 200, self._devices_payload(), "")
            if path == rules_endpoint:
                return ApiResponse(False, 404, {"detail": "Not found."}, '{"detail":"Not found."}')
            raise AssertionError(f"unexpected GET path: {path}")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    ["--json", "--config", str(config_path), "fleet-jobs", "list"],
                )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            payload.get("detail"),
            f"Fleet jobs are not available for organisation `{self.org_hash}`.",
        )

    def test_fleet_jobs_show_keeps_not_found_when_feature_exists(self) -> None:
        rules_endpoint = f"/api/organisations/{self.org_hash}/incident-automation/rules/"
        job_endpoint = f"{rules_endpoint}job-123/"

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            if path == "/api/developer/devices/":
                return ApiResponse(True, 200, self._devices_payload(), "")
            if path == job_endpoint:
                return ApiResponse(False, 404, {"detail": "Not found."}, '{"detail":"Not found."}')
            if path == rules_endpoint:
                return ApiResponse(True, 200, [], "[]")
            raise AssertionError(f"unexpected GET path: {path}")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                result = self.runner.invoke(
                    app,
                    ["--json", "--config", str(config_path), "fleet-jobs", "show", "job-123"],
                )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("detail"), "Not found.")

    def test_fleet_jobs_run_reports_feature_unavailable_for_org(self) -> None:
        simulation_endpoint = f"/api/organisations/{self.org_hash}/incident-automation/rule-simulations/"

        def fake_get(_self, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = params
            if path == "/api/developer/devices/":
                return ApiResponse(True, 200, self._devices_payload(), "")
            raise AssertionError(f"unexpected GET path: {path}")

        def fake_post(_self, path: str, *, json_data=None, data=None):  # type: ignore[no-untyped-def]
            _ = (json_data, data)
            if path == simulation_endpoint:
                return ApiResponse(False, 404, {"detail": "Not found."}, '{"detail":"Not found."}')
            raise AssertionError(f"unexpected POST path: {path}")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            self._write_authed_config(config_path)
            with patch("dataplicity_cli.cli.ApiClient.get", new=fake_get):
                with patch("dataplicity_cli.cli.ApiClient.post", new=fake_post):
                    result = self.runner.invoke(
                        app,
                        [
                            "--json",
                            "--config",
                            str(config_path),
                            "fleet-jobs",
                            "run",
                            "--data",
                            '{"name":"restart-edge","device_hashes":["abc123"],"command":"restart"}',
                        ],
                    )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            payload.get("detail"),
            f"Fleet jobs are not available for organisation `{self.org_hash}`.",
        )


if __name__ == "__main__":
    unittest.main()
