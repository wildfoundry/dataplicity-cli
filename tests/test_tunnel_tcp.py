"""Real loopback TCP qualification of reused forwarding and half-close."""
from __future__ import annotations

import asyncio
import socket
import unittest
from unittest.mock import Mock

from dataplicity_cli.remote_access import bridge_tcp_channel, run_port_forward


class PairedM2M:
    EOF = object()
    MAX_CHANNELS = 32
    frame_bytes = 1024
    def __init__(self):
        self.queues = {}
        self.other = None

    def channel_queue(self, channel):
        return self.queues.setdefault(channel, asyncio.Queue(maxsize=16))

    async def send_route(self, channel, data):
        assert len(data) <= self.frame_bytes
        await self.other.channel_queue(channel).put(data)

    async def send_channel_eof(self, channel):
        await self.other.channel_queue(channel).put(self.EOF)

    async def close_channel(self, channel):
        await self.other.channel_queue(channel).put(None)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_binary_multistream_half_close(self):
        publisher = PairedM2M()
        consumer = PairedM2M()
        publisher.other, consumer.other = consumer, publisher
        streams = set()
        service_tasks = set()

        async def service(reader, writer):
            task = asyncio.current_task()
            service_tasks.add(task)
            try:
                request = await reader.read()
                if request.startswith(b"GET "):
                    response = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
                else:
                    response = request[::-1]
                writer.write(response)
                await writer.drain()
                writer.write_eof()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                service_tasks.discard(task)

        server = await asyncio.start_server(service, "127.0.0.1", 0)
        target_port = server.sockets[0].getsockname()[1]
        next_channel = 0

        async def admit():
            nonlocal next_channel
            next_channel += 1
            channel = next_channel
            async def publisher_stream():
                reader, writer = await asyncio.open_connection("127.0.0.1", target_port)
                try:
                    await bridge_tcp_channel(publisher, channel, reader, writer)
                finally:
                    writer.close()
                    await writer.wait_closed()
            task = asyncio.create_task(publisher_stream())
            streams.add(task)
            task.add_done_callback(streams.discard)
            return channel

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            local_port = probe.getsockname()[1]
        ready = asyncio.Event()
        def progress(event):
            if event.kind == "listener_started":
                ready.set()
        forward = asyncio.create_task(run_port_forward(consumer, None, local_port, channel_factory=admit, half_close=True, event_callback=progress))
        try:
            await asyncio.wait_for(ready.wait(), 3)
            async def request(payload):
                reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                try:
                    return await asyncio.wait_for(reader.read(), 3)
                finally:
                    writer.close()
                    await writer.wait_closed()
            payloads = [b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n", b"SSH-2.0-test\r\n", bytes(range(256)) * 512]
            responses = await asyncio.gather(*(request(value) for value in payloads))
            self.assertTrue(responses[0].endswith(b"hello"))
            self.assertEqual(responses[1:], [value[::-1] for value in payloads[1:]])
            self.assertEqual(next_channel, 3)
        finally:
            forward.cancel()
            await asyncio.gather(forward, return_exceptions=True)
            for task in list(streams) + list(service_tasks):
                task.cancel()
            await asyncio.gather(*streams, *service_tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()

    async def test_full_remote_close_cancels_blocked_local_reader(self):
        m2m = PairedM2M()
        m2m.other = m2m
        reader = asyncio.StreamReader()
        writer = Mock()
        await m2m.channel_queue(1).put(None)
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(bridge_tcp_channel(m2m, 1, reader, writer), 1)


if __name__ == "__main__":
    unittest.main()
