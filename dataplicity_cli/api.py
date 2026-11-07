from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests
from requests.structures import CaseInsensitiveDict

from .config import Config, ConfigError, SessionOwner, valid_api_credential
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


@dataclass(frozen=True)
class _RefreshOutcome:
    ok: bool
    owner: Optional[SessionOwner]


class ApiClient:
    def __init__(
        self,
        config: Config,
        on_token_update: Optional[Callable[[], None]] = None,
        on_config_reload: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self._owner = config.capture_owner()
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

    def _assert_actor(self, owner):
        if owner != self._owner:
            raise ConfigError("The active account changed.")
        self.config.assert_owner(owner)

    def _auth_headers(self, owner=None) -> Dict[str, str]:
        if owner is not None:
            self._assert_actor(owner)
            self.config.reload_owned(owner)
        with self.config._mutex:
            if owner is not None:
                self._assert_actor(owner)
            if self.config.auth_method == "jwt" and self.config.access_token:
                if not valid_api_credential(self.config.access_token):
                    raise ConfigError("Invalid authentication credentials. Sign in again.")
                return {"Authorization": f"Bearer {self.config.access_token}"}
            if self.config.auth_method == "api_key" and self.config.api_key:
                if not valid_api_credential(self.config.api_key):
                    raise ConfigError("Invalid authentication credentials. Sign in again.")
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
        if not isinstance(token, str) or token.count(".") < 2:
            return None
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
            if not isinstance(payload, dict):
                return None
            exp = payload.get("exp")
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                return None
            expiry = float(exp)
            return expiry if math.isfinite(expiry) else None
        except Exception:
            return None

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

    def _notify_update(self, owner):
        if self._on_token_update is not None:
            try:
                self._on_token_update()
            except ConfigError:
                raise
            except Exception:
                # Optional notifications cannot break a committed owned update.
                pass
        self.config.assert_owner(owner)

    def _invalidate_jwt_session(self, owner=None, expected_refresh=None):
        owner = owner or self._owner
        self._assert_actor(owner)
        current = self.config.invalidate_owned(owner, expected_refresh)
        self._notify_update(current)
        return current

    def _reload_tokens(self, owner=None) -> bool:
        owner = owner or self._owner
        try:
            self._assert_actor(owner)
            before = self.config._credential_state()
            self.config.reload_owned(owner)
            if self._on_config_reload is not None and before == self.config._credential_state():
                self._on_config_reload()
            self._assert_actor(owner)
            return before != self.config._credential_state()
        except Exception:
            return False

    def _perform_refresh(self, owner):
        ticket = None
        try:
            self._assert_actor(owner)
            url = self._build_url("/api/token/refresh/")
            ticket = self.config.claim_refresh(owner)
            if ticket is None:
                return "no_refresh", None
            # Recovery ID is committed before HTTP and shared only by this
            # session/input. File locks are released before any network I/O.
            resp = self.session.post(url, json={"refresh": ticket.refresh, "request_id": ticket.request_id},
                timeout=10, allow_redirects=False, auth=_EXPLICIT_API_AUTH)
            self._assert_actor(owner)
        except ConfigError:
            return "changed", ticket
        except (ValueError, requests.RequestException):
            try:
                self._assert_actor(owner)
            except ConfigError:
                return "changed", ticket
            return "failed", ticket
        if resp.status_code >= 400:
            return ("invalid" if self._response_indicates_invalid_jwt(resp) else "failed"), ticket
        if not 200 <= resp.status_code < 300:
            return "failed", ticket
        try:
            payload = resp.json()
        except Exception:
            return "failed", ticket
        if not isinstance(payload, dict):
            return "failed", ticket
        access, refresh = payload.get("access"), payload.get("refresh")
        if not valid_api_credential(access) or (refresh is not None and not valid_api_credential(refresh)):
            return "failed", ticket
        try:
            self._assert_actor(owner)
            self.config.commit_rotation(ticket, access, refresh)
            self._notify_update(owner)
            return "ok", ticket
        except ConfigError:
            return "changed", ticket

    def _attempt_refresh(self) -> str:
        """Compatibility helper: one owned attempt, without session invalidation."""
        state, _ = self._perform_refresh(self._owner)
        return "failed" if state in ("changed", "no_refresh") else state

    def _renew(self, owner):
        with self._refresh_lock:
            try:
                self._assert_actor(owner)
                state, ticket = self._perform_refresh(owner)
                if state == "ok":
                    return _RefreshOutcome(True, owner)
                if state == "changed":
                    return _RefreshOutcome(False, None)
                if state == "invalid" and self._reload_tokens(owner):
                    state, ticket = self._perform_refresh(owner)
                    if state == "ok":
                        return _RefreshOutcome(True, owner)
                self._assert_actor(owner)
                if state in ("invalid", "no_refresh"):
                    current = self._invalidate_jwt_session(owner, ticket.refresh if ticket else None)
                    # A later owned rotation may win between the invalid reply
                    # and conditional revocation; keep and retry that successor.
                    return _RefreshOutcome(current == owner, current)
                return _RefreshOutcome(False, owner)
            except ConfigError:
                return _RefreshOutcome(False, None)

    def _refresh_access_token(self) -> bool:
        return self._renew(self._owner).ok

    def ensure_fresh_access_token(self, *, skew_seconds: float = ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
        try:
            self._assert_actor(self._owner)
            self.config.reload_owned(self._owner)
            if self.config.auth_method != "jwt" or not self.config.refresh_token:
                return False
            remaining = self.access_token_seconds_remaining()
            if remaining is None:
                return False
            if remaining > skew_seconds:
                return True
            return self._refresh_access_token()
        except ConfigError:
            return False

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
        owner = self._owner if owns_session_auth else None
        publication_owner = owner
        changed = ApiResponse(False, 0, None, "The saved account changed. Start this command again.")
        try:
            url = self._build_url(path)
        except ValueError:
            return ApiResponse(False, 0, None, INVALID_RECIPIENT)
        try:
            if owner is not None:
                self._assert_actor(owner)
                self.config.reload_owned(owner)
                if allow_refresh and self.config.auth_method == "jwt" and self.config.refresh_token:
                    self.ensure_fresh_access_token()
                self._assert_actor(owner)
            req_headers = CaseInsensitiveDict({**identity_headers(self.config.install_id), "Accept": "application/json",
                **self._auth_headers(owner)})
            req_headers.update(supplied_headers)
            # Verify the recipient again after admission/renewal callbacks.
            self._build_url(path)
            resp = self.session.request(method.upper(), url, params=params, json=json_data, data=data,
                headers=req_headers, timeout=timeout, allow_redirects=False, auth=_EXPLICIT_API_AUTH)
            if owner is not None:
                self._assert_actor(owner)
        except ConfigError:
            return changed
        except ValueError:
            return ApiResponse(False, 0, None, INVALID_RECIPIENT)
        except requests.RequestException:
            if owner is not None:
                try:
                    self._assert_actor(owner)
                except ConfigError:
                    return changed
            return ApiResponse(False, 0, None, "The API request failed. Check your connection or sign in again.")

        auth_failure = resp.status_code == 401 or self._response_indicates_invalid_jwt(resp)
        tried_refresh = False
        try:
            if owner is not None and allow_refresh and self.config.auth_method == "jwt" and auth_failure:
                self.config.reload_owned(owner)
                current_auth = self._auth_headers(owner).get("Authorization")
                if current_auth and current_auth != req_headers.get("Authorization"):
                    return self.request(method, path, params=params, json_data=json_data, data=data,
                        headers=headers, timeout=timeout, allow_refresh=False)
                tried_refresh = True
                renewed = self._renew(owner)
                if renewed.ok:
                    self._assert_actor(owner)
                    return self.request(method, path, params=params, json_data=json_data, data=data,
                        headers=headers, timeout=timeout, allow_refresh=False)
                if renewed.owner is None:
                    return changed
                publication_owner = renewed.owner
            if (owner is not None and not tried_refresh and self.config.auth_method == "jwt"
                    and req_headers.get("Authorization") == self._auth_headers(owner).get("Authorization")
                    and self._response_indicates_invalid_jwt(resp)):
                publication_owner = self._invalidate_jwt_session(owner)
            if publication_owner is not None:
                self.config.assert_owner(publication_owner)
            text = resp.text or ""
            try:
                payload = resp.json()
            except Exception:
                payload = None
            if publication_owner is not None:
                self.config.assert_owner(publication_owner)
        except ConfigError:
            return changed
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
