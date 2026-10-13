"""Shared semantic colours and bounded terminal charts."""
from __future__ import annotations

from rich.text import Text
from rich.theme import Theme

THEME = Theme({
    "success": "green", "warning": "yellow", "error": "bold red",
    "info": "cyan", "muted": "dim", "sending": "cyan",
    "receiving": "magenta", "latency": "yellow", "heading": "bold cyan",
    "table.header": "bold cyan", "table.title": "bold",
})


def status_style(value: str) -> str:
    value = value.lower().strip()
    if value in {"online", "healthy", "ready", "published", "listening", "active", "ok", "success", "completed", "listener started"}:
        return "green"
    if value in {"offline", "error", "failed", "critical", "revoked", "denied", "connection rejected", "stream closed"}:
        return "bold red"
    if value in {"starting", "pending", "reconnecting", "degraded", "warning", "limited", "paused"}:
        return "yellow"
    return "dim"


def plot(values: list, width: int, rows: int, colour: str) -> Text:
    """Peak-preserving columns; missing samples stay gaps, never zero latency."""
    width, rows = max(1, width), max(1, rows)
    # Spread short histories from the left; aggregate long histories by peak.
    columns = []
    for col in range(width):
        start, end = col * len(values) // width, (col + 1) * len(values) // width
        bucket = [v for v in values[start:end] if v is not None]
        columns.append(max(bucket) if bucket else None)
    peak = max((v for v in columns if v is not None), default=0)
    result = Text()
    for row in range(rows - 1, -1, -1):
        for value in columns:
            level = value / peak * rows if value is not None and peak else 0
            fill = max(0, min(8, int((level - row) * 8)))
            result.append(" " if value is None else ("▁▂▃▄▅▆▇█"[fill - 1] if fill else "·"), style=colour)
        if row:
            result.append("\n")
    return result
