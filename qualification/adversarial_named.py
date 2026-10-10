"""Independent, bounded staging peer that never acts on logical close notifications."""
import asyncio
from contextlib import suppress

import websockets
from websockets.exceptions import ConnectionClosed

from dataplicity_cli.client_identity import identity_headers
from dataplicity_cli.m2m import PACKETS, bencode_decode, bencode_encode
from dataplicity_cli.tunnels import websocket_url


NAMED_ERROR_CODES = frozenset(('tunnel_error', 'authentication_unavailable', 'publisher_authentication_denied', 'invalid_publisher_credential', 'human_authentication_required', 'invalid_name', 'invalid_ports', 'invalid_principal', 'invalid_transport_proof', 'invalid_identity', 'unknown_router_owner', 'wrong_router_owner', 'router_unavailable', 'router_rejected', 'session_denied', 'session_fenced', 'session_expired', 'publisher_cannot_consume', 'publisher_scope_denied', 'permission_denied', 'paid_plan_required', 'tunnels_unavailable', 'publishing_disabled', 'relay_disabled', 'name_disabled', 'credential_rotated', 'credential_expired', 'replacement_denied', 'tunnel_offline', 'publisher_revoked', 'credential_limit', 'permission_limit', 'publisher_limit', 'stream_limit', 'admission_limit', 'transport_lost'))


class RawNamedPeer:
    """Only server socket closure stops this peer; no CLI lifecycle/policy code."""
    def __init__(self, target_port=None):
        self.target_port = target_port
        self.ws = None
        self.identity = self.challenge = None
        self.bound = asyncio.Event()
        self.server_closed = asyncio.Event()
        self.closed_at = None
        self.opened = set()
        self.ignored_closes = 0
        self.received = {}
        self.tcp = {}
        self.tasks = []
        self.receiver = None
        self.error = None
        self.attempts = 0
        self.sent = 0

    async def connect(self, url):
        self.ws = await websockets.connect(websocket_url(url), additional_headers=identity_headers(),
            max_size=66560, max_queue=16, open_timeout=10, close_timeout=3)
        self.receiver = asyncio.create_task(self._receive())
        await asyncio.wait_for(self.bound.wait(), 10)
        self.check()
        if not self.identity or not self.challenge or self.server_closed.is_set():
            raise RuntimeError('Independent peer never established a live binding')
        return self.identity, self.challenge

    def mark_server_closed(self):
        if self.closed_at is None:
            self.closed_at = asyncio.get_running_loop().time()
        self.server_closed.set()

    def check(self):
        if self.error is not None:
            raise RuntimeError('Independent peer receiver failed') from self.error

    async def send(self, kind, *body):
        # No closed-channel, grant, permission, generation or local shutdown check.
        await self.ws.send(bencode_encode([PACKETS[kind], *body]))

    async def _handle(self, packet):
        kind, body = packet[0], packet[1:]
        if kind == PACKETS['ping']:
            await self.send('pong', body[0] if body else b'')
        elif kind == PACKETS['set_identity']:
            self.identity = body[0].decode('ascii')
            if self.challenge:
                self.bound.set()
        elif kind == PACKETS['instruction']:
            if len(body) != 2 or body[0] != b'router' or not isinstance(body[1], dict):
                raise RuntimeError('Invalid independent peer instruction')
            command = body[1]
            if command.get(b'action') == b'named-tunnel-binding':
                proof = command.get(b'challenge')
                if command.get(b'version') != 1 or not isinstance(proof, bytes) or len(proof) != 64:
                    raise RuntimeError('Invalid independent peer proof')
                self.challenge = proof.decode('ascii')
                if self.identity:
                    self.bound.set()
            elif command.get(b'action') == b'named-tunnel-open':
                if self.target_port is None or command.get(b'target_port') != self.target_port or len(self.tcp) >= 4:
                    raise RuntimeError('Invalid independent peer target')
                port = int(command[b'port'])
                reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.target_port), 5)
                self.tcp[port] = (reader, writer)
                self.tasks.append(asyncio.create_task(self._tcp_return(port, reader)))
        elif kind == PACKETS['notify_open']:
            if len(self.opened) >= 4:
                raise RuntimeError('Independent peer channel bound exceeded')
            self.opened.add(int(body[0]))
        elif kind in (PACKETS['notify_close'], PACKETS['route_control']):
            # Deliberately ignore closure/EOF without touching sockets or grants.
            self.ignored_closes += 1
        elif kind == PACKETS['route']:
            port, data = int(body[0]), body[1]
            if not isinstance(data, bytes) or len(data) > 65536:
                raise RuntimeError('Invalid independent peer frame')
            if port not in self.received and len(self.received) >= 4:
                raise RuntimeError('Independent peer evidence channel bound exceeded')
            buffer = self.received.setdefault(port, bytearray())
            if len(buffer) + len(data) > 65536:
                raise RuntimeError('Independent peer evidence buffer exceeded')
            buffer.extend(data)
            if self.target_port is not None:
                if port not in self.tcp:
                    raise RuntimeError('Independent peer route lacks local TCP stream')
                writer = self.tcp[port][1]
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 5)

    async def _receive(self):
        try:
            async for message in self.ws:
                packet = bencode_decode(message)
                if not isinstance(packet, list) or not packet:
                    raise RuntimeError('Invalid independent peer packet')
                await self._handle(packet)
        except ConnectionClosed:
            self.mark_server_closed()
        except Exception as exc:
            self.error = exc
        else:
            self.mark_server_closed()
        finally:
            self.bound.set()

    async def _tcp_return(self, port, reader):
        try:
            while data := await reader.read(65536):
                await self.send('route', port, data)
        except ConnectionClosed:
            pass  # Server closure only; TCP is retained until final cleanup.
        except Exception as exc:
            self.error = exc

    async def echo(self, port, payload):
        before = len(self.received.get(port, b''))
        await self.send('route', port, payload)
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            self.check()
            data = bytes(self.received.get(port, b''))[before:]
            if len(data) >= len(payload):
                if data != payload:
                    raise RuntimeError('Independent TCP echo differs')
                return
            await asyncio.sleep(0.02)
        raise RuntimeError('Independent TCP echo timed out')

    async def hostile_writes(self, port, marker, duration=3):
        deadline = asyncio.get_running_loop().time() + duration
        while asyncio.get_running_loop().time() < deadline:
            self.attempts += 1
            try:
                await self.send('route', port, marker)
                self.sent += 1
            except ConnectionClosed:
                self.mark_server_closed()
                return
            await asyncio.sleep(0.1)
        self.check()

    async def close(self):
        # Called only after assertions or when the whole acceptance fails.
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        if self.receiver:
            self.receiver.cancel()
            await asyncio.gather(self.receiver, return_exceptions=True)
        for _, writer in self.tcp.values():
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 3)


async def bind_peer(control, name, mode, target_port=None, diagnostics=None, role=None):
    def stage(operation):
        if diagnostics is not None:
            diagnostics["stage"] = (role or mode) + "_" + operation
    stage("bootstrap")
    bootstrap = await control.call('GET', 'bootstrap/', params={'name': name, 'mode': mode})
    peer = RawNamedPeer(target_port if mode == 'publisher' else None)
    try:
        stage('socket')
        identity, challenge = await peer.connect(bootstrap['m2m_url'])
        payload = {'name': name, 'identity': identity, 'challenge': challenge}
        if mode == 'publisher':
            payload['port'] = target_port
        stage('admit')
        session = await control.call('POST', 'publish/' if mode == 'publisher' else 'connect/', payload=payload)
        return peer, session
    except BaseException:
        await peer.close()
        raise


async def open_channel(control, peer, session, diagnostics=None, role=None):
    if diagnostics is not None:
        diagnostics["stage"] = role + "_channel"
    result = await control.call('POST', f"sessions/{session['session_id']}/channels/",
                                payload={'generation': session['generation']})
    port = result['port']
    deadline = asyncio.get_running_loop().time() + 10
    while port not in peer.opened:
        peer.check()
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError('Independent channel admission timed out')
        await asyncio.sleep(0.02)
    return port


async def heartbeat(control, session):
    from dataplicity_cli.tunnels import TunnelError
    while True:
        await asyncio.sleep(10)
        try:
            await control.call('POST', f"sessions/{session['session_id']}/heartbeat/",
                               payload={'generation': session['generation']})
        except TunnelError:
            # A modified peer deliberately disregards policy withdrawal.
            continue


async def prove_withdrawal(peer, port, marker, withdraw, observed, other_probe, publisher=None):
    """Attempt raw writes after withdrawal even when the server closes first."""
    started = asyncio.get_running_loop().time()
    await withdraw()
    baseline_attempts = peer.attempts
    if publisher is not None:
        # Publisher remains authorised after a consumer-only withdrawal. Its
        # ignored NotifyClose must not allow traffic back into the revoked route.
        await publisher.send('route', port, marker + b'-return')
    await asyncio.gather(peer.hostile_writes(port, marker), other_probe())
    if peer.attempts <= baseline_attempts:
        raise RuntimeError('Independent revoked peer never attempted a raw write')
    # The observer is the actual publisher-local TCP server, independent of the
    # malicious peer's policy and close handling.
    if marker in observed:
        raise RuntimeError('Revoked raw traffic reached the local TCP service')
    if marker + b'-return' in bytes(peer.received.get(port, b'')):
        raise RuntimeError('Revoked return traffic reached the independent peer')
    if not peer.server_closed.is_set():
        await asyncio.wait_for(peer.server_closed.wait(), 15)
    if peer.closed_at is None or peer.closed_at - started > 15:
        raise RuntimeError('Independent peer server closure exceeded the measured bound')
    # A leak delivered while waiting for remote closure must not escape the
    # earlier snapshot. Allow bounded local TCP/receiver work to drain, then
    # inspect both directions again before declaring the withdrawal proved.
    await asyncio.sleep(0.2)
    if marker in observed or marker + b'-return' in bytes(peer.received.get(port, b'')):
        raise RuntimeError('Revoked traffic was delivered before server closure')
    peer.check()


async def qualify_adversarial(admin, publisher_user, machine_factory, name, target_port,
                              observed, unaffected_probe, diagnostics=None):
    """Normal API grants; independent socket behavior and real TCP byte evidence."""
    from dataplicity_cli.tunnels import TunnelError
    diagnostics = diagnostics if diagnostics is not None else {}
    peers, loops = [], []
    credential = permission = None
    primary_error = None
    try:
        diagnostics['stage'] = 'permission_list'
        rows = (await admin.call('GET', 'permissions/'))['results']
        users = {row['user_id'] for row in rows if row['action'] == 'publish'
                 and row['name'] == '*' and type(row.get('user_id')) is int}
        if len(users) != 1:
            raise RuntimeError('Isolated publisher principal is ambiguous')
        diagnostics['stage'] = 'permission_grant'
        permission = await admin.call('POST', 'permissions/', payload={
            'name': name, 'action': 'consume', 'user_id': users.pop(), 'ports': [target_port]})
        diagnostics['stage'] = 'credential_create'
        credential = await admin.call('POST', 'credentials/', payload={
            'name': name, 'ports': [target_port], 'can_replace': False, 'label': 'Staging independent peer'})
        machine = machine_factory(credential['secret'])
        publisher, p_session = await bind_peer(machine, name, 'publisher', target_port, diagnostics, 'publisher')
        peers.append(publisher)
        loops.append(asyncio.create_task(heartbeat(machine, p_session)))
        hostile, h_session = await bind_peer(publisher_user, name, 'consumer', None, diagnostics, 'hostile')
        peers.append(hostile)
        loops.append(asyncio.create_task(heartbeat(publisher_user, h_session)))
        port = await open_channel(publisher_user, hostile, h_session, diagnostics, 'hostile')
        unaffected, u_session = await bind_peer(admin, name, 'consumer', None, diagnostics, 'unaffected')
        peers.append(unaffected)
        loops.append(asyncio.create_task(heartbeat(admin, u_session)))
        other_port = await open_channel(admin, unaffected, u_session, diagnostics, 'unaffected')
        attacker, a_session = await bind_peer(publisher_user, name, 'consumer', None, diagnostics, 'attacker')
        peers.append(attacker)
        loops.append(asyncio.create_task(heartbeat(publisher_user, a_session)))
        attacker_port = await open_channel(publisher_user, attacker, a_session, diagnostics, 'attacker')
        await attacker.echo(attacker_port, b'independent-attacker-authorised')
        diagnostics['stage'] = 'channel_retarget'
        forged = b'independent-forged-channel-' + name.encode()
        await attacker.send('route', other_port, forged)
        await asyncio.sleep(0.5)
        if forged in observed or any(forged in bytes(data) for peer in peers for data in peer.received.values()):
            raise RuntimeError('Independent peer retargeted another principal channel')
        await unaffected.echo(other_port, b'independent-after-forged-channel')
        await hostile.echo(port, b'independent-before-permission')
        await unaffected.echo(other_port, b'independent-authorised-other')
        diagnostics['stage'] = 'permission_withdrawal'
        marker = b'independent-revoked-consumer-' + name.encode()
        async def withdraw_permission():
            nonlocal permission
            await admin.call('DELETE', 'permissions/' + str(permission['id']) + '/')
            permission = None
        async def healthy():
            await asyncio.gather(unaffected.echo(other_port, b'independent-other-survives'), unaffected_probe())
        await prove_withdrawal(hostile, port, marker, withdraw_permission, observed, healthy, publisher)
        permission = None
        diagnostics['stage'] = 'consumer_replay'
        try:
            await publisher_user.call('GET', 'bootstrap/', params={'name': name, 'mode': 'consumer'})
        except TunnelError:
            pass
        else:
            raise RuntimeError('Withdrawn consumer was admitted again')
        # Credential withdrawal fences both ends. Both raw peers attempt writes,
        # even if server closure already prevents the send from succeeding.
        diagnostics['stage'] = 'credential_withdrawal'
        marker = b'independent-revoked-token-' + name.encode()
        async def withdraw_credential():
            nonlocal credential
            await admin.call('DELETE', 'credentials/' + str(credential['id']) + '/')
            credential = None
        credential_withdrawn = asyncio.get_running_loop().time()
        await withdraw_credential()
        await asyncio.gather(unaffected.hostile_writes(other_port, marker),
                             publisher.hostile_writes(other_port, marker + b'-return'), unaffected_probe())
        if unaffected.attempts < 1 or publisher.attempts < 1:
            raise RuntimeError('Credential withdrawal skipped a raw direction')
        if marker in observed or marker + b'-return' in bytes(unaffected.received.get(other_port, b'')):
            raise RuntimeError('Revoked credential still forwarded bytes')
        await asyncio.wait_for(publisher.server_closed.wait(), 15)
        # A consumer transport can remain live after only its publisher is
        # withdrawn. The old channel must remain fenced; ignored close packets
        # cannot permit either direction to resume.
        await asyncio.sleep(0.5)
        if marker in observed or marker + b'-return' in bytes(unaffected.received.get(other_port, b'')):
            raise RuntimeError('Revoked credential forwarded delayed bytes')
        if publisher.closed_at is None or publisher.closed_at - credential_withdrawn > 15:
            raise RuntimeError('Independent publisher closure exceeded the measured bound')
        unaffected.check(); publisher.check()
        diagnostics['stage'] = 'publisher_replay'
        try:
            await machine.call('GET', 'bootstrap/', params={'name': name, 'mode': 'publisher'})
        except TunnelError:
            pass
        else:
            raise RuntimeError('Revoked publisher token was replayed successfully')
        credential = None
    except BaseException as exc:
        # Never emit arbitrary server error codes or exception text.
        if isinstance(exc, TunnelError) and exc.code in NAMED_ERROR_CODES:
            diagnostics['error_code'] = exc.code
        primary_error = exc
        raise
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        cleanup = await asyncio.gather(*(peer.close() for peer in peers), return_exceptions=True)
        for resource, row in (('permissions', permission), ('credentials', credential)):
            if row is not None:
                try:
                    await admin.call('DELETE', resource + '/' + str(row['id']) + '/')
                except Exception as exc:
                    cleanup.append(exc)
        if primary_error is None and any(isinstance(item, BaseException) for item in cleanup):
            raise RuntimeError('Independent peer cleanup failed')
