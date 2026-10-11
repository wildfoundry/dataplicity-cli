"""Both unchanged agents must retain fresh and existing streams independently."""
import asyncio
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from qualification import staging_acceptance as live


def test_cancelled_second_agent_credential_handoff_reaps_child_and_directory(monkeypatch, tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# unchanged package')
    (source / '_version.py').write_text('__version__ = "0.5.10"')
    (tmp_path / 'REVISION').write_text('a' * 40)
    async def exercise():
        handoff = asyncio.Event()
        async def drain():
            handoff.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, stdin=Mock())
        process.stdin.drain = drain
        process.terminate = Mock()
        process.wait = AsyncMock(return_value=0)
        monkeypatch.setattr(live.asyncio, 'to_thread', AsyncMock(side_effect=lambda function: function()))
        monkeypatch.setattr(live.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
        fixture = {'device_identity': lambda: '', 'api_url': 'https://api.staging.dpenv.com',
            'm2m_url': 'wss://m2m.staging.dpenv.com/m2m/', 'device_serial': 'isolated',
            'device_auth_token': 'test-only'}
        task = asyncio.create_task(live._start_legacy(fixture, 1, {},
            agent_root=str(tmp_path), expected_sha='a' * 40))
        await asyncio.wait_for(handoff.wait(), 1)
        directory = json.loads(process.stdin.write.call_args.args[0])['remote_directory']
        assert Path(directory).exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        assert not Path(directory).exists()
    asyncio.run(exercise())


def test_both_agents_receive_fresh_and_persistent_probes(monkeypatch):
    fresh, held = AsyncMock(), AsyncMock()
    monkeypatch.setattr(live, '_exchange', fresh)
    monkeypatch.setattr(live, '_legacy_existing_exchange', held)
    modern = {'diagnostics': {}, 'local_port': 1235, 'persistent_writer': object()}
    old = {'diagnostics': {}, 'local_port': 1234, 'persistent_writer': object(), 'companion': modern}
    asyncio.run(live._legacy_exchange(old, b'probe'))
    assert fresh.await_args_list == [((1234, b'fresh:probe'),), ((1235, b'fresh:probe'),)]
    assert [call.args[0] for call in held.await_args_list] == [old, modern]


def test_companion_failure_is_fatal_and_other_probe_cancelled(monkeypatch):
    cancelled = []
    async def exchange(port, payload):
        if port == 1235:
            raise RuntimeError('fixed test failure')
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(port)
    monkeypatch.setattr(live, '_exchange', exchange)
    modern = {'diagnostics': {}, 'local_port': 1235}
    old = {'diagnostics': {}, 'local_port': 1234, 'companion': modern}
    with pytest.raises(RuntimeError, match='fixed test failure'):
        asyncio.run(live._legacy_exchange(old, b'probe'))
    assert cancelled == [1234]


def test_second_agent_cleanup_runs_when_first_cleanup_fails(monkeypatch):
    stopped = []
    async def stop(agent):
        stopped.append(agent)
        if agent == 'old':
            raise RuntimeError('cleanup failure')
    monkeypatch.setattr(live, '_stop_legacy', stop)
    with pytest.raises(RuntimeError, match='cleanup failure'):
        asyncio.run(live._stop_agent_pair('old', 'modern'))
    assert stopped == ['old', 'modern']


def test_published_metadata_must_match_before_agent_launch(tmp_path):
    source = tmp_path / 'dataplicity'
    source.mkdir()
    (source / 'client.py').write_text('# unchanged package')
    (source / '_version.py').write_text('__version__ = "0.5.13a3"')
    pin = '3bc380b0271af6c5af11c7b65e2b06467b627bef'
    (tmp_path / 'REVISION').write_text(pin)
    (tmp_path / 'ARTIFACT_SHA256').write_text('56a9fdc079c8f0b6065abb7b942f945bbb79634c2bb0ca22f9e7fed30588f7e3')
    metadata = tmp_path / 'dataplicity-0.5.13a3.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Name: dataplicity\nVersion: 0.1.67\n')
    with pytest.raises(RuntimeError, match='metadata does not match'):
        asyncio.run(live._start_legacy({}, 1, {}, agent_root=str(tmp_path),
            expected_sha=pin, published_version='0.1.68'))
