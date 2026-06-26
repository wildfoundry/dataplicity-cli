from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from dataplicity_cli.cli import app


class CliUxSnapshotsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def _invoke(self, args: list[str]):
        return self.runner.invoke(app, args)

    def test_root_help_snapshot(self) -> None:
        result = self._invoke(["--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        expected_snippets = [
            "Dataplicity CLI",
            "Examples:",
            "dataplicity setup",
            "dataplicity ls --online-only",
            "dataplicity connect",
            "setup",
            "whoami",
            "doctor",
            "endpoint-monitors",
            "user-impact",
            "heartbeat-monitors",
            "fleet-jobs",
            "logging",
            "ls",
            "connect",
        ]
        for snippet in expected_snippets:
            self.assertIn(snippet, result.output)

    def test_devices_terminal_help_snapshot(self) -> None:
        result = self._invoke(["devices", "terminal", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        expected_snippets = [
            "Open an interactive terminal session to a device.",
            "Examples:",
            "dataplicity devices terminal",
            "dataplicity devices terminal <device-hash>",
        ]
        for snippet in expected_snippets:
            self.assertIn(snippet, result.output)

    def test_doctor_help_snapshot(self) -> None:
        result = self._invoke(["doctor", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        expected_snippets = [
            "Run connectivity and auth diagnostics.",
            "Examples:",
            "dataplicity doctor",
            "dataplicity --json doctor",
        ]
        for snippet in expected_snippets:
            self.assertIn(snippet, result.output)

    def test_fleet_jobs_run_help_snapshot(self) -> None:
        result = self._invoke(["fleet-jobs", "run", "--help"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        expected_snippets = [
            "Create and start a fleet job.",
            "Examples:",
            "dataplicity fleet-jobs run --data",
        ]
        for snippet in expected_snippets:
            self.assertIn(snippet, result.output)

    def test_auth_required_message_snapshot_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            result = self._invoke(["--json", "--config", str(config_path), "whoami"])
        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("ok"), False)
        self.assertEqual(
            payload.get("detail"),
            "Authentication required. Use `dataplicity auth login` or `dataplicity auth api-key`.",
        )


if __name__ == "__main__":
    unittest.main()
