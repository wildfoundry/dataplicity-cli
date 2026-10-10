"""Named tunnel lifecycle using the central API and existing TCP forwarding."""
from __future__ import annotations

import asyncio
import re
import random
import threading
from contextlib import suppress
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from .api import ApiClient
from .client_identity import identity_headers
from .remote_access import bridge_tcp_channel, run_port_forward, _close_stream_writer
from .tunnel_transport import TunnelM2MClient


class TunnelError(RuntimeError):
    def __init__(self, message: str, code: str = "tunnel_error") -> None:
        super().__init__(message)
        self.code = code


def validate_name(name: str) -> str:
    normalized = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", normalized):
        raise TunnelError("Use a tunnel name of 1–63 letters, numbers, dots, dashes or underscores.")
    return normalized


def websocket_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "wss"} or not parts.hostname or parts.username or parts.password:
        raise TunnelError("The server returned an invalid secure relay URL.")
    # Owner hints are issued by the trusted backend and interpreted by the
    # existing router mesh. Preserve its query exactly across random ingress.
    query = parts.query + ("&" if parts.query else "") + "features=named-tunnels-v1"
    return urlunsplit(("wss", parts.netloc, parts.path, query, ""))


def frame_limit(payload: dict) -> int:
    limits = payload.get("limits", {})
    if not isinstance(limits, dict):
        raise TunnelError("The server returned invalid tunnel limits.")
    value = limits.get("frame_bytes", limits.get("max_buffer_bytes", 65536))
    if type(value) is not int or not 1024 <= value <= 65536:
        raise TunnelError("The server returned an invalid tunnel frame limit.")
    return value


class TunnelAPI:
    def __init__(self, api: ApiClient, organisation: str, token: Optional[str] = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", organisation):
            raise TunnelError("Select an organisation hash with --org.")
        if not token and (api.config.auth_method != "jwt" or not api.config.access_token):
            raise TunnelError("Run dataplicity auth login, or publish using a scoped token via --token-env or --token-stdin.")
        self.api = api
        self.base = f"/api/organisations/{organisation}/development-tunnels/"
        self.headers = {"Authorization": f"TunnelPublisher {token}"} if token else None
        self._request_lock = threading.Lock()
        self._call_lock: Optional[asyncio.Lock] = None

    def request(self, method: str, resource: str, *, payload: Optional[dict] = None, params: Optional[dict] = None) -> Any:
        # ApiClient owns a requests.Session and mutates saved JWTs on refresh.
        # Serialize control operations; forwarding never uses this lock.
        with self._request_lock:
            response = self.api.request(
                method, self.base + resource, json_data=payload, params=params,
                headers=self.headers, allow_refresh=self.headers is None,
            )
        if not response.ok:
            data = response.data if isinstance(response.data, dict) else {}
            code = data.get("error") or data.get("error_code") or "tunnel_error"
            messages = {
                0: "Unable to reach Dataplicity. Check your network connection.",
                401: "Authentication expired. Sign in again or rotate the publisher credential.",
                403: "Tunnel access denied. Ask an organisation administrator to grant access to this name and port.",
                404: "Tunnel is offline, missing or unavailable for this organisation.",
                409: "Tunnel was replaced or this session is no longer current. Start a fresh authorised command.",
                429: "Tunnel quota or admission limit reached. Retry later or contact your administrator.",
                503: "Development tunnels are unavailable for this organisation.",
            }
            reasons = {
                "session_fenced": "Tunnel was replaced. Start a fresh authorised command to use the current publisher.",
                "session_expired": "Tunnel authority expired. Start a fresh authorised command.",
                "publisher_revoked": "Publisher authority was revoked. Ask your administrator to review the credential and name policy.",
                "paid_plan_required": "Development tunnels require an eligible paid organisation plan.",
            }
            # Error bodies and transport exceptions are never dumped: they may
            # contain infrastructure details or a reflected credential.
            raise TunnelError(reasons.get(str(code), messages.get(response.status_code, "Dataplicity tunnel request failed.")), str(code))
        return response.data

    async def call(self, method: str, resource: str, **kwargs: Any) -> Any:
        if self._call_lock is None:
            self._call_lock = asyncio.Lock()
        # Queue before dispatching to a thread so cancellation does not leave
        # an executor full of obsolete admissions behind a blocked request.
        async with self._call_lock:
            return await asyncio.to_thread(self.request, method, resource, **kwargs)


class TunnelSession:
    def __init__(self, control: TunnelAPI, name: str, emit: Callable[[dict], None]) -> None:
        self.control = control
        self.name = validate_name(name)
        self.emit = emit
        self.session: Optional[dict] = None
        self.m2m: Optional[TunnelM2MClient] = None
        self.frame_bytes = 65536

    async def _transport(self, url: str) -> tuple[str, str]:
        self.m2m = TunnelM2MClient(websocket_url(url), identity_headers(self.control.api.config.install_id))
        self.m2m.frame_bytes = self.frame_bytes
        await self.m2m.connect()
        return await self.m2m.wait_for_binding()

    async def _heartbeat(self) -> None:
        assert self.session is not None
        while True:
            await asyncio.sleep(min(15, max(1, self.session.get("heartbeat_seconds", 15))))
            renewed = await self.control.call("POST", f"sessions/{self.session['session_id']}/heartbeat/", payload={"generation": self.session["generation"]})
            self.frame_bytes = min(self.frame_bytes, frame_limit(renewed))
            if self.m2m is not None:
                self.m2m.frame_bytes = self.frame_bytes

    async def _run_until_closed(self, action: Any) -> None:
        assert self.m2m is not None
        tasks = [asyncio.create_task(action), asyncio.create_task(self._heartbeat()), asyncio.create_task(self.m2m._closed_event.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[2] in done:
                raise TunnelError("Relay connection closed; established TCP streams have ended.", "transport_lost")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup_transport(self) -> None:
        if self.m2m:
            await self.m2m.close()
            self.m2m = None

    async def close(self) -> None:
        await self._cleanup_transport()
        if self.session:
            with suppress(TunnelError):
                await self.control.call("DELETE", f"sessions/{self.session['session_id']}/", params={"generation": self.session["generation"]})

    async def publish(self, port: int) -> None:
        bootstrap = await self.control.call("GET", "bootstrap/", params={"name": self.name, "mode": "publisher"})
        self.frame_bytes = frame_limit(bootstrap)
        failures = 0
        try:
            while True:
                try:
                    identity, challenge = await self._transport(bootstrap["m2m_url"])
                    if self.session is None:
                        self.session = await self.control.call("POST", "publish/", payload={"name": self.name, "port": port, "identity": identity, "challenge": challenge})
                    else:
                        self.session = await self.control.call("POST", f"sessions/{self.session['session_id']}/resume/", payload={"generation": self.session["generation"], "identity": identity, "challenge": challenge})
                    self.frame_bytes = min(self.frame_bytes, frame_limit(self.session))
                    if self.m2m is not None:
                        self.m2m.frame_bytes = self.frame_bytes
                    failures = 0
                    self.emit({"event": "published", "name": self.name, "target_port": port})
                    await self._run_until_closed(self._serve_publisher(port))
                except (OSError, asyncio.TimeoutError, TunnelError) as exc:
                    if isinstance(exc, TunnelError) and exc.code != "transport_lost":
                        raise
                    # Resume preserves the original generation. Never issue a
                    # new publish automatically or reclaim a superseded name.
                    await self._cleanup_transport()
                    failures += 1
                    if failures > 5:
                        raise TunnelError("Publisher reconnect failed. Start a fresh command after checking connectivity.") from None
                    self.emit({"event": "reconnecting", "name": self.name})
                    await asyncio.sleep(min(8, 2 ** (failures - 1)) * random.uniform(0.75, 1.25))
        finally:
            await self.close()

    async def _serve_publisher(self, port: int) -> None:
        assert self.m2m is not None and self.session is not None
        m2m = self.m2m
        streams: set[asyncio.Task] = set()

        async def serve(instruction: dict) -> None:
            channel = int(instruction[b"port"])
            writer = None
            try:
                if instruction.get(b"target_port") != port or instruction.get(b"generation") != self.session["generation"]:
                    raise TunnelError("Relay instruction does not match the admitted publisher.")
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=10)
                await bridge_tcp_channel(m2m, channel, reader, writer)
            except (OSError, asyncio.TimeoutError, TunnelError):
                self.emit({"event": "stream_closed", "name": self.name})
            finally:
                with suppress(Exception):
                    await m2m.close_channel(channel)
                if writer:
                    await _close_stream_writer(writer)

        try:
            while True:
                instruction = await m2m.instructions.get()
                if len(streams) >= m2m.MAX_CHANNELS:
                    await m2m.close_channel(int(instruction[b"port"]))
                    continue
                task = asyncio.create_task(serve(instruction))
                streams.add(task)
                task.add_done_callback(streams.discard)
        finally:
            for task in streams:
                task.cancel()
            await asyncio.gather(*streams, return_exceptions=True)

    async def connect(self, local_port: int) -> None:
        try:
            bootstrap = await self.control.call("GET", "bootstrap/", params={"name": self.name, "mode": "consumer"})
            self.frame_bytes = frame_limit(bootstrap)
            identity, challenge = await self._transport(bootstrap["m2m_url"])
            self.session = await self.control.call("POST", "connect/", payload={"name": self.name, "identity": identity, "challenge": challenge})
            assert self.m2m is not None
            self.m2m.frame_bytes = min(self.frame_bytes, frame_limit(self.session))

            async def channel_factory() -> int:
                assert self.session is not None
                channel = await self.control.call("POST", f"sessions/{self.session['session_id']}/channels/", payload={"generation": self.session["generation"]})
                if channel.get("generation") != self.session["generation"] or channel.get("target_port") != self.session["target_port"]:
                    await self.m2m.close_channel(int(channel["port"]))
                    raise TunnelError("Tunnel was replaced. Run connect again to authorise the current publisher.")
                return int(channel["port"])

            def progress(event: Any) -> None:
                self.emit({"event": event.kind, "name": self.name, "bytes": event.bytes_count, "detail": event.detail})

            await self._run_until_closed(run_port_forward(self.m2m, None, local_port, channel_factory=channel_factory, half_close=True, event_callback=progress))
        finally:
            await self.close()
