"""Presence acceptance cannot pass without ordered, real-browser evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from qualification import staging_acceptance as acceptance


def process_for(evidence):
    async def readline(): return json.dumps(evidence).encode() + b'\n'
    stdout = SimpleNamespace(readline=readline)
    class Input:
        def __init__(self): self.writes = []
        def write(self, value): self.writes.append(value)
        async def drain(self): pass
    return SimpleNamespace(stdout=stdout, stdin=Input())


def test_presence_phase_sends_stop_handshake_and_accepts_required_evidence():
    async def exercise():
        evidence = {'status': 'passed', 'phase': 'offline_observed', 'cases': {
            key: True for key in acceptance._PRESENCE_BROWSER_CASES['offline_observed']}}
        process, report = process_for(evidence), {'cases': {}}
        await acceptance._presence_browser_phase(process, 'agent_stopped', 'offline_observed', report, 1)
        assert process.stdin.writes == [b'{"phase": "agent_stopped"}\n']
        assert report['cases'] == evidence['cases']
    asyncio.run(exercise())


@pytest.mark.parametrize('evidence', [
    {'status': 'passed', 'phase': 'online_ready', 'cases': {'live_legacy_browser_terminal_connected': True}},
    {'status': 'passed', 'phase': 'offline_observed', 'cases': {}},
    {'status': 'passed', 'phase': 'offline_observed', 'cases': {'unverified_case': True}},
    {'status': 'passed', 'phase': 'offline_observed', 'cases': {
        key: False for key in acceptance._PRESENCE_BROWSER_CASES['offline_observed']}},
])
def test_wrong_incomplete_or_false_presence_evidence_fails_closed(evidence):
    async def exercise():
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(process_for(evidence), None, 'offline_observed', {'cases': {}}, 1)
    asyncio.run(exercise())


def test_presence_helper_is_mandatory(monkeypatch):
    monkeypatch.delenv('DATAPLICITY_STAGING_BROWSER_SCRIPT', raising=False)
    with pytest.raises(RuntimeError, match='helper is required'):
        asyncio.run(acceptance._start_presence_browser({}, {}))


def test_startup_failure_cleans_up_browser_process(monkeypatch, tmp_path):
    (tmp_path / 'staging-legacy-presence-browser.mjs').write_text('// test contract')
    monkeypatch.setenv('DATAPLICITY_STAGING_BROWSER_SCRIPT', str(tmp_path / 'staging-tunnels-browser.mjs'))
    process = process_for({'status': 'failed', 'phase': 'online_ready', 'cases': {}})
    process.returncode = None
    process.killed = False
    def kill(): process.killed = True; process.returncode = -9
    async def wait(): return process.returncode
    process.kill, process.wait = kill, wait
    async def spawn(*args, **kwargs): return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    fixture = {'admin_email': 'fixture@example.test', 'admin_password': 'test-only',
        'device_hash': 'a' * 64, 'run_id': 'test', 'presence_offline_timeout_seconds': 190}
    with pytest.raises(RuntimeError, match='expected phase'):
        asyncio.run(acceptance._start_presence_browser(fixture, {'cases': {}}))
    assert process.killed


@pytest.mark.parametrize('value', [30, 190, 300])
def test_offline_budget_is_carried_to_browser_and_bounds_parent_phase(monkeypatch, tmp_path, value):
    (tmp_path / 'staging-legacy-presence-browser.mjs').write_text('// test contract')
    monkeypatch.setenv('DATAPLICITY_STAGING_BROWSER_SCRIPT', str(tmp_path / 'staging-tunnels-browser.mjs'))
    process = process_for({})
    process.returncode = None
    async def spawn(*args, **kwargs): return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    phases = []
    async def phase(actual, command, expected, report, timeout):
        assert actual is process
        phases.append((command, expected, timeout))
    monkeypatch.setattr(acceptance, '_presence_browser_phase', phase)
    fixture = {'admin_email': 'fixture@example.test', 'admin_password': 'test-only',
        'device_hash': 'a' * 64, 'run_id': 'test', 'presence_offline_timeout_seconds': value}
    async def exercise():
        assert await acceptance._start_presence_browser(fixture, {'cases': {}}) is process
        payload = json.loads(process.stdin.writes[0])
        assert payload['presence_offline_timeout_seconds'] == value
        await acceptance._presence_browser_offline(process, fixture, {'cases': {}})
    asyncio.run(exercise())
    assert phases == [(None, 'online_ready', 100), ('agent_stopped', 'offline_observed', value + 5)]


@pytest.mark.parametrize('fixture', [
    {}, *[{'presence_offline_timeout_seconds': value}
        for value in (None, True, False, 29, 301, 30.0, '190', [], {})],
])
def test_missing_or_untrusted_offline_budget_fails_before_browser_spawn(monkeypatch, tmp_path, fixture):
    (tmp_path / 'staging-legacy-presence-browser.mjs').write_text('// test contract')
    monkeypatch.setenv('DATAPLICITY_STAGING_BROWSER_SCRIPT', str(tmp_path / 'staging-tunnels-browser.mjs'))
    async def forbidden(*args, **kwargs): raise AssertionError('Browser must not start with an invalid budget')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(RuntimeError, match='bounded fixture integer'):
        asyncio.run(acceptance._start_presence_browser(fixture, {'cases': {}}))


def test_api_and_dom_offline_without_router_offline_proof_is_incomplete():
    cases = {key: True for key in acceptance._PRESENCE_BROWSER_CASES['offline_observed']
        if key != 'live_legacy_router_offline_after_agent_stop'}
    assert len(cases) == 3
    report = {'cases': {}}
    with pytest.raises(RuntimeError, match='expected phase'):
        asyncio.run(acceptance._presence_browser_phase(process_for({
            'status': 'passed', 'phase': 'offline_observed', 'cases': cases}),
            'agent_stopped', 'offline_observed', report, 1))
    assert report['cases'] == {}


def sequence_process(rows):
    process = process_for({})
    outputs = iter(rows)
    async def readline():
        row = next(outputs, None)
        if row is None:
            await asyncio.Event().wait()
        return json.dumps(row).encode() + b'\n'
    process.stdout.readline = readline
    return process


def test_browser_progress_is_consumed_before_real_success_evidence():
    async def exercise():
        cases = {'live_legacy_browser_terminal_connected': True}
        process = sequence_process([
            {'status': 'progress', 'stage': 'navigation'},
            {'status': 'progress', 'stage': 'terminal_admission'},
            {'status': 'passed', 'phase': 'online_ready', 'cases': cases},
        ])
        report = {'cases': {}}
        await acceptance._presence_browser_phase(process, None, 'online_ready', report, 1)
        assert report['cases'] == cases
        assert report['presence_browser_diagnostics'] == {'stage': 'terminal_admission'}
    asyncio.run(exercise())


def test_browser_check_failure_exposes_only_fixed_stage_and_remains_fatal():
    async def exercise():
        process = sequence_process([{'status': 'failed', 'stage': 'xterm',
            'failure': 'Bearer secret', 'exception': 'private details'}])
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(process, None, 'online_ready', report, 1)
        assert report == {'cases': {}, 'presence_browser_diagnostics': {
            'stage': 'xterm', 'failure': 'check_failed'}}
    asyncio.run(exercise())


def test_browser_network_failure_stays_fatal_and_reflects_only_bounded_fields():
    async def exercise():
        network = {'host_lookup_http_status': 200, 'terminal_http_status': 403,
            'm2m_websocket_opened': True, 'm2m_websocket_closed': False,
            'm2m_frames_received': 2, 'host_lookup_requested': False, 'host_lookup_failed': False,
            'terminal_requested': True, 'terminal_request_failed': True, 'page_error_observed': True,
            'terminal_overlay_state': 'error'}
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission',
                'network': {**network, 'url': 'private', 'identity': 'secret'}}]),
                None, 'online_ready', report, 1)
        assert report['presence_browser_diagnostics'] == {
            'stage': 'terminal_admission', 'failure': 'check_failed', **network}
        assert report['cases'] == {}
    asyncio.run(exercise())


@pytest.mark.parametrize('network', [[], None, {'host_lookup_http_status': True},
    {'terminal_http_status': 600}, {'m2m_frames_received': -1},
    {'m2m_websocket_opened': 1}, {'m2m_websocket_closed': 'false'},
    *[{key: 1} for key in ('host_lookup_requested', 'host_lookup_failed', 'terminal_requested',
        'terminal_request_failed', 'page_error_observed')],
    {'terminal_overlay_state': 'private'}, {'terminal_overlay_state': []}])
def test_malformed_browser_network_values_never_become_evidence(network):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission', 'network': network}]),
                None, 'online_ready', report, 1)
        assert report['presence_browser_diagnostics'] == {
            'stage': 'terminal_admission', 'failure': 'check_failed'}
        assert report['cases'] == {}
    asyncio.run(exercise())


def test_browser_timeout_retains_last_stage_without_extending_deadline():
    async def exercise():
        process = sequence_process([{'status': 'progress', 'stage': 'online_card'}])
        report = {'cases': {}}
        with pytest.raises(asyncio.TimeoutError):
            await acceptance._presence_browser_phase(process, None, 'online_ready', report, 0.01)
        assert report['presence_browser_diagnostics'] == {'stage': 'online_card', 'failure': 'timeout'}
        assert report['cases'] == {}
    asyncio.run(exercise())


@pytest.mark.parametrize('stage', ['Bearer_secret', ['xterm'], None])
def test_browser_untrusted_progress_stage_is_rejected_and_not_reflected(stage):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([
                {'status': 'progress', 'stage': stage}]), None, 'online_ready', report, 1)
        assert report['presence_browser_diagnostics'] == {}
    asyncio.run(exercise())


@pytest.mark.parametrize('key,value', [
    *[('host_lookup_failure_reason', value) for value in
        ('dns', 'tls', 'timeout', 'connection', 'cancelled', 'other')],
    *[('task_host_lookup_failure', value) for value in
        ('dns', 'tls', 'timeout', 'connection', 'other')],
    *[(key, value) for key in ('task_host_lookup_requested',
        'task_host_lookup_allow_origin_matches_stage', 'task_host_lookup_allow_authorization')
        for value in (True, False)],
    ('task_host_lookup_http_status', 100), ('task_host_lookup_http_status', 599),
])
def test_task_lookup_diagnostics_are_bounded_and_remain_fatal(key, value):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission',
                'network': {key: value, 'errorText': 'Bearer private', 'url': 'private'}}]),
                None, 'online_ready', report, 1)
        assert report == {'cases': {}, 'presence_browser_diagnostics': {
            'stage': 'terminal_admission', 'failure': 'check_failed', key: value}}
    asyncio.run(exercise())


@pytest.mark.parametrize('network', [
    *[{key: value} for key in ('host_lookup_failure_reason', 'task_host_lookup_failure')
        for value in ('Bearer private', [], {}, True, None)],
    {'task_host_lookup_failure': 'cancelled'},
    *[{key: value} for key in ('task_host_lookup_requested',
        'task_host_lookup_allow_origin_matches_stage', 'task_host_lookup_allow_authorization')
        for value in (1, 0, 'false', None)],
    *[{'task_host_lookup_http_status': value} for value in (True, False, 99, 600, 200.0, '200')],
])
def test_untrusted_task_lookup_values_are_not_reflected(network):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission', 'network': network}]),
                None, 'online_ready', report, 1)
        assert report == {'cases': {}, 'presence_browser_diagnostics': {
            'stage': 'terminal_admission', 'failure': 'check_failed'}}
    asyncio.run(exercise())


_CDP_BOOLEANS = (
    'browser_origin_matches_stage',
    'host_lookup_cdp_observation_available', 'host_lookup_origin_matches_stage',
    'host_lookup_uses_authorization', 'host_lookup_uses_client_app',
    'host_lookup_allow_origin_matches_stage', 'host_lookup_allow_authorization',
    'host_lookup_preflight_allow_origin_matches_stage', 'host_lookup_preflight_allow_authorization',
)
_CDP_ENUMS = {
    'host_lookup_private_network_reason': ('InsecureLocalNetwork', 'InvalidLocalNetworkAccess',
        'LocalNetworkAccessPermissionDenied', 'InsecurePrivateNetwork', 'InvalidPrivateNetworkAccess',
        'UnexpectedPrivateNetworkAccess', 'PreflightMissingAllowPrivateNetwork',
        'PreflightInvalidAllowPrivateNetwork', 'PreflightMissingPrivateNetworkAccessId',
        'PreflightMissingPrivateNetworkAccessName', 'PrivateNetworkAccessPermissionUnavailable',
        'PrivateNetworkAccessPermissionDenied', 'other'),
    'host_lookup_resource_address_space': ('local', 'private', 'public', 'loopback', 'unknown'),
    'host_lookup_initiator_address_space': ('local', 'private', 'public', 'loopback', 'unknown'),
    'host_lookup_cors_reason': ('origin', 'headers', 'method', 'credentials',
        'preflight_status', 'redirect', 'private_network', 'other'),
    'host_lookup_blocked_reason': ('csp', 'mixed_content', 'corp', 'coep', 'inspector', 'other'),
    'host_lookup_denied_header': ('authorization', 'content_type', 'csrf',
        'client_app', 'requested_with', 'other'),
}


@pytest.mark.parametrize('key,value', [
    *[(key, value) for key in _CDP_BOOLEANS for value in (True, False)],
    *[(key, value) for key, values in _CDP_ENUMS.items() for value in values],
    ('host_lookup_preflight_http_status', 100), ('host_lookup_preflight_http_status', 599),
    ('chromium_major', 1), ('chromium_major', 1000),
    ('host_lookup_header_mask', 0), ('host_lookup_header_mask', 31),
    ('host_lookup_preflight_header_mask', 0), ('host_lookup_preflight_header_mask', 63),
])
def test_cdp_observation_is_bounded_and_never_satisfies_acceptance(key, value):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission', 'network': {
                    key: value, 'request_headers': 'Bearer private', 'url': 'private'}}]),
                None, 'online_ready', report, 1)
        assert report == {'cases': {}, 'presence_browser_diagnostics': {
            'stage': 'terminal_admission', 'failure': 'check_failed', key: value}}
    asyncio.run(exercise())


@pytest.mark.parametrize('key,value', [
    *[(key, value) for key in _CDP_BOOLEANS for value in (0, 1, 'true', None)],
    *[(key, value) for key in _CDP_ENUMS for value in ('Bearer private', [], {}, True, None)],
    *[('host_lookup_preflight_http_status', value) for value in (99, 600, True, 200.0, '200')],
    *[('chromium_major', value) for value in (0, 1001, True, False, 1.0, '1', None, [], {})],
    *[(key, value) for key, maximum in (('host_lookup_header_mask', 31),
        ('host_lookup_preflight_header_mask', 63))
        for value in (-1, maximum + 1, True, 0.0, '0', None)],
])
def test_cdp_untrusted_values_are_dropped_without_reflection(key, value):
    async def exercise():
        report = {'cases': {}}
        with pytest.raises(RuntimeError, match='expected phase'):
            await acceptance._presence_browser_phase(sequence_process([{
                'status': 'failed', 'stage': 'terminal_admission', 'network': {key: value}}]),
                None, 'online_ready', report, 1)
        assert report == {'cases': {}, 'presence_browser_diagnostics': {
            'stage': 'terminal_admission', 'failure': 'check_failed'}}
    asyncio.run(exercise())
