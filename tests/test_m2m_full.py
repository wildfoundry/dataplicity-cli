from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from dataplicity_cli.m2m import BencodeError, M2MClient, bencode_decode, bencode_encode


class _AsyncIterWS:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        async def _gen():
            for message in self._messages:
                if isinstance(message, Exception):
                    raise message
                yield message

        return _gen()


class M2MClientFullTest(unittest.IsolatedAsyncioTestCase):
    async def test_bencode_decode_and_encode_error_paths(self) -> None:
        with self.assertRaises(BencodeError):
            bencode_encode(object())
        with self.assertRaises(BencodeError):
            bencode_decode(b"l3:ab")
        with self.assertRaises(BencodeError):
            bencode_decode(b"i")
        with self.assertRaises(BencodeError):
            bencode_decode(b"di1ei2ee")
        with self.assertRaises(BencodeError):
            bencode_decode(b"x")
        self.assertIsNone(bencode_decode(b""))

    async def test_connect_and_send_packet_and_close(self) -> None:
        ws = _AsyncIterWS([])
        with patch("dataplicity_cli.m2m.websockets.connect", new=AsyncMock(return_value=ws)) as connect:
            client = M2MClient("wss://example.test/m2m/", extra_headers={"User-Agent": "dataplicity-cli/0.1.7"})
            await client.connect()
            self.assertIs(client.ws, ws)
            connect.assert_awaited_once_with(
                "wss://example.test/m2m/",
                additional_headers={"User-Agent": "dataplicity-cli/0.1.7"},
            )
            await client.send_packet("ping", [b"nonce"])
            self.assertTrue(ws.sent)
            await client.close()
            self.assertTrue(ws.closed)
            self.assertTrue(client._closed_event.is_set())

    async def test_connect_defaults_to_cli_identity_headers(self) -> None:
        ws = _AsyncIterWS([])
        with patch("dataplicity_cli.m2m.websockets.connect", new=AsyncMock(return_value=ws)) as connect:
            client = M2MClient("wss://example.test/m2m/")
            await client.connect()
            headers = connect.await_args.kwargs["additional_headers"]
        self.assertEqual(headers["X-Client-App"], "dataplicity-cli")
        self.assertIn("User-Agent", headers)
        self.assertIn("X-Client-Platform", headers)
        self.assertIn("X-Client-Version", headers)

    async def test_close_swallows_request_leave_errors(self) -> None:
        ws = _AsyncIterWS([])
        client = M2MClient("wss://example.test/m2m/")
        client.ws = ws
        client.send_packet = AsyncMock(side_effect=RuntimeError("leave failed"))
        await client.close()
        self.assertTrue(ws.closed)
        self.assertTrue(client._closed_event.is_set())

    async def test_wait_for_identity_timeout_and_success(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        with self.assertRaises(asyncio.TimeoutError):
            await client.wait_for_identity(timeout=0.01)

        await client._handle_packet(9, [b"id-1"])
        ident = await client.wait_for_identity(timeout=0.1)
        self.assertEqual(ident, "id-1")

    async def test_wait_for_identity_raises_when_event_set_without_identity(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        client._identity_event.set()
        with self.assertRaises(RuntimeError):
            await client.wait_for_identity(timeout=0.1)

    async def test_send_route_and_close_channel_delegate(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        client.send_packet = AsyncMock()
        await client.send_route(10, b"abc")
        await client.close_channel(10)
        client.send_packet.assert_any_await("route", [10, b"abc"])
        client.send_packet.assert_any_await("request_close", [10])

    async def test_receiver_processes_packets(self) -> None:
        messages = [
            bencode_encode([9, b"identity-2"]),
            bencode_encode([14, 555]),
            bencode_encode([6, 555, b"hello"]),
            bencode_encode([19, 555]),
            bencode_encode([7, b"nonce"]),
            "invalid-ignored",
        ]
        ws = _AsyncIterWS(messages)
        client = M2MClient("wss://example.test/m2m/")
        client.ws = ws
        client.send_packet = AsyncMock()

        await client._receiver()

        self.assertEqual(client.identity, "identity-2")
        port = await client.wait_for_channel_open(555, timeout=0.1)
        self.assertEqual(port, 555)
        q = client.channel_queue(555)
        self.assertEqual(await asyncio.wait_for(q.get(), timeout=0.1), b"hello")
        self.assertIsNone(await asyncio.wait_for(q.get(), timeout=0.1))
        client.send_packet.assert_any_await("pong", [b"nonce"])

    async def test_receiver_ignores_empty_packets(self) -> None:
        ws = _AsyncIterWS([bencode_encode([])])
        client = M2MClient("wss://example.test/m2m/")
        client.ws = ws
        await client._receiver()
        self.assertFalse(client._closed_event.is_set())

    async def test_receiver_returns_immediately_without_socket(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client._receiver()
        self.assertFalse(client._closed_event.is_set())

    async def test_receiver_sets_closed_event_on_stream_error(self) -> None:
        ws = _AsyncIterWS([RuntimeError("stream broke")])
        client = M2MClient("wss://example.test/m2m/")
        client.ws = ws
        await client._receiver()
        self.assertTrue(client._closed_event.is_set())

    async def test_set_identity_from_non_bytes_value(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client._handle_packet(9, [1234])
        self.assertEqual(client.identity, "1234")

    async def test_close_without_socket_still_sets_closed_event(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client.close()
        self.assertTrue(client._closed_event.is_set())

    async def test_channel_open_uses_requested_port(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        client.send_packet = AsyncMock()
        await client._handle_packet(14, [10])
        self.assertEqual(await client.wait_for_channel_open(10), 10)
        for port in (10, 20, 30):
            await client._handle_packet(14, [port])
        self.assertEqual(await client.wait_for_channel_open(30), 30)
        client.send_packet.assert_awaited_once_with("request_close", [20])
        self.assertEqual(client._claimed_channels, {10, 30})

    async def test_channel_open_validates_requested_port(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        for value in (None, True, False, 0, -1, "7", 7.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await client.wait_for_channel_open(value, timeout=0.1)

    async def test_channel_open_requires_an_available_port(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client._handle_packet(14, [7])
        self.assertEqual(await client.wait_for_channel_open(7), 7)
        await client._handle_packet(14, [7])
        with self.assertRaises(RuntimeError):
            await client.wait_for_channel_open(7, timeout=0.1)

    async def test_channel_close_clears_state_after_send_error(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client._handle_packet(14, [7])
        self.assertEqual(await client.wait_for_channel_open(7), 7)
        client.send_packet = AsyncMock(side_effect=RuntimeError("send failed"))
        with self.assertRaises(RuntimeError):
            await client.close_channel(7)
        self.assertNotIn(7, client._claimed_channels)


if __name__ == "__main__":
    unittest.main()
