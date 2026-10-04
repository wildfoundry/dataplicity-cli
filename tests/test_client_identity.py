from __future__ import annotations

import unittest
from unittest.mock import patch

from dataplicity_cli import __version__
from dataplicity_cli.client_identity import (
    CLIENT_APP,
    client_machine,
    client_platform,
    identity_headers,
    user_agent,
)


class ClientIdentityTest(unittest.TestCase):
    def test_linux_headers_match_backend_contract(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value="Linux"):
                with patch("dataplicity_cli.client_identity.platform.machine", return_value="x86_64"):
                    headers = identity_headers("install-id-1234", version="0.1.7")
        self.assertEqual(headers["User-Agent"], "dataplicity-cli/0.1.7 (Linux; x86_64)")
        self.assertEqual(headers["X-Client-App"], CLIENT_APP)
        self.assertEqual(headers["X-Client-Platform"], "linux")
        self.assertEqual(headers["X-Client-Version"], "0.1.7")
        self.assertEqual(headers["X-Install-Id"], "install-id-1234")

    def test_windows_user_agent_includes_windows_nt(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "nt"):
            with patch("dataplicity_cli.client_identity.platform.machine", return_value="AMD64"):
                self.assertEqual(client_platform(), "windows")
                self.assertEqual(user_agent("1.2.3"), "dataplicity-cli/1.2.3 (Windows NT; AMD64)")

    def test_macos_user_agent_includes_macintosh(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value="Darwin"):
                with patch("dataplicity_cli.client_identity.platform.machine", return_value="arm64"):
                    self.assertEqual(client_platform(), "macos")
                    self.assertEqual(user_agent("1.2.3"), "dataplicity-cli/1.2.3 (Macintosh; Darwin arm64)")

    def test_unknown_platform_falls_back_to_system_name(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value="FreeBSD"):
                with patch("dataplicity_cli.client_identity.platform.machine", return_value="amd64"):
                    self.assertEqual(client_platform(), "freebsd")
                    self.assertEqual(user_agent("9.9.9"), "dataplicity-cli/9.9.9 (FreeBSD; amd64)")

    def test_blank_system_and_machine_use_unknown(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value=""):
                with patch("dataplicity_cli.client_identity.platform.machine", return_value=""):
                    self.assertEqual(client_platform(), "unknown")
                    self.assertEqual(client_machine(), "unknown")
                    self.assertEqual(user_agent("0.1.7"), "dataplicity-cli/0.1.7 (Unknown; unknown)")

    def test_windows_system_name_without_nt_os(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value="Windows"):
                self.assertEqual(client_platform(), "windows")

    def test_macos_aliases_and_windows_system_tokens(self) -> None:
        with patch("dataplicity_cli.client_identity.os.name", "posix"):
            with patch("dataplicity_cli.client_identity.platform.system", return_value="mac"):
                self.assertEqual(client_platform(), "macos")
            with patch("dataplicity_cli.client_identity.platform.system", return_value="win32"):
                self.assertEqual(client_platform(), "windows")
            with patch("dataplicity_cli.client_identity.platform.system", return_value="macos"):
                self.assertEqual(client_platform(), "macos")
            with patch("dataplicity_cli.client_identity.platform.system", return_value="windows_nt"):
                self.assertEqual(client_platform(), "windows")

    def test_omits_install_id_when_blank(self) -> None:
        headers = identity_headers("  ", version=__version__)
        self.assertNotIn("X-Install-Id", headers)
        self.assertEqual(headers["X-Client-App"], "dataplicity-cli")
        self.assertEqual(headers["X-Client-Version"], __version__)

    def test_blank_version_falls_back_to_package_version(self) -> None:
        headers = identity_headers(version="   ")
        self.assertEqual(headers["X-Client-Version"], __version__)
        self.assertTrue(headers["User-Agent"].startswith(f"{CLIENT_APP}/{__version__} "))

    def test_default_version_uses_package_version(self) -> None:
        headers = identity_headers()
        self.assertTrue(headers["User-Agent"].startswith(f"{CLIENT_APP}/{__version__} "))
        self.assertEqual(headers["X-Client-Version"], __version__)


if __name__ == "__main__":
    unittest.main()
