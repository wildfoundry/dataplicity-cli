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
        'device_hash': 'a' * 64, 'run_id': 'test'}
    with pytest.raises(RuntimeError, match='expected phase'):
        asyncio.run(acceptance._start_presence_browser(fixture, {'cases': {}}))
    assert process.killed


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
            'm2m_frames_received': 2}
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
    {'m2m_websocket_opened': 1}, {'m2m_websocket_closed': 'false'}])
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
