"""Credential recipients are checked before admission, refresh or HTTP I/O."""
import json
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from typer.testing import CliRunner

from dataplicity_cli.api import ApiClient
from dataplicity_cli.cli import app
from dataplicity_cli.config import Config
from dataplicity_cli.tunnels import TunnelAPI


def response(status=200, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({} if payload is None else payload).encode()
    return result


class ApiRecipientTests(unittest.TestCase):
    def client(self, base="https://gateway.example.test/tenant", method="jwt"):
        config = Config(base_url=base, auth_method=method, access_token="human-access",
            refresh_token="human-refresh", api_key="organisation-secret", install_id="local-install")
        client = ApiClient(config)
        client.session.request = Mock(return_value=response())
        client.session.post = Mock(return_value=response())
        client.ensure_fresh_access_token = Mock()
        return client

    def test_rejects_foreign_or_escaping_destinations_before_renewal_or_send(self):
        targets = [
            "https://foreign.example.test/tenant/api/devices/",
            "https://gateway.example.test.attacker.test/tenant/api/",
            "https://gateway.example.test:444/tenant/api/",
            "http://gateway.example.test/tenant/api/",
            "https://gateway.example.test/outside/api/",
            "https://gateway.example.test/tenant-sibling/api/",
            "https://user:password@gateway.example.test/tenant/api/",
            "https://gateway.example.test/tenant/../outside/",
            "https://gateway.example.test/tenant/%2e%2e/outside/",
            "//foreign.example.test/api/",
            "file:///etc/passwd", "https://[invalid", "https://gateway.example.test:invalid/api/",
        ]
        for auth_method in ("jwt", "api_key"):
            for target in targets:
                with self.subTest(auth_method=auth_method, target=target):
                    client = self.client(method=auth_method)
                    result = client.request("POST", target, json_data={"command": "must not run"})
                    self.assertFalse(result.ok)
                    self.assertEqual(result.status_code, 0)
                    self.assertIsNone(result.data)
                    self.assertNotIn("human-access", result.text)
                    self.assertNotIn("organisation-secret", result.text)
                    client.ensure_fresh_access_token.assert_not_called()
                    client.session.request.assert_not_called()

    def test_keeps_same_recipient_relative_and_absolute_calls(self):
        for path in ("/api/devices/", "https://gateway.example.test:443/tenant/api/devices/"):
            with self.subTest(path=path):
                client = self.client()
                self.assertTrue(client.get(path).ok)
                call = client.session.request.call_args
                self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer human-access")
                self.assertFalse(call.kwargs["allow_redirects"])
                client.ensure_fresh_access_token.assert_called_once()

    def test_rejects_invalid_configured_recipient_without_sending_or_renewing(self):
        for base in ("", "http://gateway.example.test", "https://name:password@gateway.example.test",
                     "https://gateway.example.test/?credential=secret", "https://gateway.example.test/#fragment"):
            with self.subTest(base=base):
                client = self.client(base)
                self.assertFalse(client.get("/api/devices/").ok)
                self.assertEqual(client._attempt_refresh(), "failed")
                client.ensure_fresh_access_token.assert_not_called()
                client.session.request.assert_not_called()

    def test_explicit_loopback_development_remains_supported(self):
        for base in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
            with self.subTest(base=base):
                self.assertTrue(self.client(base).get("/api/devices/").ok)

    def test_netrc_and_session_basic_auth_cannot_replace_the_selected_api_credential(self):
        for auth_method, expected in (("jwt", "Bearer human-access"), ("api_key", "ApiKey organisation-secret")):
            for ambient in ("netrc", "session-auth"):
                with self.subTest(auth_method=auth_method, ambient=ambient):
                    client = self.client(method=auth_method)
                    client.session.request = requests.Session.request.__get__(client.session)
                    if ambient == "session-auth":
                        client.session.auth = ("ambient-user", "ambient-password")
                    client.session.send = Mock(return_value=response())
                    with patch("requests.sessions.get_netrc_auth", return_value=("netrc-user", "netrc-password")):
                        self.assertTrue(client.get("/api/devices/").ok)
                    prepared = client.session.send.call_args.args[0]
                    self.assertEqual(prepared.headers["Authorization"], expected)
                    # Corporate proxy/CA configuration remains available.
                    self.assertTrue(client.session.trust_env)

    def test_structured_session_failure_is_exact_and_status_bound(self):
        client = self.client()
        for code in ("session_expired", "token_not_valid", "token_expired", "invalid_token", "token_blacklisted"):
            self.assertTrue(client._response_indicates_invalid_jwt(response(403, {"code": code})))
        for status, payload in ((403, {"code": "permission_denied"}), (403, {"code": "device_inactive"}),
                                (404, {"code": "session_expired"}), (500, {"code": "session_expired"})):
            self.assertFalse(client._response_indicates_invalid_jwt(response(status, payload)))
        for payload in ({"error": "session_expired"}, {"detail": {"code": "session_expired"}}):
            self.assertTrue(client._response_indicates_invalid_jwt(response(401, payload)))
        for text, invalid in ((b"temporary upstream failure", False), (b"Token has expired", True)):
            wire = requests.Response(); wire.status_code = 401; wire._content = text
            self.assertEqual(client._response_indicates_invalid_jwt(wire), invalid)

    def test_tunnel_publisher_has_an_independent_client_and_no_human_state(self):
        parent = self.client("https://gateway.dataplicity.com")
        parent.session.cookies.set("human-cookie", "private", domain=".dataplicity.com")
        parent.session.auth = ("human", "password")
        control = TunnelAPI(parent, "org", token="machine-secret")
        self.assertIsNot(control.api, parent)
        self.assertIsNot(control.api.session, parent.session)
        self.assertEqual(control.api.config.base_url, "https://runtime.dataplicity.com")
        self.assertIsNone(control.api.config.auth_method)
        self.assertIsNone(control.api.config.access_token)
        self.assertIsNone(control.api.config.refresh_token)
        self.assertIsNone(control.api.config.api_key)
        self.assertEqual(list(control.api.session.cookies), [])
        self.assertIsNone(control.api.session.auth)
        control.api.session.request = Mock(return_value=response())
        control.request("GET", "bootstrap/")
        call = control.api.session.request.call_args
        self.assertEqual(call.args[1], "https://runtime.dataplicity.com/api/organisations/org/development-tunnels/bootstrap/")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "TunnelPublisher machine-secret")
        parent.session.request.assert_not_called()
        self.assertEqual(parent.config.access_token, "human-access")

    def test_request_cannot_change_recipient_or_authentication_method_during_admission(self):
        for attribute, value in (("base_url", "https://foreign.example.test"), ("auth_method", "api_key")):
            with self.subTest(attribute=attribute):
                client = self.client()
                client.ensure_fresh_access_token.side_effect = lambda: setattr(client.config, attribute, value)
                self.assertFalse(client.get("/api/devices/").ok)
                client.session.request.assert_not_called()

    def test_old_issuer_refresh_cannot_overwrite_a_reconfigured_account(self):
        client = self.client()
        def moved(*args, **kwargs):
            client.config.base_url = "https://foreign.example.test"
            client.config.access_token = "new-recipient-access"
            client.config.refresh_token = "new-recipient-refresh"
            return response(200, {"access": "old-recipient-access", "refresh": "old-recipient-refresh"})
        client.session.post.side_effect = moved
        self.assertEqual(client._attempt_refresh(), "failed")
        self.assertEqual(client.config.access_token, "new-recipient-access")
        self.assertEqual(client.config.refresh_token, "new-recipient-refresh")

    def test_refresh_keeps_netrc_out_and_rejects_malformed_payloads_without_installing_tokens(self):
        for payload in (None, [], "unexpected", {"access": 123}, {"access": "a", "refresh": 123}, {"access": "a", "refresh": ""}):
            with self.subTest(payload=payload):
                client = self.client()
                wire = requests.Response(); wire.status_code = 200
                wire._content = json.dumps(payload).encode()
                client.session.post.return_value = wire
                self.assertEqual(client._attempt_refresh(), "failed")
                self.assertEqual(client.config.access_token, "human-access")
        client = self.client()
        client.session.post = requests.Session.post.__get__(client.session)
        client.session.request = requests.Session.request.__get__(client.session)
        client.session.send = Mock(return_value=response(200, {"access": "renewed"}))
        with patch("requests.sessions.get_netrc_auth", return_value=("ambient", "password")):
            self.assertEqual(client._attempt_refresh(), "ok")
        self.assertNotIn("Authorization", client.session.send.call_args.args[0].headers)

    def test_transport_failure_does_not_reflect_a_credential_or_url(self):
        client = self.client()
        client.session.request.side_effect = requests.RequestException("header Bearer secret-value https://private.example.invalid/")
        result = client.get("/api/devices/")
        self.assertFalse(result.ok)
        self.assertNotIn("secret-value", result.text)
        self.assertNotIn("private.example.invalid", result.text)


class OverrideRecipientTests(unittest.TestCase):
    def test_changing_cli_recipient_requires_new_authentication_and_keeps_original_file(self):
        for target, retain in (("https://foreign.example.test", False), ("https://gateway.example.test/other", False),
                               ("https://GATEWAY.example.test:443/tenant/", True)):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/"cli.json"
                config = Config(base_url="https://gateway.example.test/tenant", auth_method="jwt",
                    access_token="human-access", refresh_token="human-refresh", api_key="organisation-secret", install_id="install")
                config.save(path)
                result = CliRunner().invoke(app, ["--config", str(path), "--base-url", target, "--json", "auth", "status"])
                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["has_access_token"], retain)
                self.assertEqual(payload["has_refresh_token"], retain)
                self.assertEqual(payload["has_api_key"], retain)
                self.assertEqual(Config.load(path).access_token, "human-access")

    def test_persisted_recipient_change_removes_previous_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"cli.json"
            Config(auth_method="api_key", api_key="old-key", access_token="old-access", refresh_token="old-refresh", install_id="install").save(path)
            result = CliRunner().invoke(app, ["--config", str(path), "--json", "config", "set", "--base-url", "https://new.example.test"])
            self.assertEqual(result.exit_code, 0, result.output)
            saved = Config.load(path)
            self.assertEqual(saved.base_url, "https://new.example.test")
            self.assertIsNone(saved.access_token)
            self.assertIsNone(saved.refresh_token)
            self.assertIsNone(saved.api_key)

    def test_reload_never_adopts_another_issuer_credentials(self):
        for target in ("https://new.example.test", "https://[invalid"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/"cli.json"
                Config(auth_method="jwt", access_token="old-access", refresh_token="old-refresh", install_id="install").save(path)
                with patch("dataplicity_cli.cli.ApiClient") as factory:
                    result = CliRunner().invoke(app, ["--config", str(path), "--json", "auth", "status"])
                    self.assertEqual(result.exit_code, 0, result.output)
                    config = factory.call_args.args[0]
                    reload = factory.call_args.kwargs["on_config_reload"]
                Config(base_url=target, auth_method="jwt", access_token="foreign-access", refresh_token="foreign-refresh", install_id="install").save(path)
                reload()
                self.assertIsNone(config.access_token)
                self.assertIsNone(config.refresh_token)
                self.assertIsNone(config.api_key)
                self.assertEqual(Config.load(path).access_token, "foreign-access")

    def test_password_signin_uses_no_existing_user_authority_and_discards_an_old_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"cli.json"
            Config(auth_method="jwt", access_token="old-access", refresh_token="old-refresh", api_key="old-org-key", install_id="install").save(path)
            sent = []
            def request(method, url, **kwargs):
                sent.append(url)
                self.assertNotIn("Authorization", kwargs["headers"])
                return response(200, {"status": "password_required"} if url.endswith('/bootstrap/') else {"access": "new-access", "refresh": "new-refresh"})
            with patch("requests.Session.request", side_effect=request), patch.object(ApiClient, "_attempt_refresh", side_effect=AssertionError("borrowed old user")):
                result = CliRunner().invoke(app, ["--config", str(path), "--json", "auth", "login", "--email", "new@example.test", "--password", "new-password"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(sent), 2)
            saved = Config.load(path)
            self.assertEqual(saved.access_token, "new-access")
            self.assertIsNone(saved.api_key)

    def test_key_installation_drops_saved_user_tokens_without_renewing_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"cli.json"
            Config(auth_method="jwt", access_token="old-access", refresh_token="old-refresh", install_id="install").save(path)
            with patch.object(ApiClient, "ensure_fresh_access_token", side_effect=AssertionError("borrowed old user")):
                result = CliRunner().invoke(app, ["--config", str(path), "--json", "auth", "api-key"], input="organisation-key\n")
            self.assertEqual(result.exit_code, 0, result.output)
            saved = Config.load(path)
            self.assertEqual(saved.api_key, "organisation-key")
            self.assertIsNone(saved.access_token)
            self.assertIsNone(saved.refresh_token)

    def test_publisher_startup_and_keepalive_do_not_touch_the_saved_human(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"cli.json"
            Config(auth_method="jwt", access_token="old-access", refresh_token="old-refresh", install_id="install").save(path)
            renewed = []
            async def keepalive(client):
                renewed.append(client.config.auth_method)
                await asyncio.sleep(0)
            async def publish(*args, **kwargs): await asyncio.sleep(0)
            with patch.object(ApiClient, "ensure_fresh_access_token", side_effect=AssertionError("startup borrowed saved user")), \
                    patch.object(ApiClient, "maintain_fresh_access_token", new=keepalive), \
                    patch("dataplicity_cli.tunnels.TunnelSession.publish", new=publish), \
                    patch.dict(os.environ, {"AUDIT_TUNNEL_TOKEN": "machine-secret"}):
                args = ["--config", str(path), "--json", "tunnel", "publish", "test", "--org", "org", "--port", "3000"]
                result = CliRunner().invoke(app, [*args, "--token-env", "AUDIT_TUNNEL_TOKEN"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(renewed, [])
                self.assertEqual(Config.load(path).access_token, "old-access")
                result = CliRunner().invoke(app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(renewed, ["jwt"])
