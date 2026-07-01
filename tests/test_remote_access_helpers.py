from __future__ import annotations

import asyncio
import io
import socket
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

from dataplicity_cli.remote_access import _detect_protocol, run_port_forward, run_remote_file, run_single_command


class _FakeM2M:
    def __init__(self) -> None:
        self.queues: Dict[int, asyncio.Queue[Optional[bytes]]] = {}
        self.sent_payloads: list[bytes] = []
        self.sent_routes: list[tuple[int, bytes]] = []
        self.closed_channels: list[int] = []

    def channel_queue(self, port: int) -> asyncio.Queue[Optional[bytes]]:
        if port not in self.queues:
            self.queues[port] = asyncio.Queue()
        return self.queues[port]

    async def send_route(self, port: int, data: bytes) -> None:
        self.sent_routes.append((port, data))
        self.sent_payloads.append(data)

    async def close_channel(self, port: int) -> None:
        self.closed_channels.append(port)


class _StdoutWithBuffer:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RemoteAccessHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_single_command_rejects_empty_command(self) -> None:
        fake = _FakeM2M()
        with self.assertRaises(RuntimeError):
            await run_single_command(fake, 99, "")

    async def test_run_remote_file_requires_output_or_stdout(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(1)
        await queue.put(None)
        with self.assertRaises(RuntimeError):
            await run_remote_file(fake, 1, output_path=None, allow_stdout=False)

    async def test_run_remote_file_writes_binary_output(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(2)
        await queue.put(b"hello")
        await queue.put(b" world")
        await queue.put(None)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "out.bin"
            count = await run_remote_file(fake, 2, output_path=str(output_path), allow_stdout=False)
            self.assertEqual(count, 11)
            self.assertEqual(output_path.read_bytes(), b"hello world")

    async def test_run_remote_file_writes_to_stdout_buffer(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(3)
        await queue.put(b"abc")
        await queue.put(None)
        fake_stdout = _StdoutWithBuffer()

        with patch("dataplicity_cli.remote_access.sys.stdout", fake_stdout):
            count = await run_remote_file(fake, 3, output_path=None, allow_stdout=True)

        self.assertEqual(count, 3)
        self.assertEqual(fake_stdout.buffer.getvalue(), b"abc")

    async def test_detect_protocol_classifies_known_signatures(self) -> None:
        self.assertEqual(_detect_protocol(b"GET / HTTP/1.1"), "HTTP request")
        self.assertEqual(_detect_protocol(b"HTTP/1.1 200 OK"), "HTTP response")
        self.assertEqual(_detect_protocol(b"SSH-2.0-OpenSSH_9.0"), "SSH")
        self.assertEqual(_detect_protocol(bytes([0x16, 0x03, 0x03, 0x00])), "TLS")
        self.assertIsNone(_detect_protocol(b"\x01\x02\x03"))

    async def test_run_port_forward_allocates_channel_per_local_client(self) -> None:
        fake = _FakeM2M()
        events = []
        next_channels = iter([102])

        async def channel_factory() -> int:
            return next(next_channels)

        local_port = _unused_local_port()
        forward_task = asyncio.create_task(
            run_port_forward(
                fake,
                101,
                local_port,
                channel_factory=channel_factory,
                event_callback=events.append,
            )
        )
        try:
            for _ in range(50):
                if any(event.kind == "listener_started" for event in events):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(any(event.kind == "listener_started" for event in events))

            reader_one, writer_one = await asyncio.open_connection("127.0.0.1", local_port)
            reader_two, writer_two = await asyncio.open_connection("127.0.0.1", local_port)
            try:
                writer_one.write(b"GET /one HTTP/1.1\r\n\r\n")
                writer_two.write(b"GET /two HTTP/1.1\r\n\r\n")
                await writer_one.drain()
                await writer_two.drain()

                for _ in range(50):
                    if len(fake.sent_routes) >= 2:
                        break
                    await asyncio.sleep(0.01)

                route_by_payload = {payload: port for port, payload in fake.sent_routes[:2]}
                self.assertEqual(
                    route_by_payload,
                    {
                        b"GET /one HTTP/1.1\r\n\r\n": 101,
                        b"GET /two HTTP/1.1\r\n\r\n": 102,
                    },
                )

                await fake.channel_queue(route_by_payload[b"GET /one HTTP/1.1\r\n\r\n"]).put(b"HTTP/1.1 200 OK\r\n\r\none")
                await fake.channel_queue(route_by_payload[b"GET /one HTTP/1.1\r\n\r\n"]).put(None)
                await fake.channel_queue(route_by_payload[b"GET /two HTTP/1.1\r\n\r\n"]).put(b"HTTP/1.1 200 OK\r\n\r\ntwo")
                await fake.channel_queue(route_by_payload[b"GET /two HTTP/1.1\r\n\r\n"]).put(None)

                self.assertEqual(await asyncio.wait_for(reader_one.read(1024), timeout=0.5), b"HTTP/1.1 200 OK\r\n\r\none")
                self.assertEqual(await asyncio.wait_for(reader_two.read(1024), timeout=0.5), b"HTTP/1.1 200 OK\r\n\r\ntwo")
                for _ in range(50):
                    if sorted(set(fake.closed_channels)) == [101, 102]:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(sorted(set(fake.closed_channels)), [101, 102])
                self.assertEqual(len([event for event in events if event.kind == "connection_rejected"]), 0)
            finally:
                writer_one.close()
                writer_two.close()
                await writer_one.wait_closed()
                await writer_two.wait_closed()
        finally:
            forward_task.cancel()
            with suppress(asyncio.CancelledError):
                await forward_task


if __name__ == "__main__":
    unittest.main()
