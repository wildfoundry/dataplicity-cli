from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
METADATA = ROOT / "build" / "linux" / "metadata.env"
BUILD_SCRIPT = ROOT / "build" / "linux" / "build_packages.sh"
SMOKE_SCRIPT = ROOT / "build" / "linux" / "smoke.sh"
MOCK_GATEWAY = ROOT / "scripts" / "port_forward_mock_gateway.py"
PORT_FORWARD_SMOKE = ROOT / "scripts" / "linux_port_forward_smoke.sh"


def _metadata() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in METADATA.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


class LinuxPackagingTest(unittest.TestCase):
    def test_linux_package_metadata_matches_release_contract(self) -> None:
        metadata = _metadata()
        self.assertEqual(metadata["PACKAGE_NAME"], "dataplicity-cli")
        self.assertEqual(metadata["COMMAND_NAME"], "dataplicity")
        self.assertEqual(metadata["MAINTAINER"], "WildFoundry Ltd")
        self.assertEqual(metadata["LICENSE"], "BSD-3-Clause")
        self.assertEqual(metadata["HOMEPAGE"], "https://github.com/wildfoundry/dataplicity-cli")
        self.assertEqual(metadata["DESCRIPTION"], "Dataplicity command line interface")
        self.assertEqual(metadata["INSTALL_PATH"], "/usr/bin")

    def test_linux_package_script_emits_deb_rpm_and_tarball(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("fpm", script)
        self.assertIn("--output-type deb", script)
        self.assertIn("--output-type rpm", script)
        self.assertIn('tar -C "${ARCHIVE_ROOT}" -czf', script)
        self.assertIn("${PACKAGE_NAME}_${VERSION}_${DEB_ARCH}.deb", script)
        self.assertIn("${PACKAGE_NAME}-${VERSION}-1.${RPM_ARCH}.rpm", script)
        self.assertIn('ARCHIVE_NAME="${PACKAGE_NAME}-${VERSION}-linux-${RPM_ARCH}"', script)
        self.assertIn("${DIST_DIR}/${ARCHIVE_NAME}.tar.gz", script)
        self.assertIn("${INSTALL_PATH}/${COMMAND_NAME}", script)

    def test_linux_packaging_helpers_are_executable_and_complete(self) -> None:
        self.assertTrue(BUILD_SCRIPT.exists())
        self.assertTrue(SMOKE_SCRIPT.exists())
        script = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("apt-get install --yes", script)
        self.assertIn("rpm --install --nodeps", script)
        self.assertIn("tar -C", script)
        self.assertIn("--version", script)
        self.assertIn("--help", script)
        self.assertIn('DEB_ARCH="${DEB_ARCH:-${DEFAULT_DEB_ARCH}}"', script)
        self.assertIn('RPM_ARCH="${RPM_ARCH:-${DEFAULT_RPM_ARCH}}"', script)

    def test_port_forward_mock_covers_remote_access_routes(self) -> None:
        source = MOCK_GATEWAY.read_text(encoding="utf-8")
        for snippet in (
            "/api/users/me",
            "/api/developer/devices",
            "/host",
            "/ports",
            "notify_open",
            "redirect-port",
            "m2m_url",
        ):
            self.assertIn(snippet, source)
        self.assertTrue(PORT_FORWARD_SMOKE.exists())
        smoke = PORT_FORWARD_SMOKE.read_text(encoding="utf-8")
        self.assertIn("devices port-forward", smoke)
        self.assertIn("dataplicity-cli linux port-forward ok", smoke)
        self.assertTrue((ROOT / "scripts" / "linux-vm-smoke.sh").exists())


if __name__ == "__main__":
    unittest.main()
