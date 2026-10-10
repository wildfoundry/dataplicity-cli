"""Live acceptance against staging APIs; no in-process router or broker adapters.

Called by the staging-only backend qualification command. Fixture credentials
remain in memory and are never included in returned evidence.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from dataplicity_cli.api import ApiClient
from dataplicity_cli.config import Config
from dataplicity_cli.m2m import M2MClient
from dataplicity_cli.remote_access import run_port_forward
from dataplicity_cli.tunnels import TunnelAPI, TunnelError, TunnelSession


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _api(fixture, principal, org=None):
    client = ApiClient(Config(base_url=fixture['api_url'], auth_method='jwt',
                              access_token=fixture[principal + '_jwt']))
    return TunnelAPI(client, org or fixture['organisation_hash'])


async def _ready(task, event, timeout=45):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            raise RuntimeError('Session exited before becoming ready')
        if waiter not in done:
            raise RuntimeError('Staging session did not become ready')
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _exchange(port, payload):
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 10)
    try:
        writer.write(payload)
        await writer.drain()
        result = await asyncio.wait_for(reader.readexactly(len(payload)), 20)
        if result != payload:
            raise RuntimeError('Forwarded bytes changed')
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def _denied(control, resource, *, method='GET', payload=None, params=None):
    response = await asyncio.to_thread(control.api.request, method, control.base + resource,
                                       json_data=payload, params=params, headers=control.headers,
                                       allow_refresh=False)
    if response.status_code not in {401, 403, 404, 409}:
        raise RuntimeError('Forbidden operation did not return an authentication or authorisation rejection')


async def qualify_named_and_legacy(fixture):
    """Exercise real CLI transports, public admission and an actual older agent."""
    if urlsplit(fixture['api_url']).hostname != 'api.staging.dpenv.com':
        raise RuntimeError('Live acceptance only targets staging')
    report = {'status': 'failed', 'cases': {}, 'router_node_ids': [],
              'actual_legacy_agent_verified': False}
    tasks, sessions, writers = [], [], set()
    observed = bytearray()
    name = 'qualification-' + fixture['run_id'].lower()[:32]
    ready_pub, ready_cons = asyncio.Event(), asyncio.Event()
    pub_api, consumer_api, admin_api = (_api(fixture, role) for role in ('publisher', 'consumer', 'admin'))

    async def echo(reader, writer):
        writers.add(writer)
        try:
            while chunk := await reader.read(65536):
                observed.extend(chunk)
                writer.write(chunk)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            return
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            writers.discard(writer)

    service = await asyncio.start_server(echo, '127.0.0.1', 0)
    target_port = service.sockets[0].getsockname()[1]
    local_port = _port()
    publisher = TunnelSession(pub_api, name, lambda e: ready_pub.set() if e['event'] == 'published' else None)
    consumer = TunnelSession(consumer_api, name, lambda e: ready_cons.set() if e['event'] == 'listener_started' else None)
    sessions.extend((publisher, consumer))
    publish_task = asyncio.create_task(publisher.publish(target_port)); tasks.append(publish_task)
    legacy = None
    phase = 'publisher_admission'
    try:
        await _ready(publish_task, ready_pub)
        # This is a separate authorised user, never an organisation login.
        phase = 'consumer_admission'
        consume_task = asyncio.create_task(consumer.connect(local_port)); tasks.append(consume_task)
        await _ready(consume_task, ready_cons)
        owner = publisher.m2m.identity.split('~', 1)[0]
        report['router_node_ids'].append(owner)
        phase = 'actual_legacy_agent_association_and_mesh'
        legacy = await _start_legacy(fixture, target_port, report)
        phase = 'simultaneous_named_and_legacy'
        payloads = [os.urandom(131072) for _ in range(6)]
        await asyncio.gather(*(_exchange(local_port, payload) for payload in payloads),
                             _legacy_exchange(legacy, payloads[0]))
        report['cases']['simultaneous_named_and_actual_legacy_mesh'] = True
        phase = 'permissions_and_cross_organisation'
        await _denied(_api(fixture, 'outsider'), 'bootstrap/', params={'name': name, 'mode': 'consumer'})
        await _denied(_api(fixture, 'admin', fixture['other_organisation_hash']),
                      'bootstrap/', params={'name': name, 'mode': 'consumer'})
        await _denied(consumer_api, 'publish/', method='POST',
                      payload={'name': name, 'port': target_port, 'identity': consumer.m2m.identity,
                               'challenge': consumer.m2m.challenge})
        report['cases']['permissions_and_cross_organisation'] = True

        # Withdraw a user's only consume grant while a live TCP stream exists.
        phase = 'consumer_permission_revocation'
        pr, pw = await asyncio.open_connection('127.0.0.1', local_port)
        pw.write(b'permission-check'); await pw.drain()
        assert await asyncio.wait_for(pr.readexactly(16), 15) == b'permission-check'
        denied_client = consumer.m2m
        denied_channel = next(iter(denied_client._channel_queues))
        await admin_api.call('DELETE', 'permissions/' + str(fixture['consumer_permission_id']) + '/')
        assert await asyncio.wait_for(pr.read(1), 15) == b''
        if denied_client.ws and denied_client.ws.state.name == 'OPEN':
            await denied_client.send_route(denied_channel, b'malicious-revoked-user-write')
        await asyncio.sleep(0.5)
        if b'malicious-revoked-user-write' in observed:
            raise RuntimeError('Revoked user forwarded bytes')
        pw.close(); await pw.wait_closed()
        await _denied(consumer_api, 'bootstrap/', params={'name': name, 'mode': 'consumer'})
        await _legacy_exchange(legacy, b'legacy-survives-consumer-revoke')
        report['cases']['server_side_consumer_permission_revocation'] = True
        await admin_api.call('POST', 'permissions/', payload={
            'name': '*', 'action': 'consume', 'user_id': fixture['consumer_user_id'], 'ports': [-1]})
        ready_cons = asyncio.Event()
        consumer = TunnelSession(consumer_api, name, lambda e: ready_cons.set() if e['event'] == 'listener_started' else None)
        sessions.append(consumer)
        consume_task = asyncio.create_task(consumer.connect(local_port)); tasks.append(consume_task)
        await _ready(consume_task, ready_cons)

        # Keep an admitted stream open; revoke via public API and deliberately
        # continue writing directly to the transport after server NotifyClose.
        phase = 'publisher_replacement'
        old_client, old_channel = consumer.m2m, None
        r, w = await asyncio.open_connection('127.0.0.1', local_port)
        w.write(b'before-revocation'); await w.drain()
        assert await asyncio.wait_for(r.readexactly(17), 15) == b'before-revocation'
        old_channel = next(iter(old_client._channel_queues))
        credential = await admin_api.call('POST', 'credentials/', payload={
            'name': name, 'ports': [target_port], 'can_replace': True, 'label': 'Staging qualification',
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        machine_control = TunnelAPI(ApiClient(Config(base_url=fixture['api_url'])),
                                    fixture['organisation_hash'], token=credential['secret'])
        new_ready = asyncio.Event()
        replacement = TunnelSession(machine_control, name, lambda e: new_ready.set() if e['event'] == 'published' else None)
        sessions.append(replacement)
        replacement_task = asyncio.create_task(replacement.publish(target_port)); tasks.append(replacement_task)
        await _ready(replacement_task, new_ready)
        assert await asyncio.wait_for(r.read(1), 15) == b''
        w.close(); await w.wait_closed()
        if old_client.ws and old_client.ws.state.name == 'OPEN':
            await old_client.send_route(old_channel, b'malicious-continued-write')
        await asyncio.sleep(0.5)
        if b'malicious-continued-write' in observed:
            raise RuntimeError('Revoked stream forwarded bytes')
        report['cases']['atomic_replacement_fences_existing_stream'] = True
        await _legacy_exchange(legacy, b'legacy-survives-replacement')

        # New consumer proves replacement works and has a fresh authority.
        phase = 'publisher_credential_revocation'
        fresh_ready = asyncio.Event(); fresh_port = _port()
        fresh = TunnelSession(consumer_api, name, lambda e: fresh_ready.set() if e['event'] == 'listener_started' else None)
        sessions.append(fresh)
        fresh_task = asyncio.create_task(fresh.connect(fresh_port)); tasks.append(fresh_task)
        await _ready(fresh_task, fresh_ready)
        await _exchange(fresh_port, b'replacement-works')
        await admin_api.call('DELETE', 'credentials/' + credential['id'] + '/')
        await _denied(machine_control, 'bootstrap/', params={'name': name, 'mode': 'publisher', 'port': target_port})
        await _legacy_exchange(legacy, b'legacy-survives-credential-revoke')
        report['cases']['publisher_credential_revocation'] = True

        # Deliberate socket loss triggers actual CLI publisher resume. Consumer
        # reconnect starts a fresh session, because established TCP is not replayed.
        phase = 'publisher_reconnect'
        reconnect_ready = asyncio.Event()
        reconnect = TunnelSession(pub_api, name, lambda e: reconnect_ready.set() if e['event'] == 'published' else None)
        sessions.append(reconnect)
        reconnect_task = asyncio.create_task(reconnect.publish(target_port)); tasks.append(reconnect_task)
        await _ready(reconnect_task, reconnect_ready)
        reconnect_ready.clear()
        await reconnect.m2m.ws.close()
        await _ready(reconnect_task, reconnect_ready, timeout=65)
        await _legacy_exchange(legacy, b'legacy-survives-reconnect')
        report['cases']['publisher_connection_loss_and_resume'] = True
        phase = 'actual_legacy_agent_restart'
        await _stop_legacy(legacy)
        legacy = None
        legacy = await _start_legacy(fixture, target_port, report)
        await _legacy_exchange(legacy, b'actual-legacy-agent-reconnect')
        report['cases']['actual_legacy_agent_restart_and_mesh_reconnect'] = True
        report['actual_legacy_agent_verified'] = True
        report['status'] = True
    finally:
        for session in sessions:
            with suppress(Exception):
                await session.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if legacy:
            await _stop_legacy(legacy)
        service.close(); await service.wait_closed()
        for writer in list(writers):
            writer.close()
    return report


async def _start_legacy(fixture, target_port, report):
    source = Path(os.environ.get('DATAPLICITY_LEGACY_AGENT_ROOT', '/opt/legacy-agent'))
    expected = os.environ.get('DATAPLICITY_LEGACY_AGENT_SHA', '')
    if len(expected) != 40 or not source.joinpath('dataplicity/client.py').is_file():
        raise RuntimeError('Pinned actual older agent source is missing')
    revision_file = source / 'REVISION'
    actual = revision_file.read_text().strip() if revision_file.exists() else subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != expected:
        raise RuntimeError('Actual older agent does not match the immutable qualification pin')
    assignments = ast.parse(source.joinpath('dataplicity/_version.py').read_text()).body
    version = next(ast.literal_eval(row.value) for row in assignments
                   if isinstance(row, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in row.targets))
    if any(marker in version for marker in ('a', 'b', 'rc')):
        raise RuntimeError('Older-agent qualification requires a stable released version')
    remote = tempfile.TemporaryDirectory(prefix='legacy-agent-qualification-')
    # Execute the released Client unchanged. Credentials enter over stdin;
    # process argv and captured qualification evidence contain no secrets.
    script = """
import json, logging, signal, sys
sys.path.insert(0, sys.argv[1])
from dataplicity.client import Client
logging.disable(logging.CRITICAL)
p = json.loads(sys.stdin.readline())
client = Client(rpc_url=p['api_url'], m2m_url=p['m2m_url'], serial=p['device_serial'],
                auth_token=p['device_auth_token'], remote_directory_path=p['remote_directory'])
signal.signal(signal.SIGTERM, lambda *_: client.exit())
client.run_forever()
"""
    previous_identity = await asyncio.to_thread(fixture['device_identity'])
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(source),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    process.stdin.write((json.dumps({key: fixture[key] for key in
        ('api_url', 'm2m_url', 'device_serial', 'device_auth_token')} |
        {'remote_directory': remote.name}) + '\n').encode())
    await process.stdin.drain(); process.stdin.close()
    legacy = {'process': process, 'directory': remote, 'tasks': [], 'client': None}
    try:
        identity = ''
        for _ in range(90):
            if process.returncode is not None:
                raise RuntimeError('The actual released agent exited before association')
            identity = await asyncio.to_thread(fixture['device_identity'])
            if identity and '~' in identity and identity != previous_identity:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError('Actual older agent did not authenticate and associate in staging')
        device_node = identity.split('~', 1)[0]
        api = _api(fixture, 'admin').api
        # Public ALB ingress is retried, never assumed sticky; distinct returned
        # identity prefixes are direct evidence that traffic crosses router nodes.
        for _ in range(40):
            client = M2MClient(fixture['m2m_url'] + '?device=' + fixture['device_hash'])
            await client.connect()
            client_identity = await client.wait_for_identity()
            if client_identity.split('~', 1)[0] != device_node:
                legacy['client'] = client
                report['router_node_ids'].extend((device_node, client_identity.split('~', 1)[0]))
                break
            await client.close()
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError('Unable to place actual agent and consumer on distinct staging router nodes')
        async def open_channel():
            response = await asyncio.to_thread(api.post,
                '/api/remote/devices/' + fixture['device_hash'] + '/ports/',
                json_data={'m2m_identity': client.identity, 'service': 'redirect-port', 'port': target_port})
            if not response.ok:
                raise RuntimeError('Actual older-agent redirect admission failed')
            return await client.wait_for_channel_open()
        local_port = _port()
        listening = asyncio.Event()
        forward = asyncio.create_task(run_port_forward(client, None, local_port, channel_factory=open_channel,
            event_callback=lambda event: listening.set() if event.kind == 'listener_started' else None))
        legacy['tasks'].append(forward)
        legacy['local_port'] = local_port
        await _ready(forward, listening)
        await _legacy_exchange(legacy, b'actual-released-agent-bidirectional-mesh')
        report['cases']['actual_released_legacy_agent_distinct_router_nodes'] = True
        report['legacy_agent'] = {'version': version, 'commit_sha': actual}
        report['router_node_ids'] = sorted(set(report['router_node_ids']))
        return legacy
    except BaseException:
        await _stop_legacy(legacy)
        raise


async def _legacy_exchange(legacy, payload):
    await _exchange(legacy['local_port'], payload)


async def _stop_legacy(legacy):
    for task in legacy.get('tasks', []):
        task.cancel()
    await asyncio.gather(*legacy.get('tasks', []), return_exceptions=True)
    if legacy.get('client'):
        await legacy['client'].close()
    if legacy['process'].returncode is None:
        legacy['process'].terminate()
        try:
            await asyncio.wait_for(legacy['process'].wait(), 10)
        except asyncio.TimeoutError:
            legacy['process'].kill()
            await legacy['process'].wait()
    legacy['directory'].cleanup()
