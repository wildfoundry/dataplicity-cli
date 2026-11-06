from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests
from requests.structures import CaseInsensitiveDict

from .config import Config
from .client_identity import identity_headers
from .api_recipient import INVALID_RECIPIENT, recipient_identity, recipient_url

# Refresh before the access JWT expires so long-running commands (tunnels,
# terminals, watches) do not wait for a 401 mid-flight.
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60.0
# How often long-running sessions re-check JWT lifetime while idle.
ACCESS_TOKEN_KEEPALIVE_INTERVAL_SECONDS = 120.0


class _ExplicitApiAuth(requests.auth.AuthBase):
    """Keep netrc/session Basic auth from replacing explicitly selected proof."""
    def __call__(self, request):
        return request


_EXPLICIT_API_AUTH = _ExplicitApiAuth()


@dataclass
class ApiResponse:
    ok: bool
    status_code: int
    data: Any
    text: str


class ApiClient:
    def __init__(
        self,
        config: Config,
        on_token_update: Optional[Callable[[], None]] = None,
        on_config_reload: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self._configured_base_url = config.base_url
        self.session = requests.Session()
        self.session.headers.update(identity_headers(self.config.install_id))
        self._on_token_update = on_token_update
        self._on_config_reload = on_config_reload
        self._refresh_lock = threading.Lock()

    def _build_url(self, path: str) -> str:
        if recipient_identity(self.config.base_url) != recipient_identity(self._configured_base_url):
            raise ValueError(INVALID_RECIPIENT)
        return recipient_url(self._configured_base_url, path)

    def _auth_headers(self) -> Dict[str, str]:
        if self.config.auth_method == "jwt" and self.config.access_token:
            return {"Authorization": f"Bearer {self.config.access_token}"}
        if self.config.auth_method == "api_key" and self.config.api_key:
            return {"Authorization": f"ApiKey {self.config.api_key}"}
        return {}

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        text = response.text or ""
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
            if isinstance(detail, str) and detail.strip():
                return detail
            non_field = payload.get("non_field_errors")
            if isinstance(non_field, list) and non_field:
                first = non_field[0]
                if isinstance(first, str) and first.strip():
                    return first
        return text

    @staticmethod
    def _looks_like_invalid_token_message(message: str) -> bool:
        normalized = (message or "").strip().lower()
        if not normalized:
            return False
        token_markers = (
            "token not valid",
            "not valid for any token type",
            "invalid token",
            "token is invalid or expired",
            "token has expired",
            "signature has expired",
            "expired token",
        )
        return any(marker in normalized for marker in token_markers)

    @staticmethod
    def _jwt_expiry(token: Optional[str]) -> Optional[float]:
        if not token or token.count(".") < 2:
            return None
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        except Exception:
            return None
        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return float(exp)

    def access_token_seconds_remaining(self) -> Optional[float]:
        exp = self._jwt_expiry(self.config.access_token)
        if exp is None:
            return None
        return exp - time.time()

    def _response_indicates_invalid_jwt(self, response: requests.Response) -> bool:
        if response.status_code not in {400, 401, 403}:
            return False
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("error")
            if isinstance(payload.get("detail"), dict):
                code = code or payload["detail"].get("code")
            if isinstance(code, str) and code in {"session_expired", "token_not_valid", "token_expired", "invalid_token", "token_blacklisted"}:
                return True
        return self._looks_like_invalid_token_message(self._extract_error_message(response))

    def _invalidate_jwt_session(self) -> None:
        self.config.clear_tokens()
        if self._on_token_update is not None:
            try:
                self._on_token_update()
            except Exception:
                pass

    def _reload_tokens(self) -> bool:
        if self._on_config_reload is None:
            return False
        before = (self.config.access_token, self.config.refresh_token, self.config.auth_method)
        try:
            self._on_config_reload()
        except Exception:
            return False
        after = (self.config.access_token, self.config.refresh_token, self.config.auth_method)
        return after != before

    def _attempt_refresh(self) -> str:
        """Try one refresh call. Returns 'ok', 'invalid', or 'failed'."""
        if not self.config.refresh_token:
            return "failed"
        try:
            url = self._build_url("/api/token/refresh/")
            resp = self.session.post(url, json={"refresh": self.config.refresh_token}, timeout=10, allow_redirects=False, auth=_EXPLICIT_API_AUTH)
        except Exception:
            return "failed"
        if resp.status_code >= 400:
            return "invalid" if self._response_indicates_invalid_jwt(resp) else "failed"
        if not 200 <= resp.status_code < 300:
            return "failed"
        try:
            payload = resp.json()
        except Exception:
            return "failed"
        try:
            self._build_url("/api/token/refresh/")
        except ValueError:
            return "failed"
        if not isinstance(payload, dict):
            return "failed"
        access = payload.get("access")
        refresh = payload.get("refresh")
        if not isinstance(access, str) or not access.strip() or (refresh is not None and (not isinstance(refresh, str) or not refresh.strip())):
            return "failed"
        self.config.access_token = access
        if refresh:
            self.config.refresh_token = refresh
        self.config.auth_method = "jwt"
        if self._on_token_update is not None:
            try:
                self._on_token_update()
            except Exception:
                pass
        return "ok"

    def _refresh_access_token(self) -> bool:
        with self._refresh_lock:
            result = self._attempt_refresh()
            if result == "ok":
                return True
            # Another CLI process may have rotated the refresh token on disk.
            if result == "invalid" and self._reload_tokens():
                result = self._attempt_refresh()
                if result == "ok":
                    return True
            if result == "invalid":
                self._invalidate_jwt_session()
            return False

    def ensure_fresh_access_token(self, *, skew_seconds: float = ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
        """Refresh the JWT access token when it is expired or near expiry.

        Long-running commands rely on this so heartbeats and other control
        calls do not wait until the server returns 401.
        """
        if self.config.auth_method != "jwt" or not self.config.refresh_token:
            return False
        remaining = self.access_token_seconds_remaining()
        if remaining is None:
            # Opaque/unparseable tokens: keep reactive 401 refresh only.
            return False
        if remaining > skew_seconds:
            return True
        return self._refresh_access_token()

    def refresh_session(self) -> bool:
        if self.config.auth_method != "jwt":
            return False
        return self._refresh_access_token()

    async def maintain_fresh_access_token(
        self,
        *,
        interval_seconds: float = ACCESS_TOKEN_KEEPALIVE_INTERVAL_SECONDS,
        skew_seconds: float = ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    ) -> None:
        """Periodically refresh JWTs while a long-running command is active.

        Terminals and idle port-forwards may not hit the HTTP API for a long
        time; this keeps access/refresh tokens alive on disk for mid-session
        channel opens and subsequent CLI use.
        """
        delay = max(1.0, float(interval_seconds))
        while True:
            await asyncio.sleep(delay)
            await asyncio.to_thread(self.ensure_fresh_access_token, skew_seconds=skew_seconds)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 20,
        allow_refresh: bool = True,
    ) -> ApiResponse:
        supplied_headers = CaseInsensitiveDict(headers or {})
        owns_session_auth = "Authorization" not in supplied_headers
        try:
            url = self._build_url(path)
        except ValueError:
            return ApiResponse(False, 0, None, INVALID_RECIPIENT)
        starting_auth_method = self.config.auth_method
        if allow_refresh and owns_session_auth and self.config.auth_method == "jwt" and self.config.refresh_token:
            self.ensure_fresh_access_token()
        # A callback must not change the credential recipient during renewal.
        try:
            if self._build_url(path) != url or (owns_session_auth and self.config.auth_method != starting_auth_method):
                return ApiResponse(False, 0, None, "Authentication changed; retry after signing in.")
        except ValueError:
            return ApiResponse(False, 0, None, INVALID_RECIPIENT)
        req_headers = CaseInsensitiveDict({
            **identity_headers(self.config.install_id),
            "Accept": "application/json",
            **self._auth_headers(),
        })
        req_headers.update(supplied_headers)
        try:
            resp = self.session.request(
                method.upper(),
                url,
                params=params,
                json=json_data,
                data=data,
                headers=req_headers,
                timeout=timeout,
                allow_redirects=False,
                auth=_EXPLICIT_API_AUTH,
            )
        except requests.RequestException:
            # requests exceptions can reflect credential header values or URLs.
            return ApiResponse(False, 0, None, "The API request failed. Check your connection or sign in again.")

        should_retry_with_refresh = (
            allow_refresh
            and owns_session_auth
            and self.config.auth_method == "jwt"
            and (resp.status_code == 401 or self._response_indicates_invalid_jwt(resp))
        )
        if should_retry_with_refresh:
            if self._refresh_access_token():
                return self.request(
                    method,
                    path,
                    params=params,
                    json_data=json_data,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    allow_refresh=False,
                )
        if (owns_session_auth and self.config.auth_method == "jwt"
                and req_headers.get("Authorization") == f"Bearer {self.config.access_token}"
                and self._response_indicates_invalid_jwt(resp)):
            self._invalidate_jwt_session()

        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = None
        ok = 200 <= resp.status_code < 300
        if not ok and not text:
            text = f"HTTP {resp.status_code}"
        return ApiResponse(ok, resp.status_code, payload, text)

    def get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> ApiResponse:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json_data: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> ApiResponse:
        return self.request("POST", path, json_data=json_data, data=data)
