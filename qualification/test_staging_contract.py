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
