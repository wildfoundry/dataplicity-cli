from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from .config import Config


@dataclass
class ApiResponse:
    ok: bool
    status_code: int
    data: Any
    text: str


class ApiClient:
    def __init__(self, config: Config, on_token_update: Optional[Callable[[], None]] = None) -> None:
        self.config = config
        self.session = requests.Session()
        self._on_token_update = on_token_update

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = (self.config.base_url or "").rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    def _auth_headers(self) -> Dict[str, str]:
        if self.config.auth_method == "jwt" and self.config.access_token:
            return {"Authorization": f"Bearer {self.config.access_token}"}
        if self.config.auth_method == "api_key" and self.config.api_key:
            return {"Authorization": f"ApiKey {self.config.api_key}"}
        return {}

    def _refresh_access_token(self) -> bool:
        if not self.config.refresh_token:
            return False
        url = self._build_url("/api/token/refresh/")
        try:
            resp = self.session.post(url, json={"refresh": self.config.refresh_token}, timeout=10)
        except Exception:
            return False
        if resp.status_code >= 400:
            return False
        try:
            payload = resp.json()
        except Exception:
            return False
        access = payload.get("access")
        if not access:
            return False
        self.config.access_token = access
        refresh = payload.get("refresh")
        if refresh:
            self.config.refresh_token = refresh
        self.config.auth_method = "jwt"
        if self._on_token_update is not None:
            try:
                self._on_token_update()
            except Exception:
                pass
        return True

    def refresh_session(self) -> bool:
        if self.config.auth_method != "jwt":
            return False
        return self._refresh_access_token()

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
        url = self._build_url(path)
        req_headers = {"Accept": "application/json", **self._auth_headers()}
        if headers:
            req_headers.update(headers)
        try:
            resp = self.session.request(
                method.upper(),
                url,
                params=params,
                json=json_data,
                data=data,
                headers=req_headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            return ApiResponse(False, 0, None, str(exc))

        if resp.status_code == 401 and allow_refresh and self.config.auth_method == "jwt":
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
