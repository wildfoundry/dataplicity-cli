from __future__ import annotations

import os
import platform
from typing import Dict, Optional

from . import __version__

CLIENT_APP = "dataplicity-cli"


def client_platform() -> str:
    if os.name == "nt":
        return "windows"
    system = (platform.system() or "").strip().lower()
    if system in {"windows", "win32", "win64", "windows_nt"}:
        return "windows"
    if system in {"darwin", "macos", "mac"}:
        return "macos"
    if system == "linux":
        return "linux"
    return system or "unknown"


def client_machine() -> str:
    return (platform.machine() or "").strip() or "unknown"


def user_agent(version: Optional[str] = None) -> str:
    ver = (version or __version__).strip() or __version__
    plat = client_platform()
    machine = client_machine()
    if plat == "windows":
        detail = f"Windows NT; {machine}"
    elif plat == "macos":
        detail = f"Macintosh; Darwin {machine}"
    elif plat == "linux":
        detail = f"Linux; {machine}"
    else:
        system = (platform.system() or "").strip() or "Unknown"
        detail = f"{system}; {machine}"
    return f"{CLIENT_APP}/{ver} ({detail})"


def identity_headers(
    install_id: Optional[str] = None,
    version: Optional[str] = None,
) -> Dict[str, str]:
    ver = (version or __version__).strip() or __version__
    headers = {
        "User-Agent": user_agent(ver),
        "X-Client-App": CLIENT_APP,
        "X-Client-Platform": client_platform(),
        "X-Client-Version": ver,
    }
    cleaned_install_id = (install_id or "").strip()
    if cleaned_install_id:
        headers["X-Install-Id"] = cleaned_install_id
    return headers
