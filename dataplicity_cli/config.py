from __future__ import annotations

import json
import copy
import hashlib
import threading
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import contextmanager

from .api_recipient import recipient_identity
from .config_store import ConfigError, ConfigChangedError, atomic_write, locked_config

try:
    from platformdirs import user_config_dir
except Exception:  # pragma: no cover
    user_config_dir = None

DEFAULT_BASE_URL = "https://gateway.dataplicity.com"
LEGACY_DEFAULT_BASE_URLS = {
    "https://api.prelude.dataplicity.com",
}


def _config_root() -> Path:
    override = os.getenv("DATAPLICITY_CONFIG_DIR") or os.getenv("DP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if user_config_dir:
        # Use OS-native locations (e.g. %APPDATA% on Windows, ~/Library/Application Support on macOS).
        # This is already app-scoped.
        return Path(user_config_dir("dataplicity", "Dataplicity"))
    xdg_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_home:
        return Path(xdg_home).expanduser() / "dataplicity"
    return Path.home() / ".config" / "dataplicity"


def default_config_path() -> Path:
    return _config_root() / "cli.json"


@dataclass(frozen=True)
class SessionOwner:
    identity: str
    path: Optional[Path]
    method: Optional[str]
    recipient: tuple


@dataclass(frozen=True)
class RefreshTicket:
    owner: SessionOwner
    access: Optional[str]
    refresh: str = field(repr=False)
    request_id: str


@dataclass(frozen=True)
class LogoutProof:
    base_url: str = field(repr=False)
    access: Optional[str] = field(default=None, repr=False)
    refresh: Optional[str] = field(default=None, repr=False)


_AUTH_FIELDS = {"base_url", "auth_method", "access_token", "refresh_token", "api_key"}
_SECRET_FIELDS = {"auth_method", "access_token", "refresh_token", "api_key"}


def valid_api_credential(value):
    return isinstance(value, str) and bool(value) and value.isascii() and all(33 <= ord(char) <= 126 for char in value)


def _valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    auth_method: Optional[str] = None
    access_token: Optional[str] = field(default=None, repr=False)
    refresh_token: Optional[str] = field(default=None, repr=False)
    api_key: Optional[str] = field(default=None, repr=False)
    last_email: Optional[str] = None
    preferred_login_method: Optional[str] = None
    install_id: Optional[str] = None
    local_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    refresh_attempt: Optional[dict] = field(default=None, repr=False)
    _path: Optional[Path] = field(default=None, init=False, repr=False, compare=False)
    _baseline: Optional[dict] = field(default=None, init=False, repr=False, compare=False)
    _baseline_id: Optional[str] = field(default=None, init=False, repr=False, compare=False)
    _observed: tuple = field(default=(), init=False, repr=False, compare=False)
    _extra: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _mutex: Any = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        self._observed = self._credential_state()

    def _recipient(self):
        try:
            return recipient_identity(self.base_url)
        except ValueError:
            return ("invalid",)

    def _credential_state(self):
        return self._recipient(), self.auth_method, self.access_token, self.refresh_token, self.api_key

    def _observe(self):
        current = self._credential_state()
        if current != self._observed or not _valid_uuid(self.local_session_id):
            self.local_session_id = str(uuid.uuid4())
            self.refresh_attempt = None
            self._observed = current

    @classmethod
    def _read(cls, path):
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise ConfigError("Unable to read the configuration.") from None
        try:
            raw = json.loads(raw_bytes)
            if not isinstance(raw, dict):
                raw = {}
        except (ValueError, UnicodeError):
            raw = {}
        base = raw.get("base_url") or DEFAULT_BASE_URL
        if base in LEGACY_DEFAULT_BASE_URLS:
            base = DEFAULT_BASE_URL
        identity = raw.get("local_session_id")
        if not _valid_uuid(identity):
            # Legacy readers only share an owner while the exact file is
            # unchanged. Never infer rotation/account lineage from JWT claims.
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, "dataplicity-cli-legacy:" + hashlib.sha256(raw_bytes).hexdigest()))
        config = cls(base_url=base, auth_method=raw.get("auth_method"),
            access_token=raw.get("access_token"), refresh_token=raw.get("refresh_token"), api_key=raw.get("api_key"),
            last_email=raw.get("last_email"), preferred_login_method=raw.get("preferred_login_method"),
            install_id=str(raw.get("install_id") or "").strip() or None,
            local_session_id=identity, refresh_attempt=raw.get("refresh_attempt"))
        known = set(config.to_dict())
        config._extra = {key: value for key, value in raw.items() if key not in known}
        config._mark_saved(path)
        return config

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path).resolve()
        config = cls._read(path)
        if config is None:
            config = cls()
            config._path = path
            config._baseline = config.to_dict()
        return config

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy({**self._extra, "base_url": self.base_url, "auth_method": self.auth_method,
            "access_token": self.access_token, "refresh_token": self.refresh_token, "api_key": self.api_key,
            "last_email": self.last_email, "preferred_login_method": self.preferred_login_method,
            "install_id": self.install_id, "local_session_id": self.local_session_id,
            "refresh_attempt": self.refresh_attempt})

    def _mark_saved(self, path):
        self._path = path
        self._baseline = self.to_dict()
        self._baseline_id = self.local_session_id
        self._observed = self._credential_state()

    def _adopt(self, record):
        for key in ("base_url", "auth_method", "access_token", "refresh_token", "api_key", "last_email",
                    "preferred_login_method", "install_id", "local_session_id", "refresh_attempt"):
            setattr(self, key, copy.deepcopy(getattr(record, key)))
        self._extra = copy.deepcopy(record._extra)
        self._path = record._path
        self._baseline = copy.deepcopy(record._baseline)
        self._baseline_id = record._baseline_id
        self._observed = self._credential_state()

    def save(self, path: Path) -> None:
        path = Path(path).resolve()
        with self._mutex, locked_config(path):
            self._observe()
            current = self._read(path)
            expected = self._baseline_id if self._path == path else None
            if (current.local_session_id if current else None) != expected:
                raise ConfigChangedError()
            local = self.to_dict()
            if current is None:
                payload = local
            else:
                baseline = self._baseline or {}
                payload = current.to_dict()
                replacing = self.local_session_id != expected
                changed = {key for key, value in local.items() if value != baseline.get(key)}
                if replacing:
                    for key in _AUTH_FIELDS | {"local_session_id", "refresh_attempt"}:
                        payload[key] = local[key]
                for key in changed - (_AUTH_FIELDS | {"local_session_id", "refresh_attempt"} if replacing else _SECRET_FIELDS | {"local_session_id"}):
                    if not replacing and payload.get(key) not in (baseline.get(key), local[key]):
                        raise ConfigChangedError()
                    payload[key] = local[key]
            atomic_write(path, payload)
            self._adopt(self._read(path))

    def capture_owner(self):
        with self._mutex:
            self._observe()
            return SessionOwner(self.local_session_id, self._path, self.auth_method, self._recipient())

    def _check_local(self, owner):
        if self.capture_owner() != owner:
            raise ConfigChangedError()

    @contextmanager
    def _owned_record(self, owner):
        with self._mutex:
            self._check_local(owner)
            if self._path is None:
                yield self
            else:
                with locked_config(self._path):
                    record = self._read(self._path)
                    if record is None and self._baseline_id is None:
                        yield self
                    elif record is None or record.capture_owner() != owner:
                        raise ConfigChangedError()
                    else:
                        yield record

    def assert_owner(self, owner):
        with self._owned_record(owner):
            pass

    def _persist_owned(self, record):
        if self._path is not None:
            atomic_write(self._path, record.to_dict())
            record._mark_saved(self._path)
        record._observed = record._credential_state()
        self._adopt(record)

    def reload_owned(self, owner):
        with self._owned_record(owner) as record:
            before = self._credential_state()
            self._adopt(record)
            return before != self._credential_state()

    def claim_refresh(self, owner):
        with self._owned_record(owner) as record:
            if record.auth_method != "jwt" or not isinstance(record.refresh_token, str) or not record.refresh_token:
                return None
            if not valid_api_credential(record.refresh_token):
                raise ConfigError("Invalid authentication credentials. Sign in again.")
            digest = hashlib.sha256(record.refresh_token.encode()).hexdigest()
            attempt = record.refresh_attempt
            if not isinstance(attempt, dict) or attempt.get("input_sha256") != digest or not _valid_uuid(attempt.get("request_id")):
                attempt = {"input_sha256": digest, "request_id": str(uuid.uuid4())}
                record.refresh_attempt = attempt
                self._persist_owned(record)
            else:
                self._adopt(record)
            return RefreshTicket(owner, record.access_token, record.refresh_token, attempt["request_id"])

    def commit_rotation(self, ticket, access, refresh):
        if not valid_api_credential(access) or (refresh is not None and not valid_api_credential(refresh)):
            raise ConfigError("Session renewal returned invalid credentials. Sign in again.")
        with self._owned_record(ticket.owner) as record:
            if record.refresh_token == ticket.refresh:
                record.access_token = access
                record.refresh_token = refresh if refresh is not None else ticket.refresh
                record.auth_method = "jwt"
                record.refresh_attempt = None
                self._persist_owned(record)
            else:
                # A later legitimate rotation already won; never overwrite it
                # or clear its independently pending recovery receipt.
                self._adopt(record)

    def invalidate_owned(self, owner, expected_refresh=None):
        with self._owned_record(owner) as record:
            if expected_refresh is not None and record.refresh_token != expected_refresh:
                self._adopt(record)
                return self.capture_owner()
            record.clear_tokens()
            self._persist_owned(record)
            return self.capture_owner()

    def detach_for_logout(self, owner):
        """Capture the latest owned proof and clear it in one file transaction.

        Network work must happen after this returns, without a config reference.
        A waiting rotation or primary login cannot publish into the old owner.
        """
        with self._owned_record(owner) as record:
            jwt = record.auth_method == 'jwt'
            proof = LogoutProof(record.base_url,
                record.access_token if jwt and valid_api_credential(record.access_token) else None,
                record.refresh_token if jwt and valid_api_credential(record.refresh_token) else None)
            record.clear_tokens()
            record.clear_api_key()
            self._persist_owned(record)
            return proof

    def _new_session(self):
        self.local_session_id = str(uuid.uuid4())
        self.refresh_attempt = None
        self._observed = self._credential_state()

    def install_user_tokens(self, access, refresh):
        if not valid_api_credential(access) or (refresh is not None and not valid_api_credential(refresh)):
            raise ConfigError("Sign-in returned invalid credentials. Restart sign-in.")
        with self._mutex:
            self.auth_method = "jwt"
            self.access_token = access
            self.refresh_token = refresh
            self.api_key = None
            self._new_session()

    def install_api_key(self, key):
        if not valid_api_credential(key):
            raise ConfigError("The API key is invalid. Enter it again.")
        with self._mutex:
            self.auth_method = "api_key"
            self.api_key = key
            self.access_token = None
            self.refresh_token = None
            self._new_session()

    def clear_tokens(self) -> None:
        with self._mutex:
            self.access_token = None
            self.refresh_token = None
            if self.auth_method == "jwt":
                self.auth_method = None
            self._new_session()

    def clear_api_key(self) -> None:
        with self._mutex:
            changed = self.api_key is not None or self.auth_method == "api_key"
            self.api_key = None
            if self.auth_method == "api_key":
                self.auth_method = None
            if changed:
                self._new_session()

    def ensure_install_id(self) -> bool:
        current = (self.install_id or "").strip()
        if current:
            self.install_id = current
            return False
        self.install_id = str(uuid.uuid4())
        return True
