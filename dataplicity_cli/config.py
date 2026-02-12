from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = "https://api.prelude.dataplicity.com"


def _config_root() -> Path:
    override = os.getenv("DATAPLICITY_CONFIG_DIR") or os.getenv("DP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_home:
        return Path(xdg_home).expanduser()
    return Path.home() / ".config"


def default_config_path() -> Path:
    return _config_root() / "dataplicity" / "cli.json"


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    auth_method: Optional[str] = None  # "jwt" | "api_key"
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    api_key: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls(
            base_url=raw.get("base_url") or DEFAULT_BASE_URL,
            auth_method=raw.get("auth_method"),
            access_token=raw.get("access_token"),
            refresh_token=raw.get("refresh_token"),
            api_key=raw.get("api_key"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "auth_method": self.auth_method,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "api_key": self.api_key,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def clear_tokens(self) -> None:
        self.access_token = None
        self.refresh_token = None
        if self.auth_method == "jwt":
            self.auth_method = None

    def clear_api_key(self) -> None:
        self.api_key = None
        if self.auth_method == "api_key":
            self.auth_method = None
