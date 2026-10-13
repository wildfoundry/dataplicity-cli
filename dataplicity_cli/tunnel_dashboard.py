"""Bounded, local tunnel telemetry and an interactive terminal view."""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def size(value: float) -> str:
    for unit in ("B", "KB", "MB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def chart(values: deque, width: int) -> str:
    selected = list(values)[-max(1, width):]
    peak = max(selected, default=0)
    return "".join("▁▂▃▄▅▆▇█"[min(7, int(v / peak * 7))] if peak else "▁" for v in selected)


class TunnelStats:
    """Counters persist across reconnection; chart/event storage is bounded."""

    def __init__(self, name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.clock = name, clock
        self.started = self.sampled = clock()
        self.status = "Starting"
        self.mode = ""
        self.local_port = self.target_port = None
        self.sent = self.received = self.active = self.closed = self.failed = 0
        self.last_traffic = None
        self.rtt = None
        self.limits = {}
        self.rates = (0.0, 0.0)
        self.previous = (0, 0)
        self.send_history: deque = deque(maxlen=30)
        self.receive_history: deque = deque(maxlen=30)
        self.events: deque = deque(maxlen=6)

    def record(self, event: dict) -> None:
        kind = event["event"]
        if kind in {"bytes_up", "bytes_down"}:
            count = event.get("bytes", 0)
            if type(count) is int and count > 0:
                if kind == "bytes_up":
                    self.sent += count
                else:
                    self.received += count
                self.last_traffic = self.clock()
            return
        if kind == "connection_opened":
            self.active += 1
        elif kind == "connection_closed":
            self.active = max(0, self.active - 1)
            self.closed += 1
        elif kind in {"connection_rejected", "stream_closed"}:
            self.failed += 1
        states = {"published": "Published", "listener_started": "Listening",
                  "reconnecting": "Reconnecting", "listener_stopped": "Stopped",
                  "stopped": "Stopped", "error": "Error"}
        if kind in states:
            self.status = states[kind]
        if kind in states or kind in {"connection_rejected", "stream_closed"}:
            # Text, never terminal escape sequences or Rich markup from events.
            detail = "".join(c for c in str(event.get("detail", "")) if c.isprintable())[:160]
            self.events.append((self.clock(), kind.replace("_", " "), detail))

    def update_limits(self, payload: dict) -> None:
        limits = payload.get("limits", {})
        if isinstance(limits, dict):
            for key in ("streams", "bytes_per_second"):
                value = limits.get(key)
                if type(value) is int and value > 0:
                    self.limits[key] = value

    def sample(self, latency: float = 0.0) -> None:
        now = self.clock()
        elapsed = now - self.sampled
        if elapsed > 0:
            self.rates = ((self.sent - self.previous[0]) / elapsed,
                          (self.received - self.previous[1]) / elapsed)
            self.send_history.append(self.rates[0])
            self.receive_history.append(self.rates[1])
            self.previous, self.sampled = (self.sent, self.received), now
        self.rtt = latency if type(latency) in (float, int) and math.isfinite(latency) and latency > 0 else None

    def render(self, organisation: str, width: int = 80, height: int | None = None) -> Panel:
        now = self.clock()
        table = Table.grid(padding=(0, 2))
        table.add_column()
        table.add_column(overflow="fold")
        path = (f"Local service: 127.0.0.1:{self.target_port}" if self.mode == "publisher"
                else f"127.0.0.1:{self.local_port} → {self.name} → port {self.target_port or 'unknown'}")
        table.add_row("Endpoint", Text(path))
        table.add_row("Organisation", Text(organisation))
        table.add_row("Connections", f"{self.active} active · {self.closed} closed · {self.failed} failed")
        for label, rate, history in (("Sending", self.rates[0], self.send_history),
                                     ("Receiving", self.rates[1], self.receive_history)):
            table.add_row(label, Text(f"{size(rate)}/s  {chart(history, min(30, max(1, width - 36)))}"))
        table.add_row("Transferred", f"{size(self.sent)} sent · {size(self.received)} received")
        table.add_row("CLI–relay RTT", f"{self.rtt * 1000:.0f} ms" if self.rtt is not None else "Awaiting heartbeat")
        activity = "No traffic yet" if self.last_traffic is None else f"{max(0, int(now - self.last_traffic))}s ago"
        table.add_row("Running", f"{max(0, int(now - self.started))}s · Last traffic: {activity}")
        caps = []
        if "streams" in self.limits:
            caps.append(f"{self.limits['streams']} streams")
        if "bytes_per_second" in self.limits:
            caps.append(f"{size(self.limits['bytes_per_second'])}/s")
        if caps:
            table.add_row("Server policy limits", " · ".join(caps))
        recent = Text("Recent events\n", style="bold")
        for timestamp, kind, detail in self.events:
            recent.append(f"{int(timestamp - self.started):>4}s  {kind}: {detail}\n", style="")
        return Panel(Group(table, recent, Text("Ctrl+C to stop · Rates are local payload bytes; charts show recent activity.")),
                     title=Text(f"{self.name} · {self.status}"), expand=True, height=height)
