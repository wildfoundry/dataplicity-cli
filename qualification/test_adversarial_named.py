"""The hostile peer must not use cooperative close handling as evidence."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from dataplicity_cli.m2m import PACKETS, bencode_decode
from qualification.adversarial_named import RawNamedPeer, prove_withdrawal


def test_raw_peer_ignores_logical_close_and_still_transmits_exact_frames():
    async def scenario():
        peer = RawNamedPeer(target_port=3000)
        peer.ws = AsyncMock()
        writer = AsyncMock()
        peer.tcp[7] = (None, writer)
        await peer._handle([PACKETS['notify_close'], 7])
        await peer._handle([PACKETS['route_control'], 7, b'{"eof":true}'])
        await peer.send('route', 7, b'continued-after-notification')
        assert peer.ignored_closes == 2
        assert peer.server_closed.is_set() is False
        assert peer.error is None
        writer.close.assert_not_called()
        peer.ws.close.assert_not_called()
        assert bencode_decode(peer.ws.send.call_args.args[0]) == [
            PACKETS['route'], 7, b'continued-after-notification']
    asyncio.run(scenario())


def test_already_server_closed_socket_still_gets_a_real_write_attempt():
    async def scenario():
        peer = RawNamedPeer()
        peer.ws = AsyncMock()
        peer.ws.send.side_effect = ConnectionClosed(Close(1000, 'server'), None, None)
        peer.mark_server_closed()
        await peer.hostile_writes(7, b'hostile')
        assert peer.attempts == 1 and peer.sent == 0
        peer.ws.send.assert_awaited_once()
        peer.ws.close.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'outbound', 'return', 'skipped', 'late', 'unaffected'])
def test_withdrawal_requires_attempts_both_direction_fencing_and_unaffected_traffic(failure):
    async def scenario():
        peer, publisher = RawNamedPeer(), RawNamedPeer()
        observed = bytearray()
        publisher.ws = AsyncMock()
        marker = b'hostile-marker'
        healthy_calls = []
        async def withdraw():
            peer.mark_server_closed()
            if failure == 'late':
                peer.closed_at += 16
        async def hostile(port, payload):
            if failure != 'skipped':
                peer.attempts += 1
            if failure == 'outbound':
                observed.extend(payload)
            if failure == 'return':
                peer.received[port] = bytearray(payload + b'-return')
        async def healthy():
            healthy_calls.append(True)
            if failure == 'unaffected':
                raise RuntimeError('Healthy stream was interrupted')
        peer.hostile_writes = hostile
        if failure:
            with pytest.raises(RuntimeError):
                await prove_withdrawal(peer, 7, marker, withdraw, observed, healthy, publisher)
        else:
            await prove_withdrawal(peer, 7, marker, withdraw, observed, healthy, publisher)
            assert healthy_calls == [True]
            assert bencode_decode(publisher.ws.send.call_args.args[0]) == [
                PACKETS['route'], 7, marker + b'-return']
        publisher.ws.close.assert_not_called()
    asyncio.run(scenario())


def test_invalid_raw_instruction_never_opens_arbitrary_tcp_target(monkeypatch):
    async def scenario():
        peer = RawNamedPeer(target_port=3000)
        connect = AsyncMock()
        monkeypatch.setattr(asyncio, 'open_connection', connect)
        with pytest.raises(RuntimeError, match='target'):
            await peer._handle([PACKETS['instruction'], b'router', {
                b'action': b'named-tunnel-open', b'port': 7, b'target_port': 22}])
        connect.assert_not_called()
    asyncio.run(scenario())


def test_receiver_only_records_remote_socket_close_without_closing_tcp_or_websocket():
    async def scenario():
        peer = RawNamedPeer(target_port=3000)
        class Socket:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise ConnectionClosed(Close(1000, 'server'), None, None)
            close = AsyncMock()
        peer.ws = Socket()
        writer = AsyncMock()
        peer.tcp[7] = (None, writer)
        await peer._receive()
        assert peer.server_closed.is_set() is True and peer.closed_at is not None
        writer.close.assert_not_called()
        peer.ws.close.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'retarget', 'token_forward', 'token_replay', 'cleanup'])
def test_orchestration_revokes_exact_grant_and_token_with_cleanup_and_no_pass_on_leaks(monkeypatch, failure):
    from qualification import adversarial_named as gate
    from dataplicity_cli.tunnels import TunnelError
    async def scenario():
        peers, cleanups, deletes, healthy_calls = [], [], [], []
        observed = bytearray()
        state = {'revoked': False}
        class Peer(RawNamedPeer):
            async def echo(self, port, payload):
                self.check()
            async def send(self, kind, port, data):
                if failure == 'retarget' and data.startswith(b'independent-forged'):
                    observed.extend(data)
            async def hostile_writes(self, port, marker, duration=3):
                self.attempts += 1
                if failure == 'token_forward' and marker.startswith(b'independent-revoked-token'):
                    observed.extend(marker)
            async def close(self):
                cleanups.append(self)
                if failure == 'cleanup':
                    raise RuntimeError('Private error not part of evidence')
        class Control:
            async def call(self, method, resource, **kwargs):
                if method == 'GET' and resource == 'permissions/':
                    return {'results': [{'user_id': 10, 'action': 'publish', 'name': '*'}]}
                if method == 'POST' and resource == 'permissions/':
                    assert kwargs['payload'] == {'name': 'fixture-hostile', 'action': 'consume',
                                                  'user_id': 10, 'ports': [3000]}
                    return {'id': 2}
                if method == 'POST' and resource == 'credentials/':
                    return {'id': 'owned', 'secret': 'private'}
                if method == 'DELETE':
                    deletes.append(resource)
                    if resource == 'permissions/2/':
                        peers[1].mark_server_closed()
                        peers[3].mark_server_closed()
                    if resource == 'credentials/owned/':
                        state['revoked'] = True
                        peers[0].mark_server_closed()
                    return None
                raise AssertionError('Unexpected normal API request')
        class Denied:
            async def call(self, method, resource, **kwargs):
                if failure == 'token_replay' and state['revoked']:
                    return {}
                raise TunnelError('denied')
        async def bind(control, name, mode, target_port=None, diagnostics=None, role=None):
            peer = Peer(target_port)
            peers.append(peer)
            return peer, {'session_id': str(len(peers)), 'generation': 1}
        async def channel(control, peer, session, diagnostics=None, role=None):
            return int(session['session_id'])
        async def healthy():
            healthy_calls.append(True)
        monkeypatch.setattr(gate, 'bind_peer', bind)
        monkeypatch.setattr(gate, 'open_channel', channel)
        monkeypatch.setattr(gate, 'heartbeat', AsyncMock())
        monkeypatch.setattr(gate.asyncio, 'sleep', AsyncMock())
        if failure:
            with pytest.raises(RuntimeError):
                await gate.qualify_adversarial(Control(), Denied(), lambda secret: Denied(),
                    'fixture-hostile', 3000, observed, healthy)
        else:
            await gate.qualify_adversarial(Control(), Denied(), lambda secret: Denied(),
                'fixture-hostile', 3000, observed, healthy)
            assert peers[1].attempts == peers[0].attempts == peers[2].attempts == 1
            assert healthy_calls == [True, True]
            # Consumer remains open after its publisher is revoked; raw writes
            # were required and the fenced channel, rather than local close,
            # supplies the security evidence for this direction.
            assert peers[2].server_closed.is_set() is False
        assert len(cleanups) == 4
        assert deletes.count('permissions/2/') == 1
        assert deletes.count('credentials/owned/') == 1
    asyncio.run(scenario())


def test_independent_publisher_bridges_actual_loopback_tcp_and_keeps_it_after_notify_close():
    async def scenario():
        actual_bytes = bytearray()
        async def echo(reader, writer):
            try:
                while data := await reader.read(1024):
                    actual_bytes.extend(data)
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(echo, '127.0.0.1', 0)
        target_port = server.sockets[0].getsockname()[1]
        peer = RawNamedPeer(target_port)
        frames = asyncio.Queue()
        peer.ws = AsyncMock()
        async def capture(frame):
            frames.put_nowait(bencode_decode(frame))
        peer.ws.send.side_effect = capture
        try:
            await peer._handle([PACKETS['instruction'], b'router', {
                b'action': b'named-tunnel-open', b'port': 7, b'target_port': target_port}])
            await peer._handle([PACKETS['route'], 7, b'actual-tcp-request'])
            assert await asyncio.wait_for(frames.get(), 2) == [
                PACKETS['route'], 7, b'actual-tcp-request']
            assert actual_bytes == b'actual-tcp-request'
            await peer._handle([PACKETS['notify_close'], 7])
            assert not peer.tcp[7][1].is_closing()
            await peer._handle([PACKETS['route'], 7, b'continued-despite-notify-close'])
            assert await asyncio.wait_for(frames.get(), 2) == [
                PACKETS['route'], 7, b'continued-despite-notify-close']
            assert actual_bytes.endswith(b'continued-despite-notify-close')
            peer.ws.close.assert_not_called()
        finally:
            await peer.close()
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize('direction', ['outbound', 'return'])
def test_withdrawal_rechecks_leaks_delivered_during_server_close_wait(direction):
    async def scenario():
        peer, publisher = RawNamedPeer(), RawNamedPeer()
        publisher.ws = AsyncMock()
        observed = bytearray()
        marker = b'delayed-hostile-marker'
        class DelayedClosure:
            def is_set(self):
                return False
            async def wait(self):
                if direction == 'outbound':
                    observed.extend(marker)
                else:
                    peer.received[7] = bytearray(marker + b'-return')
                peer.closed_at = asyncio.get_running_loop().time()
                return True
        peer.server_closed = DelayedClosure()
        async def withdraw():
            pass
        async def writes(port, payload):
            peer.attempts += 1
        async def healthy():
            pass
        peer.hostile_writes = writes
        with pytest.raises(RuntimeError, match='delivered before server closure'):
            await prove_withdrawal(peer, 7, marker, withdraw, observed, healthy, publisher)
        publisher.ws.close.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('code,expected', [('permission_denied', 'permission_denied'),
                                         ('session_fenced', 'session_fenced'),
                                         ('private-bearer-token', None)])
def test_independent_diagnostics_identify_failed_operation_without_reflecting_errors(code, expected):
    from qualification.adversarial_named import qualify_adversarial
    from dataplicity_cli.tunnels import TunnelError
    async def scenario():
        control = AsyncMock()
        control.call.side_effect = TunnelError('private-secret-message', code)
        diagnostics = {}
        with pytest.raises(TunnelError):
            await qualify_adversarial(control, None, None, 'fixture', 3000, bytearray(),
                                      AsyncMock(), diagnostics)
        assert diagnostics == ({'stage': 'permission_list', 'error_code': expected}
                               if expected else {'stage': 'permission_list'})
        assert 'private-secret-message' not in str(diagnostics)
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['bootstrap', 'socket', 'admit'])
def test_independent_bind_marks_the_exact_failing_stage_and_cleans_up(monkeypatch, operation):
    from qualification import adversarial_named as gate
    async def scenario():
        control = AsyncMock()
        peer = AsyncMock()
        peer.connect.return_value = ('identity', 'challenge')
        control.call.side_effect = ([RuntimeError('private')] if operation == 'bootstrap' else
            [{'m2m_url': 'wss://example.invalid/'}, RuntimeError('private')] if operation == 'admit' else
            [{'m2m_url': 'wss://example.invalid/'}])
        if operation == 'socket':
            peer.connect.side_effect = RuntimeError('private')
        monkeypatch.setattr(gate, 'RawNamedPeer', lambda *args: peer)
        diagnostics = {}
        with pytest.raises(RuntimeError):
            await gate.bind_peer(control, 'fixture', 'consumer', None, diagnostics, 'hostile')
        assert diagnostics == {'stage': 'hostile_' + operation}
        if operation != 'bootstrap':
            peer.close.assert_awaited_once()
    asyncio.run(scenario())
