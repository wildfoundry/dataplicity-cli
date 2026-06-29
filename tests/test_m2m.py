from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from dataplicity_cli.m2m import BencodeError, M2MClient, bencode_decode, bencode_encode


class BencodeTest(unittest.TestCase):
    def test_bencode_round_trip_for_nested_values(self) -> None:
        payload = [1, b"abc", "hello", [2, b"x"], {b"k": b"v"}]
        encoded = bencode_encode(payload)
        decoded = bencode_decode(encoded)
        self.assertEqual(decoded, [1, b"abc", b"hello", [2, b"x"], {b"k": b"v"}])

    def test_bencode_encode_requires_bytes_dict_keys(self) -> None:
        with self.assertRaises(BencodeError):
            bencode_encode({"not-bytes": "value"})

    def test_bencode_decode_requires_bytes(self) -> None:
        with self.assertRaises(BencodeError):
            bencode_decode("not-bytes")  # type: ignore[arg-type]


class M2MClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_packet_requires_connected_socket(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        with self.assertRaises(RuntimeError):
            await client.send_packet("ping", [b"abc"])

    async def test_handle_packet_ping_responds_with_pong(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        client.send_packet = AsyncMock()

        await client._handle_packet(7, [b"nonce"])
        client.send_packet.assert_awaited_once_with("pong", [b"nonce"])

    async def test_handle_packet_ping_without_payload_uses_empty_bytes(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        client.send_packet = AsyncMock()

        await client._handle_packet(7, [])
        client.send_packet.assert_awaited_once_with("pong", [b""])

    async def test_handle_packet_set_identity_decodes_utf8_bytes(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        self.assertFalse(client._identity_event.is_set())

        await client._handle_packet(9, [b"device-123"])
        self.assertEqual(client.identity, "device-123")
        self.assertTrue(client._identity_event.is_set())

    async def test_handle_packet_notify_open_enqueues_port(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        await client._handle_packet(14, [12345])

        port = await asyncio.wait_for(client.wait_for_channel_open(timeout=0.1), timeout=0.2)
        self.assertEqual(port, 12345)

    async def test_handle_packet_route_normalizes_to_bytes(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        port = 9000
        queue = client.channel_queue(port)

        await client._handle_packet(6, [port, "hello"])
        self.assertEqual(await asyncio.wait_for(queue.get(), timeout=0.1), b"hello")

        await client._handle_packet(6, [port, b"world"])
        self.assertEqual(await asyncio.wait_for(queue.get(), timeout=0.1), b"world")

    async def test_handle_packet_notify_close_pushes_none(self) -> None:
        client = M2MClient("wss://example.test/m2m/")
        port = 7777
        queue = client.channel_queue(port)

        await client._handle_packet(19, [port])
        self.assertIsNone(await asyncio.wait_for(queue.get(), timeout=0.1))


if __name__ == "__main__":
    unittest.main()
