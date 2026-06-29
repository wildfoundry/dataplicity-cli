from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import patch

from dataplicity_cli.remote_access import _detect_protocol, run_remote_file, run_single_command


class _FakeM2M:
    def __init__(self) -> None:
        self.queues: Dict[int, asyncio.Queue[Optional[bytes]]] = {}
        self.sent_payloads: list[bytes] = []

    def channel_queue(self, port: int) -> asyncio.Queue[Optional[bytes]]:
        if port not in self.queues:
            self.queues[port] = asyncio.Queue()
        return self.queues[port]

    async def send_route(self, port: int, data: bytes) -> None:
        _ = port
        self.sent_payloads.append(data)

    async def close_channel(self, port: int) -> None:
        _ = port


class _StdoutWithBuffer:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


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


if __name__ == "__main__":
    unittest.main()
