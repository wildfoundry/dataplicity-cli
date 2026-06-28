from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dataplicity_cli.api import ApiClient, ApiResponse
from dataplicity_cli.cli import app
from dataplicity_cli.config import Config


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload))

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ApiClientAuthExpiryTest(unittest.TestCase):
    def test_invalid_jwt_response_clears_cached_tokens(self) -> None:
        config = Config(auth_method="jwt", access_token="expired-access")
        client = ApiClient(config)
        client.session.request = lambda *args, **kwargs: _FakeHttpResponse(
            403,
            {"detail": "Given token not valid for any token type"},
        )

        response = client.get("/api/developer/devices/")

        self.assertFalse(response.ok)
        self.assertIsNone(config.auth_method)
        self.assertIsNone(config.access_token)
        self.assertIsNone(config.refresh_token)

    def test_refresh_failure_with_expired_refresh_token_clears_session(self) -> None:
        config = Config(auth_method="jwt", access_token="old-access", refresh_token="expired-refresh")
        client = ApiClient(config)
        client.session.post = lambda *args, **kwargs: _FakeHttpResponse(
            401,
            {"detail": "Token is invalid or expired"},
        )

        refreshed = client.refresh_session()

        self.assertFalse(refreshed)
        self.assertIsNone(config.auth_method)
        self.assertIsNone(config.access_token)
        self.assertIsNone(config.refresh_token)


class CliAuthExpiryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_setup_does_not_claim_logged_in_when_token_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://gateway.dataplicity.com",
                        "auth_method": "jwt",
                        "access_token": "expired",
                        "refresh_token": None,
                    }
                ),
                encoding="utf-8",
            )

            with patch("dataplicity_cli.cli.ApiClient.get") as mock_get:
                mock_get.return_value = ApiResponse(
                    ok=False,
                    status_code=403,
                    data={"detail": "Given token not valid for any token type"},
                    text='{"detail":"Given token not valid for any token type"}',
                )
                result = self.runner.invoke(app, ["--json", "--config", str(config_path), "setup"])

            self.assertEqual(result.exit_code, 2, msg=result.output)
            payload = json.loads(result.output)
            self.assertFalse(payload["ok"])
            self.assertIn("Interactive setup is unavailable", payload["detail"])

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIsNone(saved.get("auth_method"))
            self.assertIsNone(saved.get("access_token"))
            self.assertIsNone(saved.get("refresh_token"))

    def test_whoami_returns_error_when_session_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "cli.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://gateway.dataplicity.com",
                        "auth_method": "jwt",
                        "access_token": "expired",
                        "refresh_token": None,
                    }
                ),
                encoding="utf-8",
            )

            with patch("dataplicity_cli.cli.ApiClient.get") as mock_get:
                mock_get.return_value = ApiResponse(
                    ok=False,
                    status_code=403,
                    data={"detail": "Given token not valid for any token type"},
                    text='{"detail":"Given token not valid for any token type"}',
                )
                result = self.runner.invoke(app, ["--json", "--config", str(config_path), "whoami"])

            self.assertEqual(result.exit_code, 1, msg=result.output)
            payload = json.loads(result.output)
            self.assertFalse(payload["ok"])
            self.assertIn("Saved login appears expired", payload["detail"])


if __name__ == "__main__":
    unittest.main()
