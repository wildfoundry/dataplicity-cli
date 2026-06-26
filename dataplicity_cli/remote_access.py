from __future__ import annotations

import asyncio
import os
import sys
import termios
import tty
from typing import Optional

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


async def run_terminal_session(m2m: M2MClient, port: int) -> None:
    queue = m2m.channel_queue(port)
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    async def stdin_loop() -> None:
        while True:
            data = await asyncio.to_thread(os.read, stdin_fd, 1024)
            if not data:
                break
            await m2m.send_route(port, data)

    async def stdout_loop() -> None:
        while True:
            data = await queue.get()
            if data is None:
                break
            os.write(stdout_fd, data)

    with RawTerminal():
        await asyncio.gather(stdin_loop(), stdout_loop())


async def run_single_command(
    m2m: M2MClient,
    channel_port: int,
    command: str,
    *,
    timeout_seconds: Optional[float] = 30.0,
) -> bytes:
    queue = m2m.channel_queue(channel_port)
    command_text = str(command or "").strip()
    if not command_text:
        raise RuntimeError("Command cannot be empty.")

    # Send command and close the terminal session afterwards.
    await m2m.send_route(channel_port, f"{command_text}\nexit\n".encode("utf-8"))

    async def read_output() -> bytes:
        chunks = []
        while True:
            data = await queue.get()
            if data is None:
                break
            chunks.append(data)
        return b"".join(chunks)

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
) -> None:
    queue = m2m.channel_queue(channel_port)
    done = asyncio.Event()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def local_to_remote() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await m2m.send_route(channel_port, data)

        async def remote_to_local() -> None:
            while True:
                data = await queue.get()
                if data is None:
                    break
                writer.write(data)
                await writer.drain()

        await asyncio.gather(local_to_remote(), remote_to_local())
        try:
            writer.close()
            await writer.wait_closed()
        finally:
            done.set()

    server = await asyncio.start_server(handle_client, host="127.0.0.1", port=local_port)
    async with server:
        await done.wait()
