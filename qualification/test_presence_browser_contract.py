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
