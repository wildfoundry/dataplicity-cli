from __future__ import annotations

import unittest
from urllib.request import urlopen

from dataplicity_cli.cli import (
    _SsoCallbackListener,
    _coerce_timeout_seconds,
    _extract_sso_payload_from_url,
    _extract_sso_payload_from_query,
    _extract_sso_tokens,
    _parse_sso_user_artifact,
    _with_callback_hint,
)


class SsoAuthFlowTest(unittest.TestCase):
    def test_extract_sso_tokens_supports_nested_tokens(self) -> None:
        access, refresh = _extract_sso_tokens({"tokens": {"access": "a1", "refresh": "r1"}})
        self.assertEqual(access, "a1")
        self.assertEqual(refresh, "r1")

    def test_extract_sso_payload_from_query_reads_payload_json(self) -> None:
        payload = _extract_sso_payload_from_query({"payload": ['{"access":"a2","refresh":"r2"}']})
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("access"), "a2")
        self.assertEqual(payload.get("refresh"), "r2")

    def test_with_callback_hint_adds_cli_callback(self) -> None:
        url = _with_callback_hint("https://example.com/sso?foo=bar", "http://127.0.0.1:1234/callback")
        self.assertIn("foo=bar", url)
        self.assertIn("cli_callback_url=", url)

    def test_with_callback_hint_rewrites_next_target(self) -> None:
        callback = "http://127.0.0.1:1234/callback"
        url = _with_callback_hint("https://example.com/sso?next=%2Fafter-login%2F", callback)
        self.assertIn("next=http%3A%2F%2F127.0.0.1%3A1234%2Fcallback", url)
        self.assertIn("cli_callback_url=", url)
    def test_extract_sso_payload_from_url_reads_query_and_fragment(self) -> None:
        payload = _extract_sso_payload_from_url("https://dataplicity.com/cb?code=abc#state=xyz")
        self.assertEqual(payload, {"code": "abc", "state": "xyz"})

    def test_callback_listener_captures_query_payload(self) -> None:
        listener = _SsoCallbackListener()
        started = listener.start()
        self.assertTrue(started)
        self.assertIsNotNone(listener.callback_url)
        try:
            response = urlopen(f"{listener.callback_url}?access=abc&refresh=def")
            self.assertEqual(response.status, 200)
            payload = listener.wait_for_payload(timeout_seconds=1.0)
            self.assertEqual(payload, {"access": "abc", "refresh": "def"})
        finally:
            listener.stop()

    def test_coerce_timeout_seconds_handles_invalid_values(self) -> None:
        self.assertEqual(_coerce_timeout_seconds(30), 30)
        self.assertEqual(_coerce_timeout_seconds("45"), 45)
        self.assertEqual(_coerce_timeout_seconds(0), 1)
        self.assertEqual(_coerce_timeout_seconds(object()), 180)

    def test_parse_sso_user_artifact_supports_url_and_query(self) -> None:
        url_payload = _parse_sso_user_artifact("https://dataplicity.com/callback?code=abc&state=xyz")
        self.assertEqual(url_payload, {"code": "abc", "state": "xyz"})
        query_payload = _parse_sso_user_artifact("access=tok123&refresh=ref456")
        self.assertEqual(query_payload, {"access": "tok123", "refresh": "ref456"})


if __name__ == "__main__":
    unittest.main()
