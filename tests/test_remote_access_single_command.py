from __future__ import annotations

import asyncio
import unittest
from typing import Dict, Optional
from unittest.mock import patch

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
        await queue.put(
            b"\r\n__DP_CLI_BEGIN_abc123ef__Linux test-host 6.6.0\r\n"
            b"\r\n__DP_CLI_DONE_abc123ef__0\r\n$"
        )

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
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
        await queue.put(b"\r\n__DP_CLI_BEGIN_abc123ef__Linux test-host 6.6.0\n$")

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
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

    async def test_ignores_echoed_setup_before_begin_marker(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(11)
        await queue.put(
            b"root@test:/# stty -echo 2>/dev/null || true\r\n"
            b"root@test:/# printf '__DP_''CLI_BEGIN_abc123ef__'\r\n"
            b"\r\n__DP_CLI_BEGIN_abc123ef__Linux test-host 6.6.0\r\n"
            b"\r\n__DP_CLI_DONE_abc123ef__0\r\n"
        )

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
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

    async def test_payload_hides_markers_from_terminal_echo(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(13)
        await queue.put(
            b"\r\n__DP_CLI_BEGIN_abc123ef__Linux test-host 6.6.0\r\n"
            b"\r\n__DP_CLI_DONE_abc123ef__0\r\n"
        )

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
            await run_single_command(
                fake,
                13,
                "uname -a",
                timeout_seconds=1.0,
                first_response_timeout_seconds=0.1,
                idle_timeout_seconds=0.1,
            )

        payload = fake.sent_payloads[0]
        self.assertIn(b"stty -echo 2>/dev/null || true", payload)
        self.assertIn(b"PS1=''", payload)
        self.assertIn(b"PROMPT=''", payload)
        self.assertIn(b"PROMPT_COMMAND=''", payload)
        self.assertIn(b"stty echo 2>/dev/null || true", payload)
        self.assertIn(b"__DP_''CLI_BEGIN_abc123ef__", payload)
        self.assertIn(b"__DP_''CLI_DONE_abc123ef__", payload)
        self.assertNotIn(b"__DP_CLI_BEGIN_abc123ef__", payload)
        self.assertNotIn(b"__DP_CLI_DONE_abc123ef__", payload)

    async def test_terminal_echo_is_restored_after_done_marker(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(15)
        await queue.put(
            b"\r\n__DP_CLI_BEGIN_abc123ef__Linux test-host 6.6.0\r\n"
            b"\r\n__DP_CLI_DONE_abc123ef__0\r\n"
            b"stty echo 2>/dev/null || true\r\n"
        )

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
            output = await run_single_command(
                fake,
                15,
                "uname -a",
                timeout_seconds=1.0,
                first_response_timeout_seconds=0.1,
                idle_timeout_seconds=0.1,
            )

        payload = fake.sent_payloads[0]
        self.assertEqual(output, b"Linux test-host 6.6.0")
        self.assertLess(
            payload.index(b"__DP_''CLI_DONE_abc123ef__"),
            payload.index(b"stty echo 2>/dev/null || true"),
        )

    async def test_preserves_intentional_leading_newline_from_command(self) -> None:
        fake = _FakeM2M()
        queue = fake.channel_queue(17)
        await queue.put(
            b"\r\n__DP_CLI_BEGIN_abc123ef__\r\n"
            b"starts after blank\r\n"
            b"\r\n__DP_CLI_DONE_abc123ef__0\r\n"
        )

        with patch("dataplicity_cli.remote_access.secrets.token_hex", return_value="abc123ef"):
            output = await run_single_command(
                fake,
                17,
                "printf '\\nstarts after blank\\n'",
                timeout_seconds=1.0,
                first_response_timeout_seconds=0.1,
                idle_timeout_seconds=0.1,
            )

        self.assertEqual(output, b"\r\nstarts after blank")


if __name__ == "__main__":
    unittest.main()
