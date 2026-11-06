"""Real files, threads and processes must not transfer an old action to a new login."""
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from dataplicity_cli.api import ApiClient
from dataplicity_cli.config import Config
from dataplicity_cli.config_store import ConfigError, ConfigChangedError, locked_config


def http(status=200, data=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({} if data is None else data).encode()
    return response


def install(config, name):
    config.install_user_tokens(name + "-access", name + "-refresh")


def primary_writer(path, ready, go, results, name):
    config = Config.load(Path(path))
    ready.put(name)
    if not go.wait(10):
        results.put((name, "timeout")); return
    install(config, name)
    try:
        config.save(Path(path))
        results.put((name, "saved"))
    except OSError:
        results.put((name, "superseded"))


def claim_writer(path, ready, go, results):
    config = Config.load(Path(path)); owner = config.capture_owner()
    ready.put(True)
    if go.wait(10):
        results.put(config.claim_refresh(owner).request_id)


def lock_holder(path, ready, release):
    with locked_config(Path(path)):
        ready.set()
        release.wait(10)


class SessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"cli.json"
        config = Config(install_id="test-install")
        install(config, "a")
        config.save(self.path)

    def client(self):
        config = Config.load(self.path)
        def reload():
            fresh = Config.load(self.path)
            config.auth_method, config.access_token, config.refresh_token = fresh.auth_method, fresh.access_token, fresh.refresh_token
        return ApiClient(config, on_token_update=lambda: config.save(self.path), on_config_reload=reload)

    def replace(self, name):
        current = Config.load(self.path)
        install(current, name)
        current.save(self.path)

    def test_stale_settings_and_primary_writes_cannot_overwrite_a_replacement_login(self):
        for change in ("settings", "primary"):
            with self.subTest(change=change):
                old = Config.load(self.path)
                self.replace("b")
                if change == "settings": old.last_email = "old@example.test"
                else: install(old, "old-primary")
                with self.assertRaises(OSError): old.save(self.path)
                self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_an_empty_logout_invalidates_an_anonymous_pending_primary(self):
        anonymous = Config.load(self.path)
        anonymous.clear_tokens(); anonymous.save(self.path)
        pending = Config.load(self.path)
        logout = Config.load(self.path)
        logout.clear_tokens(); logout.save(self.path)
        install(pending, "pending")
        with self.assertRaises(OSError): pending.save(self.path)
        self.assertIsNone(Config.load(self.path).access_token)

    def test_refresh_response_cannot_restore_an_old_account_over_a_new_login(self):
        client, entered, resume = self.client(), Event(), Event()
        def post(*args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(10))
            return http(200, {"access": "a-renewed", "refresh": "a-rotated"})
        client.session.post = post
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.refresh_session)
            try:
                self.assertTrue(entered.wait(10)); self.replace("b")
            finally: resume.set()
            self.assertFalse(pending.result(10))
        self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_late_success_and_failure_do_not_publish_old_account_data_or_clear_new_login(self):
        for status in (200, 401, 403, 500):
            with self.subTest(status=status):
                self.replace("a")
                client, entered, resume = self.client(), Event(), Event()
                def request(*args, **kwargs):
                    entered.set(); self.assertTrue(resume.wait(10))
                    return http(status, {"private": "account-a", "code": "session_expired"})
                client.session.request = Mock(side_effect=request)
                client.session.post = Mock(return_value=http(200, {"access": "must-not-use", "refresh": "must-not-use"}))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(client.request, "POST", "/api/action/", json_data={"command": "account-a"})
                    try:
                        self.assertTrue(entered.wait(10)); self.replace("b")
                    finally: resume.set()
                    result = pending.result(10)
                self.assertFalse(result.ok); self.assertEqual(result.status_code, 0); self.assertIsNone(result.data)
                client.session.request.assert_called_once(); client.session.post.assert_not_called()
                self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_invalid_old_refresh_never_reloads_another_user_and_replays_its_mutation(self):
        client = self.client()
        def request(*args, **kwargs):
            self.replace("b")
            return http(401, {"code": "token_not_valid"})
        client.session.request = Mock(side_effect=request)
        client.session.post = Mock(side_effect=[http(401, {"detail": "Token is invalid or expired"}),
            http(200, {"access": "b-rotated", "refresh": "b-rotated-refresh"})])
        result = client.request("POST", "/api/action/", json_data={"command": "account-a"})
        self.assertFalse(result.ok); self.assertEqual(result.status_code, 0)
        client.session.request.assert_called_once(); client.session.post.assert_not_called()
        self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_owned_renewal_preserves_concurrent_preferences(self):
        client, entered, resume = self.client(), Event(), Event()
        def post(*args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(10))
            return http(200, {"access": "a-renewed", "refresh": "a-rotated"})
        client.session.post = post
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.refresh_session)
            try:
                self.assertTrue(entered.wait(10))
                preferences = Config.load(self.path)
                preferences.last_email = "latest@example.test"
                preferences.save(self.path)
            finally: resume.set()
            self.assertTrue(pending.result(10))
        saved = Config.load(self.path)
        self.assertEqual(saved.access_token, "a-renewed")
        self.assertEqual(saved.last_email, "latest@example.test")

    def test_competing_primary_processes_have_one_winner_without_partial_json(self):
        context = multiprocessing.get_context("spawn")
        ready, results, go = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=primary_writer, args=(str(self.path), ready, go, results, name)) for name in ("b", "c")]
        try:
            for process in processes: process.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {"b", "c"})
            go.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            self.assertEqual(sorted(value for _, value in outcomes), ["saved", "superseded"])
            winner = next(name for name, value in outcomes if value == "saved")
            self.assertEqual(Config.load(self.path).access_token, winner + "-access")
        finally:
            go.set()
            for process in processes:
                process.join(10)
                if process.is_alive(): process.terminate(); process.join(5)
            ready.close(); results.close()

    def test_failed_atomic_replace_keeps_the_previous_complete_private_config(self):
        config = Config.load(self.path); install(config, "b")
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): config.save(self.path)
        self.assertEqual(Config.load(self.path).access_token, "a-access")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_lost_refresh_response_recovers_with_the_same_owned_request_id(self):
        client = self.client(); bodies = []
        def lost(*args, **kwargs):
            bodies.append(kwargs["json"])
            raise requests.ConnectionError("response lost")
        client.session.post = lost
        self.assertFalse(client.refresh_session())
        self.assertRegex(bodies[0]["request_id"], r"^[0-9a-f-]{36}$")
        restored = self.client()
        restored.session.post = Mock(return_value=http(200, {"access": "a-renewed", "refresh": "a-rotated"}))
        self.assertTrue(restored.refresh_session())
        self.assertEqual(restored.session.post.call_args.kwargs["json"], bodies[0])
        self.assertEqual(Config.load(self.path).access_token, "a-renewed")

    def test_competing_refresh_processes_share_one_input_recovery_id(self):
        context = multiprocessing.get_context("spawn")
        ready, results, go = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=claim_writer, args=(str(self.path), ready, go, results)) for _ in range(2)]
        try:
            for process in processes: process.start()
            ready.get(timeout=10); ready.get(timeout=10); go.set()
            ids = [results.get(timeout=10), results.get(timeout=10)]
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(Config.load(self.path).refresh_attempt["request_id"], ids[0])
        finally:
            go.set()
            for process in processes:
                process.join(10)
                if process.is_alive(): process.terminate(); process.join(5)
            ready.close(); results.close()

    def test_busy_lock_is_bounded_and_cannot_overwrite_the_config(self):
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=lock_holder, args=(str(self.path), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            pending = Config.load(self.path); install(pending, "b")
            with patch("dataplicity_cli.config.locked_config", side_effect=lambda path: locked_config(path, timeout=.03)):
                with self.assertRaisesRegex(ConfigError, "busy"): pending.save(self.path)
            self.assertEqual(Config.load(self.path).access_token, "a-access")
        finally:
            release.set(); process.join(10)
            if process.is_alive(): process.terminate(); process.join(5)
        pending.save(self.path)
        self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_independent_preferences_merge_but_conflicting_changes_are_rejected(self):
        first, second = Config.load(self.path), Config.load(self.path)
        first.last_email = "first@example.test"; first.save(self.path)
        second.preferred_login_method = "sso"; second.save(self.path)
        self.assertEqual(Config.load(self.path).last_email, "first@example.test")
        stale = Config.load(self.path)
        second.last_email = "second@example.test"; second.save(self.path)
        stale.last_email = "stale@example.test"
        with self.assertRaises(ConfigChangedError): stale.save(self.path)

    def test_an_old_rotation_cannot_erase_a_newer_rotation_or_its_pending_receipt(self):
        old = Config.load(self.path); owner = old.capture_owner(); ticket = old.claim_refresh(owner)
        winner = Config.load(self.path); winning = winner.claim_refresh(winner.capture_owner())
        winner.commit_rotation(winning, "a-2-access", "a-2-refresh")
        following = Config.load(self.path); next_ticket = following.claim_refresh(following.capture_owner())
        old.commit_rotation(ticket, "a-2-access", "a-2-refresh")
        saved = Config.load(self.path)
        self.assertEqual(saved.refresh_token, "a-2-refresh")
        self.assertEqual(saved.refresh_attempt["request_id"], next_ticket.request_id)
        self.assertEqual(old.access_token, "a-2-access")

    def test_invalid_old_parent_cannot_revoke_a_successor_committed_before_invalidation(self):
        client, entered, resume = self.client(), Event(), Event()
        original = Config.invalidate_owned
        def delayed(config, *args, **kwargs):
            entered.set(); self.assertTrue(resume.wait(10))
            return original(config, *args, **kwargs)
        client.session.post = Mock(return_value=http(401, {"code": "token_not_valid"}))
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Config, "invalidate_owned", delayed):
            pending = pool.submit(client.refresh_session)
            try:
                self.assertTrue(entered.wait(10))
                winner = Config.load(self.path); ticket = winner.claim_refresh(winner.capture_owner())
                winner.commit_rotation(ticket, "successor-access", "successor-refresh")
            finally: resume.set()
            self.assertTrue(pending.result(10))
        self.assertEqual(Config.load(self.path).refresh_token, "successor-refresh")

    def test_raw_memory_mutation_cannot_change_the_actor_of_an_existing_client(self):
        client = self.client(); install(client.config, "b")
        client.session.request = Mock()
        self.assertFalse(client.get("/api/private/").ok)
        client.session.request.assert_not_called()
        self.assertEqual(Config.load(self.path).access_token, "a-access")

    def test_missing_file_context_cannot_follow_a_later_created_login(self):
        missing = self.path.with_name("missing.json")
        client = ApiClient(Config.load(missing)); client.session.request = Mock(return_value=http())
        self.assertTrue(client.get("/api/public/").ok)
        other = Config.load(missing); install(other, "b"); other.save(missing)
        self.assertFalse(client.get("/api/private/").ok)
        self.assertEqual(client.session.request.call_count, 1)

    def test_foreign_owner_cannot_revoke_and_changed_header_context_cannot_emit_a_proof(self):
        client = self.client()
        with self.assertRaises(ConfigError): client._invalidate_jwt_session(Config().capture_owner())
        owner = client.config.capture_owner()
        with patch.object(client.config, "reload_owned", side_effect=lambda _: install(client.config, "b")):
            with self.assertRaises(ConfigError): client._auth_headers(owner)
        self.assertEqual(Config.load(self.path).access_token, "a-access")

    def test_a_late_network_error_does_not_publish_or_persist_the_replacement_account(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                self.replace("a"); client = self.client()
                def changed(*args, **kwargs):
                    self.replace("b"); raise requests.ConnectionError("old secret")
                if refresh:
                    client.session.post = changed
                    self.assertFalse(client.refresh_session())
                else:
                    client.session.request = changed
                    result = client.get("/api/private/")
                    self.assertFalse(result.ok); self.assertIsNone(result.data)
                    self.assertNotIn("old secret", result.text)
                self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_a_response_body_decoded_after_replacement_is_not_published(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                self.replace("a"); client = self.client()
                def decode():
                    self.replace("b")
                    return {"access": "old-a-result", "refresh": "old-a-result", "private": "old account"}
                wire = http(); wire.json = decode
                if refresh:
                    client.session.post = Mock(return_value=wire)
                    self.assertFalse(client.refresh_session())
                else:
                    client.session.request = Mock(return_value=wire)
                    result = client.get("/api/private/")
                    self.assertFalse(result.ok); self.assertIsNone(result.data)
                self.assertEqual(Config.load(self.path).access_token, "b-access")

    def test_current_failure_and_transient_recovery_preserve_their_distinct_contracts(self):
        client = self.client(); client.session.request = Mock(return_value=http(403, {"code": "token_not_valid"}))
        result = client.request("GET", "/api/private/", allow_refresh=False)
        self.assertEqual(result.status_code, 403)
        self.assertIsNone(Config.load(self.path).access_token)
        self.replace("a"); client = self.client()
        client.session.request = Mock(return_value=http(401, {"code": "token_not_valid"}))
        client.session.post = Mock(side_effect=requests.ConnectionError("response lost"))
        result = client.get("/api/private/")
        self.assertEqual(result.status_code, 401)
        self.assertEqual(Config.load(self.path).refresh_token, "a-refresh")
        self.assertIsNotNone(Config.load(self.path).refresh_attempt)

    def test_an_owned_rotation_can_recover_a_late_401_without_another_refresh(self):
        client = self.client()
        def request(*args, **kwargs):
            if kwargs["headers"]["Authorization"] == "Bearer a-access":
                winner = Config.load(self.path); ticket = winner.claim_refresh(winner.capture_owner())
                winner.commit_rotation(ticket, "a-2-access", "a-2-refresh")
                return http(401, {"code": "token_not_valid"})
            return http(200, {"owned": True})
        client.session.request = Mock(side_effect=request); client.session.post = Mock()
        self.assertTrue(client.get("/api/private/").ok)
        self.assertEqual(client.session.request.call_count, 2)
        client.session.post.assert_not_called()

    def test_stale_mutation_cannot_replay_after_renewal_or_callback_changes_its_owner(self):
        client = self.client()
        client.session.request = Mock(return_value=http(401, {"code": "token_not_valid"}))
        def post(*args, **kwargs):
            self.replace("b"); return http(200, {"access": "a-2", "refresh": "a-2"})
        client.session.post = post
        self.assertEqual(client.post("/api/action/", json_data={"old": True}).status_code, 0)
        client.session.request.assert_called_once()
        self.assertFalse(client.ensure_fresh_access_token())
        self.assertEqual(Config.load(self.path).access_token, "b-access")
        self.replace("a"); config = Config.load(self.path)
        def callback(): raise ConfigChangedError()
        client = ApiClient(config, on_token_update=callback)
        client.session.post = Mock(return_value=http(200, {"access": "a-2", "refresh": "a-2"}))
        self.assertFalse(client.refresh_session())

    def test_explicit_machine_proof_cannot_change_recipient_during_header_preparation(self):
        client = self.client()
        def moved(*args):
            client.config.base_url = 'https://different.example.test'
            return {}
        client._auth_headers = moved
        client.session.request = Mock()
        result = client.request('GET', '/api/action/', headers={'Authorization':'TunnelPublisher machine'})
        self.assertEqual(result.status_code, 0)
        client.session.request.assert_not_called()

    def test_malformed_credentials_cannot_be_installed_or_forwarded(self):
        for value in ({'not':'a-token'}, 'bad\nvalue', 'space value', 'λ', ''):
            with self.subTest(value=value):
                config = Config.load(self.path)
                with self.assertRaises(ConfigError): config.install_user_tokens(value, 'refresh')
                with self.assertRaises(ConfigError): config.install_user_tokens('access', value)
                with self.assertRaises(ConfigError): config.install_api_key(value)
                ticket = config.claim_refresh(config.capture_owner())
                with self.assertRaises(ConfigError): config.commit_rotation(ticket, value, 'refresh')
                self.assertEqual(Config.load(self.path).access_token, 'a-access')
                if value:
                    for mode in ('jwt', 'api_key'):
                        bad = Config(auth_method=mode, access_token=value, api_key=value)
                        client = ApiClient(bad); client.session.request = Mock()
                        self.assertFalse(client.get('/api/private/').ok)
                        client.session.request.assert_not_called()
        config = Config(auth_method='jwt', access_token='access', refresh_token='bad\nrefresh')
        client = ApiClient(config); client.session.post = Mock()
        self.assertFalse(client.refresh_session())
        client.session.post.assert_not_called()
