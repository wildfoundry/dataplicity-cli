from __future__ import annotations

import asyncio
import unittest
from typing import Dict, Optional

from dataplicity_cli.remote_access import run_single_command


class _FakeM2M:
    def __init__(self) -> None:
        self.queues: Dict[int, asyncio.Queue[Optional[bytes]]] = {}
        self.sent_payloads: list[bytes] = []
        self.closed_channels: list[int] = []

    def channel_queue(self, port: int) -> asyncio.Queue[Optional[bytes]]:
        if port not in self.queues:
            self.queues[port] = asyncio.Queue()
        return self.queues[port]

    async def send_route(self, port: int, data: bytes) -> None:
        _ = port
        self.sent_payloads.append(data)

    async def close_channel(self, port: int) -> None:
        self.closed_channels.append(port)


class RunSingleCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_output_when_marker_observed(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(7)
        await queue.put(b"Linux test-host 6.6.0\n__DP_CLI_DONE__0\n$")

        output = await run_single_command(
            fake,
            7,
            "uname -a",
            timeout_seconds=1.0,
            first_response_timeout_seconds=0.1,
            idle_timeout_seconds=0.1,
        )

        self.assertEqual(output, b"Linux test-host 6.6.0")
        self.assertEqual(fake.closed_channels, [])

    async def test_idle_fallback_returns_collected_output_without_marker(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(9)
        await queue.put(b"Linux test-host 6.6.0\n$")

        output = await run_single_command(
            fake,
            9,
            "uname -a",
            timeout_seconds=1.0,
            first_response_timeout_seconds=0.1,
            idle_timeout_seconds=0.02,
        )

        self.assertEqual(output, b"Linux test-host 6.6.0\n$")
        self.assertEqual(fake.closed_channels, [9])

    async def test_prompt_marker_can_end_capture_without_done_marker(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(11)
        await queue.put(b"Linux test-host 6.6.0\n__DP_CLI_PROMPT__ ")

        output = await run_single_command(
            fake,
            11,
            "uname -a",
            timeout_seconds=1.0,
            first_response_timeout_seconds=0.1,
            idle_timeout_seconds=0.1,
        )

        self.assertEqual(output, b"Linux test-host 6.6.0")
        self.assertEqual(fake.closed_channels, [])


if __name__ == "__main__":
    unittest.main()
