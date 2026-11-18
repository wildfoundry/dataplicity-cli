"""Every command and alias must call only its own developer API resource."""

import json
import os
from pathlib import Path
import runpy
import ssl
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from dataplicity_cli import _pyinstaller
from dataplicity_cli.api import ApiClient, ApiResponse
from dataplicity_cli.cli import app
from dataplicity_cli.config import Config


@pytest.fixture
def invoke(tmp_path):
    path = tmp_path / "cli.json"
    Config(install_id="tests", auth_method="api_key", api_key="fixture").save(path)

    def run(*args):
        return CliRunner().invoke(app, ["--config", str(path), "--json", *args])
    return run


@pytest.mark.parametrize("resource,verbs", [
    ("fleet-jobs", "list ls show get create run cancel delete"),
    ("endpoint-monitors", "list ls show get create delete"),
    ("heartbeat-monitors", "list ls show get create delete"),
    ("user-impact", "list ls show get"),
])
@pytest.mark.parametrize("success", [True, False])
def test_resource_requests(invoke, resource, verbs, success):
    root = f"/api/developer/{resource}/"
    detail = root + "abc123/"
    body = {"command": "uname -a", "device_hashes": ["test-device"]} if resource == "fleet-jobs" else {"name": "monitor"}
    for verb in verbs.split():
        args = [resource, verb]
        row = {"hash_id": "abc123"}
        if verb in ("ls", "list"):
            method, endpoint, result = "GET", root, [row]
        elif verb in ("get", "show"):
            args.append("abc123")
            method, endpoint, result = "GET", detail, row
        elif verb in ("run", "create"):
            args.extend(["--data", json.dumps(body)])
            method, endpoint, result = "POST", root, row
        else:
            args.append("abc123")
            method, endpoint = ("POST", detail + "cancel/") if resource == "fleet-jobs" else ("DELETE", detail)
            result = {"ok": True}
        payload = result if success else {"detail": "Access denied."}
        with patch.object(ApiClient, "request", return_value=ApiResponse(success, 200 if success else 403, payload, "")) as send:
            outcome = invoke(*args)
        assert outcome.exit_code == (0 if success else 1), (args, outcome.output, outcome.exception)
        output = json.loads(outcome.output)
        if success:
            assert output == result
        else:
            assert output["detail"] == "Access denied."
        send.assert_called_once()
        assert send.call_args.args == (method, endpoint)
        if verb in ("run", "create"):
            assert send.call_args.kwargs["json_data"] == body


def test_hook_includes_shell_and_runtime_certificate_policy(tmp_path, monkeypatch):
    hook = Path(_pyinstaller.get_hook_dirs()[0]) / "hook-dataplicity_cli.py"
    monkeypatch.setattr(ssl, "__file__", str(tmp_path / "ssl.py"))
    policy = tmp_path / "codex-rejected-cas.pem"
    for expected in ([], [(str(policy), ".")]):
        data = runpy.run_path(str(hook))
        assert data["datas"] == expected
        assert f"shellingham.{os.name}" in data["hiddenimports"]
        policy.touch()


def test_transient_recipient_probe_is_anonymous(invoke):
    with patch("requests.Session.request") as send:
        send.return_value.status_code = 200
        send.return_value.json.return_value = {}
        outcome = invoke("--base-url", "https://alternate.example", "doctor")
    assert json.loads(outcome.stdout)["checks"][1]["ok"]
    assert "Authorization" not in send.call_args.kwargs["headers"]


def test_http_sso_reports_the_https_requirement(invoke):
    outcome = invoke("--base-url", "http://127.0.0.1", "auth", "sso", "--email", "qa@example.test", "--no-open-browser")
    assert outcome.exit_code == 1
    assert "HTTPS identity origin" in json.loads(outcome.stdout)["detail"]


def test_signed_timeline_ids_and_soft_deletion(invoke):
    event_id = ".signed:event_id-7"
    row = {"id": event_id, "message": "fixture", "is_invalid": True}
    with patch.object(ApiClient, "request", return_value=ApiResponse(True, 200, row, "")) as send:
        result = invoke("devices", "history", "delete", "device", "--event-id", event_id)
    assert json.loads(result.stdout)["is_deleted"] is True
    assert send.call_args.args == ("PATCH", f"/api/developer/devices/device/timeline/{event_id}/")
    data = {"results": [row], "count": 1}
    with patch.object(ApiClient, "request", return_value=ApiResponse(True, 200, data, "")):
        assert json.loads(invoke("devices", "history", "list", "device").stdout)["count"] == 0
        shown = invoke("devices", "history", "show", "device", "--event-id", event_id)
        assert json.loads(shown.stdout)["id"] == event_id
        hidden = invoke("devices", "history", "show", "device", "--event-id", event_id, "--exclude-deleted")
        assert hidden.exit_code == 2


@pytest.mark.parametrize("verb", ["get", "post", "request"])
def test_raw_http_errors_keep_the_response_and_fail(invoke, verb):
    with patch.object(ApiClient, "request", return_value=ApiResponse(False, 404, {"detail": "missing"}, "")):
        result = invoke("api", verb, "/api/missing/")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"detail": "missing"}
