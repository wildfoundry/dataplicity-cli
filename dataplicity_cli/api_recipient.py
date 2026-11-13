"""Canonical recipients for credentialed CLI API transports."""
from ipaddress import ip_address
from typing import Tuple
from urllib.parse import unquote, urlsplit

import requests


INVALID_RECIPIENT = "The API destination is invalid or outside the configured recipient."


def canonical_url(value: str) -> str:
    try:
        if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.fragment or not parsed.hostname:
            raise ValueError
        loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            try:
                loopback = ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
        # Use the same URL normalization as the actual requests transport.
        prepared = requests.Request("GET", value).prepare().url
        path = unquote(urlsplit(prepared).path)
        if "\\" in path or any(part in (".", "..") for part in path.split("/")):
            raise ValueError
        return prepared
    except (ValueError, requests.RequestException):
        raise ValueError(INVALID_RECIPIENT) from None


def recipient_identity(value: str) -> Tuple[str, str, int, str]:
    parsed = urlsplit(canonical_url(value))
    if parsed.query:
        raise ValueError(INVALID_RECIPIENT)
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80), unquote(parsed.path).rstrip("/")


def recipient_url(base: str, path: str) -> str:
    try:
        identity = recipient_identity(base)
        if not isinstance(path, str) or path.startswith("//"):
            raise ValueError
        parsed = urlsplit(path)
        candidate = path if parsed.scheme or parsed.netloc else f"{base.rstrip('/')}/{path.lstrip('/')}"
        target = canonical_url(candidate)
        parts = urlsplit(target)
        origin = (parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80))
        target_path = unquote(parts.path)
        prefix = identity[3]
        if origin != identity[:3] or (prefix and target_path != prefix and not target_path.startswith(prefix + "/")):
            raise ValueError
        return target
    except (ValueError, requests.RequestException):
        raise ValueError(INVALID_RECIPIENT) from None
