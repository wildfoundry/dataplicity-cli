from __future__ import annotations

import unittest
from unittest.mock import Mock

from dataplicity_cli.api import ApiClient
from dataplicity_cli.config import Config


class ApiClientTest(unittest.TestCase):
    def test_request_sets_status_text_when_error_body_is_empty(self) -> None:
        config = Config(base_url="https://example.com")
        client = ApiClient(config)

        response = Mock()
        response.status_code = 404
        response.text = ""
        response.json.side_effect = ValueError("no json")

        client.session.request = Mock(return_value=response)

        result = client.get("/api/developer/endpoint-monitors/")

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.text, "HTTP 404")


if __name__ == "__main__":
    unittest.main()
