import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlsplit

import pytest
import requests
from rich.console import Console
import typer

from dataplicity_cli.api import ApiClient, ApiResponse
from dataplicity_cli.config import Config
from dataplicity_cli.native_auth import NativeLogin
from dataplicity_cli.cli import (
    _SsoCallbackListener, _attempt_sso_auto_complete, _attempt_sso_login,
    _coerce_timeout_seconds, _exchange_native_callback, _extract_sso_tokens,
)


def state_for(tmp_path):
    return SimpleNamespace(api=Mock(), config=Config(base_url='https://identity.example',
        auth_method='jwt', access_token='old-access', refresh_token='old-refresh'),
        config_path=tmp_path/'config.json', json_output=False, console=Console())


def listener_for(state):
    login = NativeLogin(state.config.base_url, 'http://127.0.0.1:49152/private')
    return SimpleNamespace(login=login,
        wait_for_payload=Mock(return_value={'code': 'c'*43, 'state': login.state}))


def test_callback_accepts_one_matching_proof_and_never_raw_tokens():
    listener = _SsoCallbackListener()
    assert listener.start()
    listener.login = NativeLogin('https://identity.example', listener.callback_url)
    proof = {'code': 'c'*43, 'state': listener.login.state}
    try:
        for payload, headers in (
            ({'access': 'injected-access', 'refresh': 'injected-refresh'}, {}),
            ({**proof, 'state': 'another-login'}, {}), ({**proof, 'state': '非ASCII'}, {}),
            ({**proof, 'issuer': 'https://evil.example'}, {}), (proof, {'Host': 'evil.example'}),
        ):
            response = requests.get(listener.callback_url, params=payload, headers=headers, timeout=3)
            assert response.status_code == 400
        assert requests.post(listener.callback_url, json=proof, timeout=3).status_code == 405
        assert requests.get(listener.callback_url+'?'+urlencode(proof)+'&code='+'d'*43, timeout=3).status_code == 400
        response = requests.get(listener.callback_url, params=proof, timeout=3)
        assert response.status_code == 200 and response.history[0].status_code == 303
        assert response.url.endswith('/complete') and not urlsplit(response.url).query
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        assert listener.login.verifier not in response.text and 'injected-access' not in response.text
        assert listener.wait_for_payload(1) == proof
        assert requests.get(listener.callback_url, params=proof, timeout=3).status_code == 410
        assert listener.wait_for_payload(0.01) is None
    finally:
        listener.stop()


def test_exchange_uses_pinned_anonymous_issuer_without_refreshing_old_login(tmp_path):
    state = state_for(tmp_path)
    listener = listener_for(state)
    previous_api = state.api
    captured = []
    def request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        return SimpleNamespace(status_code=200, text='', json=lambda: {'access':'new-access', 'refresh':'new-refresh'})
    with patch('dataplicity_cli.api.requests.Session.request', side_effect=request), patch.object(ApiClient, '_attempt_refresh', side_effect=AssertionError('Borrowed old refresh')):
        assert _exchange_native_callback(state, listener, listener.wait_for_payload())
    assert len(captured) == 1
    method, url, kwargs = captured[0]
    assert method == 'POST' and url == 'https://identity.example/api/auth/native/token/'
    assert 'Authorization' not in kwargs['headers'] and kwargs['allow_redirects'] is False
    assert kwargs['json']['code_verifier'] == listener.login.verifier
    assert kwargs['json']['redirect_uri'] == listener.login.callback_url
    saved = json.loads(state.config_path.read_text())
    assert saved['access_token'] == 'new-access' and saved['refresh_token'] == 'new-refresh'
    assert listener.login.verifier not in state.config_path.read_text()
    previous_api.get.assert_not_called()
    assert state.api is not previous_api
    previous_api.post.assert_not_called()


@pytest.mark.parametrize('change', ['origin', 'invalid_origin', 'wrong_state', 'token_payload', 'missing_context'])
def test_changed_or_invalid_context_does_not_send_a_grant_or_replace_credentials(tmp_path, change):
    state = state_for(tmp_path)
    listener = listener_for(state)
    proof = listener.wait_for_payload()
    if change == 'origin': state.config.base_url = 'https://foreign.example'
    if change == 'invalid_origin': state.config.base_url = 'http://foreign.example'
    if change == 'wrong_state': proof['state'] = 'another-login'
    if change == 'token_payload': proof = {'access':'injected', 'refresh':'injected'}
    if change == 'missing_context': listener.login = None
    with patch.object(ApiClient, 'post') as posted:
        assert not _exchange_native_callback(state, listener, proof)
        posted.assert_not_called()
    assert state.config.access_token == 'old-access' and not state.config_path.exists()


@pytest.mark.parametrize('ok,payload', [(False, {}), (True, {'access':'new'}),
    (True, {'access':True, 'refresh':'new'}), (True, None)])
def test_failed_or_malformed_exchange_preserves_credentials(tmp_path, ok, payload):
    state = state_for(tmp_path)
    listener = listener_for(state)
    with patch.object(ApiClient, 'post', return_value=ApiResponse(ok, 400 if not ok else 200, payload, '')):
        assert not _exchange_native_callback(state, listener, listener.wait_for_payload())
    assert state.config.access_token == 'old-access' and not state.config_path.exists()


def test_auto_complete_exchanges_proof_without_polling_session_completion(tmp_path):
    state = state_for(tmp_path)
    listener = listener_for(state)
    with patch('dataplicity_cli.cli._exchange_native_callback', return_value=True) as exchange:
        assert _attempt_sso_auto_complete(state, listener, timeout_seconds=1)
    exchange.assert_called_once()
    assert not _attempt_sso_auto_complete(state, None, timeout_seconds=180)
    state.api.get.assert_not_called()


@pytest.mark.parametrize('automatic', [True, False])
def test_sso_browser_and_manual_handoffs_use_pkce_and_never_token_paste(tmp_path, automatic):
    state = state_for(tmp_path)
    listener = SimpleNamespace(callback_url='http://127.0.0.1:49152/callback', login=None,
        start=Mock(return_value=True), stop=Mock())
    url = 'https://identity.example/sso?next=bound-relay'
    def post(path, **kwargs):
        if path == '/api/auth/bootstrap/':
            assert kwargs['json_data'] == {'email':'user@example.invalid', **listener.login.discovery_fields()}
            return ApiResponse(True, 200, {'status':'sso_redirect', 'redirect_url':url}, '')
        assert path == '/api/auth/native/token/'
        return ApiResponse(True, 200, {'access':'new-access', 'refresh':'new-refresh'}, '')
    with patch('dataplicity_cli.cli._ctx', return_value=state), patch('dataplicity_cli.cli._SsoCallbackListener', return_value=listener), patch.object(ApiClient, 'post', side_effect=post), patch('dataplicity_cli.cli._attempt_sso_auto_complete', return_value=True), patch('dataplicity_cli.cli.typer.prompt', side_effect=lambda *a,**k: urlencode({'code':'c'*43, 'state':listener.login.state})) as prompt, patch('dataplicity_cli.cli.webbrowser.open') as opened:
        assert _attempt_sso_login(Mock(), 'user@example.invalid', open_browser=automatic)
    if automatic:
        opened.assert_called_once_with(url)
        prompt.assert_not_called()
    else:
        opened.assert_not_called()
        assert prompt.call_args.kwargs['hide_input'] is True
        assert state.config.access_token == 'new-access'
    listener.stop.assert_called_once()


def test_json_headless_sso_fails_without_raw_token_paste(tmp_path):
    state = state_for(tmp_path)
    state.json_output = True
    listener = SimpleNamespace(callback_url='http://127.0.0.1:49152/callback', login=None,
        start=Mock(return_value=True), stop=Mock())
    with patch('dataplicity_cli.cli._ctx', return_value=state), patch('dataplicity_cli.cli._SsoCallbackListener', return_value=listener), patch.object(ApiClient, 'post', return_value=ApiResponse(True, 200, {'status':'sso_redirect', 'redirect_url':'https://identity.example/sso'}, '')), patch('dataplicity_cli.cli.typer.prompt') as prompt:
        with pytest.raises(typer.Exit) as stopped:
            _attempt_sso_login(Mock(), 'user@example.invalid', open_browser=False)
    assert stopped.value.exit_code == 2
    prompt.assert_not_called()
    assert state.config.access_token == 'old-access'


def test_token_json_extraction_and_timeout_inputs():
    assert _extract_sso_tokens({'tokens': {'access':'a', 'refresh':'r'}}) == ('a', 'r')
    assert _coerce_timeout_seconds(0) == 1
    assert _coerce_timeout_seconds('45') == 45
    assert _coerce_timeout_seconds(object()) == 180
