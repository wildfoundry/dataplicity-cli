import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from dataplicity_cli.cli import _resolve_m2m_url, _open_remote_access_port


class GatewayRemoteAuthTests(unittest.TestCase):
    def test_requests_select_the_authenticated_plane(self):
        for method, prefix in [('api_key', 'developer'), ('jwt', 'remote')]:
            api = Mock()
            api.get.return_value = SimpleNamespace(ok=True, data={'m2m_url': 'https://relay.example/m2m/'})
            state = SimpleNamespace(config=SimpleNamespace(auth_method=method), api=api)
            url = asyncio.run(_resolve_m2m_url(state, 'device'))
            self.assertIn('device=device', url)
            api.get.assert_called_once_with(f'/api/{prefix}/devices/device/host/')
            _open_remote_access_port(state, 'device', {'service': 'terminal'})
            api.post.assert_called_once_with(f'/api/{prefix}/devices/device/ports/', json_data={'service': 'terminal'})
