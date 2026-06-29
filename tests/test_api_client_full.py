from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from dataplicity_cli.api import ApiClient, ApiResponse
from dataplicity_cli.config import Config


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ApiClientFullTest(unittest.TestCase):
    def test_build_url_supports_absolute_and_relative(self) -> None:
        client = ApiClient(Config(base_url="https://example.test/"))
        self.assertEqual(client._build_url("/v1/status"), "https://example.test/v1/status")
        self.assertEqual(client._build_url("https://other.test/x"), "https://other.test/x")

    def test_auth_headers_for_jwt_api_key_and_none(self) -> None:
        jwt_client = ApiClient(Config(auth_method="jwt", access_token="tok"))
        key_client = ApiClient(Config(auth_method="api_key", api_key="key"))
        none_client = ApiClient(Config())
        self.assertEqual(jwt_client._auth_headers(), {"Authorization": "Bearer tok"})
        self.assertEqual(key_client._auth_headers(), {"Authorization": "ApiKey key"})
        self.assertEqual(none_client._auth_headers(), {})

    def test_extract_error_message_prefers_detail_and_non_field_errors(self) -> None:
        resp_detail = _FakeResponse(400, {"detail": "bad detail"}, text="fallback")
        resp_nfe = _FakeResponse(400, {"non_field_errors": ["first"]}, text="fallback")
        resp_text = _FakeResponse(500, ValueError("json"), text="plain text")
        self.assertEqual(ApiClient._extract_error_message(resp_detail), "bad detail")
        self.assertEqual(ApiClient._extract_error_message(resp_nfe), "first")
        self.assertEqual(ApiClient._extract_error_message(resp_text), "plain text")

    def test_invalid_token_message_detection(self) -> None:
        self.assertTrue(ApiClient._looks_like_invalid_token_message("Given token not valid for any token type"))
        self.assertFalse(ApiClient._looks_like_invalid_token_message("different error"))
        self.assertFalse(ApiClient._looks_like_invalid_token_message(""))

    def test_response_invalid_jwt_requires_auth_status_codes(self) -> None:
        valid_status = _FakeResponse(401, {"detail": "token has expired"})
        wrong_status = _FakeResponse(404, {"detail": "token has expired"})
        self.assertTrue(ApiClient(Config())._response_indicates_invalid_jwt(valid_status))
        self.assertFalse(ApiClient(Config())._response_indicates_invalid_jwt(wrong_status))

    def test_invalidate_session_clears_tokens_even_if_callback_errors(self) -> None:
        cfg = Config(auth_method="jwt", access_token="a", refresh_token="r")
        client = ApiClient(cfg, on_token_update=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        client._invalidate_jwt_session()
        self.assertIsNone(cfg.auth_method)
        self.assertIsNone(cfg.access_token)
        self.assertIsNone(cfg.refresh_token)

    def test_refresh_access_token_paths(self) -> None:
        cfg = Config(base_url="https://example.test", auth_method="jwt", refresh_token="ref")
        update = Mock()
        client = ApiClient(cfg, on_token_update=update)

        client.session.post = Mock(side_effect=requests.RequestException("network"))
        self.assertFalse(client._refresh_access_token())

        cfg = Config(base_url="https://example.test", auth_method="jwt", access_token="a", refresh_token="ref")
        client = ApiClient(cfg, on_token_update=update)
        client.session.post = Mock(return_value=_FakeResponse(401, {"detail": "Token is invalid or expired"}))
        self.assertFalse(client._refresh_access_token())
        self.assertIsNone(cfg.auth_method)

        cfg = Config(base_url="https://example.test", auth_method="jwt", refresh_token="ref")
        client = ApiClient(cfg, on_token_update=update)
        client.session.post = Mock(return_value=_FakeResponse(200, ValueError("no json")))
        self.assertFalse(client._refresh_access_token())

        cfg = Config(base_url="https://example.test", auth_method="jwt", refresh_token="ref")
        client = ApiClient(cfg, on_token_update=update)
        client.session.post = Mock(return_value=_FakeResponse(200, {"refresh": "r2"}))
        self.assertFalse(client._refresh_access_token())

        cfg = Config(base_url="https://example.test", auth_method="jwt", refresh_token="ref")
        update_ok = Mock()
        client = ApiClient(cfg, on_token_update=update_ok)
        client.session.post = Mock(return_value=_FakeResponse(200, {"access": "a2", "refresh": "r2"}))
        self.assertTrue(client._refresh_access_token())
        self.assertEqual(cfg.access_token, "a2")
        self.assertEqual(cfg.refresh_token, "r2")
        self.assertEqual(cfg.auth_method, "jwt")
        update_ok.assert_called_once()

    def test_refresh_success_ignores_callback_exception(self) -> None:
        cfg = Config(base_url="https://example.test", auth_method="jwt", refresh_token="ref")
        client = ApiClient(cfg, on_token_update=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        client.session.post = Mock(return_value=_FakeResponse(200, {"access": "new-token"}))
        self.assertTrue(client._refresh_access_token())
        self.assertEqual(cfg.access_token, "new-token")

    def test_refresh_session_only_when_jwt(self) -> None:
        client = ApiClient(Config(auth_method="api_key", api_key="key"))
        self.assertFalse(client.refresh_session())

    def test_request_exception_returns_failed_response(self) -> None:
        client = ApiClient(Config(base_url="https://example.test"))
        client.session.request = Mock(side_effect=requests.RequestException("timeout"))
        resp = client.get("/status")
        self.assertEqual(resp, ApiResponse(False, 0, None, "timeout"))

    def test_request_retries_once_after_refresh_success(self) -> None:
        cfg = Config(base_url="https://example.test", auth_method="jwt", access_token="old", refresh_token="ref")
        client = ApiClient(cfg)
        first = _FakeResponse(401, {"detail": "Token has expired"}, text="unauthorized")
        second = _FakeResponse(200, {"ok": True}, text='{"ok": true}')
        client.session.request = Mock(side_effect=[first, second])
        client._refresh_access_token = Mock(return_value=True)  # type: ignore[assignment]

        resp = client.request("GET", "/x")
        self.assertTrue(resp.ok)
        self.assertEqual(resp.status_code, 200)
        client._refresh_access_token.assert_called_once()
        self.assertEqual(client.session.request.call_count, 2)

    def test_request_handles_error_text_fallback_and_invalidation(self) -> None:
        cfg = Config(base_url="https://example.test", auth_method="jwt", access_token="old")
        client = ApiClient(cfg)
        client._refresh_access_token = Mock(return_value=False)  # type: ignore[assignment]
        client.session.request = Mock(return_value=_FakeResponse(403, {"detail": "invalid token"}, text=""))
        resp = client.post("/x", json_data={"a": 1})
        self.assertFalse(resp.ok)
        self.assertEqual(resp.text, "HTTP 403")
        self.assertIsNone(cfg.auth_method)

    def test_request_merges_extra_headers(self) -> None:
        cfg = Config(base_url="https://example.test")
        client = ApiClient(cfg)
        response = _FakeResponse(200, {"ok": True}, text='{"ok":true}')
        client.session.request = Mock(return_value=response)
        client.request("GET", "/x", headers={"X-Test": "1"})
        called_headers = client.session.request.call_args.kwargs["headers"]
        self.assertEqual(called_headers["Accept"], "application/json")
        self.assertEqual(called_headers["X-Test"], "1")


if __name__ == "__main__":
    unittest.main()
