from __future__ import annotations

import json
import re
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

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def _assert_output_contains(self, result, expected_snippets: list[str]) -> None:
        self.assertEqual(result.exit_code, 0, msg=result.output)
        output = self._strip_ansi(result.output)
        for snippet in expected_snippets:
            self.assertIn(snippet, output)

    def test_root_help_snapshot(self) -> None:
        result = self._invoke(["--help"])
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
        self._assert_output_contains(result, expected_snippets)

    def test_devices_terminal_help_snapshot(self) -> None:
        result = self._invoke(["devices", "terminal", "--help"])
        expected_snippets = [
            "Open an interactive terminal session to a device.",
            "Examples:",
            "dataplicity devices terminal",
            "dataplicity devices terminal <device-hash>",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_devices_run_help_snapshot(self) -> None:
        result = self._invoke(["devices", "run", "--help"])
        expected_snippets = [
            "Run a single command on a selected device and print output.",
            "dataplicity devices run --command",
            "dataplicity devices run <device-hash> --command",
            "optional in interactive mode",
            "--connect-timeout",
            "--no-timeout",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_devices_ssh_help_snapshot(self) -> None:
        result = self._invoke(["devices", "ssh", "--help"])
        expected_snippets = [
            "Open SSH to a device using an automatic secure tunnel.",
            "keys from ssh-agent by default",
            "dataplicity devices ssh <device-hash>",
            "--user",
            "--remote-port",
            "--local-port",
            "--identity-file",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_devices_port_forward_help_snapshot(self) -> None:
        result = self._invoke(["devices", "port-forward", "--help"])
        expected_snippets = [
            "Forward a local port to a remote device port with live metrics.",
            "dataplicity devices port-forward --remote-port 22 --local-port 2022",
            "--remote-port",
            "--local-port",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_doctor_help_snapshot(self) -> None:
        result = self._invoke(["doctor", "--help"])
        expected_snippets = [
            "Run connectivity and auth diagnostics.",
            "Examples:",
            "dataplicity doctor",
            "dataplicity --json doctor",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_fleet_jobs_run_help_snapshot(self) -> None:
        result = self._invoke(["fleet-jobs", "run", "--help"])
        expected_snippets = [
            "Create and start a fleet job.",
            "Examples:",
            "dataplicity fleet-jobs run --data",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_logging_list_help_snapshot(self) -> None:
        result = self._invoke(["logging", "list", "--help"])
        expected_snippets = [
            "List raw log lines.",
            "--device",
            "--path",
            "--search",
            "--since",
            "--all-scopes",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_logging_path_map_help_snapshot(self) -> None:
        result = self._invoke(["logging", "path-map", "--help"])
        expected_snippets = [
            "Show recommended path filters for scoped log queries.",
            "dataplicity logging path-map",
        ]
        self._assert_output_contains(result, expected_snippets)

    def test_version_flag_snapshot(self) -> None:
        result = self._invoke(["--version"])
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(result.output.strip(), "dataplicity-cli 0.1.4")

    def test_auth_required_message_snapshot_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            result = self._invoke(["--json", "--config", str(config_path), "whoami"])
        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("ok"), False)
        self.assertEqual(
            payload.get("detail"),
            "Authentication required. Use `dataplicity auth sso`, `dataplicity auth login`, or `dataplicity auth api-key`.",
        )

    def test_logging_default_scope_snapshot_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            result = self._invoke(["--json", "--config", str(config_path), "logging", "list"])
        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload.get("ok"), False)
        self.assertIn("Authentication required", payload.get("detail", ""))


if __name__ == "__main__":
    unittest.main()
