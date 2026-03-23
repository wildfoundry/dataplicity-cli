from __future__ import annotations

import asyncio
import json
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer
from rich.console import Console
from rich.table import Table

from .api import ApiClient
from .config import Config, default_config_path
from .m2m import M2MClient
from .remote_access import run_port_forward, run_remote_file, run_terminal_session


app = typer.Typer(add_completion=False, help="Dataplicity CLI")
auth_app = typer.Typer(help="Authentication commands")
orgs_app = typer.Typer(help="Organisation commands")
devices_app = typer.Typer(help="Device commands")
config_app = typer.Typer(help="Configuration commands")
api_app = typer.Typer(help="Raw API commands")

app.add_typer(auth_app, name="auth")
app.add_typer(orgs_app, name="org")
app.add_typer(devices_app, name="devices")
app.add_typer(config_app, name="config")
app.add_typer(api_app, name="api")


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


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output JSON instead of tables"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config file"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override API base URL"),
) -> None:
    path = config_path or default_config_path()
    config = Config.load(path)
    if base_url:
        config.base_url = base_url.rstrip("/")
    console = Console()
    ctx.obj = AppContext(config=config, config_path=path, console=console, json_output=json_output, api=ApiClient(config))


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Dataplicity API base URL"),
) -> None:
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
    state = _ctx(ctx)
    bootstrap = state.api.get("/api/auth/bootstrap/", params={"email": email})
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
        detail = None
        if isinstance(response.data, dict):
            detail = response.data.get("detail") or response.data.get("error") or response.data.get("non_field_errors")
        message = detail or response.text or "Login failed"
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
) -> None:
    state = _ctx(ctx)
    response = state.api.get("/api/auth/bootstrap/", params={"email": email})
    if not response.ok:
        message = response.text or "Unable to start SSO"
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

    if open_browser:
        webbrowser.open(redirect_url)

    sso_complete_url = state.api._build_url("/api/auth/sso/complete/")
    if not state.json_output:
        state.console.print("Complete SSO in your browser, then open:")
        state.console.print(f"[blue]{sso_complete_url}[/blue]")
        state.console.print("Paste the JSON payload below.")

    raw = typer.prompt("SSO JSON")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        message = "Invalid JSON payload."
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    access = None
    refresh = None
    if isinstance(tokens, dict):
        access = tokens.get("access")
        refresh = tokens.get("refresh")
    if isinstance(payload, dict):
        access = access or payload.get("access")
        refresh = refresh or payload.get("refresh")

    if not access:
        message = "No access token found in payload."
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
        _print_json({"ok": True, "detail": "SSO login complete"})
    else:
        state.console.print("[green]SSO login complete.[/green]")


@auth_app.command("api-key")
def auth_api_key_set(ctx: typer.Context) -> None:
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
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get("/api/v1/organisations/")
    if not response.ok:
        message = response.text or "Unable to fetch organisation"
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
) -> None:
    state = _ctx(ctx)
    _require_auth(state)
    params: Dict[str, Any] = {"page_size": page_size}
    if search:
        params["search"] = search
    response = state.api.get("/api/developer/devices/", params=params)
    if not response.ok:
        message = response.text or "Unable to list devices"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)

    payload = response.data or []
    if state.json_output:
        _print_json(payload)
        return

    devices: List[Dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("devices"), list):
        devices = payload["devices"]
    elif isinstance(payload, list):
        devices = payload

    table = Table(title="Devices")
    table.add_column("Serial")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Online")
    table.add_column("Class")
    table.add_column("Network")
    for device in devices:
        table.add_row(
            str(device.get("hash_id") or device.get("serial") or ""),
            str(device.get("name") or ""),
            str(device.get("status") or ""),
            str(device.get("online") if device.get("online") is not None else ""),
            str(device.get("device_class_name") or ""),
            str(device.get("network_name") or ""),
        )
    state.console.print(table)


@devices_app.command("show")
def devices_show(ctx: typer.Context, device_hash: str = typer.Argument(...)) -> None:
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get(f"/api/developer/devices/{device_hash}/")
    if not response.ok:
        message = response.text or "Unable to fetch device"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data)
        return
    table = Table(title=f"Device {device_hash}")
    table.add_column("Key")
    table.add_column("Value")
    if isinstance(response.data, dict):
        for key, value in response.data.items():
            table.add_row(str(key), json.dumps(_sanitize_payload(value)))
    state.console.print(table)


@devices_app.command("reboot")
def devices_reboot(ctx: typer.Context, device_hash: str = typer.Argument(...)) -> None:
    state = _ctx(ctx)
    _require_auth(state)
    payload = {"command": "restart", "params": {}}
    response = state.api.post(f"/api/developer/devices/{device_hash}/execute_command/", json_data=payload)
    if not response.ok:
        message = response.text or "Unable to reboot device"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)
    if state.json_output:
        _print_json(response.data or {"ok": True})
    else:
        state.console.print("[green]Reboot command sent.[/green]")


@devices_app.command("provisioning-key")
def devices_provisioning_key(
    ctx: typer.Context,
    output: Optional[Path] = typer.Option(
        None, "--output", help="Write provisioning details to a file"
    ),
) -> None:
    state = _ctx(ctx)
    _require_auth(state)
    response = state.api.get("/api/developer/devices/provisioning-key/")
    if not response.ok:
        message = response.text or "Unable to fetch provisioning key"
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
        raise RuntimeError("Remote Access host lookup failed")
    m2m_url = response.data.get("m2m_url")
    if not m2m_url:
        raise RuntimeError("Remote Access host missing m2m_url")
    ws_base = m2m_url.replace("https://", "wss://").replace("http://", "ws://")
    joiner = "&" if "?" in ws_base else "?"
    return f"{ws_base}{joiner}device={device_hash}"


@devices_app.command("terminal")
def devices_terminal(ctx: typer.Context, device_hash: str = typer.Argument(...)) -> None:
    state = _ctx(ctx)
    _require_auth(state)

    async def runner() -> None:
        ws_url = await _resolve_m2m_url(state, device_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        identity = await m2m.wait_for_identity()
        response = state.api.post(
            f"/api/developer/devices/{device_hash}/ports/",
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
    device_hash: str = typer.Argument(...),
    remote_port: int = typer.Option(..., "--remote-port", help="Remote device port"),
    local_port: int = typer.Option(..., "--local-port", help="Local listen port"),
) -> None:
    state = _ctx(ctx)
    _require_auth(state)

    async def runner() -> None:
        ws_url = await _resolve_m2m_url(state, device_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        identity = await m2m.wait_for_identity()
        response = state.api.post(
            f"/api/developer/devices/{device_hash}/ports/",
            json_data={
                "m2m_identity": identity,
                "service": "redirect-port",
                "port": remote_port,
            },
        )
        if not response.ok:
            raise RuntimeError(response.text or "Unable to open port redirect")
        channel_port = await m2m.wait_for_channel_open()
        if not state.json_output:
            state.console.print(
                f"[green]Forwarding device:{remote_port} -> localhost:{local_port}[/green]"
            )
        await run_port_forward(m2m, channel_port, local_port)
        await m2m.close()

    try:
        asyncio.run(runner())
    except Exception as exc:
        message = str(exc) or "Port forward failed"
        if state.json_output:
            _print_json({"ok": False, "detail": message})
        else:
            _show_error(state.console, message)
        raise typer.Exit(code=1)


@devices_app.command("remote-file")
def devices_remote_file(
    ctx: typer.Context,
    device_hash: str = typer.Argument(...),
    path: str = typer.Option(..., "--path", help="Remote file path"),
    output: Optional[Path] = typer.Option(None, "--output", help="Write file content to a path"),
    stdout: bool = typer.Option(False, "--stdout", help="Write file content to stdout"),
) -> None:
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

    async def runner() -> int:
        ws_url = await _resolve_m2m_url(state, device_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
        identity = await m2m.wait_for_identity()
        response = state.api.post(
            f"/api/developer/devices/{device_hash}/ports/",
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


def _parse_kv_pairs(items: Optional[List[str]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        payload[key] = value
    return payload


@api_app.command("get")
def api_get(
    ctx: typer.Context,
    path: str = typer.Argument(...),
    params: Optional[List[str]] = typer.Option(None, "--param"),
) -> None:
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
