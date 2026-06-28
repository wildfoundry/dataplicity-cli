from __future__ import annotations

import asyncio
import os
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Callable, Optional

from .m2m import M2MClient


class RawTerminal:
    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old: Optional[list] = None

    def __enter__(self) -> "RawTerminal":
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None and self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


class CommandTimeoutError(TimeoutError):
    pass


@dataclass
class PortForwardEvent:
    kind: str
    timestamp: float
    bytes_count: int = 0
    detail: str = ""


PortForwardEventCallback = Callable[[PortForwardEvent], None]


def _detect_protocol(sample: bytes) -> Optional[str]:
    if not sample:
        return None
    upper = sample[:16].upper()
    if upper.startswith(b"GET ") or upper.startswith(b"POST ") or upper.startswith(b"PUT "):
        return "HTTP request"
    if upper.startswith(b"HEAD ") or upper.startswith(b"PATCH ") or upper.startswith(b"DELETE "):
        return "HTTP request"
    if upper.startswith(b"HTTP/"):
        return "HTTP response"
    if sample.startswith(b"SSH-"):
        return "SSH"
    if len(sample) >= 3 and sample[0] == 0x16 and sample[1] == 0x03 and sample[2] in {0x00, 0x01, 0x02, 0x03, 0x04}:
        return "TLS"
    return None


async def run_terminal_session(m2m: M2MClient, port: int) -> None:
    queue = m2m.channel_queue(port)
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stop_event = asyncio.Event()

    async def stdin_loop() -> None:
        while not stop_event.is_set():
            ready, _, _ = await asyncio.to_thread(select.select, [stdin_fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(stdin_fd, 1024)
            if not data:
                break
            await m2m.send_route(port, data)

    async def stdout_loop() -> None:
        while True:
            data = await queue.get()
            if data is None:
                stop_event.set()
                break
            os.write(stdout_fd, data)

    with RawTerminal():
        stdin_task = asyncio.create_task(stdin_loop())
        stdout_task = asyncio.create_task(stdout_loop())
        done, pending = await asyncio.wait(
            {stdin_task, stdout_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stdout_task in done:
            stop_event.set()
        for task in pending:
            task.cancel()
        await asyncio.gather(stdin_task, stdout_task, return_exceptions=True)


async def run_single_command(
    m2m: M2MClient,
    channel_port: int,
    command: str,
    *,
    timeout_seconds: Optional[float] = 30.0,
    first_response_timeout_seconds: float = 8.0,
    idle_timeout_seconds: float = 8.0,
) -> bytes:
    queue = m2m.channel_queue(channel_port)
    command_text = str(command or "").strip()
    if not command_text:
        raise RuntimeError("Command cannot be empty.")

    done_marker = "__DP_CLI_DONE__"
    wrapped_command = (
        command_text
        + "\n"
        + "__dp_cli_status=$?\n"
        + "printf '\\n__DP_' 'CLI_DONE__%s\\n' \"$__dp_cli_status\"\n"
        + "exit\n"
    )
    await m2m.send_route(channel_port, wrapped_command.encode("utf-8"))

    async def read_output() -> bytes:
        chunks = []
        marker_bytes = done_marker.encode("utf-8")
        saw_any_data = False
        while True:
            if not saw_any_data:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=first_response_timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        "Remote shell did not produce any output; command execution may be unavailable on this device."
                    ) from exc
            else:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=idle_timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        "Remote shell became idle before command completion; no completion marker was observed."
                    ) from exc
            if data is None:
                break
            saw_any_data = True
            chunks.append(data)
            merged = b"".join(chunks)
            marker_at = merged.find(marker_bytes)
            if marker_at != -1:
                # Stop at sentinel instead of waiting for socket teardown.
                return merged[:marker_at].rstrip(b"\r\n")
        return b"".join(chunks).rstrip(b"\r\n")

    if timeout_seconds is None:
        return await read_output()

    try:
        return await asyncio.wait_for(read_output(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        await m2m.close_channel(channel_port)
        raise CommandTimeoutError(
            f"Command timed out after {int(timeout_seconds)}s. Retry with --no-timeout for long-running commands."
        ) from exc


async def run_remote_file(
    m2m: M2MClient,
    channel_port: int,
    output_path: Optional[str],
    *,
    allow_stdout: bool,
) -> int:
    queue = m2m.channel_queue(channel_port)
    bytes_written = 0
    output_file = None
    try:
        if output_path:
            output_file = open(output_path, "wb")
            writer = output_file
        else:
            if not allow_stdout:
                raise RuntimeError("Specify --output or --stdout to write file content.")
            writer = getattr(sys.stdout, "buffer", sys.stdout)
        while True:
            data = await queue.get()
            if data is None:
                break
            writer.write(data)
            bytes_written += len(data)
        if hasattr(writer, "flush"):
            writer.flush()
        return bytes_written
    finally:
        if output_file:
            output_file.close()


async def run_port_forward(
    m2m: M2MClient,
    channel_port: int,
    local_port: int,
    *,
    event_callback: Optional[PortForwardEventCallback] = None,
) -> None:
    queue = m2m.channel_queue(channel_port)
    active_client = asyncio.Lock()

    def emit(kind: str, *, bytes_count: int = 0, detail: str = "") -> None:
        if event_callback:
            event_callback(
                PortForwardEvent(
                    kind=kind,
                    timestamp=time.monotonic(),
                    bytes_count=bytes_count,
                    detail=detail,
                )
            )

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_label = str(peer) if peer else "unknown"
        if active_client.locked():
            emit("connection_rejected", detail=peer_label)
            writer.close()
            await writer.wait_closed()
            return

        await active_client.acquire()
        acquired = True
        emit("connection_opened", detail=peer_label)
        local_started = asyncio.Event()
        first_remote_byte_seen = False
        protocols_seen = set()

        async def local_to_remote() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                local_started.set()
                await m2m.send_route(channel_port, data)
                emit("bytes_up", bytes_count=len(data))
                protocol = _detect_protocol(data[:32])
                if protocol and protocol not in protocols_seen:
                    protocols_seen.add(protocol)
                    emit("protocol_detected", detail=protocol)

        async def remote_to_local() -> None:
            nonlocal first_remote_byte_seen
            while True:
                data = await queue.get()
                if data is None:
                    break
                if local_started.is_set() and not first_remote_byte_seen:
                    first_remote_byte_seen = True
                    emit("first_remote_byte")
                writer.write(data)
                await writer.drain()
                emit("bytes_down", bytes_count=len(data))
                protocol = _detect_protocol(data[:32])
                if protocol and protocol not in protocols_seen:
                    protocols_seen.add(protocol)
                    emit("protocol_detected", detail=protocol)

        try:
            await asyncio.gather(local_to_remote(), remote_to_local())
        finally:
            writer.close()
            await writer.wait_closed()
            emit("connection_closed", detail=peer_label)
            if acquired and active_client.locked():
                active_client.release()

    server = await asyncio.start_server(handle_client, host="127.0.0.1", port=local_port)
    emit("listener_started", detail=f"127.0.0.1:{local_port}")
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        emit("listener_stopped")
        raise
