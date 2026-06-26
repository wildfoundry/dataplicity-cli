from __future__ import annotations

import asyncio
import json
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .api import ApiClient
from .config import Config, default_config_path
from .m2m import M2MClient
from .remote_access import run_port_forward, run_remote_file, run_terminal_session


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


def _friendly_response_message(default_message: str, response_data: Any, response_text: str) -> str:
    if isinstance(response_data, dict):
        detail = response_data.get("detail") or response_data.get("error") or response_data.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail
        non_field = response_data.get("non_field_errors")
        if isinstance(non_field, list) and non_field:
            return str(non_field[0])
    return response_text or default_message


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
    orgs = _extract_orgs(org_response.data or []) if org_response.ok else []
    devices = _extract_devices(devices_response.data or []) if devices_response.ok else []
    online_count = sum(1 for device in devices if bool(device.get("online")))
    payload = {
        "base_url": state.config.base_url,
        "auth_method": state.config.auth_method,
        "organisation": orgs[0] if orgs else None,
        "devices_total": len(devices),
        "devices_online": online_count,
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
) -> None:
    """Start SSO login flow for your email.

    Examples:
      dataplicity auth sso --email you@example.com
      dataplicity auth sso --email you@example.com --no-open-browser
    """
    state = _ctx(ctx)
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
    if online_only:
        devices = [device for device in devices if bool(device.get("online"))]
    devices = sorted(
        devices,
        key=lambda d: (
            not bool(d.get("online")),
            str(d.get("name") or "").lower(),
            _device_hash(d).lower(),
        ),
    )
    online_count = sum(1 for device in devices if bool(device.get("online")))

    table = Table(title="Devices")
    table.add_column("Serial", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Online")
    table.add_column("Class")
    table.add_column("Network")
    for device in devices:
        online = bool(device.get("online"))
        table.add_row(
            _device_hash(device),
            _device_name(device),
            str(device.get("status") or ""),
            "yes" if online else "no",
            str(device.get("device_class_name") or ""),
            str(device.get("network_name") or ""),
            style="green" if online else "",
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
    """Forward a local port to a remote device port.

    Examples:
      dataplicity devices port-forward --remote-port 22 --local-port 2022
      dataplicity devices port-forward <device-hash> --remote-port 80 --local-port 8080
    """
    state = _ctx(ctx)
    _require_auth(state)
    resolved_hash = _resolve_device_hash_interactive(state, device_hash, action_name="port-forward")

    async def runner() -> None:
        ws_url = await _resolve_m2m_url(state, resolved_hash)
        m2m = M2MClient(ws_url)
        await m2m.connect()
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
