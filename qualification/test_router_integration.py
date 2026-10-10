"""Cross-repository transport qualification, explicitly invoked with router deps.

Uses real CLI TunnelSession/TunnelM2MClient, M2MHandler.handle, Router,
trusted bind/open/renew/revoke HTTP operations, TLS websockets, random ingress
proxying and local TCP sockets. The central Prelude auth/registry broker and
Redis peer discovery are adapters; this is not full Django/staging evidence.

Run with network permission and both repositories/dependency sets importable:
  pytest -q qualification/test_router_integration.py
"""
import asyncio
import hashlib
import json
import socket
import ssl
import subprocess
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest
from aiohttp import BasicAuth, ClientSession, web

from dataplicity_cli.m2m import bencode_decode
from dataplicity_cli.tunnel_transport import TunnelM2MClient
from dataplicity_cli.tunnels import TunnelError, TunnelSession, websocket_url
from router2 import constants, packets, redis_keys
from router2.packet_codecs import BencodeCodec
from router2.handlers.m2m import M2MHandler
from router2.handlers.tunnels import operation
from router2.router import Router


class PeerDirectory:
    def __init__(self):
        self.records = {}

    async def get(self, key):
        return self.records.get(key)


class Broker:
    """Test-only central authority adapter; all router operations are real HTTP."""
    def __init__(self, http, ingress, publisher_url, consumer_url, port):
        self.http, self.ingress = http, ingress
        self.publisher_url, self.consumer_url, self.port = publisher_url, consumer_url, port
        self.publisher = None
        self.rows = {}
        self.auth = {"Authorization": BasicAuth("dataplicity", constants.ROUTER_SECRET).encode()}

    async def operation(self, action, body):
        async with self.http.post(self.ingress + "/m2m/tunnels/" + action,
                                  json={"owner_node_id": "router-0", **body}, headers=self.auth) as response:
            payload = await response.json()
            if response.status != 200:
                raise TunnelError(payload.get("error", "router_error"))
            return payload

    def control(self, role):
        broker = self
        class Control:
            api = SimpleNamespace(config=SimpleNamespace(install_id="qualification"))

            async def call(self, method, resource, *, payload=None, params=None):
                if resource == "bootstrap/":
                    return {"m2m_url": broker.publisher_url if role == "publisher" else broker.consumer_url,
                            "limits": {"frame_bytes": 1024}}
                if resource in ("publish/", "connect/"):
                    sid = str(uuid4())
                    row = {"session_id": sid, "organisation_id": "qualification-org", "principal_id": sid,
                           "role": role, "name": "test-api", "generation": 1, "target_port": broker.port,
                           "authority_version": 1, "lease_seconds": 45, "limits": {"frame_bytes": 1024}}
                    await broker.operation("bind", {**row, "identity": payload["identity"], "challenge": payload["challenge"]})
                    broker.rows[sid] = row
                    if role == "publisher":
                        broker.publisher = sid
                    return {**row, "heartbeat_seconds": 1}
                sid = resource.split("/")[1]
                if method == "DELETE":
                    return await broker.operation("revoke", {"session_id": sid})
                if resource.endswith("channels/"):
                    return await broker.operation("open", {"consumer_session_id": sid, "publisher_session_id": broker.publisher,
                                                           "generation": 1, "authority_version": 1, "lease_seconds": 45})
                if resource.endswith("heartbeat/"):
                    await broker.operation("renew", {"sessions": [{"session_id": sid, "authority_version": 1}]})
                    return {"limits": {"frame_bytes": 1024}}
                raise AssertionError(resource)
        return Control()


class UncooperativeClient(TunnelM2MClient):
    """Keep writing after server channel close, independently of normal CLI pumps."""
    async def _handle_packet(self, kind, body):
        if kind == 19:  # Existing NotifyClose deliberately ignored.
            return
        await super()._handle_packet(kind, body)


async def eventually(predicate, timeout=3):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def unused_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
async def test_router_instruction_codec_matches_cli_binding_and_open():
    client = TunnelM2MClient("wss://qualification.invalid/m2m/", {})
    for data in ({b"action": b"named-tunnel-binding", b"version": 1, b"challenge": b"a" * 64},
                 {b"action": b"named-tunnel-open", b"port": 1000, b"target_port": 3000, b"generation": 1}):
        wire = bencode_decode(BencodeCodec().encode(packets.Instruction(b"router", data)))
        await client._handle_packet(wire[0], wire[1:])
    assert client.challenge == "a" * 64
    assert client.instructions.get_nowait()[b"target_port"] == 3000


@pytest.mark.asyncio
async def test_actual_cli_router_http_multistream_half_close_and_uncooperative_revocation(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "ROUTER_NAMED_TUNNELS_ENABLED", True)
    monkeypatch.setattr(constants, "ROUTER_NAMED_TUNNELS_INGEST_ENABLED", True)
    monkeypatch.setattr(constants, "ROUTER_NAMED_TUNNELS_RELAY_ENABLED", True)
    monkeypatch.setattr(constants, "ROUTER_LOCAL_STATE_ONLY", True)
    monkeypatch.setattr(constants, "LIMIT_BANDWIDTH", False)
    cert, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,IP:127.0.0.2",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    ca = tmp_path / "trusted-ca.pem"
    default_ca = ssl.get_default_verify_paths().cafile
    ca.write_bytes((Path(default_ca).read_bytes() if default_ca else b"") + cert.read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    directory = PeerDirectory()
    runners, services, tasks, writers = [], [], [], set()
    received = bytearray()

    async def service(reader, writer):
        writers.add(writer)
        try:
            request = bytearray()
            while data := await reader.read(1024):
                request.extend(data)
                received.extend(data)
            body = hashlib.sha256(request).hexdigest().encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 64\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.write_eof()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            writers.discard(writer)

    local = await asyncio.start_server(service, "127.0.0.1", 0)
    target = local.sockets[0].getsockname()[1]
    adversary = None
    try:
        for index, host in enumerate(("127.0.0.1", "127.0.0.2")):
            node_id = "router-" + str(index)
            router_service = SimpleNamespace(node_id=node_id, identity_prefix=node_id.encode(), host=host,
                host_bytes=host.encode(), mesh=SimpleNamespace(enabled=True), redis_control=directory,
                redis_pool=None, shutting_down=False, lifecycle_log=Mock(), secret=constants.ROUTER_SECRET.encode())
            router_service.router = Router(router_service)
            counter = iter(range(1000 + index * 1000, 1999 + index * 1000))
            async def allocate(counter=counter):
                return next(counter)
            router_service.router.allocate_port = allocate
            app = web.Application()
            app["service"], app["m2m_handlers"] = router_service, {}
            app.router.add_get("/m2m/", M2MHandler.handle)
            app.router.add_post("/m2m/tunnels/{operation}", operation)
            runner = web.AppRunner(app)
            await runner.setup()
            runners.append(runner)
            inner = web.TCPSite(runner, host, 0 if index == 0 else inner_port)
            await inner.start()
            inner_port = inner._server.sockets[0].getsockname()[1]
            outer = web.TCPSite(runner, host, 0 if index == 0 else outer_port, ssl_context=tls)
            await outer.start()
            outer_port = outer._server.sockets[0].getsockname()[1]
            directory.records[redis_keys.ROUTER_PEERS + node_id.encode()] = json.dumps({"mesh_host": host}).encode()
            services.append(router_service)
        monkeypatch.setattr(constants, "ROUTER_PORT", inner_port)
        async with ClientSession() as http:
            broker = Broker(http, f"http://127.0.0.2:{inner_port}",
                            f"wss://127.0.0.1:{outer_port}/m2m/",
                            f"wss://127.0.0.2:{outer_port}/m2m/?named_owner=router-0", target)
            published, listening = asyncio.Event(), asyncio.Event()
            publisher = TunnelSession(broker.control("publisher"), "test-api", lambda event: published.set() if event["event"] == "published" else None)
            consumer = TunnelSession(broker.control("consumer"), "test-api", lambda event: listening.set() if event["event"] == "listener_started" else None)
            tasks.append(asyncio.create_task(publisher.publish(target)))
            await asyncio.wait_for(published.wait(), 5)
            port = unused_port()
            tasks.append(asyncio.create_task(consumer.connect(port)))
            await asyncio.wait_for(listening.wait(), 5)
            assert consumer.m2m.identity.startswith("router-0~")  # Arrived via router1.

            async def request(payload):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                try:
                    response = await asyncio.wait_for(reader.read(), 5)
                    assert response.startswith(b"HTTP/1.1 200 OK")
                    assert response.split(b"\r\n\r\n", 1)[1] == hashlib.sha256(payload).hexdigest().encode()
                finally:
                    writer.close()
                    await writer.wait_closed()

            await asyncio.gather(*(request(b"GET / HTTP/1.1\r\n\r\n" + bytes([index]) * (128 * 1024 if index == 0 else 4096)) for index in range(6)))
            await eventually(lambda: not services[0].router.named_tunnels.channels)
            assert not services[1].router.named_tunnels.sessions

            adversary = UncooperativeClient(websocket_url(broker.consumer_url), {})
            await adversary.connect()
            identity, challenge = await adversary.wait_for_binding()
            bad_control = broker.control("consumer")
            bad = await bad_control.call("POST", "connect/", payload={"identity": identity, "challenge": challenge})
            admitted = await bad_control.call("POST", f"sessions/{bad['session_id']}/channels/", payload={"generation": 1})
            channel = admitted["port"]
            before = len(received)
            await adversary.send_route(channel, b"before-revoke")
            await eventually(lambda: len(received) > before)
            await broker.operation("revoke", {"port": channel})
            after = len(received)
            for _ in range(10):
                await adversary.send_route(channel, b"must-not-reach-service")
            await asyncio.sleep(0.1)
            assert len(received) == after
            assert channel not in services[0].router.routes
            await request(b"GET /unaffected HTTP/1.1\r\n\r\n")
            await broker.operation("revoke", {"session_id": bad["session_id"]})
            await eventually(lambda: bad["session_id"] not in services[0].router.named_tunnels.sessions)
            await request(b"GET /still-authorised HTTP/1.1\r\n\r\n")
            # Client ignored NotifyClose; server state alone prevented every write.
            assert b"must-not-reach-service" not in received
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()
            await adversary.close()
            adversary = None
            await eventually(lambda: not services[0].router.named_tunnels.sessions)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if adversary:
            await adversary.close()
        for writer in list(writers):
            writer.close()
        local.close()
        await local.wait_closed()
        for runner in reversed(runners):
            await runner.cleanup()
