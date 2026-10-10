from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dataplicity_cli.api import ApiResponse
from dataplicity_cli.tunnels import TunnelAPI, TunnelError, TunnelSession, validate_name, websocket_url


class ControlTests(unittest.TestCase):
    def test_names_and_secure_owner_urls(self):
        self.assertEqual(validate_name(" Test.API_1 "), "test.api_1")
        for value in ["", "-dash", "spaces are invalid", "../secret", "a" * 64, "a;echo", "é"]:
            with self.assertRaises(TunnelError):
                validate_name(value)
        self.assertEqual(websocket_url("https://relay.test/m2m/?other=1"), "wss://relay.test/m2m/?other=1&features=named-tunnels-v1")
        self.assertEqual(websocket_url("wss://relay.test/m2m/"), "wss://relay.test/m2m/?features=named-tunnels-v1")
        for url in ["http://relay.test", "wss:///m2m/", "wss://user:password@relay.test"]:
            with self.assertRaises(TunnelError):
                websocket_url(url)

    def test_auth_separation_and_errors_never_reflect_secrets(self):
        api = Mock(config=SimpleNamespace(auth_method="jwt", access_token="jwt"))
        control = TunnelAPI(api, "org-1")
        api.request.return_value = ApiResponse(True, 200, {"results": []}, "")
        self.assertEqual(control.request("GET", ""), {"results": []})
        api.request.assert_called_with("GET", "/api/organisations/org-1/development-tunnels/", json_data=None, params=None, headers=None, allow_refresh=True)
        for status in [0, 401, 403, 404, 409, 429, 503, 500]:
            api.request.return_value = ApiResponse(False, status, {"error_code": "fenced", "detail": "secret reflected"}, "secret reflected")
            with self.assertRaises(TunnelError) as error:
                control.request("POST", "publish/")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(error.exception.code, "fenced")
        api.request.return_value = ApiResponse(False, 403, None, "secret")
        with self.assertRaises(TunnelError):
            control.request("GET", "")
        api.config.auth_method = "api_key"
        with self.assertRaises(TunnelError):
            TunnelAPI(api, "org-1")
        with self.assertRaises(TunnelError):
            TunnelAPI(api, "../org")
        machine = TunnelAPI(api, "org-1", token="machine")
        api.request.return_value = ApiResponse(True, 201, {"session_id": "s"}, "")
        machine.request("POST", "publish/", payload={"name": "api"})
        self.assertEqual(api.request.call_args.kwargs["headers"], {"Authorization": "TunnelPublisher machine"})
        self.assertFalse(api.request.call_args.kwargs["allow_refresh"])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        control = Mock(api=SimpleNamespace(config=SimpleNamespace(install_id="install")))
        control.call = AsyncMock()
        return TunnelSession(control, "api", Mock())

    async def test_async_control_and_transport(self):
        api = Mock(config=SimpleNamespace(auth_method="jwt", access_token="jwt"))
        api.request.return_value = ApiResponse(True, 200, {"version": 1}, "")
        self.assertEqual(await TunnelAPI(api, "org").call("GET", "bootstrap/"), {"version": 1})
        session = self.session()
        m2m = Mock(connect=AsyncMock(), wait_for_binding=AsyncMock(return_value=("id", "proof")), close=AsyncMock())
        with patch("dataplicity_cli.tunnels.TunnelM2MClient", return_value=m2m) as factory:
            self.assertEqual(await session._transport("https://relay.test/m2m/"), ("id", "proof"))
        self.assertIn("features=named-tunnels-v1", factory.call_args.args[0])
        await session.close()
        m2m.close.assert_awaited_once()
        self.assertIsNone(session.m2m)

    async def test_heartbeat_and_transport_closure(self):
        session = self.session()
        session.session = {"session_id": "s", "generation": 4, "heartbeat_seconds": 1}
        session.control.call.side_effect = TunnelError("revoked", "revoked")
        with patch("dataplicity_cli.tunnels.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TunnelError):
                await session._heartbeat()
        session.control.call.assert_awaited_with("POST", "sessions/s/heartbeat/", payload={"generation": 4})

        async def pending():
            await asyncio.Future()

        session.m2m = Mock(_closed_event=asyncio.Event())
        session._heartbeat = pending
        task = asyncio.create_task(session._run_until_closed(pending()))
        await asyncio.sleep(0)
        session.m2m._closed_event.set()
        with self.assertRaises(TunnelError) as error:
            await task
        self.assertEqual(error.exception.code, "transport_lost")
        session._heartbeat = AsyncMock(side_effect=TunnelError("revoked"))
        session.m2m._closed_event.clear()
        with self.assertRaises(TunnelError):
            await session._run_until_closed(pending())
        session._heartbeat = pending
        await session._run_until_closed(asyncio.sleep(0))

    async def test_publish_resume_never_fresh_publishes_after_loss(self):
        session = self.session()
        session._transport = AsyncMock(return_value=("id", "proof"))
        session._serve_publisher = Mock(return_value=object())
        session._run_until_closed = AsyncMock(side_effect=TunnelError("lost", "transport_lost"))
        session._cleanup_transport = AsyncMock()
        session.control.call.side_effect = [
            {"m2m_url": "https://relay.test"},
            {"session_id": "s", "generation": 1},
            TunnelError("superseded", "fenced"),
            None,
        ]
        with patch("dataplicity_cli.tunnels.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TunnelError) as error:
                await session.publish(3000)
        self.assertEqual(error.exception.code, "fenced")
        paths = [call.args[1] for call in session.control.call.await_args_list]
        self.assertEqual(paths, ["bootstrap/", "publish/", "sessions/s/resume/", "sessions/s/"])
        session.control.call.assert_any_await("DELETE", "sessions/s/", params={"generation": 1})
        self.assertEqual(session.emit.call_args_list[1].args[0]["event"], "reconnecting")

    async def test_bounded_reconnect_and_cleanup_denial(self):
        session = self.session()
        session._transport = AsyncMock(side_effect=OSError("failed"))
        session.control.call.return_value = {"m2m_url": "https://relay.test"}
        with patch("dataplicity_cli.tunnels.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(TunnelError, "reconnect failed"):
                await session.publish(3000)
        self.assertEqual(session._transport.await_count, 6)
        session.session = {"session_id": "s", "generation": 1}
        session.control.call.side_effect = TunnelError("expired")
        await session.close()

    async def test_consumer_uses_fixed_generation_per_socket_and_cleanup(self):
        session = self.session()
        session.control.call.side_effect = [
            {"m2m_url": "https://owner.test/m2m/"},
            {"session_id": "consumer", "generation": 9, "target_port": 3000},
            {"port": 22, "generation": 9, "target_port": 3000},
            None,
        ]
        session._transport = AsyncMock(return_value=("id", "proof"))
        session.m2m = Mock(close=AsyncMock())

        async def forward(client, channel, local, **kwargs):
            self.assertEqual(local, 8080)
            self.assertTrue(kwargs["half_close"])
            self.assertEqual(await kwargs["channel_factory"](), 22)
            kwargs["event_callback"](SimpleNamespace(kind="bytes_up", bytes_count=4, detail=""))

        async def run(action):
            await action

        session._run_until_closed = run
        with patch("dataplicity_cli.tunnels.run_port_forward", new=forward):
            await session.connect(8080)
        session.control.call.assert_any_await("POST", "sessions/consumer/channels/", payload={"generation": 9})
        self.assertEqual(session.emit.call_args.args[0]["bytes"], 4)

    async def test_consumer_rejects_unexpected_generation(self):
        session = self.session()
        session.control.call.side_effect = [
            {"m2m_url": "https://owner.test/m2m/"},
            {"session_id": "consumer", "generation": 9, "target_port": 3000},
            {"port": 22, "generation": 10, "target_port": 3000},
            None,
        ]
        session._transport = AsyncMock(return_value=("id", "proof"))
        m2m = Mock(close=AsyncMock(), close_channel=AsyncMock())
        session.m2m = m2m
        async def forward(client, channel, local, **kwargs):
            await kwargs["channel_factory"]()
        async def run(action):
            await action
        session._run_until_closed = run
        with patch("dataplicity_cli.tunnels.run_port_forward", new=forward):
            with self.assertRaisesRegex(TunnelError, "replaced"):
                await session.connect(8080)
        m2m.close_channel.assert_awaited_once_with(22)

    async def test_publisher_rejects_mismatched_targets_and_closes_on_cancel(self):
        session = self.session()
        session.session = {"session_id": "s", "generation": 5}
        session.m2m = Mock(instructions=asyncio.Queue(), MAX_CHANNELS=1, close_channel=AsyncMock())
        loop = asyncio.create_task(session._serve_publisher(3000))
        await session.m2m.instructions.put({b"port": 1, b"target_port": 3001, b"generation": 5})
        for _ in range(5):
            await asyncio.sleep(0)
        session.m2m.close_channel.assert_awaited_with(1)
        writer = Mock()
        reader = Mock()
        started = asyncio.Event()
        async def bridge(*args):
            started.set()
            await asyncio.Future()
        with patch("dataplicity_cli.tunnels.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))), patch("dataplicity_cli.tunnels.bridge_tcp_channel", new=bridge), patch("dataplicity_cli.tunnels._close_stream_writer", new=AsyncMock()) as close:
            await session.m2m.instructions.put({b"port": 2, b"target_port": 3000, b"generation": 5})
            await started.wait()
            await session.m2m.instructions.put({b"port": 3, b"target_port": 3000, b"generation": 5})
            for _ in range(3):
                await asyncio.sleep(0)
            session.m2m.close_channel.assert_awaited_with(3)
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            close.assert_awaited_once_with(writer)
        session.m2m.close_channel.assert_awaited_with(2)


if __name__ == "__main__":
    unittest.main()
