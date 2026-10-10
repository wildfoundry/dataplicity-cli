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
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

from dataplicity_cli.api import ApiClient
from dataplicity_cli.config import Config
from dataplicity_cli.m2m import M2MClient
from dataplicity_cli.remote_access import run_port_forward
from dataplicity_cli.tunnels import TunnelAPI, TunnelSession


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _api(fixture, principal, org=None):
    # Normal rotating refresh tokens represent one user session. Reuse its
    # client/config and lock across this user's tunnel controls; otherwise one
    # rotation blacklists refresh tokens still held by sibling probe sessions.
    clients = fixture.setdefault('_qualification_api_clients', {})
    if principal not in clients:
        client = ApiClient(Config(base_url=fixture['api_url'], auth_method='jwt',
                                 access_token=fixture[principal + '_jwt'],
                                 refresh_token=fixture.get(principal + '_refresh_jwt')))
        clients[principal] = (client, threading.Lock())
    client, lock = clients[principal]
    control = TunnelAPI(client, org or fixture['organisation_hash'])
    control._request_lock = lock
    return control


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
    def request():
        with control._request_lock:
            return control.api.request(method, control.base + resource, json_data=payload,
                                       params=params, headers=control.headers,
                                       allow_refresh=control.headers is None)
    response = await asyncio.to_thread(request)
    if response.status_code not in {401, 403, 404, 409}:
        raise RuntimeError('Forbidden operation did not return an authentication or authorisation rejection')


async def qualify_named_and_legacy(fixture):
    """Exercise real CLI transports, public admission and an actual older agent."""
    if (urlsplit(fixture['api_url']).scheme != 'https'
            or urlsplit(fixture['api_url']).hostname != 'api.staging.dpenv.com'):
        raise RuntimeError('Live acceptance only targets staging')
    relay = urlsplit(fixture['m2m_url'])
    if relay.scheme != 'wss' or relay.hostname != 'm2m.staging.dpenv.com':
        raise RuntimeError('Actual agent relay must be the secure staging endpoint')
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
    presence_browser = None
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
        # Stay within the fixture's measured protective one-MiB/s bidirectional
        # budget; quota-exhaustion tests must be a separate acceptance case.
        payloads = [os.urandom(65536) for _ in range(6)]
        await asyncio.gather(*(_exchange(local_port, payload) for payload in payloads),
                             _legacy_exchange(legacy, payloads[0]))
        report['cases']['simultaneous_named_and_actual_legacy_mesh'] = True
        reader, writer = await asyncio.open_connection('127.0.0.1', local_port)
        try:
            payload = os.urandom(65536)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            assert await asyncio.wait_for(reader.readexactly(len(payload)), 20) == payload
            assert await asyncio.wait_for(reader.read(1), 20) == b''
            report['cases']['actual_tcp_half_close_through_staging'] = True
        finally:
            writer.close()
            await writer.wait_closed()
        phase = '104_simultaneous_named_streams_and_legacy'
        await asyncio.wait_for(_concurrency_acceptance(fixture, name, target_port, legacy), 180)
        report['cases']['104_simultaneous_named_streams_and_actual_legacy_mesh'] = True
        phase = 'actual_http_websocket_ssh_postgresql_protocols'
        await _protocol_acceptance(fixture, name, legacy, report)
        phase = 'actual_legacy_agent_wormhole_http'
        await _wormhole_acceptance(fixture, local_port, legacy)
        report['cases']['actual_legacy_agent_wormhole_http_with_named_traffic'] = True
        phase = 'live_frontend_active'
        await _live_browser(fixture, name, 'active', report)
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
        await consumer.close()
        consume_task.cancel()
        await asyncio.gather(consume_task, return_exceptions=True)
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
        credential_reader, credential_writer = await asyncio.open_connection('127.0.0.1', fresh_port)
        credential_writer.write(b'credential-active'); await credential_writer.drain()
        assert await asyncio.wait_for(credential_reader.readexactly(17), 15) == b'credential-active'
        credential_client = fresh.m2m
        credential_channel = next(iter(credential_client._channel_queues))
        await admin_api.call('DELETE', 'credentials/' + credential['id'] + '/')
        assert await asyncio.wait_for(credential_reader.read(1), 15) == b''
        if credential_client.ws and credential_client.ws.state.name == 'OPEN':
            await credential_client.send_route(credential_channel, b'malicious-revoked-credential-write')
        await asyncio.sleep(0.5)
        if b'malicious-revoked-credential-write' in observed:
            raise RuntimeError('Revoked credential forwarded bytes')
        credential_writer.close(); await credential_writer.wait_closed()
        await _denied(machine_control, 'bootstrap/', params={'name': name, 'mode': 'publisher', 'port': target_port})
        await _legacy_exchange(legacy, b'legacy-survives-credential-revoke')
        report['cases']['publisher_credential_revocation'] = True
        phase = 'live_frontend_revoked'
        await _live_browser(fixture, name, 'revoked', report)

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
        presence_browser = await _start_presence_browser(fixture, report)
        await _stop_legacy(legacy)
        legacy = None
        await _presence_browser_phase(presence_browser, 'agent_stopped', 'offline_observed', report, 125)
        legacy = await _start_legacy(fixture, target_port, report)
        await _legacy_exchange(legacy, b'actual-legacy-agent-reconnect')
        await _presence_browser_phase(presence_browser, 'agent_restarted', 'reconnected', report, 95)
        await asyncio.wait_for(presence_browser.wait(), 10)
        if presence_browser.returncode != 0:
            raise RuntimeError('Retained live presence browser did not exit successfully')
        report['cases']['actual_legacy_agent_restart_and_mesh_reconnect'] = True
        report['cases']['existing_legacy_stream_survives_named_operations'] = True
        report['actual_legacy_agent_verified'] = True
        report['status'] = 'passed'
    except Exception as exc:
        report['failure_type'] = type(exc).__name__
        report['failed_phase'] = phase
    finally:
        if presence_browser and presence_browser.returncode is None:
            presence_browser.kill()
            await presence_browser.wait()
        if legacy:
            report['legacy_diagnostics'] = _legacy_diagnostics(legacy)
        # Cancel lifecycle loops before closing transports so publishers cannot
        # interpret qualification cleanup as a loss and start reconnecting.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions:
            with suppress(Exception):
                await session.close()
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
import json, logging, signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
from dataplicity.client import Client
logging.disable(logging.CRITICAL)
p = json.loads(sys.stdin.readline())
client = Client(rpc_url=p['api_url'], m2m_url=p['m2m_url'], serial=p['device_serial'],
                auth_token=p['device_auth_token'], remote_directory_path=p['remote_directory'])
signal.signal(signal.SIGTERM, lambda *_: client.exit())
def report_state():
    while not client.exit_event.is_set():
        # Only fixed, typed state fields. Agent credentials and traffic never
        # enter this diagnostic pipe; the released Client remains unchanged.
        print(json.dumps({'shared_close_event': client.port_forward.close_event.is_set(),
                          'agent_channel_count': len(client.m2m.m2m_client.channels),
                          'agent_m2m_closed': client.m2m.m2m_client.is_closed}), flush=True)
        time.sleep(0.2)
threading.Thread(target=report_state, daemon=True).start()
client.run_forever()
"""
    previous_identity = await asyncio.to_thread(fixture['device_identity'])
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(source),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    process.stdin.write((json.dumps({key: fixture[key] for key in
        ('api_url', 'm2m_url', 'device_serial', 'device_auth_token')} |
        {'remote_directory': remote.name}) + '\n').encode())
    await process.stdin.drain(); process.stdin.close()
    legacy = {'process': process, 'directory': remote, 'tasks': [], 'client': None,
              'diagnostics': {'admission_stage': 0, 'http_status': 0, 'bytes_up': 0, 'bytes_down': 0,
                              'probe_count': 0, 'persistent_probe_count': 0}}
    async def collect_state():
        while line := await process.stdout.readline():
            try:
                state = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(state, dict):
                continue
            for key in ('shared_close_event', 'agent_m2m_closed'):
                if type(state.get(key)) is bool:
                    legacy['diagnostics'][key] = state[key]
            if type(state.get('agent_channel_count')) is int and 0 <= state['agent_channel_count'] <= 4096:
                legacy['diagnostics']['agent_channel_count'] = state['agent_channel_count']
    legacy['tasks'].append(asyncio.create_task(collect_state()))
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
        admin_control = _api(fixture, 'admin')
        api = admin_control.api
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
            legacy['diagnostics'].pop('expected_channel_match', None)
            legacy['diagnostics'].pop('admission_failure', None)
            legacy['diagnostics']['admission_stage'] = 1
            def admit():
                with admin_control._request_lock:
                    return api.post('/api/remote/devices/' + fixture['device_hash'] + '/ports/',
                        json_data={'m2m_identity': client.identity, 'service': 'redirect-port', 'port': target_port})
            response = await asyncio.to_thread(admit)
            legacy['diagnostics']['http_status'] = response.status_code
            legacy['diagnostics']['admission_stage'] = 2
            return await _legacy_admitted_channel(client, response, identity, legacy['diagnostics'])
        local_port = _port()
        listening = asyncio.Event()
        def progress(event):
            if event.kind == 'listener_started':
                listening.set()
            elif event.kind in ('bytes_up', 'bytes_down'):
                legacy['diagnostics'][event.kind] += event.bytes_count
        forward = asyncio.create_task(run_port_forward(client, None, local_port, channel_factory=open_channel,
            event_callback=progress))
        legacy['tasks'].append(forward)
        legacy['local_port'] = local_port
        await _ready(forward, listening)
        await _legacy_exchange(legacy, b'actual-released-agent-bidirectional-mesh')
        legacy['persistent_reader'], legacy['persistent_writer'] = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', local_port), 10)
        legacy['persistent_lock'] = asyncio.Lock()
        await _legacy_existing_exchange(legacy, b'actual-existing-legacy-stream')
        report['cases']['actual_released_legacy_agent_distinct_router_nodes'] = True
        report['legacy_agent'] = {'version': version, 'commit_sha': actual}
        report['router_node_ids'] = sorted(set(report['router_node_ids']))
        return legacy
    except BaseException:
        report['legacy_diagnostics'] = _legacy_diagnostics(legacy)
        await _stop_legacy(legacy)
        raise


class LegacyAdmissionTimeout(RuntimeError):
    """No ports API response before the probe deadline."""


class LegacyAdmissionRejected(RuntimeError):
    """Ports API explicitly rejected a legacy channel."""


class LegacyAdmissionMalformed(RuntimeError):
    """Ports API did not return the requested client/device/service route."""


class LegacyNotifyOpenMismatch(RuntimeError):
    """NotifyOpen identified a different channel from the API admission."""


class LegacyNotifyOpenTimeout(RuntimeError):
    """Ports API accepted the channel but NotifyOpen did not arrive."""


class LegacyPayloadTimeout(RuntimeError):
    """Admitted legacy channel did not return the probe payload."""


class LegacyAgentGlobalShutdown(RuntimeError):
    """Unchanged released agent set its shared port-forward shutdown event."""


class LegacyRouterSocketClosed(RuntimeError):
    """The legacy client router WebSocket closed during the probe."""


class LegacyExistingStreamTimeout(RuntimeError):
    """The already established legacy TCP stream did not return probe bytes."""


class LegacyExistingStreamClosed(RuntimeError):
    """The already established legacy TCP stream closed during named operations."""


class LegacyAgentExited(RuntimeError):
    """The unchanged agent subprocess exited during the probe."""


def _legacy_failure(legacy, fallback, *, admission=True):
    diagnostic = legacy['diagnostics']
    if legacy['process'].returncode is not None:
        return LegacyAgentExited()
    if diagnostic.get('shared_close_event') is True:
        return LegacyAgentGlobalShutdown()
    if legacy['client'].ws is None or legacy['client'].ws.state.name != 'OPEN':
        return LegacyRouterSocketClosed()
    if admission:
        failure = diagnostic.get('admission_failure')
        if failure == 'rejected':
            return LegacyAdmissionRejected()
        if failure == 'malformed':
            return LegacyAdmissionMalformed()
        if failure == 'mismatch':
            return LegacyNotifyOpenMismatch()
        stage = diagnostic['admission_stage']
        if stage < 2:
            return LegacyAdmissionTimeout()
        if diagnostic['http_status'] not in range(200, 300):
            return LegacyAdmissionRejected()
        if stage < 3:
            return LegacyNotifyOpenTimeout()
    return fallback()


async def _legacy_admitted_channel(client, response, device_identity, diagnostic):
    payload = response.data
    if not response.ok or isinstance(payload, dict) and payload.get('status') in ('fail', 'error'):
        diagnostic['admission_failure'] = 'rejected'
        raise LegacyAdmissionRejected()
    port = payload.get('port') if isinstance(payload, dict) else None
    route = payload.get('route') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('status') != 'ok'
            or type(port) is not int or not 0 < port < 2 ** 63
            or payload.get('service') != 'redirect-port'
            or not isinstance(route, list) or len(route) != 4
            or type(route[1]) is not int or type(route[3]) is not int
            or route != [client.identity, port, device_identity, port]):
        diagnostic['admission_failure'] = 'malformed'
        raise LegacyAdmissionMalformed()
    opened = await client.wait_for_channel_open()
    diagnostic['expected_channel_match'] = type(opened) is int and opened == port
    if type(opened) is not int or opened != port:
        diagnostic['admission_failure'] = 'mismatch'
        raise LegacyNotifyOpenMismatch()
    diagnostic['admission_stage'] = 3
    return port


async def _legacy_exchange(legacy, payload):
    diagnostic = legacy['diagnostics']
    diagnostic.update(admission_stage=0, http_status=0, bytes_up=0, bytes_down=0)
    diagnostic.pop('admission_failure', None)
    diagnostic['probe_count'] = diagnostic.get('probe_count', 0) + 1
    probes = [asyncio.create_task(_exchange(legacy['local_port'], b'fresh:' + payload))]
    if legacy.get('persistent_writer'):
        # Keep testing freshly admitted connections as well as the old stream;
        # establishing a new stream never substitutes for continuity evidence.
        probes.append(asyncio.create_task(_legacy_existing_exchange(legacy, payload)))
    try:
        await asyncio.gather(*probes)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        # Distinct safe exception names survive backend evidence filtering.
        # No retries, replacement probes or weaker success criteria hide failure.
        raise _legacy_failure(legacy, LegacyPayloadTimeout) from exc
    finally:
        for probe in probes:
            probe.cancel()
        await asyncio.gather(*probes, return_exceptions=True)


async def _legacy_existing_exchange(legacy, payload):
    payload = b'persistent:' + payload
    async with legacy['persistent_lock']:
        reader, writer = legacy['persistent_reader'], legacy['persistent_writer']
        if writer.is_closing() or reader.at_eof():
            raise LegacyExistingStreamClosed()
        try:
            writer.write(payload)
            await writer.drain()
            result = await asyncio.wait_for(reader.readexactly(len(payload)), 20)
        except asyncio.TimeoutError as exc:
            raise _legacy_failure(legacy, LegacyExistingStreamTimeout,
                                  admission=not legacy.get('persistent_admitted')) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise _legacy_failure(legacy, LegacyExistingStreamClosed,
                                  admission=not legacy.get('persistent_admitted')) from exc
        if result != payload:
            raise RuntimeError('Established legacy stream changed probe bytes')
        legacy['persistent_admitted'] = True
        legacy['diagnostics']['persistent_probe_count'] = legacy['diagnostics'].get('persistent_probe_count', 0) + 1


def _legacy_diagnostics(legacy):
    diagnostic = dict(legacy['diagnostics'])
    client = legacy.get('client')
    diagnostic['websocket_open'] = bool(client and client.ws and client.ws.state.name == 'OPEN')
    diagnostic['agent_running'] = legacy['process'].returncode is None
    writer, reader = legacy.get('persistent_writer'), legacy.get('persistent_reader')
    diagnostic['persistent_stream_open'] = bool(writer and not writer.is_closing() and not reader.at_eof())
    limits = {'admission_stage': 3, 'http_status': 599, 'bytes_up': 2 ** 30, 'bytes_down': 2 ** 30,
              'agent_channel_count': 4096, 'probe_count': 256, 'persistent_probe_count': 256}
    booleans = {'shared_close_event', 'agent_m2m_closed', 'websocket_open', 'agent_running', 'persistent_stream_open', 'expected_channel_match'}
    return {key: value for key, value in diagnostic.items() if
            key in limits and type(value) is int and 0 <= value <= limits[key]
            or key in booleans and type(value) is bool}


async def _stop_legacy(legacy):
    if legacy.get('persistent_writer'):
        legacy['persistent_writer'].close()
        with suppress(OSError):
            await legacy['persistent_writer'].wait_closed()
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


async def _mobile_device(fixture, method, payload=None):
    control = _api(fixture, 'admin')
    device_hash = fixture['device_hash']
    if not re.fullmatch(r'[0-9a-f]{64}', device_hash):
        raise RuntimeError('Invalid isolated fixture device')
    def request():
        with control._request_lock:
            response = control.api.request(method, f'/devices/{device_hash}/', json_data=payload)
        if not response.ok or not isinstance(response.data, dict):
            raise RuntimeError('Normal device API failed')
        return response.data
    return await asyncio.to_thread(request)


def _wormhole_url(device):
    slug = device.get('wormhole_override') or device.get('wormhole_slug')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{6,38}[a-z0-9]', slug):
        raise RuntimeError('Invalid staging Wormhole hostname')
    return f'https://{slug}.wormhole.staging.dpenv.com/qualification-health'


async def _wormhole_acceptance(fixture, named_port, legacy):
    """Actual released agent's built-in web service and public staging ingress."""
    from aiohttp import ClientSession, ClientTimeout, web
    marker = 'staging-wormhole-' + os.urandom(16).hex()
    async def health(request):
        return web.Response(text=marker)
    app = web.Application(client_max_size=1024)
    app.router.add_get('/qualification-health', health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    original = None
    try:
        # The released agent's existing `web` service targets 127.0.0.1:80.
        # Root is already configured for this isolated qualification container.
        await web.TCPSite(runner, '127.0.0.1', 80).start()
        original = await _mobile_device(fixture, 'GET')
        if original.get('wormhole_enabled') is not False:
            raise RuntimeError('Isolated fixture Wormhole was unexpectedly enabled')
        enabled = await _mobile_device(fixture, 'PATCH', {'wormhole_enabled': True})
        if enabled.get('wormhole_enabled') is not True:
            raise RuntimeError('Normal device API did not enable Wormhole')
        url = _wormhole_url(enabled)
        async def public_probe():
            # No credentials, redirects or retries can substitute for this exact
            # TLS-verified staging hostname and the actual agent-local marker.
            async with ClientSession(timeout=ClientTimeout(total=45)) as client:
                async with client.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise RuntimeError('Actual agent Wormhole response failed')
                    body = await response.content.readexactly(len(marker))
                    extra = await response.content.read(1)
                    if body != marker.encode() or extra:
                        raise RuntimeError('Actual agent Wormhole response failed')
        probes = [asyncio.create_task(probe) for probe in (
            public_probe(), _exchange(named_port, b'named-during-wormhole'),
            _legacy_exchange(legacy, b'legacy-during-wormhole'))]
        try:
            await asyncio.gather(*probes)
        finally:
            for probe in probes:
                probe.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
    finally:
        try:
            if original is not None and original.get('wormhole_enabled') is False:
                disabled = await _mobile_device(fixture, 'PATCH', {'wormhole_enabled': False})
                if disabled.get('wormhole_enabled') is not False:
                    raise RuntimeError('Normal device API did not disable Wormhole')
        finally:
            await runner.cleanup()


async def _concurrency_acceptance(fixture, name, target_port, legacy):
    """Bounded functional concurrency, under ordinary server-owned quotas."""
    tasks, sessions, connections = [], [], []
    try:
        ports = []
        for index in range(4):
            pub_ready, cons_ready = asyncio.Event(), asyncio.Event()
            def published(event, ready=pub_ready):
                if event['event'] == 'published':
                    ready.set()
            def listening(event, ready=cons_ready):
                if event['event'] == 'listener_started':
                    ready.set()
            tunnel_name = f'{name}-concurrent-{index}'
            publisher = TunnelSession(_api(fixture, 'admin'), tunnel_name, published)
            consumer = TunnelSession(_api(fixture, 'admin'), tunnel_name, listening)
            sessions.extend((publisher, consumer))
            pub_task = asyncio.create_task(publisher.publish(target_port)); tasks.append(pub_task)
            await _ready(pub_task, pub_ready)
            port = _port()
            cons_task = asyncio.create_task(consumer.connect(port)); tasks.append(cons_task)
            await _ready(cons_task, cons_ready)
            ports.append(port)

        async def admitted(index):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', ports[index // 26]), 10)
            # Register ownership before any awaited I/O, including failed admission.
            payload = index.to_bytes(4, 'big') + os.urandom(1020)
            connections.append((reader, writer, payload))
            writer.write(payload); await writer.drain()
            if await asyncio.wait_for(reader.readexactly(len(payload)), 30) != payload:
                raise RuntimeError('Concurrent stream payload changed')

        admissions = [asyncio.create_task(admitted(index)) for index in range(104)]
        tasks.extend(admissions)
        await asyncio.gather(*admissions)
        if len(connections) != 104 or any(writer.is_closing() or reader.at_eof()
                                         for reader, writer, _ in connections):
            raise RuntimeError('Concurrent streams were not all retained')
        # _legacy_exchange requires both a fresh admission and the existing
        # cross-router stream; neither substitutes for the other.
        await _legacy_exchange(legacy, b'legacy-during-104-held-streams')
        async def retained(reader, writer, payload):
            payload = b'held:' + payload
            writer.write(payload); await writer.drain()
            if await asyncio.wait_for(reader.readexactly(len(payload)), 30) != payload:
                raise RuntimeError('Retained concurrent stream payload changed')
        checks = [asyncio.create_task(retained(*connection)) for connection in connections]
        tasks.extend(checks)
        await asyncio.gather(*checks)
        if any(writer.is_closing() or reader.at_eof() for reader, writer, _ in connections):
            raise RuntimeError('Concurrent stream closed during legacy probes')
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _, writer, _ in connections:
            writer.close()
        for _, writer, _ in connections:
            with suppress(OSError):
                await writer.wait_closed()
        for session in sessions:
            with suppress(Exception):
                await session.close()


async def _protocol_acceptance(fixture, name, legacy, report):
    """Real protocol stacks through public staging admission and CLI forwarding."""
    from aiohttp import ClientSession, WSMsgType, web
    from qualification.protocols import SSHFixture

    async def through(target_port, suffix, probe):
        ready_pub, ready_cons = asyncio.Event(), asyncio.Event()
        publisher = TunnelSession(_api(fixture, 'publisher'), name + suffix,
            lambda event: ready_pub.set() if event['event'] == 'published' else None)
        consumer = TunnelSession(_api(fixture, 'consumer'), name + suffix,
            lambda event: ready_cons.set() if event['event'] == 'listener_started' else None)
        pub_task = asyncio.create_task(publisher.publish(target_port))
        cons_task = None
        try:
            await _ready(pub_task, ready_pub)
            local_port = _port()
            cons_task = asyncio.create_task(consumer.connect(local_port))
            await _ready(cons_task, ready_cons)
            await asyncio.gather(asyncio.wait_for(probe(local_port), 45),
                                 _legacy_exchange(legacy, ('legacy-during-' + suffix).encode()))
        finally:
            pub_task.cancel()
            if cons_task:
                cons_task.cancel()
            await asyncio.gather(pub_task, *([cons_task] if cons_task else []), return_exceptions=True)
            await consumer.close()
            await publisher.close()

    async def websocket_echo(request):
        websocket = web.WebSocketResponse(compress=False)
        await websocket.prepare(request)
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                await websocket.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await websocket.send_bytes(message.data)
        return websocket

    app = web.Application()
    app.router.add_get('/health', lambda request: web.json_response({'service': 'live-staging-tunnel'}))
    app.router.add_get('/echo', websocket_echo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        async def http_and_websocket(port):
            async with ClientSession() as http:
                async with http.get(f'http://127.0.0.1:{port}/health') as response:
                    assert response.status == 200
                    assert await response.json() == {'service': 'live-staging-tunnel'}
                async with http.ws_connect(f'http://127.0.0.1:{port}/echo', compress=0) as websocket:
                    await websocket.send_json({'protocol': 'websocket', 'live_staging': True})
                    assert await websocket.receive_json() == {'protocol': 'websocket', 'live_staging': True}
                    payload = os.urandom(65536)
                    await websocket.send_bytes(payload)
                    assert (await websocket.receive()).data == payload
                    await websocket.send_str('still-open')
                    assert (await websocket.receive()).data == 'still-open'
        await through(site._server.sockets[0].getsockname()[1], '-http', http_and_websocket)
        report['cases']['actual_http_and_websocket_through_staging'] = True
    finally:
        await runner.cleanup()

    ssh = SSHFixture()
    try:
        await through(ssh.server.server_address[1], '-ssh', lambda port: asyncio.to_thread(ssh.probe, port))
        report['cases']['actual_pinned_key_ssh_through_staging'] = True
    finally:
        await asyncio.to_thread(ssh.close)

    # PostgreSQL's actual wire protocol is forwarded to the staging database.
    # The first query begins a READ ONLY transaction, compatible with RDS Proxy.
    db_host = os.environ.get('SQL_HOST')
    db_password = os.environ.get('SQL_PASSWORD')
    if not db_host or not db_password:
        raise RuntimeError('Staging readonly PostgreSQL qualification credentials are missing')
    proxy_tasks, proxy_writers = set(), set()

    async def pg_proxy(reader, writer):
        task = asyncio.current_task(); proxy_tasks.add(task)
        remote_writer = None
        proxy_writers.add(writer)
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(db_host, int(os.environ.get('SQL_PORT', '5432'))), 10)
            proxy_writers.add(remote_writer)
            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                if destination.can_write_eof():
                    destination.write_eof()
            await asyncio.gather(copy(reader, remote_writer), copy(remote_reader, writer))
        except (OSError, asyncio.CancelledError):
            return
        finally:
            writer.close()
            if remote_writer:
                remote_writer.close()
                proxy_writers.discard(remote_writer)
            proxy_writers.discard(writer)
            proxy_tasks.discard(task)

    pg_server = await asyncio.start_server(pg_proxy, '127.0.0.1', 0)
    try:
        def pg_probe(port):
            import psycopg
            with psycopg.connect(host=db_host, hostaddr='127.0.0.1', port=port,
                    user=os.environ.get('SQL_USER', 'iotuser'), password=db_password,
                    dbname=os.environ.get('SQL_DATABASE', 'iot'), connect_timeout=10,
                    sslmode=os.environ.get('SQL_SSLMODE', 'require')) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('transaction_read_only'), 749, repeat('named-tunnel-', 1000)")
                    assert cursor.fetchone() == ('on', 749, 'named-tunnel-' * 1000)
        await through(pg_server.sockets[0].getsockname()[1], '-pg', lambda port: asyncio.to_thread(pg_probe, port))
        report['cases']['actual_readonly_postgresql_through_staging'] = True
    finally:
        pg_server.close(); await pg_server.wait_closed()
        for writer in list(proxy_writers):
            writer.close()
        for task in list(proxy_tasks):
            task.cancel()
        await asyncio.gather(*proxy_tasks, return_exceptions=True)


_PRESENCE_BROWSER_CASES = {
    'online_ready': {'live_legacy_browser_terminal_connected'},
    'offline_observed': {'live_legacy_http_offline_snapshot_received',
        'live_legacy_card_and_badge_offline_without_reload', 'live_legacy_metadata_preserved'},
    'reconnected': {'live_legacy_browser_reconnect_without_reload',
        'live_legacy_browser_terminal_echo_after_restart'},
}


async def _presence_browser_phase(process, command, expected, report, timeout):
    if command:
        process.stdin.write((json.dumps({'phase': command}) + '\n').encode())
        await process.stdin.drain()
    output = await asyncio.wait_for(process.stdout.readline(), timeout)
    if not output or len(output) > 32768:
        raise RuntimeError('Retained live presence browser provided no bounded evidence')
    evidence = json.loads(output)
    cases = evidence.get('cases', {})
    allowed = set().union(*_PRESENCE_BROWSER_CASES.values())
    if (evidence.get('status') != 'passed' or evidence.get('phase') != expected
            or not isinstance(cases, dict) or not set(cases) <= allowed
            or not _PRESENCE_BROWSER_CASES[expected] <= set(cases)
            or not all(value is True for value in cases.values())):
        raise RuntimeError('Retained live presence browser failed the expected phase')
    report['cases'].update(cases)


async def _start_presence_browser(fixture, report):
    named_script = os.environ.get('DATAPLICITY_STAGING_BROWSER_SCRIPT', '')
    script = Path(named_script).with_name('staging-legacy-presence-browser.mjs') if named_script else None
    if script is None or not script.is_file():
        raise RuntimeError('Retained live presence browser helper is required')
    payload = {'base_url': 'https://staging.dpenv.com', 'username': fixture['admin_email'],
        'password': fixture['admin_password'], 'device_hash': fixture['device_hash'],
        'output_dir': '/tmp/staging-legacy-presence-' + fixture['run_id']}
    process = await asyncio.create_subprocess_exec('node', str(script),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        process.stdin.write((json.dumps(payload) + '\n').encode())
        await process.stdin.drain()
        await _presence_browser_phase(process, None, 'online_ready', report, 100)
        return process
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def _live_browser(fixture, name, phase, report):
    script = os.environ.get('DATAPLICITY_STAGING_BROWSER_SCRIPT', '')
    if not script or not Path(script).is_file():
        raise RuntimeError('Actual live browser qualification helper is missing')
    process = await asyncio.create_subprocess_exec('node', script,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    payload = {'base_url': 'https://staging.dpenv.com', 'username': fixture['admin_email'],
               'password': fixture['admin_password'], 'org_hash': fixture['organisation_hash'],
               'name': name, 'phase': phase, 'output_dir': '/tmp/staging-tunnels-browser-' + fixture['run_id']}
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 180)
        if process.returncode != 0 or len(output) > 32768:
            raise RuntimeError('Actual staging browser qualification failed')
        evidence = json.loads(output)
        cases = evidence.get('cases', {})
        if evidence.get('status') != 'passed' or not cases or not all(value is True for value in cases.values()):
            raise RuntimeError('Actual staging browser did not provide successful evidence')
        report['cases'].update(cases)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
