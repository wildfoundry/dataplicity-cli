"""Explicit Core sign-in revocation, independent of the detached local owner."""
import time

import requests

from .api import _EXPLICIT_API_AUTH
from .api_recipient import recipient_url
from .config import LogoutProof


def revoke_account(proof: LogoutProof, *, everywhere: bool = False) -> str:
    """Never renew, follow redirects, borrow cookies, or write credentials.

    The paired backend accepts a consumed refresh only as revocation proof.
    A network/server failure is ambiguous and must not start another mutation.
    """
    if not proof.access and not proof.refresh:
        return 'unavailable'
    try:
        path = '/api/auth/logout/everywhere/' if everywhere else '/api/auth/logout/'
        url = recipient_url(proof.base_url, path)
    except ValueError:
        return 'unavailable'
    deadline = time.monotonic() + 3.0
    session = requests.Session()
    try:
        for access in ((proof.access, None) if proof.access and proof.refresh else (proof.access,)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session.cookies.clear()
            response = session.post(url,
                headers={'Authorization': 'Bearer ' + access} if access else {},
                json={} if access else {'refresh': proof.refresh},
                auth=_EXPLICIT_API_AUTH, allow_redirects=False, timeout=remaining, stream=True)
            try:
                if 200 <= response.status_code < 300:
                    return 'revoked'
                if response.status_code not in (401, 403):
                    break
            finally:
                response.close()
    except requests.RequestException:
        pass
    finally:
        session.close()
    return 'failed'
