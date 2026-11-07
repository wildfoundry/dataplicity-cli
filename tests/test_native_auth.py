import base64
from dataclasses import FrozenInstanceError
import hashlib
import json
from urllib.parse import urlencode

import pytest

from dataplicity_cli.native_auth import NativeLogin, identity_origin


def pending():
    return NativeLogin('https://identity.example/', 'http://127.0.0.1:49152/private-callback')


def test_pkce_verifier_stays_local_and_the_issuer_is_immutable():
    login = pending()
    fields = login.discovery_fields()
    assert fields['native_client_id'] == 'cli'
    assert fields['native_code_challenge_method'] == 'S256'
    assert fields['native_code_challenge'] == base64.urlsafe_b64encode(
        hashlib.sha256(login.verifier.encode()).digest()).decode().rstrip('=')
    assert login.verifier not in json.dumps(fields)
    assert login.verifier not in repr(login)
    with pytest.raises(FrozenInstanceError):
        login.origin = 'https://foreign.example'
    payload = {'code': 'c'*43, 'state': login.state}
    assert login.exchange_fields(payload) == {
        'grant_type': 'authorization_code', 'client_id': 'cli',
        'redirect_uri': login.callback_url, 'code': 'c'*43, 'code_verifier': login.verifier,
    }
    assert login.origin == 'https://identity.example'


@pytest.mark.parametrize('value', [
    'http://identity.example', 'https://user:password@identity.example',
    'https://identity.example/path', 'https://identity.example?issuer=other',
    'https://identity.example#fragment', 'https://identity.example:broken', 'https://', None,
])
def test_native_issuer_must_be_a_configured_https_origin(value):
    with pytest.raises(ValueError, match='HTTPS identity origin'):
        identity_origin(value)


@pytest.mark.parametrize('callback', [
    'http://localhost:49152/callback', 'http://evil.example:49152/callback',
    'https://127.0.0.1:49152/callback', 'http://127.0.0.1:80/callback',
    'http://127.0.0.1/callback', 'http://127.0.0.1:broken/callback',
    'http://user@127.0.0.1:49152/callback', 'http://127.0.0.1:49152/callback?state=x',
    'http://127.0.0.1:49152/callback#refresh=x', 'http://127.0.0.1:49152/../callback',
])
def test_no_dns_or_privileged_port_callback_fallback(callback):
    with pytest.raises(ValueError, match='private loopback'):
        NativeLogin('https://identity.example', callback)


@pytest.mark.parametrize('changes', [
    {'state': 'another-login'}, {'state': '非ASCII'}, {'state': True},
    {'code': 'too-short'}, {'code': True}, {'access': 'injected-token'},
    {'refresh': 'injected-refresh'}, {'issuer': 'https://foreign.example'},
])
def test_foreign_state_or_additional_token_and_issuer_fields_never_form_a_grant(changes):
    login = pending()
    payload = {'code': 'c'*43, 'state': login.state, **changes}
    assert login.accept_payload(payload) is None
    assert login.exchange_fields(payload) is None


def test_manual_handoff_accepts_only_closed_code_state_proofs_for_its_callback():
    login = pending()
    payload = {'code': 'c'*43, 'state': login.state}
    query = urlencode(payload)
    for value in (query, login.callback_url+'?'+query, json.dumps(payload)):
        assert login.parse_artifact(value) == payload
    for value in (
        'access=stolen&refresh=stolen', '{"access":"stolen","refresh":"stolen"}',
        query+'&code='+'d'*43, login.callback_url+'?'+query+'#access=stolen',
        'https://foreign.example/callback?'+query, '{broken-json', '[', 'http://[',
        'x'*4097, None,
    ):
        assert login.parse_artifact(value) is None
    assert login.accept_payload(None) is None
