from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import websockets


class BencodeError(ValueError):
    pass


def bencode_encode(obj: Any) -> bytes:
    parts: List[bytes] = []

    def add(value: Any) -> None:
        if isinstance(value, bytes):
            parts.append(str(len(value)).encode("ascii") + b":" + value)
        elif isinstance(value, str):
            raw = value.encode("utf-8")
            parts.append(str(len(raw)).encode("ascii") + b":" + raw)
        elif isinstance(value, int):
            parts.append(f"i{value}e".encode("ascii"))
        elif isinstance(value, (list, tuple)):
            parts.append(b"l")
            for item in value:
                add(item)
            parts.append(b"e")
        elif isinstance(value, dict):
            parts.append(b"d")
            for key in sorted(value.keys()):
                if not isinstance(key, bytes):
                    raise BencodeError("dict keys must be bytes")
                add(key)
                add(value[key])
            parts.append(b"e")
        else:
            raise BencodeError(f"unsupported type: {type(value)!r}")

    add(obj)
    return b"".join(parts)


def bencode_decode(data: bytes) -> Any:
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("bencode decode expects bytes")
    buffer = memoryview(data)
    idx = 0

    def read(count: int) -> bytes:
        nonlocal idx
        if idx + count > len(buffer):
            raise BencodeError("unexpected end of data")
        chunk = buffer[idx : idx + count]
        idx += count
        return bytes(chunk)

    def read_one() -> int:
        nonlocal idx
        if idx >= len(buffer):
            raise BencodeError("unexpected end of data")
        value = buffer[idx]
        idx += 1
        return int(value)

    def decode() -> Any:
        if idx >= len(buffer):
            return None
        marker = read_one()
        if marker == ord("e"):
            return None
        if marker == ord("i"):
            digits = bytearray()
            while True:
                c = read_one()
                if c == ord("e"):
                    break
                digits.append(c)
            return int(digits.decode("ascii"))
        if marker == ord("l"):
            items = []
            while True:
                item = decode()
                if item is None:
                    break
                items.append(item)
            return items
        if marker == ord("d"):
            items: Dict[bytes, Any] = {}
            while True:
                key = decode()
                if key is None:
                    break
                if not isinstance(key, (bytes, bytearray)):
                    raise BencodeError("dict key must be bytes")
                items[bytes(key)] = decode()
            return items
        if ord("0") <= marker <= ord("9"):
            digits = bytearray([marker])
            while True:
                c = read_one()
                if c == ord(":"):
                    break
                digits.append(c)
            size = int(digits.decode("ascii"))
            return read(size)
        raise BencodeError("invalid bencode payload")

    return decode()


PACKETS: Dict[str, int] = {
    "null": 0,
    "request_join": 1,
    "request_identify": 2,
    "welcome": 3,
    "log": 4,
    "request_send": 5,
    "route": 6,
    "ping": 7,
    "pong": 8,
    "set_identity": 9,
    "request_open": 10,
    "request_close": 11,
    "request_close_all": 12,
    "keep_alive": 13,
    "notify_open": 14,
    "request_login": 15,
    "instruction": 16,
    "notify_login_success": 17,
    "notify_login_fail": 18,
    "notify_close": 19,
    "request_leave": 20,
    "route_control": 21,
    "request_send_control": 22,
}


class M2MClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.identity: Optional[str] = None

        self._recv_task: Optional[asyncio.Task] = None
        self._identity_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._channel_open_queue: asyncio.Queue[int] = asyncio.Queue()
        self._channel_queues: Dict[int, asyncio.Queue[Optional[bytes]]] = {}

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url)
        self._recv_task = asyncio.create_task(self._receiver())

    async def close(self) -> None:
        if self.ws:
            try:
                await self.send_packet("request_leave")
            except Exception:
                pass
            await self.ws.close()
        if self._recv_task:
            self._recv_task.cancel()
        self._closed_event.set()

    async def wait_for_identity(self, timeout: float = 10.0) -> str:
        if not self.identity:
            await asyncio.wait_for(self._identity_event.wait(), timeout=timeout)
        if not self.identity:
            raise RuntimeError("identity not available")
        return self.identity

    async def wait_for_channel_open(self, timeout: float = 20.0) -> int:
        return await asyncio.wait_for(self._channel_open_queue.get(), timeout=timeout)

    def channel_queue(self, port: int) -> asyncio.Queue[Optional[bytes]]:
        if port not in self._channel_queues:
            self._channel_queues[port] = asyncio.Queue()
        return self._channel_queues[port]

    async def send_packet(self, packet_type: str, packet_body: Optional[List[Any]] = None) -> None:
        if not self.ws:
            raise RuntimeError("M2M socket not connected")
        packet_number = PACKETS[packet_type]
        payload = [packet_number]
        if packet_body:
            payload.extend(packet_body)
        encoded = bencode_encode(payload)
        await self.ws.send(encoded)

    async def send_route(self, port: int, data: bytes) -> None:
        await self.send_packet("route", [port, data])

    async def close_channel(self, port: int) -> None:
        await self.send_packet("request_close", [port])

    async def _receiver(self) -> None:
        if not self.ws:
            return
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                packet = bencode_decode(message)
                if not isinstance(packet, list) or not packet:
                    continue
                packet_type = packet[0]
                packet_body = packet[1:]
                await self._handle_packet(packet_type, packet_body)
        except Exception:
            self._closed_event.set()

    async def _handle_packet(self, packet_type: int, packet_body: List[Any]) -> None:
        if packet_type == PACKETS["ping"]:
            if packet_body:
                await self.send_packet("pong", [packet_body[0]])
            else:
                await self.send_packet("pong", [b""])
            return

        if packet_type == PACKETS["set_identity"]:
            if packet_body:
                identity_raw = packet_body[0]
                if isinstance(identity_raw, bytes):
                    self.identity = identity_raw.decode("utf-8", "ignore")
                else:
                    self.identity = str(identity_raw)
                self._identity_event.set()
            return

        if packet_type == PACKETS["notify_open"]:
            if packet_body:
                port = int(packet_body[0])
                await self._channel_open_queue.put(port)
            return

        if packet_type == PACKETS["notify_close"]:
            if packet_body:
                port = int(packet_body[0])
                queue = self.channel_queue(port)
                await queue.put(None)
            return

        if packet_type == PACKETS["route"]:
            if len(packet_body) >= 2:
                port = int(packet_body[0])
                payload = packet_body[1]
                if isinstance(payload, str):
                    payload_bytes = payload.encode("utf-8")
                else:
                    payload_bytes = bytes(payload)
                queue = self.channel_queue(port)
                await queue.put(payload_bytes)
