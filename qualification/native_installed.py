"""Installed native commands against real staging, with a separate ECS peer.

Credentials enter only through the fixture environment/private CLI config.
Only fixed case names and platform identifiers are emitted as evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import hashlib
import os
from pathlib import Path
import platform
import re
import tempfile
import uuid

import requests

PLATFORMS = {'windows-x64', 'macos-x64', 'macos-arm64', 'linux-deb-x64', 'linux-rpm-x64'}


def validate_fixture(fixture):
    if fixture.get('api_url') != 'https://api.staging.dpenv.com':
        raise RuntimeError('Native qualification requires staging')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', fixture.get('run_id', '')):
        raise RuntimeError('Invalid isolated run identifier')
    if not isinstance(fixture.get('organisation_hash'), str) or not fixture['organisation_hash']:
        raise RuntimeError('Missing isolated organisation')
    return 'native-' + fixture['run_id'].lower()


def installed_binary(binary, target):
    if target not in PLATFORMS:
        raise RuntimeError('Unknown native platform')
    expected_os = 'Windows' if target.startswith('windows') else 'Darwin' if target.startswith('macos') else 'Linux'
    if platform.system() != expected_os:
        raise RuntimeError('Native qualification requires its actual operating system')
    binary = Path(binary).resolve(strict=True)
    if not binary.is_file():
        raise RuntimeError('Missing installed native command')
    expected = ('Dataplicity/Dataplicity CLI/dataplicity.exe' if target.startswith('windows')
                else '/usr/local/bin/dataplicity' if target.startswith('macos')
                else '/usr/bin/dataplicity')
    if not str(binary).replace('\\', '/').endswith(expected):
        raise RuntimeError('Command is not in its installer destination')
    with binary.open('rb') as stream:
        magic = stream.read(4)
    valid = (magic[:2] == b'MZ' if target.startswith('windows') else
             magic in {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe'}
             if target.startswith('macos') else magic == b'\x7fELF')
    if not valid:
        raise RuntimeError('Installed command is not a native executable')
    machine = platform.machine().lower()
    if target == 'macos-arm64' and machine not in {'arm64', 'aarch64'}:
        raise RuntimeError('ARM qualification requires an ARM runner')
    if target != 'macos-arm64' and machine not in {'amd64', 'x86_64'}:
        raise RuntimeError('x64 qualification requires an x64 runner')
    return binary


def login_config(fixture, role, destination):
    response = requests.post(fixture['api_url'] + '/api/token/', json={
        'email': fixture[role + '_email'], 'password': fixture[role + '_password']}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError('Normal user login failed')
    data = response.json()
    if not all(isinstance(data.get(key), str) and data[key] for key in ('access', 'refresh')):
        raise RuntimeError('Normal user login returned invalid tokens')
    config = {'base_url': fixture['api_url'], 'auth_method': 'jwt',
              'access_token': data['access'], 'refresh_token': data['refresh'],
              'install_id': str(uuid.uuid4())}
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream)
    return config


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


async def echo_service(prefix):
    clients = set()
    async def echo(reader, writer):
        clients.add(writer)
        try:
            while True:
                size = int.from_bytes(await reader.readexactly(4), 'big')
                if not 0 < size <= 4096:
                    raise RuntimeError('Native fixture frame exceeds its bound')
                payload = await reader.readexactly(size)
                writer.write(prefix + payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            clients.discard(writer)
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    return server, clients


async def exact(reader, writer, payload, prefix):
    writer.write(len(payload).to_bytes(4, 'big') + payload); await writer.drain()
    if await asyncio.wait_for(reader.readexactly(len(prefix) + len(payload)), 30) != prefix + payload:
        raise RuntimeError('Actual native forwarding changed bytes')


async def stop(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def launch(binary, config, fixture, name, operation, port):
    args = [str(binary), '--config', str(config), '--json', 'tunnel', operation, name,
            '--org', fixture['organisation_hash'], '--port' if operation == 'publish' else '--local-port', str(port)]
    # The fixture environment contains passwords; installed commands need only config.
    env = {key: value for key, value in os.environ.items() if key != 'DATAPLICITY_NATIVE_FIXTURE'}
    process = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
    event = 'published' if operation == 'publish' else 'listener_started'
    try:
        async with asyncio.timeout(60):
            while line := await process.stdout.readline():
                data = json.loads(line)
                if data.get('event') == event:
                    return process
                if data.get('event') in {'failed', 'connection_rejected'} or data.get('ok') is False:
                    raise RuntimeError('Installed command rejected admission')
        raise RuntimeError('Installed command exited before admission')
    except BaseException:
        await stop(process)
        raise


async def native_client(fixture, binary, target):
    name = validate_fixture(fixture)
    binary = installed_binary(binary, target)
    processes, writers = [], []
    server, clients = await echo_service(b'native:')
    cases = {}
    directory = tempfile.TemporaryDirectory(prefix='native-cli-staging-')
    config_index = 0
    async def own_config():
        nonlocal config_index
        config_index += 1
        path = Path(directory.name) / f'cli-{config_index}.json'
        await asyncio.to_thread(login_config, fixture, 'native', path)
        return path
    try:
        port = free_port()
        processes.append(await launch(binary, await own_config(), fixture, name + '-from-stage', 'connect', port))
        reader, writer = await asyncio.open_connection('127.0.0.1', port); writers.append(writer)
        await exact(reader, writer, os.urandom(2048), b'stage:')
        cases['installed_consumer_to_staging_publisher'] = True
        service_port = server.sockets[0].getsockname()[1]
        processes.append(await launch(binary, await own_config(), fixture, name + '-from-native', 'publish', service_port))
        cases['installed_native_publisher_admitted'] = True
        processes.append(await launch(binary, await own_config(), fixture, name + '-from-stage', 'publish', service_port))
        if await asyncio.wait_for(reader.read(1), 30) != b'':
            raise RuntimeError('Native replacement did not fence existing stream')
        cases['installed_replacement_fences_existing_stream'] = True
        fresh_port = free_port()
        processes.append(await launch(binary, await own_config(), fixture, name + '-from-stage', 'connect', fresh_port))
        new_reader, new_writer = await asyncio.open_connection('127.0.0.1', fresh_port); writers.append(new_writer)
        await exact(new_reader, new_writer, os.urandom(2048), b'native:')
        cases['installed_replacement_routes_fresh_stream'] = True
        # Peer writes this marker only after an actual ECS consumer receives
        # the native publisher's exact bytes. Native helpers must await it.
        await exact(new_reader, new_writer, b'peer-check-ready', b'native:')
        # Inventory receives its own independently issued rotating session too.
        config = await own_config()
        control_path = f'/api/organisations/{fixture["organisation_hash"]}/development-tunnels/names/{name}-peer-verified/'
        async with asyncio.timeout(90):
            while True:
                saved = json.loads(config.read_text())
                response = await asyncio.to_thread(requests.get, fixture['api_url'] + control_path,
                    headers={'Authorization': 'Bearer ' + saved['access_token']}, timeout=10)
                if response.status_code == 200:
                    cases['staging_consumer_to_installed_native_publisher'] = True
                    break
                if response.status_code != 404:
                    raise RuntimeError('Native peer verification inventory failed')
                await asyncio.sleep(1)
        digest = hashlib.sha256()
        with binary.open('rb') as executable:
            while chunk := executable.read(65536):
                digest.update(chunk)
        return {'status': 'passed', 'platform': target, 'cases': cases,
                'installed_os': platform.system(), 'installed_machine': platform.machine().lower(),
                'installed_executable_sha256': digest.hexdigest()}
    finally:
        try:
            # Terminate every command before removing any private configuration.
            # Independent processes cannot race rotating refresh tokens.
            stopped = await asyncio.gather(*(stop(process) for process in reversed(processes)),
                                           return_exceptions=True)
            for writer in writers + list(clients):
                writer.close()
            server.close(); await server.wait_closed()
            for error in stopped:
                if isinstance(error, BaseException):
                    raise error
        finally:
            directory.cleanup()


async def native_peer(fixture, ready=None):
    """ECS counterpart using ordinary user authentication and the real router."""
    from dataplicity_cli.api import ApiClient
    from dataplicity_cli.config import Config
    from dataplicity_cli.tunnels import TunnelAPI, TunnelSession
    from qualification.staging_acceptance import _ready
    name = validate_fixture(fixture)
    server, clients = await echo_service(b'stage:')
    sessions, tasks, writers = [], [], []
    try:
        with tempfile.TemporaryDirectory(prefix='native-peer-staging-') as directory:
            data = await asyncio.to_thread(login_config, fixture, 'peer', Path(directory) / 'cli.json')
            control = TunnelAPI(ApiClient(Config(**data)), fixture['organisation_hash'])
            def session(tunnel_name, event):
                signal = asyncio.Event()
                client = TunnelSession(control, tunnel_name,
                    lambda message: signal.set() if message['event'] == event else None)
                sessions.append(client)
                return client, signal
            publisher, published = session(name + '-from-stage', 'published')
            task = asyncio.create_task(publisher.publish(server.sockets[0].getsockname()[1])); tasks.append(task)
            await _ready(task, published)
            if ready:
                ready()
            # Poll presence before attempting one real forwarding exchange.
            async with asyncio.timeout(900):
                while True:
                    def inventory():
                        with control._request_lock:
                            return control.api.get(control.base + 'names/' + name + '-from-native/')
                    response = await asyncio.to_thread(inventory)
                    if response.status_code == 200:
                        break
                    if response.status_code != 404:
                        raise RuntimeError('Native publisher inventory failed')
                    await asyncio.sleep(1)
            consumer, listening = session(name + '-from-native', 'listener_started')
            port = free_port()
            task = asyncio.create_task(consumer.connect(port)); tasks.append(task)
            await _ready(task, listening)
            reader, writer = await asyncio.open_connection('127.0.0.1', port); writers.append(writer)
            await exact(reader, writer, os.urandom(2048), b'native:')
            marker, marked = session(name + '-peer-verified', 'published')
            task = asyncio.create_task(marker.publish(server.sockets[0].getsockname()[1])); tasks.append(task)
            await _ready(task, marked)
            if await asyncio.wait_for(reader.read(1), 900) != b'':
                raise RuntimeError('Unexpected peer stream data after exact exchange')
            return {'staging_publisher_admitted': True,
                    'staging_consumer_received_installed_publisher_bytes': True}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions:
            with suppress(Exception):
                await session.close()
        for writer in writers + list(clients):
            writer.close()
        server.close(); await server.wait_closed()


async def run_native_peers(fixtures, ready):
    if set(fixtures) != PLATFORMS:
        raise RuntimeError('All installed native platforms are required')
    admitted = set()
    tasks = {}
    def marked(target):
        admitted.add(target)
        if admitted == PLATFORMS:
            ready()
    try:
        tasks = {target: asyncio.create_task(native_peer(fixture, lambda target=target: marked(target)))
                 for target, fixture in fixtures.items()}
        results = await asyncio.gather(*tasks.values())
        return dict(zip(tasks, results))
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--platform', required=True, choices=sorted(PLATFORMS))
    args = parser.parse_args()
    try:
        fixture = json.loads(os.environ.pop('DATAPLICITY_NATIVE_FIXTURE'))
        result = asyncio.run(asyncio.wait_for(native_client(fixture, args.binary, args.platform), 240))
    except Exception as exc:
        result = {'status': 'failed', 'platform': args.platform, 'failure_type': type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
