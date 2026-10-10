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
        assert fresh == [b'before-replacement', b'after-replacement']
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
        assert fresh == [b'after-revocation']
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
