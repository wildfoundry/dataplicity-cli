"""Bounded, local tunnel telemetry and an interactive terminal view."""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable

from rich.console import Group
from rich.layout import Layout

from .terminal import plot, status_style
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
        self.send_history: deque = deque(maxlen=60)
        self.receive_history: deque = deque(maxlen=60)
        self.events: deque = deque(maxlen=24)
        self.history: deque = deque(maxlen=3600)
        self.reconnects = self.peak_active = 0
        self.peak_sent = self.peak_received = 0.0
        self.first_ready = None
        self.state_changed = self.started
        self.ready_seconds = 0.0

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
            self.peak_active = max(self.peak_active, self.active)
        elif kind == "connection_closed":
            self.active = max(0, self.active - 1)
            self.closed += 1
        elif kind in {"connection_rejected", "stream_closed"}:
            self.failed += 1
        states = {"published": "Published", "listener_started": "Listening",
                  "reconnecting": "Reconnecting", "listener_stopped": "Stopped",
                  "stopped": "Stopped", "error": "Error"}
        if kind in states:
            now = self.clock()
            if self.status in {"Published", "Listening"}:
                self.ready_seconds += max(0, now - self.state_changed)
            self.status = states[kind]
            self.state_changed = now
            if self.status in {"Published", "Listening"} and self.first_ready is None:
                self.first_ready = now
            if kind == "reconnecting":
                self.reconnects += 1
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
        if elapsed > 0:
            ready = self.status in {"Published", "Listening"}
            self.history.append((now, *self.rates, self.rtt if ready else None, self.active, ready))
            self.peak_sent = max(self.peak_sent, self.rates[0])
            self.peak_received = max(self.peak_received, self.rates[1])

    def availability(self) -> tuple:
        now = self.clock()
        observed = max(0, now - self.first_ready) if self.first_ready is not None else 0
        ready = self.ready_seconds + (max(0, now - self.state_changed) if self.status in {"Published", "Listening"} else 0)
        return (100 * ready / observed if observed else None, max(0, observed - ready))

    def render(self, organisation: str, width: int = 80, height: int | None = None) -> Panel:
        now = self.clock()
        if height is not None and height >= 24 and width >= 70:
            return self._full_screen(organisation, width, height)
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
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
                     title=Text(f"{self.name} · {self.status}", style=status_style(self.status)), expand=True, height=height, border_style=status_style(self.status))

    def _full_screen(self, organisation: str, width: int, height: int) -> Panel:
        now = self.clock()
        tone = status_style(self.status)
        path = (f"127.0.0.1:{self.target_port} → private relay" if self.mode == "publisher"
                else f"127.0.0.1:{self.local_port} → {self.name} → port {self.target_port or 'unknown'}")
        percent, downtime = self.availability()
        availability = f"Local readiness {percent:.1f}%" if percent is not None else "Waiting for first connection"
        header = Text(f"{self.mode.upper() or 'TUNNEL'}  {path}\n", style="bold cyan", no_wrap=True, overflow="ellipsis")
        header.append(f"Organisation {organisation}\n", style="dim")
        header.append(f"{availability} · Downtime {downtime:.1f}s · Reconnects {self.reconnects} · Uptime {int(now - self.started)}s", style=tone)
        summary = Text(f"{self.active} active  ·  {self.closed} closed  ·  {self.failed} failed  ·  Peak {self.peak_active} simultaneous\n", style="bold")
        summary.append(f"{size(self.sent)} sent", style="cyan")
        summary.append("  /  ")
        summary.append(f"{size(self.received)} received", style="magenta")
        activity = "No traffic yet" if self.last_traffic is None else f"Last traffic {int(now - self.last_traffic)}s ago"
        summary.append(f"  ·  {activity}", style="dim")
        elapsed = now - self.started
        span = 60 if elapsed <= 60 else 300 if elapsed <= 300 else 900 if elapsed <= 900 else 3600
        span_label = f"{span}s" if span == 60 else f"{span // 60}m"
        window = [sample for sample in self.history if sample[0] >= now - span]
        graph_width = max(1, (width - 12) // 2)

        def graph(title, index, unit, colour, current, peak=None, rows=3):
            values = [None] * 60
            for sample in window:
                slot = max(0, min(59, int((sample[0] - (now - span)) / span * 60)))
                value = sample[index]
                if value is not None:
                    values[slot] = max(value, values[slot] or 0)
            valid = [value for value in values if value is not None]
            maximum = max(valid, default=0)
            window_max = unit(maximum) if valid else "—"
            caption = f"Peak {unit(peak)}" if peak is not None else f"Window max {window_max}"
            text = Text(f"{current}  ·  {caption}\n", style=f"bold {colour}", no_wrap=True, overflow="ellipsis")
            text.append_text(plot(values, graph_width, rows, colour))
            text.append(f"\n−{span_label}" + " " * max(1, graph_width - len(span_label) - 5) + "now", style="dim")

            return Panel(text, title=title, border_style=colour)

        traffic = Layout()
        traffic.split_row(
            Layout(graph("↑ Sending · payload", 1, lambda v: f"{size(v)}/s", "cyan", f"{size(self.rates[0])}/s", self.peak_sent)),
            Layout(graph("↓ Receiving · payload", 2, lambda v: f"{size(v)}/s", "magenta", f"{size(self.rates[1])}/s", self.peak_received)),
        )
        diagnostics = Layout()
        diagnostics.split_row(
            Layout(graph("CLI–relay RTT · heartbeat", 3, lambda v: f"{v * 1000:.0f} ms", "yellow",
                         f"{self.rtt * 1000:.0f} ms" if self.rtt is not None and self.status in {"Published", "Listening"} else "Awaiting heartbeat", rows=2)),
            Layout(graph("Concurrent TCP streams", 4, lambda v: str(int(v)), "green", f"{self.active} active", rows=2)),
        )
        events = Text("Connectivity (recent samples): ", style="bold", no_wrap=True, overflow="ellipsis")
        for sample in window[-max(1, width - 40):]:
            events.append("●" if sample[5] else "×", style="green" if sample[5] else "yellow")
        events.append("\n● locally ready  × unavailable  ·  Recent events\n", style="dim")
        for timestamp, kind, detail in list(self.events)[-max(1, height - 25):]:
            events.append(f"{int(timestamp - self.started):>5}s  {kind}: {detail}\n", style=status_style(kind))
        caps = []
        if "streams" in self.limits:
            caps.append(f"{self.limits['streams']} streams")
        if "bytes_per_second" in self.limits:
            caps.append(f"{size(self.limits['bytes_per_second'])}/s")
        footer = Text(f"Ctrl+C stop · {span_label} charts · RTT to relay, not service latency", style="dim", no_wrap=True, overflow="ellipsis")
        if caps:
            footer.append(" · Policy: " + ", ".join(caps))
        layout = Layout()
        layout.split_column(Layout(header, size=3), Layout(summary, size=2),
                            Layout(traffic, size=7), Layout(diagnostics, size=6),
                            Layout(events), Layout(footer, size=1))
        return Panel(layout, title=Text(f" {self.name} · {self.status} ", style=f"bold {tone}"),
                     height=height, border_style=tone, padding=(0, 1))
