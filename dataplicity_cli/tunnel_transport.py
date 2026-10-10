"""Capability-gated named tunnel support on the existing M2M transport."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import websockets

from .m2m import M2MClient, PACKETS


class TunnelM2MClient(M2MClient):
    EOF = object()
    MAX_CHANNELS = 32
    MAX_FRAME = 65536
    frame_bytes = MAX_FRAME
    QUEUE_FRAMES = 16

    def __init__(self, url: str, extra_headers: Dict[str, str]) -> None:
        super().__init__(url, extra_headers=extra_headers)
        self.challenge: str = ""
        self._binding_event = asyncio.Event()
        self.instructions: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_CHANNELS)
        self._channel_open_queue = asyncio.Queue(maxsize=self.MAX_CHANNELS)

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.url, additional_headers=self.extra_headers,
            max_size=self.MAX_FRAME + 1024, max_queue=16,
        )
        self._recv_task = asyncio.create_task(self._receiver())

    async def wait_for_binding(self) -> tuple[str, str]:
        identity = await self.wait_for_identity()
        await asyncio.wait_for(self._binding_event.wait(), timeout=10)
        return identity, self.challenge

    async def _receiver(self) -> None:
        try:
            await super()._receiver()
        finally:
            self._wake_channels()

    def _wake_channels(self) -> None:
        self._closed_event.set()
        for queue in self._channel_queues.values():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(super().close(), timeout=10)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake_channels()
            if self._recv_task:
                self._recv_task.cancel()
                await asyncio.gather(self._recv_task, return_exceptions=True)

    def channel_queue(self, port: int) -> asyncio.Queue:
        if port not in self._channel_queues:
            if len(self._channel_queues) >= self.MAX_CHANNELS:
                raise RuntimeError("Tunnel stream limit reached")
            self._channel_queues[port] = asyncio.Queue(maxsize=self.QUEUE_FRAMES)
        return self._channel_queues[port]

    async def close_channel(self, port: int) -> None:
        try:
            await asyncio.wait_for(super().close_channel(port), timeout=5)
        finally:
            self._channel_queues.pop(port, None)

    async def send_channel_eof(self, port: int) -> None:
        await self.send_packet("request_send_control", [port, b'{"eof":true}'])

    async def _enqueue(self, port: int, payload: Any) -> None:
        queue = self._channel_queues.get(port)
        if queue is None:
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # A slow stream must not stall all streams on this transport.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
            await super().close_channel(port)

    async def _handle_packet(self, packet_type: int, packet_body: List[Any]) -> None:
        if packet_type == PACKETS["instruction"] and packet_body:
            # Existing Instruction is (sender, data), including router messages.
            if len(packet_body) != 2 or packet_body[0] != b"router":
                raise ValueError("Invalid tunnel instruction sender")
            instruction = packet_body[1]
            if not isinstance(instruction, dict):
                raise ValueError("Invalid tunnel instruction")
            action = instruction.get(b"action")
            if action == b"named-tunnel-binding":
                challenge = instruction.get(b"challenge", b"")
                if instruction.get(b"version") != 1 or not isinstance(challenge, bytes) or len(challenge) != 64:
                    raise ValueError("Router does not support named tunnels v1")
                self.challenge = challenge.decode("ascii")
                self._binding_event.set()
            elif action == b"named-tunnel-open":
                self.instructions.put_nowait(instruction)
            return
        if packet_type == PACKETS["notify_open"] and packet_body:
            self.channel_queue(int(packet_body[0]))
            return
        if packet_type == PACKETS["route"] and len(packet_body) >= 2:
            port, data = int(packet_body[0]), packet_body[1]
            if not isinstance(data, bytes) or len(data) > self.MAX_FRAME:
                raise ValueError("Invalid tunnel frame")
            await self._enqueue(port, data)
            return
        if packet_type == PACKETS["route_control"] and len(packet_body) >= 2:
            data = packet_body[1]
            if not isinstance(data, bytes) or len(data) > 256 or json.loads(data) != {"eof": True}:
                raise ValueError("Invalid tunnel control")
            await self._enqueue(int(packet_body[0]), self.EOF)
            return
        if packet_type == PACKETS["notify_close"] and packet_body:
            await self._enqueue(int(packet_body[0]), None)
            return
        await super()._handle_packet(packet_type, packet_body)
