from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from dataplicity_cli.m2m import bencode_encode
from dataplicity_cli.tunnel_transport import TunnelM2MClient


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = TunnelM2MClient("wss://relay.test/m2m/?features=named-tunnels-v1", {})
        client.send_packet = AsyncMock()
        return client

    async def test_binding_and_identity(self):
        client = self.client()
        await client._handle_packet(9, [b"identity"])
        await client._handle_packet(16, [b"router", {b"action": b"named-tunnel-binding", b"version": 1, b"challenge": b"a" * 64}])
        self.assertEqual(await client.wait_for_binding(), ("identity", "a" * 64))
        with self.assertRaises(ValueError):
            await client._handle_packet(16, [b"router", 42])
        with self.assertRaises(ValueError):
            await client._handle_packet(16, [b"router", {b"action": b"named-tunnel-binding", b"version": 2}])
        await client._handle_packet(16, [b"router", {b"action": b"unrelated"}])
        with self.assertRaises(ValueError):
            await client._handle_packet(16, [b"untrusted", {}])
        with self.assertRaises(ValueError):
            await client._handle_packet(16, [{}])

    async def test_data_eof_and_close(self):
        client = self.client()
        await client._handle_packet(14, [7])
        queue = client.channel_queue(7)
        self.assertIs(queue, client.channel_queue(7))
        await client._handle_packet(6, [7, b"hello"])
        await client._handle_packet(21, [7, b'{"eof":true}'])
        await client._handle_packet(19, [7])
        self.assertEqual(queue.get_nowait(), b"hello")
        self.assertIs(queue.get_nowait(), client.EOF)
        self.assertIsNone(queue.get_nowait())
        await client.send_channel_eof(7)
        client.send_packet.assert_awaited_once_with("request_send_control", [7, b'{"eof":true}'])
        await client._handle_packet(6, [999, b"unadmitted"])
        self.assertNotIn(999, client._channel_queues)
        await client.close_channel(7)
        self.assertNotIn(7, client._channel_queues)
        await client._handle_packet(16, [b"router", {b"action": b"named-tunnel-open", b"port": 11}])
        self.assertEqual(client.instructions.get_nowait()[b"port"], 11)

    async def test_malformed_and_bounded_queues(self):
        client = self.client()
        client.channel_queue(1)
        for data in ["bad", b"x" * 65537]:
            with self.assertRaises(ValueError):
                await client._handle_packet(6, [1, data])
        for data in ["bad", b"x" * 257, b'{"eof":false}']:
            with self.assertRaises(ValueError):
                await client._handle_packet(21, [1, data])
        for _ in range(client.QUEUE_FRAMES):
            await client._enqueue(1, b"data")
        await client._enqueue(1, b"overflow")
        self.assertIsNone(client.channel_queue(1).get_nowait())
        client.send_packet.assert_awaited_once_with("request_close", [1])
        for port in range(2, client.MAX_CHANNELS + 1):
            client.channel_queue(port)
        with self.assertRaises(RuntimeError):
            client.channel_queue(client.MAX_CHANNELS + 1)

    async def test_transport_loss_wakes_all_streams_and_connect_is_bounded(self):
        class Socket:
            close = AsyncMock()
            def __aiter__(self):
                async def receive():
                    yield bencode_encode([14, 1])
                    yield bencode_encode([6, 1, b"data"])
                return receive()

        client = self.client()
        socket = Socket()
        with patch("dataplicity_cli.tunnel_transport.websockets.connect", new=AsyncMock(return_value=socket)) as connect:
            await client.connect()
            await client._recv_task
            connect.assert_awaited_once_with(client.url, additional_headers={}, max_size=66560, max_queue=16)
        self.assertTrue(client._closed_event.is_set())
        self.assertIsNone(client.channel_queue(1).get_nowait())
        await client.close()
        socket.close.assert_awaited_once()
        self.assertIsNone(client.channel_queue(1).get_nowait())
        empty = self.client()
        await empty.close()

    async def test_shutdown_timeout_still_wakes_streams(self):
        client = self.client()
        queue = client.channel_queue(1)
        with patch("dataplicity_cli.m2m.M2MClient.close", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            await client.close()
        self.assertIsNone(queue.get_nowait())
        self.assertTrue(client._closed_event.is_set())


if __name__ == "__main__":
    unittest.main()
