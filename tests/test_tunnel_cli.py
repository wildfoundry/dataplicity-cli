from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import app


class TunnelCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cli.json"
        self.path.write_text(json.dumps({"base_url": "https://gateway.test", "auth_method": "jwt", "access_token": "human", "refresh_token": "refresh"}))
        self.runner = CliRunner()

    def invoke(self, args, **kwargs):
        return self.runner.invoke(app, ["--config", str(self.path), *args], **kwargs)

    def test_help_and_listing_are_existing_cli_commands(self):
        for args in [["tunnel", "--help"], ["tunnel", "publish", "--help"], ["tunnel", "connect", "--help"], ["tunnel", "token", "create", "--help"]]:
            self.assertEqual(self.invoke(args).exit_code, 0)
        with patch("dataplicity_cli.cli.ApiClient.request", return_value=ApiResponse(True, 200, {"results": [{"name": "test-api"}], "count": 1}, "")) as request:
            result = self.invoke(["tunnel", "ls", "--org", "org-1", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["results"][0]["name"], "test-api")
        self.assertEqual(request.call_args.args, ("GET", "/api/organisations/org-1/development-tunnels/"))

    def test_org_selection_does_not_consult_device_inventory(self):
        def request(_self, method, path, **kwargs):
            if path == "/api/development-tunnels/organisations/":
                return ApiResponse(True, 200, {"results": [{"hash_id": "org-1"}]}, "")
            self.assertEqual(path, "/api/organisations/org-1/development-tunnels/")
            return ApiResponse(True, 200, {"results": []}, "")
        with patch("dataplicity_cli.cli.ApiClient.request", new=request):
            result = self.invoke(["tunnel", "ls", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        with patch("dataplicity_cli.cli.ApiClient.get", return_value=ApiResponse(True, 200, {"results": [{"hash_id": "a"}, {"hash_id": "b"}]}, "")):
            result = self.invoke(["tunnel", "ls", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--org", result.output)

    def test_broad_api_key_rejected_and_no_token_literal_option(self):
        self.path.write_text(json.dumps({"auth_method": "api_key", "api_key": "broad"}))
        result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--org", "org-1"])
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("broad", result.output)
        result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--token", "secret"])
        self.assertEqual(result.exit_code, 2)

    def test_machine_token_input_never_overwrites_human_session(self):
        with patch.dict(os.environ, {"TUNNEL_TOKEN_TEST": "machine-secret"}), patch("dataplicity_cli.tunnels.TunnelSession.publish", new=AsyncMock()) as publish:
            result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--org", "org-1", "--token-env", "TUNNEL_TOKEN_TEST"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("machine-secret", result.output)
        self.assertEqual(json.loads(self.path.read_text())["access_token"], "human")
        publish.assert_awaited_once_with(3000)
        with patch("dataplicity_cli.tunnels.TunnelSession.publish", new=AsyncMock()):
            result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--org", "org-1", "--token-stdin"], input="machine-secret\n")
        self.assertEqual(result.exit_code, 0, result.output)
        result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--token-stdin"], input="machine\n")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--org", result.output)
        result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--org", "org-1", "--token-env", "MISSING_TOKEN_VARIABLE"])
        self.assertEqual(result.exit_code, 1)
        result = self.invoke(["tunnel", "publish", "test-api", "--port", "3000", "--token-env", "X", "--token-stdin"])
        self.assertEqual(result.exit_code, 1)

    def test_connect_fixed_local_port_and_safe_errors(self):
        with patch("dataplicity_cli.tunnels.TunnelSession.connect", new=AsyncMock()) as connect:
            result = self.invoke(["tunnel", "connect", "test-api", "--local-port", "8080", "--org", "org-1"])
        self.assertEqual(result.exit_code, 0, result.output)
        connect.assert_awaited_once_with(8080)
        with patch("dataplicity_cli.tunnels.TunnelSession.connect", new=AsyncMock(side_effect=OSError("secret internal detail"))):
            result = self.invoke(["--json", "tunnel", "connect", "test-api", "--local-port", "8080", "--org", "org-1"])
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("secret", result.output)
        self.assertIn("port", json.loads(result.output)["detail"])

    def test_credential_secret_only_shown_on_issue_and_rotate(self):
        issued = {"id": "00000000-0000-0000-0000-000000000001", "secret": "issued-secret"}
        with patch("dataplicity_cli.cli.ApiClient.request", return_value=ApiResponse(True, 201, issued, "")) as request:
            result = self.invoke(["tunnel", "token", "create", "test-api", "--port", "3000", "--allow-replace", "--org", "org-1"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "issued-secret\n")
        self.assertTrue(request.call_args.kwargs["json_data"]["can_replace"])
        self.assertEqual(request.call_args.kwargs["json_data"]["ports"], [3000])
        with patch("dataplicity_cli.cli.ApiClient.request", return_value=ApiResponse(True, 200, issued, "")):
            result = self.invoke(["--json", "tunnel", "token", "rotate", issued["id"], "--org", "org-1"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["secret"], "issued-secret")
        with patch("dataplicity_cli.cli.ApiClient.request", return_value=ApiResponse(True, 200, {"results": [issued]}, "")):
            result = self.invoke(["tunnel", "token", "ls", "--org", "org-1"])
        self.assertNotIn("issued-secret", result.output)
        with patch("dataplicity_cli.cli.ApiClient.request", return_value=ApiResponse(True, 204, None, "")):
            result = self.invoke(["--json", "tunnel", "token", "revoke", issued["id"], "--org", "org-1"])
        self.assertTrue(json.loads(result.output)["ok"])


class TunnelDisplayTests(unittest.TestCase):
    setUp = TunnelCLITests.setUp
    invoke = TunnelCLITests.invoke

    def test_terminal_dashboard_plain_json_and_shutdown(self):
        import asyncio
        import io
        from rich.console import Console

        async def publish(session, port):
            session.stats.mode, session.stats.target_port = 'publisher', port
            session._emit({'event': 'published', 'name': session.name, 'target_port': port})
            session._emit({'event': 'bytes_up', 'name': session.name, 'bytes': 4096})
            await asyncio.sleep(0.01)

        for extra, expected_dashboard in (([], True), (['--plain'], False)):
            output = io.StringIO()
            console = Console(file=output, force_terminal=True, width=80, _environ={"TERM": "xterm"})
            with patch('dataplicity_cli.cli.Console', return_value=console), \
                 patch('dataplicity_cli.tunnels.TunnelSession.publish', new=publish):
                result = self.invoke(['tunnel', 'publish', 'api', '--port', '3000', '--org', 'org-1', *extra])
            self.assertEqual(result.exit_code, 0, str(result.exception))
            self.assertEqual('CLI–relay RTT' in output.getvalue(), expected_dashboard)
            if expected_dashboard:
                self.assertIn('4.0 KB sent', output.getvalue())
                self.assertIn('\x1b[?25h', output.getvalue())
                # Rich omits alt-screen CSI (?1049) on Windows even with screen=True.
                if sys.platform != "win32":
                    self.assertIn('\x1b[?1049h', output.getvalue())
                    self.assertIn('\x1b[?1049l', output.getvalue())
            else:
                self.assertIn('published: api', output.getvalue())

        with patch('dataplicity_cli.cli.Console', return_value=Console(force_terminal=True, _environ={"TERM": "xterm"})), \
             patch('dataplicity_cli.tunnels.TunnelSession.publish', new=publish):
            result = self.invoke(['--json', 'tunnel', 'publish', 'api', '--port', '3000', '--org', 'org-1'])
        self.assertEqual(result.exit_code, 0, str(result.exception))
        events = [json.loads(line) for line in result.output.splitlines()]
        self.assertEqual([event['event'] for event in events], ['published', 'bytes_up'])
        self.assertEqual(events[1]['bytes'], 4096)

        for failure in (asyncio.CancelledError(), OSError('secret-internal-detail')):
            output = io.StringIO()
            with patch('dataplicity_cli.cli.Console', return_value=Console(file=output, force_terminal=True, _environ={"TERM": "xterm"})), \
                 patch('dataplicity_cli.tunnels.TunnelSession.publish', new=AsyncMock(side_effect=failure)):
                result = self.invoke(['tunnel', 'publish', 'api', '--port', '3000', '--org', 'org-1'])
            self.assertEqual(result.exit_code, 0 if isinstance(failure, asyncio.CancelledError) else 1)
            self.assertNotIn('secret-internal-detail', output.getvalue())
            self.assertIn('\x1b[?25h', output.getvalue())
            if sys.platform != "win32":
                self.assertIn('\x1b[?1049l', output.getvalue())


if __name__ == "__main__":
    unittest.main()
