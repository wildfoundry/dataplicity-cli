from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.cli import _browser_login_url_from_bootstrap, app


MFA_WEBAUTHN_AND_TOTP = {
    "detail": ["Multi-factor authentication required."],
    "error_code": ["mfa_required"],
    "mfa": {
        "type": "WEBAUTHN",
        "available_types": ["TOTP", "WEBAUTHN"],
        "webauthn": {
            "token": "signed-challenge",
            "options": {"allowCredentials": [{"type": "public-key", "id": "abc"}]},
        },
    },
}

MFA_WEBAUTHN_ONLY = {
    "detail": ["Multi-factor authentication required."],
    "error_code": ["mfa_required"],
    "mfa": {
        "type": "WEBAUTHN",
        "available_types": ["WEBAUTHN"],
        "webauthn": {
            "token": "signed-challenge",
            "options": {"allowCredentials": [{"type": "public-key", "id": "abc"}]},
        },
    },
}

BROWSER_LOGIN_URL = (
    "https://www.dataplicity.com/login/?cli=1"
    "&cli_callback=http://127.0.0.1:43111/callback"
    "&cli_state=one-time-state"
)


def _mfa_text(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


class _FakeLoopbackListener:
    def __init__(self, payload: dict | None = None) -> None:
        self.callback_url = "http://127.0.0.1:43111/callback"
        self.payload = payload or {"access": "browser-access", "refresh": "browser-refresh"}
        self.stopped = False

    def start(self) -> bool:
        return True

    def wait_for_payload(self, timeout_seconds: float) -> dict | None:
        _ = timeout_seconds
        payload, self.payload = self.payload, None
        return payload

    def stop(self) -> None:
        self.stopped = True


class AuthLoginMfaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def _invoke(self, args: list[str], **kwargs):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            result = self.runner.invoke(app, ["--config", str(config_path), *args], **kwargs)
            saved = None
            if config_path.exists():
                saved = json.loads(config_path.read_text(encoding="utf-8"))
            return result, saved

    def test_browser_login_url_from_cli_browser_login_status(self) -> None:
        url = _browser_login_url_from_bootstrap(
            {"status": "cli_browser_login", "redirect_url": BROWSER_LOGIN_URL}
        )
        self.assertEqual(url, BROWSER_LOGIN_URL)

    def test_browser_login_url_ignored_for_plain_password_bootstrap(self) -> None:
        url = _browser_login_url_from_bootstrap({"status": "password", "email": "a@example.com"})
        self.assertIsNone(url)

    def test_login_does_not_claim_browser_redirect_for_password_bootstrap(self) -> None:
        def fake_post(path, json_data=None, data=None):
            _ = json_data, data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            return ApiResponse(False, 400, MFA_WEBAUTHN_AND_TOTP, _mfa_text(MFA_WEBAUTHN_AND_TOTP))

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=_FakeLoopbackListener()
        ), patch("dataplicity_cli.cli.webbrowser.open") as mock_open:
            result, _saved = self._invoke(
                ["auth", "login", "--email", "mfa@example.com", "--password", "secret"],
                input="654321\n",
            )

        self.assertNotIn("Redirecting to browser sign-in", result.output)
        mock_open.assert_not_called()

    def test_login_opens_gateway_cli_browser_login_url(self) -> None:
        bootstraps: list[dict] = []

        def fake_post(path, json_data=None, data=None):
            _ = data
            if path == "/api/auth/bootstrap/":
                bootstraps.append(json_data or {})
                if json_data and json_data.get("callback_url"):
                    self.assertEqual(json_data.get("cli_callback"), json_data.get("callback_url"))
                    return ApiResponse(
                        True,
                        200,
                        {"status": "cli_browser_login", "redirect_url": BROWSER_LOGIN_URL},
                        "",
                    )
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            return ApiResponse(False, 400, MFA_WEBAUTHN_ONLY, _mfa_text(MFA_WEBAUTHN_ONLY))

        listener = _FakeLoopbackListener()
        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=listener
        ), patch("dataplicity_cli.cli.webbrowser.open") as mock_open:
            result, saved = self._invoke(
                ["auth", "login", "--email", "key@example.com", "--password", "secret"]
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Redirecting to browser sign-in", result.output)
        self.assertNotIn("signed-challenge", result.output)
        mock_open.assert_called_once_with(BROWSER_LOGIN_URL)
        self.assertEqual(saved["access_token"], "browser-access")
        self.assertEqual(saved["preferred_login_method"], "email-password")
        self.assertTrue(any(item.get("callback_url") for item in bootstraps))
        self.assertTrue(listener.stopped)

    def test_login_falls_back_to_totp_when_browser_login_url_is_missing(self) -> None:
        posts: list[dict] = []

        def fake_post(path, json_data=None, data=None):
            _ = data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            posts.append(json_data or {})
            if json_data and json_data.get("mfa_code") == "654321" and json_data.get("mfa_type") == "TOTP":
                return ApiResponse(True, 200, {"access": "access-token", "refresh": "refresh-token"}, "")
            return ApiResponse(False, 400, MFA_WEBAUTHN_AND_TOTP, _mfa_text(MFA_WEBAUTHN_AND_TOTP))

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=_FakeLoopbackListener()
        ), patch("dataplicity_cli.cli.webbrowser.open"):
            result, saved = self._invoke(
                ["auth", "login", "--email", "mfa@example.com", "--password", "secret"],
                input="654321\n",
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Logged in", result.output)
        self.assertNotIn("Redirecting to browser sign-in", result.output)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[1]["mfa_code"], "654321")
        self.assertEqual(posts[1]["mfa_type"], "TOTP")
        self.assertEqual(saved["access_token"], "access-token")

    def test_login_webauthn_only_does_not_dump_challenge_or_crash(self) -> None:
        def fake_post(path, json_data=None, data=None):
            _ = json_data, data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            return ApiResponse(False, 400, MFA_WEBAUTHN_ONLY, _mfa_text(MFA_WEBAUTHN_ONLY))

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=_FakeLoopbackListener()
        ), patch("dataplicity_cli.cli.webbrowser.open"):
            result, saved = self._invoke(
                ["auth", "login", "--email", "key@example.com", "--password", "secret"]
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("signed-challenge", result.output)
        self.assertNotIn("allowCredentials", result.output)
        self.assertNotIn("Redirecting to browser sign-in", result.output)
        self.assertIn("security key", result.output.lower())
        self.assertIn("WebAuthn", result.output)
        self.assertTrue(saved is None or not saved.get("access_token"))

    def test_json_login_reports_mfa_required_without_webauthn_blob(self) -> None:
        def fake_post(path, json_data=None, data=None):
            _ = json_data, data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            return ApiResponse(False, 400, MFA_WEBAUTHN_AND_TOTP, _mfa_text(MFA_WEBAUTHN_AND_TOTP))

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post):
            result, _saved = self._invoke(
                [
                    "--json",
                    "auth",
                    "login",
                    "--email",
                    "mfa@example.com",
                    "--password",
                    "secret",
                ]
            )

        self.assertEqual(result.exit_code, 3, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "mfa_required")
        self.assertEqual(payload["mfa_type"], "TOTP")
        self.assertIn("TOTP", payload["mfa_available_types"])
        self.assertNotIn("webauthn", payload)
        self.assertNotIn("browser_login_url", payload)
        self.assertIn("authenticator app code", payload["detail"])
        self.assertNotIn("signed-challenge", result.output)

    def test_mfa_code_defaults_type_to_totp(self) -> None:
        posts: list[dict] = []

        def fake_post(path, json_data=None, data=None):
            _ = data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            posts.append(json_data or {})
            return ApiResponse(True, 200, {"access": "a", "refresh": "r"}, "")

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post):
            result, saved = self._invoke(
                [
                    "auth",
                    "login",
                    "--email",
                    "mfa@example.com",
                    "--password",
                    "secret",
                    "--mfa-code",
                    "111222",
                ]
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(posts[0]["mfa_code"], "111222")
        self.assertEqual(posts[0]["mfa_type"], "TOTP")
        self.assertEqual(saved["access_token"], "a")

    def test_explicit_webauthn_type_opens_browser_when_login_url_exists(self) -> None:
        def fake_post(path, json_data=None, data=None):
            _ = data
            if path == "/api/auth/bootstrap/":
                if json_data and json_data.get("callback_url"):
                    return ApiResponse(
                        True,
                        200,
                        {"status": "cli_browser_login", "redirect_url": BROWSER_LOGIN_URL},
                        "",
                    )
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            raise AssertionError("token login should not be attempted for WebAuthn")

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=_FakeLoopbackListener()
        ), patch("dataplicity_cli.cli.webbrowser.open") as mock_open:
            result, saved = self._invoke(
                [
                    "auth",
                    "login",
                    "--email",
                    "key@example.com",
                    "--password",
                    "secret",
                    "--mfa-type",
                    "WEBAUTHN",
                ]
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("Redirecting to browser sign-in", result.output)
        mock_open.assert_called_once_with(BROWSER_LOGIN_URL)
        self.assertEqual(saved["access_token"], "browser-access")

    def test_explicit_webauthn_type_does_not_claim_redirect_without_login_url(self) -> None:
        def fake_post(path, json_data=None, data=None):
            _ = json_data, data
            if path == "/api/auth/bootstrap/":
                return ApiResponse(True, 200, {"status": "password"}, '{"status":"password"}')
            raise AssertionError("token login should not be attempted for WebAuthn")

        with patch("dataplicity_cli.cli.ApiClient.post", side_effect=fake_post), patch(
            "dataplicity_cli.cli._SsoCallbackListener", return_value=_FakeLoopbackListener()
        ), patch("dataplicity_cli.cli.webbrowser.open") as mock_open:
            result, _saved = self._invoke(
                [
                    "auth",
                    "login",
                    "--email",
                    "key@example.com",
                    "--password",
                    "secret",
                    "--mfa-type",
                    "WEBAUTHN",
                ]
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertNotIn("Redirecting to browser sign-in", result.output)
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
