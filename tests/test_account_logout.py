"""Sign-out detaches before I/O and can never publish into a replacement owner."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import Mock, patch

import pytest
import requests
from typer.testing import CliRunner

from dataplicity_cli.account_logout import revoke_account
from dataplicity_cli.cli import app
from dataplicity_cli.config import Config, LogoutProof
from dataplicity_cli.config_store import ConfigChangedError


def response(status):
    result = requests.Response()
    result.status_code = status
    result._content = b'{}'
    result.close = Mock()
    return result


def signed_in(path):
    config = Config(base_url='https://gateway.example.test', auth_method='jwt',
        access_token='owned-access', refresh_token='owned-refresh')
    config.save(path)
    return config


def test_detachment_captures_latest_owned_rotation_and_invalidates_its_pending_work(tmp_path):
    path = tmp_path/'cli.json'
    config = signed_in(path)
    owner = config.capture_owner()
    other = Config.load(path)
    ticket = other.claim_refresh(other.capture_owner())
    other.commit_rotation(ticket, 'rotated-access', 'rotated-refresh')
    pending = other.claim_refresh(other.capture_owner())
    proof = config.detach_for_logout(owner)
    assert (proof.access, proof.refresh) == ('rotated-access', 'rotated-refresh')
    saved = Config.load(path)
    assert saved.auth_method is None and saved.access_token is None and saved.refresh_token is None
    assert saved.refresh_attempt is None and saved.capture_owner() != owner
    with pytest.raises(ConfigChangedError):
        other.commit_rotation(pending, 'late-access', 'late-refresh')
    assert Config.load(path).access_token is None
    assert 'rotated' not in repr(proof)


def test_stale_logout_cannot_detach_a_replacement_primary_even_with_identical_credentials(tmp_path):
    path = tmp_path/'cli.json'
    old = signed_in(path)
    owner = old.capture_owner()
    replacement = Config.load(path)
    replacement.install_user_tokens('owned-access', 'owned-refresh')
    replacement.save(path)
    with pytest.raises(ConfigChangedError):
        old.detach_for_logout(owner)
    assert Config.load(path).capture_owner() == replacement.capture_owner()


@pytest.mark.parametrize('everywhere', [False, True])
def test_cli_clears_before_server_io_and_late_success_preserves_a_new_primary(tmp_path, everywhere):
    path = tmp_path/'cli.json'
    signed_in(path)
    def send(session, url, **kwargs):
        detached = Config.load(path)
        assert detached.auth_method is None
        assert detached.access_token is None and detached.refresh_token is None
        suffix = 'everywhere/' if everywhere else ''
        assert url == 'https://gateway.example.test/api/auth/logout/' + suffix
        assert kwargs['headers'] == {'Authorization': 'Bearer owned-access'}
        assert kwargs['json'] == {}
        assert not kwargs['allow_redirects']
        detached.install_user_tokens('replacement-access', 'replacement-refresh')
        detached.save(path)
        return response(200)
    with patch.object(requests.Session, 'post', send):
        result = CliRunner().invoke(app, ['--config', str(path), '--json', 'auth', 'logout',
            *(['--everywhere'] if everywhere else [])])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['server_session_revocation'] == 'revoked'
    assert json.loads(result.output)['scope'] == ('account' if everywhere else 'sign_in')
    assert Config.load(path).access_token == 'replacement-access'
    assert 'owned-access' not in result.output and 'owned-refresh' not in result.output


@pytest.mark.parametrize('everywhere', [False, True])
def test_explicit_access_rejection_uses_only_the_captured_refresh_and_no_ambient_auth(everywhere):
    session = requests.Session()
    session.cookies.set('unrelated', 'private-cookie')
    session.auth = ('ambient', 'password')
    # Explicit auth must beat session Basic and netrc, including refresh-only.
    responses = [response(401), response(200)]
    session.send = Mock(side_effect=responses)
    with patch('dataplicity_cli.account_logout.requests.Session', return_value=session), \
            patch('requests.sessions.get_netrc_auth', return_value=('netrc', 'password')):
        assert revoke_account(LogoutProof('https://gateway.example.test', 'owned-access', 'owned-refresh'), everywhere=everywhere) == 'revoked'
    first, second = [call.args[0] for call in session.send.call_args_list]
    assert first.headers['Authorization'] == 'Bearer owned-access'
    assert 'Authorization' not in second.headers
    assert json.loads(first.body) == {}
    assert json.loads(second.body) == {'refresh': 'owned-refresh'}
    assert 'Cookie' not in first.headers and 'Cookie' not in second.headers
    suffix = 'everywhere/' if everywhere else ''
    assert first.url == second.url == 'https://gateway.example.test/api/auth/logout/' + suffix
    assert session.trust_env
    for call in session.send.call_args_list:
        assert call.kwargs['allow_redirects'] is False
        assert call.kwargs['stream'] is True
        assert 0 < call.kwargs['timeout'] <= 3
    for item in responses:
        item.close.assert_called_once()


@pytest.mark.parametrize('status', [302, 307, 400, 429, 500, 503])
@pytest.mark.parametrize('everywhere', [False, True])
def test_ambiguous_or_non_auth_failure_never_starts_another_mutation(status, everywhere):
    with patch.object(requests.Session, 'post', return_value=response(status)) as send:
        assert revoke_account(LogoutProof('https://gateway.example.test', 'access', 'refresh'), everywhere=everywhere) == 'failed'
    assert send.call_count == 1


def test_connection_failure_never_renews_or_retries_and_cli_reports_local_completion(tmp_path):
    path = tmp_path/'cli.json'
    signed_in(path)
    with patch.object(requests.Session, 'post', side_effect=requests.Timeout('private-credential')) as send:
        result = CliRunner().invoke(app, ['--config', str(path), 'auth', 'logout'])
    assert result.exit_code == 0
    assert 'credentials cleared' in result.output
    assert 'could not be confirmed' in result.output
    assert 'private-credential' not in result.output
    assert Config.load(path).access_token is None
    assert send.call_count == 1


@pytest.mark.parametrize('method', ['api_key', None])
def test_non_user_realms_clear_locally_without_sending_core_proof(tmp_path, method):
    path = tmp_path/'cli.json'
    config = Config(auth_method=method, api_key='organisation-secret',
        access_token='inactive-user-access', refresh_token='inactive-user-refresh')
    config.save(path)
    with patch.object(requests.Session, 'post') as send:
        result = CliRunner().invoke(app, ['--config', str(path), '--json', 'auth', 'logout'])
    assert result.exit_code == 0
    assert json.loads(result.output)['server_session_revocation'] == 'unavailable'
    saved = Config.load(path)
    assert saved.api_key is None and saved.access_token is None and saved.auth_method is None
    send.assert_not_called()


@pytest.mark.parametrize('base', ['https://user:private@gateway.example.test', 'http://foreign.example.test', 'file:///private'])
def test_invalid_recipient_never_receives_the_detached_proof(base):
    proof = LogoutProof(base, 'private-access', 'private-refresh')
    with patch.object(requests.Session, 'post') as send:
        assert revoke_account(proof) == 'unavailable'
    send.assert_not_called()
    assert 'private' not in repr(proof)


def test_refresh_only_and_exhausted_budget_are_bounded_without_renewal():
    with patch.object(requests.Session, 'post', return_value=response(403)) as send:
        assert revoke_account(LogoutProof('https://gateway.example.test', refresh='owned-refresh')) == 'failed'
    assert send.call_args.kwargs['headers'] == {}
    assert send.call_args.kwargs['json'] == {'refresh': 'owned-refresh'}
    with patch('dataplicity_cli.account_logout.time.monotonic', side_effect=[0, 4]), \
            patch.object(requests.Session, 'post') as send:
        assert revoke_account(LogoutProof('https://gateway.example.test', 'access')) == 'failed'
    send.assert_not_called()


@pytest.mark.parametrize('everywhere', [False, True])
def test_actual_http_fallback_never_adopts_server_cookies_or_a_new_saved_account(tmp_path, everywhere):
    path = tmp_path/'cli.json'
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers.get('Authorization'), self.headers.get('Cookie'), body))
            if len(calls) == 1:
                current = Config.load(path)
                assert current.access_token is None and current.refresh_token is None
                current.install_user_tokens('replacement-access', 'replacement-refresh')
                current.save(path)
            self.send_response(401 if len(calls) == 1 else 200)
            self.send_header('Set-Cookie', 'ambient=must-not-be-forwarded; Path=/')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        config = Config(base_url=f'http://127.0.0.1:{server.server_port}', auth_method='jwt',
            access_token='owned-access', refresh_token='owned-refresh')
        config.save(path)
        with patch('requests.sessions.get_netrc_auth', return_value=('ambient', 'password')):
            result = CliRunner().invoke(app, ['--config', str(path), '--json', 'auth', 'logout',
                *(['--everywhere'] if everywhere else [])])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)['server_session_revocation'] == 'revoked'
        endpoint = '/api/auth/logout/everywhere/' if everywhere else '/api/auth/logout/'
        assert calls == [
            (endpoint, 'Bearer owned-access', None, {}),
            (endpoint, None, None, {'refresh': 'owned-refresh'}),
        ]
        assert Config.load(path).access_token == 'replacement-access'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize('json_output', [False, True])
@pytest.mark.parametrize('user_login', [False, True])
def test_everywhere_failure_is_explicit_after_local_detachment(tmp_path, json_output, user_login):
    path = tmp_path/'cli.json'
    if user_login:
        signed_in(path)
    else:
        Config(auth_method='api_key', api_key='organisation-secret').save(path)
    with patch.object(requests.Session, 'post', side_effect=requests.Timeout('private-proof')) as send:
        result = CliRunner().invoke(app, ['--config', str(path),
            *(['--json'] if json_output else []), 'auth', 'logout', '--everywhere'])
    assert result.exit_code == 1, result.output
    assert Config.load(path).auth_method is None
    if json_output:
        output = json.loads(result.output)
        assert output['ok'] is False and output['scope'] == 'account'
        assert output['server_session_revocation'] == ('failed' if user_login else 'unavailable')
    else:
        assert ('could not be confirmed' if user_login else 'requires a Core user sign-in') in result.output
    assert 'private-proof' not in result.output and 'organisation-secret' not in result.output
    assert send.call_count == (1 if user_login else 0)


def test_successful_everywhere_command_names_its_scope(tmp_path):
    path = tmp_path/'cli.json'
    signed_in(path)
    with patch.object(requests.Session, 'post', return_value=response(200)):
        result = CliRunner().invoke(app, ['--config', str(path), 'auth', 'logout', '--everywhere'])
    assert result.exit_code == 0 and 'Logged out everywhere.' in result.output


def test_logout_help_describes_explicit_global_scope():
    result = CliRunner().invoke(app, ['auth', 'logout', '--help'])
    assert result.exit_code == 0
    assert '--everywhere' in result.output
    assert 'Other apps/devices remain signed in unless' in ' '.join(result.output.split())
