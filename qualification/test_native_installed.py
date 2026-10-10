"""Fail-closed contracts for real installed-client qualification orchestration."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualification import native_installed as native


def fixture():
    return {'api_url': 'https://api.staging.dpenv.com', 'run_id': 'run-123',
            'organisation_hash': 'isolated-org', 'native_email': 'ephemeral@example.invalid',
            'native_password': 'private-password'}


@pytest.mark.parametrize('url', ['https://api.dataplicity.com', 'http://api.staging.dpenv.com',
                                  'https://api.staging.dpenv.com.evil.invalid'])
def test_native_fixture_cannot_target_non_staging(url):
    data = fixture(); data['api_url'] = url
    with pytest.raises(RuntimeError, match='requires staging'):
        native.validate_fixture(data)


def test_source_script_cannot_stand_in_for_installed_command(tmp_path):
    source = tmp_path / 'dataplicity'
    source.write_text('#!/usr/bin/python\nprint("passed")')
    with pytest.raises(RuntimeError, match='installer destination'):
        native.installed_binary(source, 'linux-deb-x64')


def test_private_configuration_uses_normal_login_without_credentials_output(monkeypatch, tmp_path):
    calls = []
    def login(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {'access': 'private-access', 'refresh': 'private-refresh'})
    monkeypatch.setattr(native.requests, 'post', login)
    config = tmp_path / 'cli.json'
    result = native.login_config(fixture(), 'native', config)
    assert calls == [('https://api.staging.dpenv.com/api/token/', {
        'json': {'email': 'ephemeral@example.invalid', 'password': 'private-password'}, 'timeout': 20})]
    assert result['auth_method'] == 'jwt'
    assert json.loads(config.read_text()) == result
    assert config.stat().st_mode & 0o777 == 0o600
    assert 'private-password' not in config.read_text()
    with pytest.raises(FileExistsError):
        native.login_config(fixture(), 'native', config)


@pytest.mark.parametrize('response', [SimpleNamespace(status_code=403),
    SimpleNamespace(status_code=200, json=lambda: {'access': 'only-access'})])
def test_failed_or_incomplete_login_never_writes_configuration(monkeypatch, tmp_path, response):
    monkeypatch.setattr(native.requests, 'post', lambda *args, **kwargs: response)
    destination = tmp_path / 'cli.json'
    with pytest.raises(RuntimeError):
        native.login_config(fixture(), 'native', destination)
    assert not destination.exists()


def test_installed_command_receives_only_private_config_path_not_fixture_credentials(monkeypatch):
    calls = []
    class Output:
        async def readline(self):
            return b'{"event":"published"}\n'
    process = SimpleNamespace(stdout=Output(), returncode=None)
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    monkeypatch.setenv('DATAPLICITY_NATIVE_FIXTURE', json.dumps(fixture()))
    monkeypatch.setattr(native.asyncio, 'create_subprocess_exec', spawn)
    result = asyncio.run(native.launch('/usr/bin/dataplicity', '/private/cli.json',
                                      fixture(), 'native-test', 'publish', 3000))
    assert result is process
    args, kwargs = calls[0]
    assert args[0] == '/usr/bin/dataplicity'
    assert '--config' in args and '/private/cli.json' in args
    assert 'DATAPLICITY_NATIVE_FIXTURE' not in kwargs['env']
    assert 'private-password' not in repr(args)
    assert kwargs['stderr'] == asyncio.subprocess.DEVNULL


@pytest.mark.parametrize('failure', [False, True])
def test_peer_gate_requires_all_five_platforms_and_cleans_tasks(monkeypatch, failure):
    finished, ready_calls = [], []
    async def peer(data, ready):
        try:
            ready()
            await asyncio.sleep(0)
            if failure and data['target'] == 'windows-x64':
                raise RuntimeError('Real peer failed')
            return {'actual_peer_bytes': True}
        finally:
            finished.append(data['target'])
    monkeypatch.setattr(native, 'native_peer', peer)
    fixtures = {target: {'target': target} for target in native.PLATFORMS}
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(native.run_native_peers(fixtures, lambda: ready_calls.append(True)))
    else:
        result = asyncio.run(native.run_native_peers(fixtures, lambda: ready_calls.append(True)))
        assert set(result) == native.PLATFORMS
        assert all(value['actual_peer_bytes'] for value in result.values())
    assert ready_calls == [True]
    assert set(finished) == native.PLATFORMS
    with pytest.raises(RuntimeError, match='All installed'):
        asyncio.run(native.run_native_peers({'windows-x64': {}}, lambda: None))


@pytest.mark.parametrize('corrupted', [False, True])
def test_exact_payload_exchange_is_framed_and_checks_origin_prefix(corrupted):
    written = []
    class Writer:
        def write(self, payload):
            written.append(payload)
        async def drain(self):
            pass
    class Reader:
        async def readexactly(self, length):
            assert length == 12
            return b'wrong:abcdef' if corrupted else b'stage:abcdef'
    if corrupted:
        with pytest.raises(RuntimeError, match='changed bytes'):
            asyncio.run(native.exact(Reader(), Writer(), b'abcdef', b'stage:'))
    else:
        asyncio.run(native.exact(Reader(), Writer(), b'abcdef', b'stage:'))
    assert written == [b'\x00\x00\x00\x06abcdef']


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancellation'])
def test_native_commands_have_separate_logins_and_stop_before_private_directory_removal(
        monkeypatch, tmp_path, outcome):
    configs, processes, stopped = [], [], []
    binary = tmp_path / 'installed-executable'
    binary.write_bytes(b'\x7fELFfixture')
    class Server:
        sockets = [SimpleNamespace(getsockname=lambda: ('127.0.0.1', 3000))]
        def close(self):
            pass
        async def wait_closed(self):
            pass
    class Writer:
        def close(self):
            pass
    class Reader:
        async def read(self, size):
            return b''
    async def echo(prefix):
        return Server(), set()
    def login(data, role, destination):
        # Every command and inventory reader receives a different refresh token.
        value = {'access_token': f'access-{len(configs)}', 'refresh_token': f'refresh-{len(configs)}'}
        destination.write_text(json.dumps(value))
        configs.append(destination)
        return value
    async def launch(binary, config, *args):
        process = SimpleNamespace(config=config)
        processes.append(process)
        return process
    async def stop(process):
        # Configuration must still exist throughout asynchronous process teardown.
        assert process.config.exists()
        await asyncio.sleep(0)
        assert process.config.exists()
        stopped.append(process)
    async def connect(*args):
        return Reader(), Writer()
    async def exact(*args):
        if outcome == 'failure':
            raise RuntimeError('Real forwarding failed')
        if outcome == 'cancellation':
            raise asyncio.CancelledError()
    async def in_thread(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(native.asyncio, 'to_thread', in_thread)
    monkeypatch.setattr(native, 'installed_binary', lambda *args: binary)
    monkeypatch.setattr(native, 'echo_service', echo)
    monkeypatch.setattr(native, 'login_config', login)
    monkeypatch.setattr(native, 'launch', launch)
    monkeypatch.setattr(native, 'stop', stop)
    monkeypatch.setattr(native, 'exact', exact)
    monkeypatch.setattr(native, 'free_port', lambda: 8080)
    monkeypatch.setattr(native.asyncio, 'open_connection', connect)
    monkeypatch.setattr(native.requests, 'get', lambda *args, **kwargs: SimpleNamespace(status_code=200))
    if outcome == 'success':
        result = asyncio.run(native.native_client(fixture(), binary, 'linux-deb-x64'))
        assert all(result['cases'].values())
        assert len(configs) == 5
        assert len(processes) == 4
        assert len(set(process.config for process in processes)) == 4
    else:
        error = RuntimeError if outcome == 'failure' else asyncio.CancelledError
        with pytest.raises(error):
            asyncio.run(native.native_client(fixture(), binary, 'linux-deb-x64'))
    assert len(stopped) == len(processes)
    assert configs and all(not config.exists() and not config.parent.exists() for config in configs)
