"""Qualification must fail closed when actual released-agent evidence is absent."""
import asyncio

import pytest

from qualification.staging_acceptance import _start_legacy, qualify_named_and_legacy


def test_live_acceptance_rejects_non_staging_before_any_fixture_effects():
    with pytest.raises(RuntimeError, match='only targets staging'):
        asyncio.run(qualify_named_and_legacy({'api_url': 'https://api.dataplicity.com'}))


def test_missing_actual_agent_is_failure(monkeypatch, tmp_path):
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='source is missing'):
        asyncio.run(_start_legacy({}, 12345, {}))


def test_agent_revision_must_match_qualification_pin(monkeypatch, tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# actual agent would be installed here')
    (tmp_path / 'REVISION').write_text('b' * 40)
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='immutable qualification pin'):
        asyncio.run(_start_legacy({}, 12345, {}))


def test_alpha_agent_cannot_satisfy_legacy_release_gate(monkeypatch, tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# alpha agent would be installed here')
    (source / '_version.py').write_text('__version__ = "0.5.13a3"')
    (tmp_path / 'REVISION').write_text('a' * 40)
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATAPLICITY_LEGACY_AGENT_SHA', 'a' * 40)
    with pytest.raises(RuntimeError, match='stable released version'):
        asyncio.run(_start_legacy({}, 12345, {}))


def test_actual_agent_cannot_be_sent_to_production_relay():
    with pytest.raises(RuntimeError, match='secure staging endpoint'):
        asyncio.run(qualify_named_and_legacy({
            'api_url': 'https://api.staging.dpenv.com',
            'm2m_url': 'wss://m2m.dataplicity.com/m2m/'}))


@pytest.mark.parametrize('stage,status,agent_shutdown,router_open,expected', [
    (1, 0, False, True, 'LegacyAdmissionTimeout'),
    (2, 403, False, True, 'LegacyAdmissionRejected'),
    (2, 201, False, True, 'LegacyNotifyOpenTimeout'),
    (3, 201, False, True, 'LegacyPayloadTimeout'),
    (3, 201, True, True, 'LegacyAgentGlobalShutdown'),
    (3, 201, False, False, 'LegacyRouterSocketClosed'),
])
def test_failed_legacy_probe_keeps_failure_and_distinguishes_admission_delivery(
        monkeypatch, stage, status, agent_shutdown, router_open, expected):
    from types import SimpleNamespace
    from qualification import staging_acceptance as acceptance
    diagnostic = {}
    calls = []
    async def timed_out(_port, _payload):
        calls.append(1)
        diagnostic.update(admission_stage=stage, http_status=status,
                          shared_close_event=agent_shutdown)
        raise asyncio.TimeoutError()
    monkeypatch.setattr(acceptance, '_exchange', timed_out)
    legacy = {'diagnostics': diagnostic, 'local_port': 12345,
              'process': SimpleNamespace(returncode=None),
              'client': SimpleNamespace(ws=SimpleNamespace(state=SimpleNamespace(
                  name='OPEN' if router_open else 'CLOSED')))}
    with pytest.raises(getattr(acceptance, expected)):
        asyncio.run(acceptance._legacy_exchange(legacy, b'probe'))
    assert calls == [1], 'Qualification must not conceal a failure with a retry'


def test_sibling_fixture_tunnels_share_rotating_user_session_and_request_lock():
    from qualification.staging_acceptance import _api
    fixture = {'api_url': 'https://api.staging.dpenv.com', 'organisation_hash': 'same-org',
               'publisher_jwt': 'initial-access', 'publisher_refresh_jwt': 'initial-refresh'}
    first = _api(fixture, 'publisher')
    first.api.config.access_token = 'rotated-access'
    first.api.config.refresh_token = 'rotated-refresh'
    second = _api(fixture, 'publisher')
    assert second.api.config.access_token == 'rotated-access'
    assert second.api.config.refresh_token == 'rotated-refresh'
    assert second._request_lock is first._request_lock


@pytest.mark.parametrize('machine', [False, True])
def test_negative_admission_refreshes_user_session_but_never_scoped_credential(machine, monkeypatch):
    from unittest.mock import Mock
    from qualification.staging_acceptance import _api, _denied
    from dataplicity_cli.tunnels import TunnelAPI
    from dataplicity_cli.api import ApiClient
    from dataplicity_cli.config import Config
    control = (TunnelAPI(ApiClient(Config(base_url='https://api.staging.dpenv.com')), 'org', token='scoped')
               if machine else _api({'api_url': 'https://api.staging.dpenv.com',
                   'organisation_hash': 'org', 'consumer_jwt': 'access'}, 'consumer'))
    control.api.request = Mock(return_value=Mock(status_code=403))
    async def in_current_thread(function):
        return function()
    monkeypatch.setattr(asyncio, 'to_thread', in_current_thread)
    asyncio.run(_denied(control, 'bootstrap/'))
    assert control.api.request.call_args.kwargs['allow_refresh'] is (not machine)


def test_continuity_probe_keeps_same_stream_and_still_admits_new_connections(monkeypatch):
    from types import SimpleNamespace
    from qualification import staging_acceptance as acceptance
    async def exercise():
        reader = asyncio.StreamReader()
        class Writer:
            def is_closing(self):
                return False
            def write(self, data):
                reader.feed_data(data)
            async def drain(self):
                return
        writer = Writer()
        fresh = []
        async def new_exchange(_port, payload):
            fresh.append(payload)
        monkeypatch.setattr(acceptance, '_exchange', new_exchange)
        legacy = {'diagnostics': {}, 'local_port': 12345,
                  'persistent_reader': reader, 'persistent_writer': writer,
                  'persistent_lock': asyncio.Lock()}
        await acceptance._legacy_exchange(legacy, b'before-replacement')
        await acceptance._legacy_exchange(legacy, b'after-replacement')
        assert legacy['persistent_writer'] is writer
        assert fresh == [b'fresh:before-replacement', b'fresh:after-replacement']
        assert legacy['diagnostics']['probe_count'] == 2
        assert legacy['diagnostics']['persistent_probe_count'] == 2
    asyncio.run(exercise())


def test_closed_existing_stream_cannot_pass_because_new_connection_works(monkeypatch):
    from types import SimpleNamespace
    from qualification import staging_acceptance as acceptance
    async def exercise():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        fresh = []
        async def new_exchange(_port, payload):
            fresh.append(payload)
        monkeypatch.setattr(acceptance, '_exchange', new_exchange)
        legacy = {'diagnostics': {}, 'local_port': 12345, 'persistent_reader': reader,
                  'persistent_writer': SimpleNamespace(is_closing=lambda: False),
                  'persistent_lock': asyncio.Lock()}
        with pytest.raises(acceptance.LegacyExistingStreamClosed):
            await acceptance._legacy_exchange(legacy, b'after-revocation')
        assert fresh == [b'fresh:after-revocation']
    asyncio.run(exercise())


def test_legacy_failure_diagnostics_drop_secrets_unknowns_and_unbounded_values():
    from types import SimpleNamespace
    from qualification.staging_acceptance import _legacy_diagnostics
    legacy = {'diagnostics': {'http_status': 201, 'admission_stage': 3, 'shared_close_event': False,
                             'agent_channel_count': 2, 'bytes_up': 2 ** 50,
                             'payload': 'secret', 'token': 'secret', 'probe_count': True},
              'process': SimpleNamespace(returncode=None),
              'client': SimpleNamespace(ws=SimpleNamespace(state=SimpleNamespace(name='OPEN')))}
    evidence = _legacy_diagnostics(legacy)
    assert evidence == {'http_status': 201, 'admission_stage': 3, 'shared_close_event': False,
                        'agent_channel_count': 2, 'websocket_open': True, 'agent_running': True,
                        'persistent_stream_open': False}


@pytest.mark.parametrize('payload,expected', [
    ({'status': 'fail'}, 'LegacyAdmissionRejected'),
    ({'status': 'error'}, 'LegacyAdmissionRejected'),
    ({'status': 'ok'}, 'LegacyAdmissionMalformed'),
    ({'status': 'ok', 'port': True, 'service': 'redirect-port', 'route': ['client', True, 'device', True]}, 'LegacyAdmissionMalformed'),
    ({'status': 'ok', 'port': 749, 'service': 'terminal', 'route': ['client', 749, 'device', 749]}, 'LegacyAdmissionMalformed'),
    ({'status': 'ok', 'port': 749, 'service': 'redirect-port', 'route': ['someone-else', 749, 'device', 749]}, 'LegacyAdmissionMalformed'),
])
def test_legacy_http_success_cannot_admit_failed_or_wrong_route(monkeypatch, payload, expected):
    from types import SimpleNamespace
    from qualification import staging_acceptance as acceptance
    async def forbidden_wait():
        raise AssertionError('Invalid API response must fail before waiting for NotifyOpen')
    client = SimpleNamespace(identity='client', wait_for_channel_open=forbidden_wait)
    response = SimpleNamespace(ok=True, data=payload)
    with pytest.raises(getattr(acceptance, expected)):
        asyncio.run(acceptance._legacy_admitted_channel(client, response, 'device', {}))


def test_legacy_notify_open_must_match_admitted_port_without_retry_or_discard():
    from qualification import staging_acceptance as acceptance
    from dataplicity_cli.api import ApiResponse
    from dataplicity_cli.m2m import M2MClient
    async def exercise():
        client = M2MClient('wss://qualification.invalid/m2m/')
        client.identity = 'client'
        response = ApiResponse(True, 200, {'status': 'ok', 'port': 749,
            'service': 'redirect-port', 'route': ['client', 749, 'device', 749]}, '')
        client._channel_open_queue.put_nowait(748)
        client._channel_open_queue.put_nowait(749)
        diagnostic = {}
        with pytest.raises(acceptance.LegacyNotifyOpenMismatch):
            await acceptance._legacy_admitted_channel(client, response, 'device', diagnostic)
        assert diagnostic['expected_channel_match'] is False
        assert client._channel_open_queue.get_nowait() == 749
        client._channel_open_queue.put_nowait(749)
        assert await acceptance._legacy_admitted_channel(client, response, 'device', diagnostic) == 749
        assert diagnostic['expected_channel_match'] is True
    asyncio.run(exercise())


@pytest.mark.parametrize('admitted,expected', [
    (False, 'LegacyNotifyOpenTimeout'), (True, 'LegacyExistingStreamTimeout')])
def test_initial_persistent_timeout_distinguishes_admission_from_established_stream(admitted, expected):
    from types import SimpleNamespace
    from qualification import staging_acceptance as acceptance
    async def timeout(_length):
        raise asyncio.TimeoutError()
    async def drain():
        return
    async def exercise():
        legacy = {'diagnostics': {'admission_stage': 2, 'http_status': 200},
                  'process': SimpleNamespace(returncode=None),
                  'client': SimpleNamespace(ws=SimpleNamespace(state=SimpleNamespace(name='OPEN'))),
                  'persistent_lock': asyncio.Lock(), 'persistent_admitted': admitted,
                  'persistent_reader': SimpleNamespace(at_eof=lambda: False, readexactly=timeout),
                  'persistent_writer': SimpleNamespace(is_closing=lambda: False, write=lambda data: None, drain=drain)}
        with pytest.raises(getattr(acceptance, expected)):
            await acceptance._legacy_existing_exchange(legacy, b'probe')
    asyncio.run(exercise())
