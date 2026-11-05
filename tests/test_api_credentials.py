"""Machine publisher calls must not borrow or invalidate a human session."""
import unittest
from unittest.mock import Mock

from dataplicity_cli.api import ApiClient
from dataplicity_cli.config import Config
from dataplicity_cli.tunnels import TunnelAPI, TunnelError


class CredentialBoundaryTests(unittest.TestCase):
    def client(self):
        api = ApiClient(Config(auth_method="jwt", access_token="human-access", refresh_token="human-refresh"))
        api.session.request = Mock(return_value=Mock(status_code=200, text="{}", json=Mock(return_value={})))
        api.ensure_fresh_access_token = Mock()
        return api

    def test_publisher_uses_runtime_without_human_tokens_or_refresh(self):
        api = self.client()
        TunnelAPI(api, "org", token="machine-secret").request("GET", "bootstrap/")
        call = api.session.request.call_args
        self.assertEqual(call.args[1], "https://runtime.dataplicity.com/api/organisations/org/development-tunnels/bootstrap/")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "TunnelPublisher machine-secret")
        self.assertFalse(call.kwargs["allow_redirects"])
        api.ensure_fresh_access_token.assert_not_called()

    def test_human_calls_keep_gateway_and_their_refresh_lifecycle(self):
        api = self.client()
        TunnelAPI(api, "org").request("GET", "bootstrap/")
        call = api.session.request.call_args
        self.assertEqual(call.args[1], "https://gateway.dataplicity.com/api/organisations/org/development-tunnels/bootstrap/")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer human-access")
        api.ensure_fresh_access_token.assert_called_once()

    def test_rejected_publisher_does_not_erase_saved_human_tokens(self):
        api = self.client()
        api.session.request.return_value = Mock(status_code=401, text="invalid token", json=Mock(return_value={"detail": "invalid token"}))
        with self.assertRaises(TunnelError):
            TunnelAPI(api, "org", token="revoked-machine").request("GET", "bootstrap/")
        self.assertEqual(api.config.access_token, "human-access")
        self.assertEqual(api.config.refresh_token, "human-refresh")
        api.ensure_fresh_access_token.assert_not_called()

    def test_refresh_never_follows_a_redirect_with_the_human_secret(self):
        api = self.client()
        api.session.post = Mock(return_value=Mock(status_code=302))
        self.assertEqual(api._attempt_refresh(), "failed")
        self.assertFalse(api.session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(api.config.refresh_token, "human-refresh")


    def test_explicit_lowercase_machine_authority_has_no_human_session_lifecycle(self):
        api = self.client()
        api.session.request.return_value = Mock(status_code=401, text="invalid token", json=Mock(return_value={"detail": "invalid token"}))
        api._refresh_access_token = Mock()
        response = api.request('GET', '/api/runtime/', headers={'authorization': 'TunnelPublisher revoked'})
        self.assertFalse(response.ok)
        self.assertEqual(api.session.request.call_args.kwargs['headers']['Authorization'], 'TunnelPublisher revoked')
        self.assertEqual(sum(key.lower() == 'authorization' for key in api.session.request.call_args.kwargs['headers']), 1)
        api.ensure_fresh_access_token.assert_not_called()
        api._refresh_access_token.assert_not_called()
        self.assertEqual(api.config.access_token, 'human-access')

    def test_publisher_standard_https_aliases_and_custom_authority(self):
        from dataplicity_cli.tunnels import publisher_origin
        for suffix in ('dataplicity.com', 'staging.dpenv.com'):
            for alias in ('api', 'app-api', 'gateway'):
                self.assertEqual(publisher_origin(f'https://{alias}.{suffix}:443/'), f'https://runtime.{suffix}')
        for origin in ('https://gateway.dataplicity.com:8443', 'https://custom.example/runtime', 'https://gateway.dataplicity.com.attacker.test', 'https://gateway.dataplicity.com:invalid', 'https://[invalid'):
            self.assertEqual(publisher_origin(origin), origin)
