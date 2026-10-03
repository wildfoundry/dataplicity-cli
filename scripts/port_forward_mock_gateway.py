#!/usr/bin/env python3
"""Local HTTP + M2M mock used to smoke-test packaged CLI port forwarding."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataplicity_cli.m2m import PACKETS, bencode_decode, bencode_encode  # noqa: E402

try:
    import websockets
except ImportError as exc:  # pragma: no cover - exercised in packaging smoke
    raise SystemExit("websockets is required to run the port-forward mock gateway") from exc


DEVICE_BODY = b"dataplicity-cli linux port-forward ok\n"
DEVICE_HASH = "testdevhash"
IDENTITY = "cli-linux-smoke-identity"


class PortForwardMockGateway:
    def __init__(self, host: str, api_port: int, m2m_port: int, device_port: int) -> None:
        self.host = host
        self.api_port = api_port
        self.m2m_port = m2m_port
        self.device_port = device_port
        self.websocket: Any = None
        self.next_channel = 101
        self.channels: Dict[int, Tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    def public_host(self, request_host: Optional[str] = None) -> str:
        host = (request_host or "127.0.0.1").split("@")[-1]
        if host.startswith("["):
            return host.split("]")[0].lstrip("[")
        if host.count(":") == 1:
            return host.split(":", 1)[0]
        return host

    async def handle_device(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        payload = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            + f"Content-Length: {len(DEVICE_BODY)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + DEVICE_BODY
        )
        writer.write(payload)
        await writer.drain()
        writer.close()

    async def _send_packet(self, packet_type: str, body: Optional[list] = None) -> None:
        if self.websocket is None:
            raise RuntimeError("M2M client is not connected")
        payload = [PACKETS[packet_type]]
        if body:
            payload.extend(body)
        await self.websocket.send(bencode_encode(payload))

    async def _pump_device_to_cli(self, channel: int, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await self._send_packet("route", [channel, data])
        except (ConnectionError, asyncio.IncompleteReadError, websockets.ConnectionClosed):
            return
        finally:
            try:
                await self._send_packet("notify_close", [channel])
            except Exception:
                pass

    async def handle_m2m(self, websocket: Any) -> None:
        self.websocket = websocket
        await self._send_packet("set_identity", [IDENTITY.encode("utf-8")])
        try:
            async for message in websocket:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                packet = bencode_decode(message)
                if not isinstance(packet, list) or not packet:
                    continue
                packet_type = int(packet[0])
                body = packet[1:]
                if packet_type == PACKETS["ping"]:
                    nonce = body[0] if body else b""
                    await self._send_packet("pong", [nonce])
                elif packet_type == PACKETS["route"] and len(body) >= 2:
                    channel = int(body[0])
                    data = body[1]
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    streams = self.channels.get(channel)
                    if streams is None:
                        continue
                    _reader, writer = streams
                    writer.write(bytes(data))
                    await writer.drain()
                elif packet_type == PACKETS["request_close"] and body:
                    channel = int(body[0])
                    streams = self.channels.pop(channel, None)
                    if streams is not None:
                        streams[1].close()
                    await self._send_packet("notify_close", [channel])
        except websockets.ConnectionClosed:
            pass
        finally:
            self.websocket = None
            for _channel, streams in list(self.channels.items()):
                streams[1].close()
            self.channels.clear()

    async def open_channel(self) -> int:
        channel = self.next_channel
        self.next_channel += 1
        reader, writer = await asyncio.open_connection("127.0.0.1", self.device_port)
        self.channels[channel] = (reader, writer)
        asyncio.create_task(self._pump_device_to_cli(channel, reader))
        await self._send_packet("notify_open", [channel])
        return channel

    async def handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_bytes = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        header_text = header_bytes.decode("iso-8859-1", "replace")
        request_line = header_text.split("\r\n", 1)[0]
        parts = request_line.split()
        if len(parts) < 2:
            writer.close()
            return
        method, raw_path = parts[0].upper(), parts[1]
        headers = {}
        for line in header_text.split("\r\n")[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        body = b""
        content_length = int(headers.get("content-length") or 0)
        if content_length:
            body = await reader.readexactly(content_length)
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        host_header = headers.get("host")
        status, payload, content_type = await self.route_http(method, path, host_header, body)
        response = (
            f"HTTP/1.1 {status} {'OK' if status < 400 else 'ERROR'}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + payload
        writer.write(response)
        await writer.drain()
        writer.close()

    async def route_http(
        self,
        method: str,
        path: str,
        host_header: Optional[str],
        body: bytes,
    ) -> Tuple[int, bytes, str]:
        hostname = self.public_host(host_header)
        if method == "GET" and path in {"/api/users/me", "/profile"}:
            return 200, json.dumps(
                {
                    "email": "linux-smoke@example.test",
                    "plan": "pro",
                    "port_forwarding_ports": [-1],
                }
            ).encode("utf-8"), "application/json"
        if method == "GET" and path == "/api/developer/devices":
            return 200, json.dumps(
                {
                    "devices": [
                        {
                            "hash": DEVICE_HASH,
                            "name": "linux-smoke-device",
                            "online": True,
                            "active": True,
                        }
                    ]
                }
            ).encode("utf-8"), "application/json"
        if method == "GET" and path.startswith("/api/developer/devices/"):
            return 200, json.dumps(
                {
                    "hash": DEVICE_HASH,
                    "name": "linux-smoke-device",
                    "online": True,
                    "active": True,
                    "port_forwarding_ports": [-1],
                }
            ).encode("utf-8"), "application/json"
        if method == "GET" and path.endswith("/host"):
            m2m_url = f"http://{hostname}:{self.m2m_port}/m2m"
            return 200, json.dumps({"m2m_url": m2m_url}).encode("utf-8"), "application/json"
        if method == "POST" and path.endswith("/ports"):
            if self.websocket is None:
                return 503, json.dumps({"detail": "M2M client is not connected"}).encode("utf-8"), "application/json"
            channel = await self.open_channel()
            return 201, json.dumps(
                {"port": channel, "service": "redirect-port"}
            ).encode("utf-8"), "application/json"
        return 404, json.dumps({"detail": f"unhandled {method} {path}"}).encode("utf-8"), "application/json"

    async def serve(self) -> None:
        device_server = await asyncio.start_server(self.handle_device, "127.0.0.1", self.device_port)
        api_server = await asyncio.start_server(self.handle_http, self.host, self.api_port)
        m2m_server = await websockets.serve(self.handle_m2m, self.host, self.m2m_port)
        async with device_server, api_server, m2m_server:
            await asyncio.Future()


def write_config(path: Path, base_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "auth_method": "api_key",
                "api_key": "linux-smoke-key",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--m2m-port", type=int, default=18001)
    parser.add_argument("--device-port", type=int, default=18080)
    parser.add_argument("--write-config", type=Path)
    parser.add_argument("--public-host", default="127.0.0.1")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.write_config:
        write_config(args.write_config, f"http://{args.public_host}:{args.api_port}")
        print(f"wrote {args.write_config}", file=sys.stderr)
    gateway = PortForwardMockGateway(args.host, args.api_port, args.m2m_port, args.device_port)
    print(
        f"mock gateway api=http://{args.host}:{args.api_port} "
        f"m2m=ws://{args.host}:{args.m2m_port} device=127.0.0.1:{args.device_port}",
        file=sys.stderr,
        flush=True,
    )
    await gateway.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
