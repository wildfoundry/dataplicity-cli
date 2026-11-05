"""PKCE login state is local; callbacks carry only a code and matching state."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

_CODE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def identity_origin(raw: str) -> str:
    try:
        if not isinstance(raw, str):
            raise ValueError()
        parts = urlsplit(raw)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError()
        parts.port
    except (TypeError, ValueError):
        raise ValueError("Native sign-in requires a configured HTTPS identity origin.") from None
    return raw.rstrip("/")


@dataclass(frozen=True)
class NativeLogin:
    origin: str
    callback_url: str
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    def __post_init__(self):
        object.__setattr__(self, "origin", identity_origin(self.origin))
        try:
            callback = urlsplit(self.callback_url)
            if (callback.scheme != "http" or callback.hostname != "127.0.0.1"
                    or callback.port is None or not 1024 <= callback.port <= 65535
                    or callback.username or callback.password or callback.query or callback.fragment
                    or not re.fullmatch(r"/[A-Za-z0-9/_-]{1,160}", callback.path)):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("Native sign-in requires a private loopback callback.") from None

    def discovery_fields(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest())
        return {
            "native_client_id": "cli", "native_redirect_uri": self.callback_url,
            "native_state": self.state, "native_code_challenge": challenge.decode().rstrip("="),
            "native_code_challenge_method": "S256",
        }

    def accept_payload(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict) or set(payload) != {"code", "state"}:
            return None
        code, state = payload["code"], payload["state"]
        if (not isinstance(code, str) or not _CODE.fullmatch(code)
                or not isinstance(state, str) or not state.isascii()
                or not secrets.compare_digest(state, self.state)):
            return None
        return code

    def parse_artifact(self, raw: str):
        """Allow a manual code/state handoff, never a token URL or token JSON."""
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except ValueError:
                return None
        else:
            try:
                parts = urlsplit(raw)
            except ValueError:
                return None
            if parts.fragment:
                return None
            if parts.scheme or parts.netloc:
                if f"{parts.scheme}://{parts.netloc}{parts.path}" != self.callback_url:
                    return None
                raw = parts.query
            values = parse_qs(raw, keep_blank_values=True)
            if any(len(value) != 1 for value in values.values()):
                return None
            payload = {key: value[0] for key, value in values.items()}
        return payload if self.accept_payload(payload) else None

    def exchange_fields(self, payload):
        code = self.accept_payload(payload)
        if code is None:
            return None
        return {
            "grant_type": "authorization_code", "client_id": "cli",
            "redirect_uri": self.callback_url, "code": code, "code_verifier": self.verifier,
        }
