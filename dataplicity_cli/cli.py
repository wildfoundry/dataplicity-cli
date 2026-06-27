from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import typer
from rich.console import Console
from rich.live import Live
from rich.prompt import Prompt
from rich.table import Table

from . import __version__
from .api import ApiClient
from .config import Config, default_config_path
from .m2m import M2MClient


app = typer.Typer(
    add_completion=True,
    help=(
        "Dataplicity CLI\n\n"
        "Fast access to auth, devices, and remote operations.\n"
        "Use `dataplicity setup` for guided first-time configuration.\n\n"
        "Examples:\n"
        "  dataplicity setup\n"
        "  dataplicity ls --online-only\n"
        "  dataplicity connect\n"
        "  dataplicity whoami"
    ),
)
auth_app = typer.Typer(help="Authentication commands")
orgs_app = typer.Typer(help="Organisation commands")
devices_app = typer.Typer(help="Device commands")
config_app = typer.Typer(help="Configuration commands")
api_app = typer.Typer(help="Raw API commands")
endpoint_monitors_app = typer.Typer(help="Endpoint monitor commands")
user_impact_app = typer.Typer(help="User impact commands")
heartbeat_monitors_app = typer.Typer(help="Heartbeat monitor commands")
fleet_jobs_app = typer.Typer(help="Fleet job commands")
logging_app = typer.Typer(help="Logging commands", no_args_is_help=True)

LOGGING_MAX_OUTPUT_ITEMS = 200
LOGGING_MAX_FIELD_CHARS = 2000

app.add_typer(auth_app, name="auth")
app.add_typer(orgs_app, name="org")
app.add_typer(devices_app, name="devices")
app.add_typer(config_app, name="config")
app.add_typer(api_app, name="api")
app.add_typer(endpoint_monitors_app, name="endpoint-monitors")
app.add_typer(user_impact_app, name="user-impact")
app.add_typer(heartbeat_monitors_app, name="heartbeat-monitors")
app.add_typer(fleet_jobs_app, name="fleet-jobs")
app.add_typer(logging_app, name="logging")


@dataclass
class AppContext:
    config: Config
    config_path: Path
    console: Console
    json_output: bool
    api: ApiClient


def _ctx(ctx: typer.Context) -> AppContext:
    if not ctx.obj:
        raise typer.Exit(code=1)
    return ctx.obj


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dataplicity-cli {__version__}")
        raise typer.Exit()


SENSITIVE_KEYS = {
    "access",
    "refresh",
    "api_key",
    "apiKey",
    "token",
    "secret",
    "private_key",
    "provisioning_key",
}


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_payload(val)
            for key, val in value.items()
            if key not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(_sanitize_payload(data), indent=2) + "\n")


def _show_error(console: Console, message: str) -> None:
    console.print(f"[red]Error:[/red] {message}")


def _extract_sso_tokens(payload: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else None
    access = tokens.get("access") if tokens else None
    refresh = tokens.get("refresh") if tokens else None
    access = access or payload.get("access") or payload.get("token")
    refresh = refresh or payload.get("refresh")
    return access, refresh


def _decode_json_payload(raw: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _extract_sso_payload_from_query(query: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    payload: Dict[str, Any] = {}
    for key in ("access", "refresh", "code", "state", "token"):
        values = query.get(key)
        if values:
            payload[key] = values[0]
    for key in ("payload", "json", "data"):
        values = query.get(key)
        if not values:
            continue
        nested = _decode_json_payload(values[0])
        if nested:
            payload.update(nested)
            break
    return payload if payload else None


def _extract_sso_payload_from_url(url: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(url)
    query_payload = _extract_sso_payload_from_query(parse_qs(parsed.query, keep_blank_values=True))
    fragment_payload = _extract_sso_payload_from_query(parse_qs(parsed.fragment, keep_blank_values=True))
    if query_payload and fragment_payload:
        merged = dict(query_payload)
        merged.update(fragment_payload)
        return merged
    return query_payload or fragment_payload


def _parse_sso_user_artifact(raw: str) -> Optional[Dict[str, Any]]:
    text = raw.strip()
    if not text:
        return None
    payload = _decode_json_payload(text)
    if payload:
        return payload
    if text.startswith("http://") or text.startswith("https://"):
        return _extract_sso_payload_from_url(text)
    return _extract_sso_payload_from_query(parse_qs(text, keep_blank_values=True))


def _with_callback_hint(url: str, callback_url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    # Prefer standards/common redirect targets when present so the IdP/browser
    # flow can return directly to the loopback listener.
    for redirect_key in ("redirect_uri", "redirect_url", "return_to", "return", "next"):
        if redirect_key in query:
            query[redirect_key] = [callback_url]
            break
    if "cli_callback_url" in query:
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    query["cli_callback_url"] = [callback_url]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


class _SsoCallbackListener:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.callback_url: Optional[str] = None

    def start(self) -> bool:
        payload_queue = self._queue

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover
                return

            def _write_html(self, status: int, message: str) -> None:
                tone = "info"
                if status >= 400:
                    tone = "error"
                elif "received" in message.lower() or "complete" in message.lower():
                    tone = "success"
                badge = {
                    "info": "Waiting",
                    "success": "Success",
                    "error": "Issue",
                }[tone]
                escaped_message = html.escape(message)
                body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dataplicity CLI Authentication</title>
  <style>
    :root {{
      --dp-primary: #1976d2;
      --dp-appbar-bg: linear-gradient(90deg, #2e638c 0%, #5156a2 50%);
      --dp-success: #28a745;
      --dp-error: #dc3545;
      --dp-text: #2d3748;
      --dp-muted: #6c757d;
      --dp-surface: #ffffff;
      --dp-background: #f7f9fb;
      --dp-border: #dee2e6;
      --shadow-md: 0 10px 28px rgba(0, 0, 0, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Lato, "Segoe UI", Arial, sans-serif;
      color: var(--dp-text);
      background:
        radial-gradient(1200px 550px at 5% -10%, rgba(80, 86, 162, 0.2) 0%, rgba(80, 86, 162, 0) 60%),
        radial-gradient(900px 400px at 95% 0%, rgba(25, 118, 210, 0.2) 0%, rgba(25, 118, 210, 0) 65%),
        var(--dp-background);
      display: grid;
      place-items: center;
      padding: 28px;
    }}
    .shell {{
      width: min(720px, 100%);
      border-radius: 16px;
      overflow: hidden;
      border: 1px solid var(--dp-border);
      box-shadow: var(--shadow-md);
      background: var(--dp-surface);
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      background: var(--dp-appbar-bg);
      color: #fff;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-size: 0.93rem;
    }}
    .brand svg {{
      width: 30px;
      height: 30px;
      border-radius: 8px;
      flex: 0 0 auto;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    }}
    .chip {{
      border-radius: 999px;
      font-size: 0.78rem;
      padding: 0.36rem 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border: 1px solid rgba(255, 255, 255, 0.25);
      background: rgba(255, 255, 255, 0.12);
    }}
    .content {{ padding: 30px 26px 24px; }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(1.35rem, 2.5vw, 1.75rem);
      line-height: 1.25;
      font-weight: 700;
    }}
    p {{
      margin: 0;
      color: var(--dp-muted);
      font-size: 1rem;
      line-height: 1.55;
    }}
    .hint {{
      margin-top: 18px;
      display: inline-block;
      border-radius: 10px;
      border: 1px solid var(--dp-border);
      background: #fbfcff;
      padding: 0.7rem 0.9rem;
      color: #4a5568;
      font-size: 0.92rem;
    }}
    .tone-info .status-accent {{ color: var(--dp-primary); }}
    .tone-success .status-accent {{ color: var(--dp-success); }}
    .tone-error .status-accent {{ color: var(--dp-error); }}
  </style>
</head>
<body>
  <main class="shell tone-{tone}">
    <section class="header">
      <div class="brand">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 70.46 70.46" aria-hidden="true">
          <rect fill="#f4b321" x="0" y="0" width="70.46" height="70.46" rx="18" />
          <g transform="translate(6 -2)">
            <circle fill="#f4b321" cx="30.33" cy="40.12" r="30.33" />
            <path d="M57.36,29.8c-3.23-1.55-5.66-3.16-7.46-3.71l-.16-.25A19.83,19.83,0,0,0,40.94,6.07c7.49-2.27,8.61-4.49,8.61-4.49A48.11,48.11,0,0,1,39.9,3.41C44.2,1.35,43.79,0,43.79,0c-2.66,1.63-11.07,2.59-14,2.88a19.81,19.81,0,0,0-19.12,23.4,23.34,23.34,0,1,0,43,12.52c0-.24,0-.47,0-.7l.68.35c5.8,2.78,11.8,2.32,13.4-1S63.16,32.57,57.36,29.8Z" />
            <path fill="#fff" d="M49.53,52.08a19.84,19.84,0,0,0-6.71-21.65,11.07,11.07,0,0,0,2.93-7.77c0-5.44-3.39-9.84-7.58-9.84A6.74,6.74,0,0,0,33,15.5c-1.47-4-4.63-6.83-8.3-6.83-5.07,0-9.18,5.34-9.18,11.92A13.42,13.42,0,0,0,19,30a20,20,0,0,0-3.16,2.85c.68,1.85,1.29,4.39,2.3,7.28,2.36,6.7,6,12.71,2.15,14.07-3,1.06-6.77-1.36-9.39-5.68a19.75,19.75,0,0,0,1.32,5.08,23.33,23.33,0,0,0,37.26-1.49Z" />
            <path d="M24.15,16.58a3.3,3.3,0,0,0-1.12.2,1.45,1.45,0,1,1-2,2,3.31,3.31,0,1,0,3.11-2.19Z" />
            <path d="M37.31,17a3.3,3.3,0,0,0-1.12.2,1.45,1.45,0,1,1-2,2A3.31,3.31,0,1,0,37.31,17Z" />
            <path fill="#f4b321" d="M38.44,26.26S33.67,23.53,31,23.53s-7.45,2.73-7.45,2.73,3.91,5.09,7.45,5.09S38.44,26.26,38.44,26.26Z" />
          </g>
        </svg>
        <span>Dataplicity CLI</span>
      </div>
      <span class="chip">{badge}</span>
    </section>
    <section class="content">
      <h1 class="status-accent">{escaped_message}</h1>
      <p>Finish the sign-in flow in your browser. This tab is only used for the secure callback.</p>
      <p class="hint">You can close this tab and return to your terminal.</p>
    </section>
  </main>
</body>
</html>
""".encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _capture_payload(self) -> Optional[Dict[str, Any]]:
                parsed = urlparse(self.path)
                query_payload = _extract_sso_payload_from_query(parse_qs(parsed.query, keep_blank_values=True))
                if query_payload:
                    return query_payload
                if self.command != "POST":
                    return None
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    return None
                raw = self.rfile.read(length).decode("utf-8", "ignore")
                if "application/json" in (self.headers.get("Content-Type") or ""):
                    return _decode_json_payload(raw)
                form_payload = _extract_sso_payload_from_query(parse_qs(raw, keep_blank_values=True))
                return form_payload

            def do_GET(self) -> None:  # noqa: N802
                payload = self._capture_payload()
                if payload:
                    payload_queue.put(payload)
                    self._write_html(200, "Authentication received.")
                    return
                self._write_html(200, "Waiting for authentication callback.")

            def do_POST(self) -> None:  # noqa: N802
                payload = self._capture_payload()
                if payload:
                    payload_queue.put(payload)
                    self._write_html(200, "Authentication received.")
                    return
                self._write_html(400, "No authentication payload was provided.")

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError:
            return False
        host, port = self._server.server_address[:2]
        self.callback_url = f"http://{host}:{port}/callback"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        return True

    def wait_for_payload(self, timeout_seconds: float) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=1.0)


def _apply_tokens_or_none(state: AppContext, payload: Any) -> bool:
    access, refresh = _extract_sso_tokens(payload)
    if not access:
        return False
    state.config.access_token = access
    state.config.refresh_token = refresh
    state.config.auth_method = "jwt"
    state.config.save(state.config_path)
    return True


def _try_complete_sso_from_code(state: AppContext, code_payload: Dict[str, Any]) -> bool:
    code = code_payload.get("code")
    if not code:
        return False
    body: Dict[str, Any] = {"code": code}
    if code_payload.get("state"):
        body["state"] = code_payload["state"]
    response = state.api.post("/api/auth/sso/complete/", json_data=body)
    if not response.ok:
        return False
    return _apply_tokens_or_none(state, response.data)


def _attempt_sso_auto_complete(
    state: AppContext,
    listener: Optional[_SsoCallbackListener],
    *,
    timeout_seconds: int,
) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 1)
    while time.monotonic() < deadline:
        if listener:
            payload = listener.wait_for_payload(timeout_seconds=1.0)
            if payload:
                if _apply_tokens_or_none(state, payload):
                    return True
                if _try_complete_sso_from_code(state, payload):
                    return True
        response = state.api.get("/api/auth/sso/complete/")
        if response.ok and _apply_tokens_or_none(state, response.data):
            return True
        time.sleep(1.0)
    return False


def _coerce_timeout_seconds(value: Any, default: int = 180) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return default
    return max(timeout, 1)


def _friendly_response_message(default_message: str, response_data: Any, response_text: str) -> str:
    if isinstance(response_data, dict):
        detail = response_data.get("detail") or response_data.get("error") or response_data.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail
        non_field = response_data.get("non_field_errors")
        if isinstance(non_field, list) and non_field:
            return str(non_field[0])
    return response_text or default_message


def _format_rate(bytes_per_second: float) -> str:
    value = float(bytes_per_second)
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB/s"


def _format_bytes(total_bytes: int) -> str:
    value = float(total_bytes)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _sparkline(values: List[float], width: int = 28) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if width <= 0:
        return ""
    if not values:
        return bars[0] * width
    sample = values[-width:]
    if len(sample) < width:
        sample = ([sample[0]] * (width - len(sample))) + sample
    low = min(sample)
    high = max(sample)
    if high <= low:
        return bars[0] * width
    out = []
    for value in sample:
        idx = int((value - low) / (high - low) * (len(bars) - 1))
        idx = max(0, min(idx, len(bars) - 1))
        out.append(bars[idx])
    return "".join(out)


def _extract_port_numbers(value: Any) -> Set[int]:
    found: Set[int] = set()
    if isinstance(value, int):
        if value == -1:
            found.add(-1)
        elif 1 <= value <= 65535:
            found.add(value)
    elif isinstance(value, str):
        text = value.strip()
        if "-" in text:
            left, _, right = text.partition("-")
            try:
                low = int(left.strip())
                high = int(right.strip())
            except ValueError:
                return found
            low, high = min(low, high), max(low, high)
            if 1 <= low <= 65535 and 1 <= high <= 65535:
                size = high - low + 1
                if size <= 4096:
                    found.update(range(low, high + 1))
        else:
            for token in text.replace(",", " ").split():
                try:
                    number = int(token)
                except ValueError:
                    continue
                if number == -1:
                    found.add(-1)
                elif 1 <= number <= 65535:
                    found.add(number)
    elif isinstance(value, dict):
        low = value.get("start") or value.get("min") or value.get("from")
        high = value.get("end") or value.get("max") or value.get("to")
        if isinstance(low, int) and isinstance(high, int):
            low, high = min(low, high), max(low, high)
            if 1 <= low <= 65535 and 1 <= high <= 65535:
                size = high - low + 1
                if size <= 4096:
                    found.update(range(low, high + 1))
    return found


def _extract_port_forwarding_ports(payload: Any) -> Optional[List[int]]:
    if not isinstance(payload, dict):
        return None
    if "port_forwarding_ports" not in payload:
        return None
    value = payload.get("port_forwarding_ports")
    if isinstance(value, list):
        found: Set[int] = set()
        for item in value:
            found.update(_extract_port_numbers(item))
        ports = sorted(port for port in found if port == -1 or 1 <= port <= 65535)
        return ports
    found = _extract_port_numbers(value)
    if not found:
        return []
    return sorted(port for port in found if port == -1 or 1 <= port <= 65535)


def _load_user_profile_payload(state: AppContext) -> Optional[Dict[str, Any]]:
    primary = state.api.get("/api/users/me/")
    if primary.ok and isinstance(primary.data, dict):
        return primary.data
    legacy = state.api.get("/profile/")
    if legacy.ok and isinstance(legacy.data, dict):
        return legacy.data
    return None


def _find_allowed_ports(payload: Any) -> Set[int]:
    allowed: Set[int] = set()
    queue_items: List[Any] = [payload]
    while queue_items:
        current = queue_items.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                key_text = str(key).lower()
                if key_text in {
                    "allowed_ports",
                    "supported_ports",
                    "forwardable_ports",
                    "redirect_ports",
                    "port_whitelist",
                    "ports_allowed",
                    "allowed_port_ranges",
                    "supported_port_ranges",
                } or ("port" in key_text and ("allow" in key_text or "support" in key_text or "entitle" in key_text)):
                    allowed.update(_extract_port_numbers(value))
                    if isinstance(value, list):
                        for item in value:
                            allowed.update(_extract_port_numbers(item))
                if isinstance(value, (dict, list)):
                    queue_items.append(value)
        elif isinstance(current, list):
            queue_items.extend(current)
    return {port for port in allowed if port == -1 or 1 <= port <= 65535}


def _find_plan_label(payload: Any) -> Optional[str]:
    queue_items: List[Any] = [payload]
    while queue_items:
        current = queue_items.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                key_text = str(key).lower()
                if key_text in {"plan", "plan_name", "subscription", "tier", "package"}:
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                    if isinstance(value, dict):
                        name = value.get("name") or value.get("plan_name") or value.get("tier")
                        if isinstance(name, str) and name.strip():
                            return name.strip()
                if isinstance(value, (dict, list)):
                    queue_items.append(value)
        elif isinstance(current, list):
            queue_items.extend(current)
    return None


def _discover_port_forward_capabilities(state: AppContext, device_hash: str) -> Dict[str, Any]:
    sources: List[Any] = []
    profile_payload = _load_user_profile_payload(state)
    if profile_payload:
        sources.append(profile_payload)
    org_response = state.api.get("/api/v1/organisations/")
    if org_response.ok:
        sources.append(org_response.data)
    device_response = state.api.get(f"/api/developer/devices/{device_hash}/")
    if device_response.ok:
        sources.append(device_response.data)
    allowed_ports: Set[int] = set()
    plan_label: Optional[str] = None
    for payload in sources:
        if plan_label is None:
            plan_label = _find_plan_label(payload)
        profile_ports = _extract_port_forwarding_ports(payload)
        if profile_ports is not None:
            allowed_ports.update(profile_ports)
        allowed_ports.update(_find_allowed_ports(payload))
    all_ports_allowed = -1 in allowed_ports
    if all_ports_allowed:
        allowed_ports.discard(-1)
    return {
        "plan_label": plan_label,
        "allowed_ports": sorted(allowed_ports),
        "all_ports_allowed": all_ports_allowed,
        "source_count": len(sources),
    }


@dataclass
class _PortForwardDashboard:
    device_hash: str
    local_port: int
    remote_port: int
    plan_label: Optional[str]
    allowed_ports: List[int]
    all_ports_allowed: bool = False
    started_at: float = field(default_factory=time.monotonic)
    up_total_bytes: int = 0
    down_total_bytes: int = 0
    up_sample_bytes: int = 0
    down_sample_bytes: int = 0
    last_sample_at: float = field(default_factory=time.monotonic)
    up_history: List[float] = field(default_factory=list)
    down_history: List[float] = field(default_factory=list)
    active_clients: int = 0
    total_connections: int = 0
    rejected_connections: int = 0
    current_connection_started_at: Optional[float] = None
    first_byte_latencies_ms: List[float] = field(default_factory=list)
    protocols_seen: List[str] = field(default_factory=list)
    listener_ready: bool = False
    listener_detail: str = ""
    stop_reason: str = "running"

    def apply_event(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        timestamp = float(getattr(event, "timestamp", time.monotonic()))
        bytes_count = int(getattr(event, "bytes_count", 0))
        detail = str(getattr(event, "detail", ""))
        if kind == "listener_started":
            self.listener_ready = True
            self.listener_detail = detail
            return
        if kind == "listener_stopped":
            self.listener_ready = False
            self.stop_reason = "stopped"
            return
        if kind == "connection_opened":
            self.active_clients = 1
            self.total_connections += 1
            self.current_connection_started_at = timestamp
            return
        if kind == "connection_closed":
            self.active_clients = 0
            self.current_connection_started_at = None
            return
        if kind == "connection_rejected":
            self.rejected_connections += 1
            return
        if kind == "first_remote_byte" and self.current_connection_started_at is not None:
            latency_ms = (timestamp - self.current_connection_started_at) * 1000.0
            self.first_byte_latencies_ms.append(latency_ms)
            self.first_byte_latencies_ms = self.first_byte_latencies_ms[-64:]
            return
        if kind == "bytes_up":
            self.up_total_bytes += bytes_count
            self.up_sample_bytes += bytes_count
            return
        if kind == "bytes_down":
            self.down_total_bytes += bytes_count
            self.down_sample_bytes += bytes_count
            return
        if kind == "protocol_detected" and detail:
            if detail not in self.protocols_seen:
                self.protocols_seen.append(detail)
                self.protocols_seen = self.protocols_seen[-8:]

    def tick(self) -> None:
        now = time.monotonic()
        elapsed = max(now - self.last_sample_at, 1e-6)
        self.up_history.append(self.up_sample_bytes / elapsed)
        self.down_history.append(self.down_sample_bytes / elapsed)
        self.up_history = self.up_history[-120:]
        self.down_history = self.down_history[-120:]
        self.up_sample_bytes = 0
        self.down_sample_bytes = 0
        self.last_sample_at = now

    def render(self) -> Table:
        uptime = max(time.monotonic() - self.started_at, 0.0)
        latest_up = self.up_history[-1] if self.up_history else 0.0
        latest_down = self.down_history[-1] if self.down_history else 0.0
        p95 = 0.0
        if self.first_byte_latencies_ms:
            ordered = sorted(self.first_byte_latencies_ms)
            idx = int(0.95 * (len(ordered) - 1))
            p95 = ordered[idx]

        table = Table(title="Port Forward Live", expand=True)
        table.add_column("Metric")
        table.add_column("Value", overflow="fold")
        table.add_row("Path", f"localhost:{self.local_port} -> device:{self.remote_port} ({self.device_hash})")
        table.add_row("Uptime", f"{uptime:.1f}s")
        table.add_row("Status", "listening" if self.listener_ready else self.stop_reason)
        if self.plan_label:
            table.add_row("Plan", self.plan_label)
        if self.all_ports_allowed:
            table.add_row("Allowed remote ports", "All ports allowed")
        elif self.allowed_ports:
            if len(self.allowed_ports) <= 16:
                table.add_row("Allowed remote ports", ", ".join(str(port) for port in self.allowed_ports))
            else:
                preview = ", ".join(str(port) for port in self.allowed_ports[:16])
                table.add_row("Allowed remote ports", f"{preview}, ... ({len(self.allowed_ports)} total)")
        else:
            table.add_row("Allowed remote ports", "Unknown (not advertised by API payload)")
        table.add_row(
            "Connections",
            f"{self.total_connections} opened, {self.active_clients} active, {self.rejected_connections} rejected",
        )
        table.add_row(
            "Traffic",
            f"up { _format_bytes(self.up_total_bytes) } / down { _format_bytes(self.down_total_bytes) }",
        )
        table.add_row(
            "Throughput",
            f"up { _format_rate(latest_up) }  { _sparkline(self.up_history) }\n"
            f"down { _format_rate(latest_down) }  { _sparkline(self.down_history) }",
        )
        table.add_row("First-byte latency (p95)", f"{p95:.1f} ms")
        if self.protocols_seen:
            table.add_row("Detected traffic", ", ".join(self.protocols_seen))
            if "HTTP request" in self.protocols_seen or "HTTP response" in self.protocols_seen:
                table.add_row("HTTP hint", "Use a browser or curl against localhost to validate status and latency.")
            if "TLS" in self.protocols_seen:
                table.add_row("TLS hint", "TLS traffic detected; certificate/SNI issues happen at application layer.")
            if "SSH" in self.protocols_seen:
                table.add_row("SSH hint", "SSH detected; test with `ssh -p <local-port> user@127.0.0.1`.")
        return table


def _is_configured_for_auth(config: Config) -> bool:
    if config.auth_method == "jwt" and bool(config.access_token):
        return True
    if config.auth_method == "api_key" and bool(config.api_key):
        return True
    return False


def _require_auth(ctx: AppContext) -> None:
    if ctx.config.auth_method == "jwt" and ctx.config.access_token:
        return
    if ctx.config.auth_method == "api_key" and ctx.config.api_key:
        return
    message = "Authentication required. Use `dataplicity auth login` or `dataplicity auth api-key`."
    if ctx.json_output:
        _print_json({"ok": False, "detail": message})
    else:
        _show_error(ctx.console, message)
    raise typer.Exit(code=2)


def _extract_devices(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("devices"), list):
        return [item for item in payload["devices"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _device_hash(device: Dict[str, Any]) -> str:
    return str(device.get("hash_id") or device.get("serial") or "").strip()


def _device_name(device: Dict[str, Any]) -> str:
    return str(device.get("name") or "").strip()


def _resolve_device_hash_interactive(
    state: AppContext,
    provided_hash: Optional[str],
    *,
    action_name: str,
) -> str:
    if provided_hash:
        return provided_hash
    if state.json_output:
        message = f"`{action_name}` requires a device hash in --json mode."
        _print_json({"ok": False, "detail": message})
        raise typer.Exit(code=2)
    response = state.api.get("/api/developer/devices/", params={"page_size": 250})
    if not response.ok:
        message = _friendly_response_message("Unable to list devices", response.data, response.text)
        _show_error(state.console, message)
        raise typer.Exit(code=1)
    devices = _extract_devices(response.data or [])
    if not devices:
        _show_error(state.console, "No devices available for this account.")
        raise typer.Exit(code=2)

    devices = sorted(
        devices,
        key=lambda d: (
            not bool(d.get("online")),
            str(d.get("name") or "").lower(),
            _device_hash(d).lower(),
        ),
    )
    table = Table(title=f"Select a device for {action_name}")
    table.add_column("#", style="cyan")
    table.add_column("Serial")
    table.add_column("Name")
    table.add_column("Online")
    for index, device in enumerate(devices, start=1):
        table.add_row(
            str(index),
            _device_hash(device),
            _device_name(device),
            "yes" if bool(device.get("online")) else "no",
        )
    state.console.print(table)
    state.console.print("Tip: pass a device hash directly to skip this prompt.")
    choice = Prompt.ask("Choose a device number", default="1")
    try:
        idx = int(choice)
    except ValueError:
        _show_error(state.console, "Please enter a valid number.")
        raise typer.Exit(code=2)
    if idx < 1 or idx > len(devices):
        _show_error(state.console, f"Please choose a number between 1 and {len(devices)}.")
        raise typer.Exit(code=2)
    return _device_hash(devices[idx - 1])


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show CLI version and exit",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of tables"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config file"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override API base URL"),
) -> None:
    _ = version
    path = config_path or default_config_path()
    config = Config.load(path)
    if base_url:
        config.base_url = base_url.rstrip("/")
    console = Console()
    api_client = ApiClient(config, on_token_update=lambda: config.save(path))
    if config.auth_method == "jwt" and config.refresh_token:
        api_client.refresh_session()
    ctx.obj = AppContext(config=config, config_path=path, console=console, json_output=json_output, api=api_client)


@app.command("setup")
def setup(ctx: typer.Context) -> None:
    """Run a guided first-time setup flow.

    Examples:
      dataplicity setup
      dataplicity --config ./cli.json setup
    """
    state = _ctx(ctx)
    if state.config.auth_method == "jwt" and state.config.access_token:
        if state.json_output:
            _print_json({"ok": True, "detail": "Already logged in", "auth_method": "jwt"})
        else:
            state.console.print("[green]Already logged in.[/green] You can run `dataplicity devices list`.")
        return
    if state.config.auth_method == "api_key" and state.config.api_key:
        if state.json_output:
            _print_json({"ok": True, "detail": "API key already configured", "auth_method": "api_key"})
        else:
            state.console.print("[green]API key already configured.[/green] You can run `dataplicity devices list`.")
        return

    if state.json_output:
        _print_json(
            {
                "ok": False,
                "detail": "Interactive setup is unavailable in --json mode. Use `auth login`, `auth sso`, or `auth api-key`.",
            }
        )
        raise typer.Exit(code=2)

    state.console.print("[bold]Welcome to Dataplicity CLI setup[/bold]")
    state.console.print(f"API host: [blue]{state.config.base_url}[/blue]")
    method = Prompt.ask(
        "How would you like to authenticate?",
        choices=["email-password", "sso", "api-key"],
        default="email-password",
    )
    if method == "api-key":
        auth_api_key_set(ctx)
    elif method == "sso":
        email = typer.prompt("Email")
        auth_sso(ctx, email=email, open_browser=True)
    else:
        email = typer.prompt("Email")
        password = typer.prompt("Password", hide_input=True)
        auth_login(ctx, email=email, password=password, mfa_code=None, mfa_type=None)
    state.console.print("[green]Setup complete.[/green] Try `dataplicity devices list` next.")


@app.command("whoami")
def whoami(ctx: typer.Context) -> None:
    """Show current CLI identity and fleet summary.

    Examples:
      dataplicity whoami
      dataplicity --json whoami
    """
    state = _ctx(ctx)
    _require_auth(state)
    org_response = state.api.get("/api/v1/organisations/")
    devices_response = state.api.get("/api/developer/devices/", params={"page_size": 250})
    profile_payload = _load_user_profile_payload(state)
    profile_ports = _extract_port_forwarding_ports(profile_payload) if profile_payload else None
    profile_all_ports = bool(profile_ports and -1 in profile_ports)
    display_ports = sorted(port for port in (profile_ports or []) if port != -1)
    orgs = _extract_orgs(org_response.data or []) if org_response.ok else []
    devices = _extract_devices(devices_response.data or []) if devices_response.ok else []
    online_count = sum(1 for device in devices if bool(device.get("online")))
    payload = {
        "base_url": state.config.base_url,
        "auth_method": state.config.auth_method,
        "organisation": orgs[0] if orgs else None,
        "devices_total": len(devices),
        "devices_online": online_count,
        "port_forwarding_ports": [-1] if profile_all_ports else display_ports,
    }
    if state.json_output:
        _print_json(payload)
        return
    table = Table(title="Dataplicity CLI status")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("API host", state.config.base_url)
    table.add_row("Auth", str(state.config.auth_method or "none"))
    if orgs:
        org = orgs[0]
        table.add_row("Organisation", str(org.get("name") or "(unnamed)"))
        table.add_row("Organisation hash", str(org.get("hash_id") or org.get("hash") or ""))
    table.add_row("Devices", f"{len(devices)} total / {online_count} online")
    if profile_ports is not None:
        if profile_all_ports:
            table.add_row("Port forwarding ports", "all")
        elif display_ports:
            table.add_row("Port forwarding ports", ", ".join(str(port) for port in display_ports))
        else:
            table.add_row("Port forwarding ports", "(none)")
    state.console.print(table)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Run connectivity and auth diagnostics.

    Examples:
      dataplicity doctor
      dataplicity --json doctor
    """
    state = _ctx(ctx)
    checks: List[Dict[str, Any]] = []
    checks.append({"name": "base_url_configured", "ok": bool(state.config.base_url), "detail": state.config.base_url})

    gateway_probe = state.api.get("/developers/schema/")
    gateway_reachable = gateway_probe.status_code in {200, 401, 403, 404}
    checks.append(
        {
            "name": "gateway_reachable",
            "ok": gateway_reachable,
            "detail": (
                f"status={gateway_probe.status_code}"
                if gateway_probe.status_code
                else (gateway_probe.text or "request failed")
            ),
        }
    )

    auth_configured = _is_configured_for_auth(state.config)
    checks.append(
        {
            "name": "auth_configured",
            "ok": auth_configured,
            "detail": state.config.auth_method or "none",
        }
    )

    if auth_configured:
        org_probe = state.api.get("/api/v1/organisations/")
        checks.append(
            {
                "name": "auth_valid",
                "ok": org_probe.ok,
                "detail": _friendly_response_message(
                    f"status={org_probe.status_code}",
                    org_probe.data,
                    org_probe.text,
                ),
            }
        )
        if org_probe.ok:
            devices_probe = state.api.get("/api/developer/devices/", params={"page_size": 1})
            checks.append(
                {
                    "name": "devices_endpoint",
                    "ok": devices_probe.ok,
                    "detail": _friendly_response_message(
                        f"status={devices_probe.status_code}",
                        devices_probe.data,
                        devices_probe.text,
                    ),
                }
            )

    overall_ok = all(check.get("ok") for check in checks)
    if state.json_output:
        _print_json({"ok": overall_ok, "checks": checks})
        if not overall_ok:
            raise typer.Exit(code=1)
        return

    table = Table(title="Dataplicity CLI doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in checks:
        status_text = "PASS" if check.get("ok") else "FAIL"
        style = "green" if check.get("ok") else "red"
        table.add_row(str(check.get("name") or ""), f"[{style}]{status_text}[/{style}]", str(check.get("detail") or ""))
    state.console.print(table)
    if overall_ok:
        state.console.print("[green]Everything looks good.[/green]")
    else:
        state.console.print("[yellow]Some checks failed.[/yellow] Run `dataplicity setup` or `dataplicity auth status`.")
        raise typer.Exit(code=1)


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Dataplicity API base URL"),
) -> None:
    """Update CLI configuration values.

    Examples:
      dataplicity config set --base-url https://gateway.dataplicity.com
    """
    state = _ctx(ctx)
    if base_url:
        state.config.base_url = base_url.rstrip("/")
    state.config.save(state.config_path)
    if state.json_output:
        _print_json({"ok": True, "base_url": state.config.base_url})
    else:
        state.console.print(f"[green]Saved:[/green] base_url={state.config.base_url}")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show the active CLI configuration.

    Examples:
      dataplicity config show
      dataplicity --json config show
    """
    state = _ctx(ctx)
    payload = {
        "base_url": state.config.base_url,
        "auth_method": state.config.auth_method,
        "has_access_token": bool(state.config.access_token),
        "has_refresh_token": bool(state.config.refresh_token),
        "has_api_key": bool(state.config.api_key),
    }
    if state.json_output:
        _print_json(payload)
        return
    table = Table(title="Dataplicity CLI config")
    table.add_column("Key")
    table.add_column("Value")
    for key, value in payload.items():
        table.add_row(key, str(value))
    state.console.print(table)


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    email: str = typer.Option(..., "--email", prompt=True),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    mfa_code: Optional[str] = typer.Option(None, "--mfa-code"),
    mfa_type: Optional[str] = typer.Option(None, "--mfa-type"),
) -> None:
    """Sign in with email and password.

    Examples:
      dataplicity auth login --email you@example.com
      dataplicity auth login --email you@example.com --mfa-code 123456
    """
    state = _ctx(ctx)
    bootstrap = state.api.post("/api/auth/bootstrap/", json_data={"email": email})
    if bootstrap.ok and isinstance(bootstrap.data, dict) and bootstrap.data.get("status") == "sso_redirect":
        message = "SSO is required for this account. Use `dataplicity auth sso --email ...`."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=3)

    payload: Dict[str, Any] = {"email": email, "password": password}
    if mfa_code:
        payload["mfa_code"] = mfa_code
    if mfa_type:
        payload["mfa_type"] = mfa_type

    response = state.api.post("/api/token/", json_data=payload)
    if not response.ok:
        message = _friendly_response_message("Login failed. Check your credentials and try again.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    access = None
    refresh = None
    if isinstance(response.data, dict):
        access = response.data.get("access")
        refresh = response.data.get("refresh")
    if not access:
        message = "Login succeeded but no access token was returned."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    state.config.access_token = access
    state.config.refresh_token = refresh
    state.config.auth_method = "jwt"
    state.config.save(state.config_path)
    if state.json_output:
        _print_json({"ok": True, "detail": "Logged in"})
    else:
        state.console.print("[green]Logged in.[/green]")


@auth_app.command("sso")
def auth_sso(
    ctx: typer.Context,
    email: str = typer.Option(..., "--email", prompt=True),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser", help="Open SSO login in the browser"),
    timeout: int = typer.Option(180, "--timeout", help="Seconds to wait for automatic browser callback"),
) -> None:
    """Start SSO login flow for your email.

    Examples:
      dataplicity auth sso --email you@example.com
      dataplicity auth sso --email you@example.com --no-open-browser
      dataplicity auth sso --email you@example.com --timeout 300
    """
    state = _ctx(ctx)
    timeout_seconds = _coerce_timeout_seconds(timeout)
    response = state.api.post("/api/auth/bootstrap/", json_data={"email": email})
    if not response.ok:
        message = _friendly_response_message("Unable to start SSO.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    if not isinstance(response.data, dict) or response.data.get("status") != "sso_redirect":
        message = "SSO is not enabled for this account."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)

    redirect_url = response.data.get("redirect_url")
    if not redirect_url:
        message = "SSO redirect URL missing."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    listener: Optional[_SsoCallbackListener] = None
    browser_url = redirect_url
    if open_browser:
        listener = _SsoCallbackListener()
        if listener.start() and listener.callback_url:
            browser_url = _with_callback_hint(redirect_url, listener.callback_url)
            if not state.json_output:
                state.console.print(f"Listening for SSO callback on [blue]{listener.callback_url}[/blue]")
        else:
            listener = None
        webbrowser.open(browser_url)

    if not state.json_output:
        state.console.print("Waiting for browser sign-in to complete...")
    try:
        if _attempt_sso_auto_complete(state, listener, timeout_seconds=timeout_seconds):
            if state.json_output:
                _print_json({"ok": True, "detail": "SSO login complete"})
            else:
                state.console.print("[green]SSO login complete.[/green]")
            return
    finally:
        if listener:
            listener.stop()

    sso_complete_url = state.api._build_url("/api/auth/sso/complete/")
    if state.json_output:
        _print_json(
            {
                "ok": False,
                "detail": "Automatic SSO completion timed out. Complete sign-in in your browser and run `dataplicity auth sso` again.",
                "sso_complete_url": sso_complete_url,
            }
        )
        raise typer.Exit(code=2)

    state.console.print("[yellow]Automatic callback did not complete in time.[/yellow]")
    state.console.print("Complete SSO in your browser, then open:")
    state.console.print(f"[blue]{sso_complete_url}[/blue]")
    state.console.print("Paste either the final browser URL, query string, or JSON payload below.")

    raw = typer.prompt("SSO response")
    payload = _parse_sso_user_artifact(raw)
    if payload is None:
        _show_error(state.console, "Could not parse SSO response.")
        raise typer.Exit(code=1)
    if not _apply_tokens_or_none(state, payload) and not _try_complete_sso_from_code(state, payload):
        _show_error(state.console, "No access token found in payload.")
        raise typer.Exit(code=1)
    state.console.print("[green]SSO login complete.[/green]")


@auth_app.command("api-key")
def auth_api_key_set(ctx: typer.Context) -> None:
    """Store an organisation API key for CLI use.

    Examples:
      dataplicity auth api-key
    """
    state = _ctx(ctx)
    api_key = typer.prompt("API key", hide_input=True)
    state.config.api_key = api_key.strip()
    state.config.auth_method = "api_key"
    state.config.save(state.config_path)
    if state.json_output:
        _print_json({"ok": True, "detail": "API key saved"})
    else:
        state.console.print("[green]API key saved.[/green]")


@auth_app.command("logout")
def auth_logout(ctx: typer.Context) -> None:
    """Clear saved authentication credentials.

    Examples:
      dataplicity auth logout
    """
    state = _ctx(ctx)
    state.config.clear_tokens()
    state.config.clear_api_key()
    state.config.save(state.config_path)
    if state.json_output:
        _print_json({"ok": True, "detail": "Logged out"})
    else:
        state.console.print("[green]Logged out.[/green]")


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Show saved auth state (without secrets).

    Examples:
      dataplicity auth status
      dataplicity --json auth status
    """
    state = _ctx(ctx)
    payload = {
        "auth_method": state.config.auth_method,
        "has_access_token": bool(state.config.access_token),
        "has_refresh_token": bool(state.config.refresh_token),
        "has_api_key": bool(state.config.api_key),
    }
    if state.json_output:
        _print_json(payload)
        return
    table = Table(title="Auth status")
    table.add_column("Key")
    table.add_column("Value")
    for key, value in payload.items():
        table.add_row(key, str(value))
    state.console.print(table)


def _extract_orgs(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "organisations", "organizations"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


@orgs_app.command("show")
def orgs_show(ctx: typer.Context) -> None:
    """Show your primary organisation details.

    Examples:
      dataplicity org show
      dataplicity --json org show
    """
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get("/api/v1/organisations/")
    if not response.ok:
        message = _friendly_response_message("Unable to fetch organisation.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    orgs = _extract_orgs(response.data or [])
    if not orgs:
        message = "No organisation found for this user."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)

    org = orgs[0]
    if state.json_output:
        _print_json(org)
        return

    table = Table(title="Organisation")
    table.add_column("Hash")
    table.add_column("Name")
    table.add_row(str(org.get("hash_id") or org.get("hash") or ""), str(org.get("name") or ""))
    state.console.print(table)
    if len(orgs) > 1:
        state.console.print(f"[yellow]Warning:[/yellow] API returned {len(orgs)} organisations; showing the first.")


@devices_app.command("list")
def devices_list(
    ctx: typer.Context,
    search: Optional[str] = typer.Option(None, "--search", help="Search term"),
    page_size: int = typer.Option(250, "--page-size", help="Page size"),
    online_only: bool = typer.Option(False, "--online-only", help="Show only online devices"),
) -> None:
    """List devices in your fleet.

    Examples:
      dataplicity devices list
      dataplicity devices list --online-only
      dataplicity devices list --search edge
    """
    state = _ctx(ctx)
    _require_auth(state)
    params: Dict[str, Any] = {"page_size": page_size}
    if search:
        params["search"] = search
    response = state.api.get("/api/developer/devices/", params=params)
    if not response.ok:
        message = _friendly_response_message("Unable to list devices.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    payload = response.data or []
    if state.json_output:
        _print_json(payload)
        return

    devices = _extract_devices(payload)

    def _is_online(device: Dict[str, Any]) -> bool:
        return str(device.get("status") or "").strip().lower() == "online"

    if online_only:
        devices = [device for device in devices if _is_online(device)]
    devices = sorted(
        devices,
        key=lambda d: (
            not _is_online(d),
            str(d.get("name") or "").lower(),
            _device_hash(d).lower(),
        ),
    )
    online_count = sum(1 for device in devices if _is_online(device))

    table = Table(title="Devices")
    table.add_column("Serial", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Class")
    for device in devices:
        status = str(device.get("status") or "").strip()
        device_class = (
            str((device.get("device_class") or {}).get("name") or "").strip()
            if isinstance(device.get("device_class"), dict)
            else ""
        )
        table.add_row(
            _device_hash(device),
            _device_name(device),
            status,
            device_class,
            style="green" if _is_online(device) else "",
        )
    state.console.print(table)
    state.console.print(f"Showing {len(devices)} devices ({online_count} online).")


@devices_app.command("ls")
def devices_ls(
    ctx: typer.Context,
    search: Optional[str] = typer.Option(None, "--search", help="Search term"),
    page_size: int = typer.Option(250, "--page-size", help="Page size"),
    online_only: bool = typer.Option(False, "--online-only", help="Show only online devices"),
) -> None:
    """Alias for `devices list`.

    Examples:
      dataplicity devices ls
      dataplicity devices ls --online-only
    """
    devices_list(ctx, search=search, page_size=page_size, online_only=online_only)


@devices_app.command("show")
def devices_show(ctx: typer.Context, device_hash: Optional[str] = typer.Argument(None)) -> None:
    """Show detailed metadata for a single device.

    Examples:
      dataplicity devices show
      dataplicity devices show <device-hash>
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="show")
    response = state.api.get(f"/api/developer/devices/{resolved_hash}/")
    if not response.ok:
        message = _friendly_response_message("Unable to fetch device.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data)
        return
    table = Table(title=f"Device {resolved_hash}")
    table.add_column("Key")
    table.add_column("Value")
    if isinstance(response.data, dict):
        for key, value in response.data.items():
            table.add_row(str(key), json.dumps(_sanitize_payload(value)))
    state.console.print(table)


@devices_app.command("reboot")
def devices_reboot(ctx: typer.Context, device_hash: Optional[str] = typer.Argument(None)) -> None:
    """Reboot a device.

    Examples:
      dataplicity devices reboot
      dataplicity devices reboot <device-hash>
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="reboot")
    payload = {"command": "restart", "params": {}}
    response = state.api.post(f"/api/developer/devices/{resolved_hash}/execute_command/", json_data=payload)
    if not response.ok:
        message = _friendly_response_message("Unable to reboot device.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data or {"ok": True})
    else:
        state.console.print(f"[green]Reboot command sent to {resolved_hash}.[/green]")


@devices_app.command("provisioning-key")
def devices_provisioning_key(
    ctx: typer.Context,
    output: Optional[Path] = typer.Option(
        None, "--output", help="Write provisioning details to a file"
    ),
) -> None:
    """Fetch provisioning details for developer onboarding.

    Examples:
      dataplicity devices provisioning-key --output ./provisioning.json
    """
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get("/api/developer/devices/provisioning-key/")
    if not response.ok:
        message = _friendly_response_message("Unable to fetch provisioning details.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(response.data or {}, indent=2), encoding="utf-8")
        if state.json_output:
            _print_json({"ok": True, "detail": f"Written to {output}"})
        else:
            state.console.print(f"[green]Written to {output}[/green]")
        return
    message = "Provisioning details retrieved. Use --output to write them to a file."
    if state.json_output:
        _print_json({"ok": True, "detail": message})
    else:
        state.console.print(message)


async def _resolve_m2m_url(state: AppContext, device_hash: str) -> str:
    response = state.api.get(f"/api/developer/devices/{device_hash}/host/")
    if not response.ok or not isinstance(response.data, dict):
        detail = _friendly_response_message("Remote Access host lookup failed.", response.data, response.text)
        raise RuntimeError(detail)
    m2m_url = response.data.get("m2m_url")
    if not m2m_url:
        raise RuntimeError("Remote Access host missing m2m_url")
    ws_base = m2m_url.replace("https://", "wss://").replace("http://", "ws://")
    joiner = "&" if "?" in ws_base else "?"
    return f"{ws_base}{joiner}device={device_hash}"


@devices_app.command("terminal")
def devices_terminal(ctx: typer.Context, device_hash: Optional[str] = typer.Argument(None)) -> None:
    """Open an interactive terminal session to a device.

    Examples:
      dataplicity devices terminal
      dataplicity devices terminal <device-hash>
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="terminal")

    async def runner() -> None:
        from .remote_access import run_terminal_session

        ws_url = await _resolve_m2m_url(state, resolved_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        identity = await m2m.wait_for_identity()
        response = state.api.post(
            f"/api/developer/devices/{resolved_hash}/ports/",
            json_data={"m2m_identity": identity, "service": "terminal"},
        )
        if not response.ok:
            raise RuntimeError(response.text or "Unable to open terminal")
        channel_port = await m2m.wait_for_channel_open()
        await run_terminal_session(m2m, channel_port)
        await m2m.close()

    try:
        asyncio.run(runner())
    except Exception as exc:
        message = str(exc) or "Terminal session failed"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)


@devices_app.command("port-forward")
def devices_port_forward(
    ctx: typer.Context,
    device_hash: Optional[str] = typer.Argument(None),
    remote_port: int = typer.Option(..., "--remote-port", help="Remote device port"),
    local_port: int = typer.Option(..., "--local-port", help="Local listen port"),
) -> None:
    """Forward a local port to a remote device port with live metrics.

    Examples:
      dataplicity devices port-forward --remote-port 22 --local-port 2022
      dataplicity devices port-forward <device-hash> --remote-port 80 --local-port 8080
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="port-forward")
    capabilities = _discover_port_forward_capabilities(state, resolved_hash)
    allowed_ports = capabilities.get("allowed_ports") or []
    all_ports_allowed = bool(capabilities.get("all_ports_allowed"))
    plan_label = capabilities.get("plan_label")
    if (not all_ports_allowed) and allowed_ports and remote_port not in allowed_ports:
        message = (
            f"Remote port {remote_port} is not allowed for this account plan."
            f" Allowed ports: {', '.join(str(port) for port in allowed_ports[:32])}"
        )
        if len(allowed_ports) > 32:
            message += f", ... ({len(allowed_ports)} total)"
        if state.json_output:
            _print_json(
                {
                    "ok": False,
                    "detail": message,
                    "plan": plan_label,
                    "allowed_ports": allowed_ports,
                }
            )
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)

    async def runner() -> Dict[str, Any]:
        from .remote_access import run_port_forward

        dashboard = _PortForwardDashboard(
            device_hash=resolved_hash,
            local_port=local_port,
            remote_port=remote_port,
            plan_label=plan_label,
            allowed_ports=allowed_ports,
            all_ports_allowed=all_ports_allowed,
        )
        ws_url = await _resolve_m2m_url(state, resolved_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        try:
            identity = await m2m.wait_for_identity()
            response = state.api.post(
                f"/api/developer/devices/{resolved_hash}/ports/",
                json_data={
                    "m2m_identity": identity,
                    "service": "redirect-port",
                    "port": remote_port,
                },
            )
            if not response.ok:
                detail = _friendly_response_message("Unable to open port redirect", response.data, response.text)
                raise RuntimeError(detail)
            channel_port = await m2m.wait_for_channel_open()
            forward_task = asyncio.create_task(
                run_port_forward(
                    m2m,
                    channel_port,
                    local_port,
                    event_callback=dashboard.apply_event,
                )
            )
            if state.json_output:
                await forward_task
            else:
                with Live(dashboard.render(), console=state.console, refresh_per_second=4, transient=False) as live:
                    while not forward_task.done():
                        await asyncio.sleep(0.5)
                        dashboard.tick()
                        live.update(dashboard.render())
                    dashboard.tick()
                    live.update(dashboard.render())
                await forward_task
            return {
                "ok": True,
                "device_hash": resolved_hash,
                "remote_port": remote_port,
                "local_port": local_port,
                "plan": plan_label,
                "allowed_ports": allowed_ports,
                "all_ports_allowed": all_ports_allowed,
                "bytes_up": dashboard.up_total_bytes,
                "bytes_down": dashboard.down_total_bytes,
                "connections": dashboard.total_connections,
                "rejected_connections": dashboard.rejected_connections,
                "protocols": dashboard.protocols_seen,
            }
        finally:
            await m2m.close()

    try:
        result = asyncio.run(runner())
    except KeyboardInterrupt:
        if state.json_output:
            _print_json({"ok": True, "detail": "Port forward stopped by user"})
        else:
            state.console.print("[yellow]Port forward stopped (Ctrl-C).[/yellow]")
        raise typer.Exit(code=0)
    except Exception as exc:
        message = str(exc) or "Port forward failed"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(result)


@devices_app.command("remote-file")
def devices_remote_file(
    ctx: typer.Context,
    device_hash: Optional[str] = typer.Argument(None),
    path: str = typer.Option(..., "--path", help="Remote file path"),
    output: Optional[Path] = typer.Option(None, "--output", help="Write file content to a path"),
    stdout: bool = typer.Option(False, "--stdout", help="Write file content to stdout"),
) -> None:
    """Read a remote file via secure relay.

    Examples:
      dataplicity devices remote-file --path /etc/os-release --stdout
      dataplicity devices remote-file <device-hash> --path /var/log/syslog --output ./syslog.txt
    """
    state = _ctx(ctx)
    _require_auth(state)
    if output and stdout:
        message = "Use either --output or --stdout (not both)."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if not output and not stdout:
        message = "Specify --output or --stdout to receive file content."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="remote-file")

    async def runner() -> int:
        from .remote_access import run_remote_file

        ws_url = await _resolve_m2m_url(state, resolved_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        identity = await m2m.wait_for_identity()
        response = state.api.post(
            f"/api/developer/devices/{resolved_hash}/ports/",
            json_data={
                "m2m_identity": identity,
                "service": "remote-file",
                "path": path,
            },
        )
        if not response.ok:
            raise RuntimeError(response.text or "Unable to open remote file")
        channel_port = await m2m.wait_for_channel_open()
        bytes_written = await run_remote_file(
            m2m,
            channel_port,
            str(output) if output else None,
            allow_stdout=stdout,
        )
        await m2m.close()
        return bytes_written

    try:
        bytes_written = asyncio.run(runner())
    except Exception as exc:
        message = str(exc) or "Remote file failed"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    if state.json_output:
        _print_json({"ok": True, "bytes": bytes_written, "output": str(output) if output else None})
    else:
        state.console.print(f"[green]Received {bytes_written} bytes.[/green]")


@devices_app.command("connect")
def devices_connect(ctx: typer.Context, device_hash: Optional[str] = typer.Argument(None)) -> None:
    """Alias for `devices terminal`.

    Examples:
      dataplicity devices connect
      dataplicity devices connect <device-hash>
    """
    devices_terminal(ctx, device_hash=device_hash)


@devices_app.command("run")
def devices_run(
    ctx: typer.Context,
    device_hash: Optional[str] = typer.Argument(None),
    command: str = typer.Option(..., "--command", "-c", help="Single shell command to run"),
    timeout: int = typer.Option(30, "--timeout", min=1, help="Seconds before command times out"),
    no_timeout: bool = typer.Option(
        False,
        "--no-timeout",
        help="Disable timeout and wait until command completes",
    ),
) -> None:
    """Run a single command on a device and print output.

    Examples:
      dataplicity devices run --command "uname -a"
      dataplicity devices run <device-hash> --command "systemctl status dataplicity"
      dataplicity devices run <device-hash> --command "tail -f /var/log/syslog" --no-timeout
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="run command")
    timeout_seconds: Optional[float] = None if no_timeout else float(timeout)

    async def runner() -> str:
        from .remote_access import run_single_command

        ws_url = await _resolve_m2m_url(state, resolved_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        try:
            identity = await m2m.wait_for_identity()
            response = state.api.post(
                f"/api/developer/devices/{resolved_hash}/ports/",
                json_data={"m2m_identity": identity, "service": "terminal"},
            )
            if not response.ok:
                raise RuntimeError(response.text or "Unable to open terminal")
            channel_port = await m2m.wait_for_channel_open()
            output_bytes = await run_single_command(
                m2m,
                channel_port,
                command,
                timeout_seconds=timeout_seconds,
            )
            return output_bytes.decode("utf-8", "replace")
        finally:
            await m2m.close()

    try:
        output = asyncio.run(runner())
    except Exception as exc:
        message = str(exc) or "Remote command failed"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    if state.json_output:
        _print_json(
            {
                "ok": True,
                "device_hash": resolved_hash,
                "command": command,
                "timeout": None if no_timeout else timeout,
                "output": output,
            }
        )
    else:
        sys.stdout.write(output)
        if output and not output.endswith("\n"):
            sys.stdout.write("\n")


@app.command("ls")
def top_level_ls(
    ctx: typer.Context,
    search: Optional[str] = typer.Option(None, "--search", help="Search term"),
    page_size: int = typer.Option(250, "--page-size", help="Page size"),
    online_only: bool = typer.Option(False, "--online-only", help="Show only online devices"),
) -> None:
    """Shortcut for `devices list`.

    Examples:
      dataplicity ls
      dataplicity ls --online-only
    """
    devices_list(ctx, search=search, page_size=page_size, online_only=online_only)


@app.command("connect")
def top_level_connect(ctx: typer.Context, device_hash: Optional[str] = typer.Argument(None)) -> None:
    """Shortcut for `devices terminal`.

    Examples:
      dataplicity connect
      dataplicity connect <device-hash>
    """
    devices_terminal(ctx, device_hash=device_hash)


def _parse_kv_pairs(items: Optional[List[str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        payload[key] = value
    return payload


def _parse_json_payload_or_exit(state: AppContext, data: str) -> Dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        message = "Invalid JSON payload."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if not isinstance(payload, dict):
        message = "JSON payload must be an object."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    return payload


def _extract_objects(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _resource_key(item: Dict[str, Any]) -> str:
    for key in ("hash_id", "id", "uuid", "slug", "name"):
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _resource_name(item: Dict[str, Any]) -> str:
    for key in ("name", "title", "label", "endpoint"):
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _resource_status(item: Dict[str, Any]) -> str:
    for key in ("status", "state", "health", "severity"):
        value = item.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _render_resource_table(state: AppContext, title: str, payload: Any) -> None:
    rows = _extract_objects(payload)
    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Status")
    if not rows:
        state.console.print(table)
        state.console.print("No results.")
        return
    for row in rows:
        table.add_row(_resource_key(row), _resource_name(row), _resource_status(row))
    state.console.print(table)
    state.console.print(f"Showing {len(rows)} results.")


def _list_resource(
    state: AppContext,
    *,
    endpoint: str,
    title: str,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    _require_auth(state)
    response = state.api.get(endpoint, params=params)
    if not response.ok:
        message = _friendly_response_message(f"Unable to list {title.lower()}.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data or [])
        return
    _render_resource_table(state, title, response.data or [])


def _show_resource(
    state: AppContext,
    *,
    endpoint: str,
    resource_id: str,
    not_found_label: str,
) -> None:
    _require_auth(state)
    response = state.api.get(f"{endpoint}{resource_id}/")
    if not response.ok:
        message = _friendly_response_message(f"Unable to fetch {not_found_label}.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data or {})
        return
    table = Table(title=f"{not_found_label.title()} {resource_id}")
    table.add_column("Key")
    table.add_column("Value")
    if isinstance(response.data, dict):
        for key, value in response.data.items():
            table.add_row(str(key), json.dumps(_sanitize_payload(value)))
    state.console.print(table)


def _create_resource(state: AppContext, *, endpoint: str, data: str, label: str) -> None:
    _require_auth(state)
    payload = _parse_json_payload_or_exit(state, data)
    response = state.api.post(endpoint, json_data=payload)
    if not response.ok:
        message = _friendly_response_message(f"Unable to create {label}.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data or {"ok": True})
    else:
        state.console.print(f"[green]{label.title()} created.[/green]")


def _delete_resource(state: AppContext, *, endpoint: str, resource_id: str, label: str) -> None:
    _require_auth(state)
    response = state.api.request("DELETE", f"{endpoint}{resource_id}/")
    if not response.ok:
        message = _friendly_response_message(f"Unable to delete {label}.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data if response.data is not None else {"ok": True})
    else:
        state.console.print(f"[green]{label.title()} deleted.[/green]")


@endpoint_monitors_app.command("list")
def endpoint_monitors_list(
    ctx: typer.Context,
    page_size: int = typer.Option(100, "--page-size", help="Page size"),
    search: Optional[str] = typer.Option(None, "--search", help="Search term"),
) -> None:
    """List endpoint monitors.

    Examples:
      dataplicity endpoint-monitors list
      dataplicity endpoint-monitors list --search production
    """
    params: Dict[str, Any] = {"page_size": page_size}
    if search:
        params["search"] = search
    _list_resource(_ctx(ctx), endpoint="/api/developer/endpoint-monitors/", title="Endpoint monitors", params=params)


@endpoint_monitors_app.command("show")
def endpoint_monitors_show(ctx: typer.Context, monitor_id: str = typer.Argument(...)) -> None:
    """Show an endpoint monitor.

    Examples:
      dataplicity endpoint-monitors show <monitor-id>
    """
    _show_resource(_ctx(ctx), endpoint="/api/developer/endpoint-monitors/", resource_id=monitor_id, not_found_label="endpoint monitor")


@endpoint_monitors_app.command("create")
def endpoint_monitors_create(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="JSON payload"),
) -> None:
    """Create an endpoint monitor.

    Examples:
      dataplicity endpoint-monitors create --data '{"name":"api-check","url":"https://example.com/health"}'
    """
    _create_resource(_ctx(ctx), endpoint="/api/developer/endpoint-monitors/", data=data, label="endpoint monitor")


@endpoint_monitors_app.command("delete")
def endpoint_monitors_delete(ctx: typer.Context, monitor_id: str = typer.Argument(...)) -> None:
    """Delete an endpoint monitor.

    Examples:
      dataplicity endpoint-monitors delete <monitor-id>
    """
    _delete_resource(_ctx(ctx), endpoint="/api/developer/endpoint-monitors/", resource_id=monitor_id, label="endpoint monitor")


@user_impact_app.command("list")
def user_impact_list(
    ctx: typer.Context,
    page_size: int = typer.Option(100, "--page-size", help="Page size"),
    unresolved_only: bool = typer.Option(False, "--unresolved-only", help="Show unresolved impact items"),
) -> None:
    """List user impact items.

    Examples:
      dataplicity user-impact list
      dataplicity user-impact list --unresolved-only
    """
    params: Dict[str, Any] = {"page_size": page_size}
    if unresolved_only:
        params["resolved"] = "false"
    _list_resource(_ctx(ctx), endpoint="/api/developer/user-impact/", title="User impact", params=params)


@user_impact_app.command("show")
def user_impact_show(ctx: typer.Context, impact_id: str = typer.Argument(...)) -> None:
    """Show a user impact item.

    Examples:
      dataplicity user-impact show <impact-id>
    """
    _show_resource(_ctx(ctx), endpoint="/api/developer/user-impact/", resource_id=impact_id, not_found_label="user impact item")


@heartbeat_monitors_app.command("list")
def heartbeat_monitors_list(
    ctx: typer.Context,
    page_size: int = typer.Option(100, "--page-size", help="Page size"),
    search: Optional[str] = typer.Option(None, "--search", help="Search term"),
) -> None:
    """List heartbeat monitors.

    Examples:
      dataplicity heartbeat-monitors list
      dataplicity heartbeat-monitors list --search cron
    """
    params: Dict[str, Any] = {"page_size": page_size}
    if search:
        params["search"] = search
    _list_resource(_ctx(ctx), endpoint="/api/developer/heartbeat-monitors/", title="Heartbeat monitors", params=params)


@heartbeat_monitors_app.command("show")
def heartbeat_monitors_show(ctx: typer.Context, monitor_id: str = typer.Argument(...)) -> None:
    """Show a heartbeat monitor.

    Examples:
      dataplicity heartbeat-monitors show <monitor-id>
    """
    _show_resource(
        _ctx(ctx),
        endpoint="/api/developer/heartbeat-monitors/",
        resource_id=monitor_id,
        not_found_label="heartbeat monitor",
    )


@heartbeat_monitors_app.command("create")
def heartbeat_monitors_create(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="JSON payload"),
) -> None:
    """Create a heartbeat monitor.

    Examples:
      dataplicity heartbeat-monitors create --data '{"name":"daily-job","interval_seconds":86400}'
    """
    _create_resource(_ctx(ctx), endpoint="/api/developer/heartbeat-monitors/", data=data, label="heartbeat monitor")


@heartbeat_monitors_app.command("delete")
def heartbeat_monitors_delete(ctx: typer.Context, monitor_id: str = typer.Argument(...)) -> None:
    """Delete a heartbeat monitor.

    Examples:
      dataplicity heartbeat-monitors delete <monitor-id>
    """
    _delete_resource(_ctx(ctx), endpoint="/api/developer/heartbeat-monitors/", resource_id=monitor_id, label="heartbeat monitor")


@fleet_jobs_app.command("list")
def fleet_jobs_list(
    ctx: typer.Context,
    page_size: int = typer.Option(100, "--page-size", help="Page size"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
) -> None:
    """List fleet jobs.

    Examples:
      dataplicity fleet-jobs list
      dataplicity fleet-jobs list --status running
    """
    params: Dict[str, Any] = {"page_size": page_size}
    if status:
        params["status"] = status
    _list_resource(_ctx(ctx), endpoint="/api/developer/fleet-jobs/", title="Fleet jobs", params=params)


@fleet_jobs_app.command("show")
def fleet_jobs_show(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Show a fleet job.

    Examples:
      dataplicity fleet-jobs show <job-id>
    """
    _show_resource(_ctx(ctx), endpoint="/api/developer/fleet-jobs/", resource_id=job_id, not_found_label="fleet job")


@fleet_jobs_app.command("run")
def fleet_jobs_run(
    ctx: typer.Context,
    data: str = typer.Option(..., "--data", help="JSON payload"),
) -> None:
    """Create and start a fleet job.

    Examples:
      dataplicity fleet-jobs run --data '{"name":"restart-edge","device_hashes":["abc123"],"command":"restart"}'
    """
    _create_resource(_ctx(ctx), endpoint="/api/developer/fleet-jobs/", data=data, label="fleet job")


@fleet_jobs_app.command("cancel")
def fleet_jobs_cancel(ctx: typer.Context, job_id: str = typer.Argument(...)) -> None:
    """Cancel a fleet job.

    Examples:
      dataplicity fleet-jobs cancel <job-id>
    """
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.post(f"/api/developer/fleet-jobs/{job_id}/cancel/")
    if not response.ok:
        message = _friendly_response_message("Unable to cancel fleet job.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data if response.data is not None else {"ok": True})
    else:
        state.console.print("[green]Fleet job cancellation requested.[/green]")


@logging_app.command("list")
def logging_list(
    ctx: typer.Context,
    page_size: int = typer.Option(200, "--page-size", help="Number of records to request (max 200)"),
    device_hash: Optional[str] = typer.Option(None, "--device", help="Filter by device hash"),
    level: Optional[str] = typer.Option(None, "--level", help="Filter by log level"),
    path: Optional[str] = typer.Option(None, "--path", help="Filter by web-style path scope (for example /devices/<hash>)"),
    search: Optional[str] = typer.Option(None, "--search", help="Free-text search term"),
    since: str = typer.Option("1h", "--since", help="Start of time window (relative like 15m, 2h, 1d, or ISO timestamp)"),
    until: Optional[str] = typer.Option(None, "--until", help="End of time window (relative or ISO timestamp)"),
    all_scopes: bool = typer.Option(
        False,
        "--all-scopes",
        help="Allow broad cross-fleet queries. Requires a bounded time window.",
    ),
) -> None:
    """List log events with safe query constraints.

    Examples:
      dataplicity logging list --device <device-hash> --since 4h
      dataplicity logging list --path /devices/<device-hash> --level error --since 24h
      dataplicity logging list --all-scopes --search timeout --since 30m
    """
    state = _ctx(ctx)
    max_page_size = LOGGING_MAX_OUTPUT_ITEMS
    if page_size < 1 or page_size > max_page_size:
        message = f"--page-size must be between 1 and {max_page_size}."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)
    if not any([device_hash, level, path, search, all_scopes]):
        message = (
            "Refusing broad log query. Provide at least one scope filter "
            "(--device, --path, --search, or --level), or pass --all-scopes."
        )
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        since_iso = _parse_log_time_expr(since, now=now)
        until_iso = _parse_log_time_expr(until, now=now) if until else None
    except ValueError as exc:
        if state.json_output:
            _print_json({"ok": False, "detail": str(exc)})
        else:
            _show_error(state.console, str(exc))
        raise typer.Exit(code=2)
    if until_iso and until_iso < since_iso:
        message = "--until must be greater than or equal to --since."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=2)
    _require_auth(state)
    params: Dict[str, Any] = {"page_size": page_size, "since": since_iso}
    if until_iso:
        params["until"] = until_iso
    if device_hash:
        params["device"] = device_hash
    if level:
        params["level"] = level
    if path:
        params["path"] = path
    if search:
        params["search"] = search
    response = state.api.get("/api/developer/logging/", params=params)
    if not response.ok:
        message = _friendly_response_message("Unable to list logging.", response.data, response.text)
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    safe_payload, was_truncated, dropped_items = _truncate_logging_payload(response.data or [])
    if state.json_output:
        _print_json(safe_payload)
        return
    _render_resource_table(state, "Logging", safe_payload)
    if was_truncated:
        state.console.print(
            f"[yellow]Output truncated:[/yellow] showing first {LOGGING_MAX_OUTPUT_ITEMS} records "
            f"(dropped {dropped_items}). Narrow with --device/--path/--search or reduce the time window."
        )


def _parse_log_time_expr(value: Optional[str], *, now: dt.datetime) -> str:
    if not value:
        raise ValueError("Empty time value.")
    raw = value.strip()
    if not raw:
        raise ValueError("Empty time value.")
    relative_match = re.fullmatch(r"(?i)(\d+)\s*([smhdw])", raw)
    parsed: Optional[dt.datetime]
    if relative_match:
        quantity = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        multiplier = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 60 * 60 * 24,
            "w": 60 * 60 * 24 * 7,
        }[unit]
        parsed = now - dt.timedelta(seconds=quantity * multiplier)
    else:
        text = raw.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "Invalid time value. Use relative values like 15m/2h/1d or an ISO timestamp."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        else:
            parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _truncate_log_value(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return f"{value[:max_chars]}... [truncated {len(value) - max_chars} chars]"
    if isinstance(value, list):
        return [_truncate_log_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_log_value(item, max_chars=max_chars) for key, item in value.items()}
    return value


def _truncate_logging_payload(payload: Any) -> Tuple[Any, bool, int]:
    items: Optional[List[Any]] = None
    container: Optional[Dict[str, Any]] = None
    key_name = ""

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        container = dict(payload)
        for key in ("results", "items", "data"):
            candidate = container.get(key)
            if isinstance(candidate, list):
                key_name = key
                items = candidate
                break

    if items is None:
        return _truncate_log_value(payload, max_chars=LOGGING_MAX_FIELD_CHARS), False, 0

    total_items = len(items)
    kept_items = items[:LOGGING_MAX_OUTPUT_ITEMS]
    truncated = total_items > LOGGING_MAX_OUTPUT_ITEMS
    dropped = total_items - len(kept_items)
    kept_items = [_truncate_log_value(item, max_chars=LOGGING_MAX_FIELD_CHARS) for item in kept_items]

    if container is None:
        return kept_items, truncated, dropped

    container[key_name] = kept_items
    if truncated:
        container["__cli_truncated__"] = {
            "reason": "result set too large",
            "max_items": LOGGING_MAX_OUTPUT_ITEMS,
            "dropped_items": dropped,
        }
    return container, truncated, dropped


@logging_app.command("path-map")
def logging_path_map(ctx: typer.Context) -> None:
    """Show recommended path filters for scoped log queries.

    Examples:
      dataplicity logging path-map
    """
    state = _ctx(ctx)
    payload = {
        "scopes": [
            {
                "name": "device",
                "flag": "--device <device-hash>",
                "path_hint": "--path /devices/<device-hash>",
                "example": "dataplicity logging list --device <device-hash> --since 4h",
            },
            {
                "name": "fleet job",
                "flag": "--search <fleet-job-id>",
                "path_hint": "--path /fleet-jobs/<job-id>",
                "example": "dataplicity logging list --path /fleet-jobs/<job-id> --since 2h",
            },
            {
                "name": "endpoint monitor",
                "flag": "--search <monitor-id>",
                "path_hint": "--path /endpoint-monitors/<monitor-id>",
                "example": "dataplicity logging list --path /endpoint-monitors/<monitor-id> --since 24h",
            },
            {
                "name": "heartbeat monitor",
                "flag": "--search <monitor-id>",
                "path_hint": "--path /heartbeat-monitors/<monitor-id>",
                "example": "dataplicity logging list --path /heartbeat-monitors/<monitor-id> --since 24h",
            },
        ]
    }
    if state.json_output:
        _print_json(payload)
        return
    table = Table(title="Logging path map")
    table.add_column("Scope", style="cyan")
    table.add_column("Primary filter")
    table.add_column("Web-style path filter")
    table.add_column("Example")
    for scope in payload["scopes"]:
        table.add_row(
            str(scope["name"]),
            str(scope["flag"]),
            str(scope["path_hint"]),
            str(scope["example"]),
        )
    state.console.print(table)


@logging_app.command("show")
def logging_show(ctx: typer.Context, log_id: str = typer.Argument(...)) -> None:
    """Show a single log event.

    Examples:
      dataplicity logging show <log-id>
    """
    _show_resource(_ctx(ctx), endpoint="/api/developer/logging/", resource_id=log_id, not_found_label="log event")


@api_app.command("get")
def api_get(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    params: Optional[List[str]] = typer.Option(None, "--param"),
) -> None:
    """Issue a raw authenticated GET request.

    Examples:
      dataplicity api get /api/developer/devices/
      dataplicity api get /api/developer/devices/ --param page_size=10
    """
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get(path, params=_parse_kv_pairs(params))
    if state.json_output:
        _print_json(response.data if response.data is not None else {"ok": response.ok})
    else:
        _print_json(response.data if response.data is not None else response.text)


@api_app.command("post")
def api_post(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    data: Optional[str] = typer.Option(None, "--data", help="JSON payload"),
) -> None:
    """Issue a raw authenticated POST request.

    Examples:
      dataplicity api post /api/developer/devices/<hash>/execute_command/ --data '{"command":"restart"}'
    """
    state = _ctx(ctx)
    _require_auth(state)
    json_data = None
    if data:
        try:
            json_data = json.loads(data)
        except json.JSONDecodeError:
            message = "Invalid JSON payload."
            if state.json_output:
                _print_json({"ok": False, "detail": message})
            else:
                _show_error(state.console, message)
            raise typer.Exit(code=1)
    response = state.api.post(path, json_data=json_data)
    if state.json_output:
        _print_json(response.data if response.data is not None else {"ok": response.ok})
    else:
        _print_json(response.data if response.data is not None else response.text)


@api_app.command("request")
def api_request(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    method: str = typer.Option("GET", "--method", help="HTTP method"),
    params: Optional[List[str]] = typer.Option(None, "--param"),
    data: Optional[str] = typer.Option(None, "--data", help="JSON payload"),
) -> None:
    """Issue an arbitrary authenticated API request.

    Examples:
      dataplicity api request /api/developer/devices/ --method GET --param page_size=5
      dataplicity api request /api/developer/devices/<hash>/ --method PATCH --data '{"name":"edge-01"}'
    """
    state = _ctx(ctx)
    _require_auth(state)
    json_data = None
    if data:
        try:
            json_data = json.loads(data)
        except json.JSONDecodeError:
            message = "Invalid JSON payload."
            if state.json_output:
                _print_json({"ok": False, "detail": message})
            else:
                _show_error(state.console, message)
            raise typer.Exit(code=1)
    response = state.api.request(
        method,
        path,
        params=_parse_kv_pairs(params),
        json_data=json_data,
    )
    if state.json_output:
        _print_json(response.data if response.data is not None else {"ok": response.ok})
    else:
        _print_json(response.data if response.data is not None else response.text)
