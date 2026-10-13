import asyncio
import io
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console

from dataplicity_cli.tunnel_dashboard import TunnelStats, chart, size
from dataplicity_cli.tunnels import TunnelSession


def test_rates_are_payload_deltas_and_history_is_bounded():
    now = [0.0]
    stats = TunnelStats("api", lambda: now[0])
    stats.record({"event": "bytes_up", "bytes": 4096})
    stats.record({"event": "bytes_down", "bytes": 2048})
    for bad in (True, -1, "99", 0):
        stats.record({"event": "bytes_up", "bytes": bad})
    stats.sample()
    now[0] = 2
    stats.sample(0.024)
    assert stats.rates == (2048, 1024)
    assert (stats.sent, stats.received, stats.rtt) == (4096, 2048, 0.024)
    stats.record({"event": "reconnecting"})
    assert stats.sent == 4096
    for latency in (None, "bad", float("inf"), -1, 0):
        stats.sample(latency)
        assert stats.rtt is None
    for n in range(40):
        now[0] += 1
        stats.sample()
        stats.record({"event": "published", "detail": "ready"})
    assert stats.rates == (0, 0)
    assert len(stats.send_history) == 30 and len(stats.events) == 6


def test_counts_states_limits_and_safe_responsive_rendering():
    stats = TunnelStats("api")
    console = Console(file=io.StringIO(), width=80)
    console.print(stats.render("org"))
    assert "No traffic yet" in console.file.getvalue()
    stats.mode, stats.target_port = "publisher", 3000
    stats.update_limits({"limits": {"streams": 32, "bytes_per_second": 1048576}})
    stats.update_limits({"limits": None})
    stats.update_limits({"limits": {"streams": True, "bytes_per_second": -1}})
    stats.record({"event": "connection_opened"})
    stats.record({"event": "connection_closed"})
    stats.record({"event": "connection_rejected", "detail": "quota reached"})
    stats.record({"event": "stream_closed"})
    assert (stats.active, stats.closed, stats.failed) == (0, 1, 2)
    for event in ("listener_started", "listener_stopped", "stopped", "error"):
        stats.record({"event": event, "detail": "[red]literal\x1b\n"})
    stats.record({"event": "protocol_detected"})
    stats.record({"event": "bytes_up", "bytes": 4})
    stats.sample(0.024)
    console.print(stats.render("org"))
    output = console.file.getvalue()
    assert "127.0.0.1:3000" in output and "24 ms" in output
    assert "32 streams" in output and "1.0 MB/s" in output
    assert "[red]literal" in output and "\x1b" not in output
    stats.mode, stats.local_port, stats.target_port = "consumer", 8080, 3000
    narrow = Console(file=io.StringIO(), width=36)
    narrow.print(stats.render("org", 36))
    assert all(len(line) <= 36 for line in narrow.file.getvalue().splitlines())


def test_units_and_chart_zero_spike_and_window():
    assert size(1) == "1.0 B"
    assert size(1024) == "1.0 KB"
    assert size(1024**2) == "1.0 MB"
    assert size(1024**3) == "1.0 GB"
    assert chart(deque(), 10) == ""
    assert chart(deque([0, 0]), 10) == "▁▁"
    assert chart(deque([0, 1, 2]), 2) == "▄█"


def test_publisher_bridge_reports_bytes_and_closes_counts():
    async def run():
        events = []
        session = TunnelSession(SimpleNamespace(), "api", events.append)
        instructions = asyncio.Queue()
        session.m2m = SimpleNamespace(instructions=instructions, MAX_CHANNELS=32,
                                      close_channel=AsyncMock())
        session.session = {"generation": 1}
        writer = Mock()
        async def bridge(*args, event_callback):
            event_callback(SimpleNamespace(kind="bytes_up", bytes_count=17, detail=""))
            event_callback(SimpleNamespace(kind="bytes_down", bytes_count=9, detail=""))
        with patch("dataplicity_cli.tunnels.asyncio.open_connection", new=AsyncMock(return_value=(object(), writer))), \
             patch("dataplicity_cli.tunnels.bridge_tcp_channel", new=bridge), \
             patch("dataplicity_cli.tunnels._close_stream_writer", new=AsyncMock()):
            task = asyncio.create_task(session._serve_publisher(3000))
            await instructions.put({b"port": 1, b"target_port": 3000, b"generation": 1})
            for _ in range(20):
                await asyncio.sleep(0)
                if session.stats.closed:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert (session.stats.sent, session.stats.received) == (17, 9)
        assert (session.stats.active, session.stats.closed) == (0, 1)
    asyncio.run(run())


def test_full_screen_border_stays_fixed_with_events_and_resize():
    stats = TunnelStats("api")
    for width, height in ((120, 30), (80, 24), (36, 12)):
        console = Console(file=io.StringIO(), width=width, height=height)
        for count in (0, 1, 20):
            for _ in range(count):
                stats.record({"event": "reconnecting", "detail": "A long retry message " * 8})
            lines = console.render_lines(stats.render("org", width, height))
            assert len(lines) == height
            assert all(sum(segment.cell_length for segment in line) == width for line in lines)
            assert lines[0][0].text.startswith("╭")
            assert lines[-1][0].text.startswith("╰")
