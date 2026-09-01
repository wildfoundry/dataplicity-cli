from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "build" / "winget" / "prepare_manifest.py"
SPEC = importlib.util.spec_from_file_location("prepare_manifest", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WingetManifestTest(unittest.TestCase):
    def test_prepares_versioned_manifest_with_release_artifact(self) -> None:
        source_dir = (
            Path(__file__).parents[1]
            / "build"
            / "winget"
            / "manifests"
            / "w"
            / "Wildfoundry"
            / "DataplicityCLI"
            / "0.1.6"
        )
        installer_url = (
            "https://github.com/wildfoundry/dataplicity-cli/releases/download/"
            "v0.1.7/dataplicity-cli-0.1.7-windows-x64.msi"
        )
        installer_sha256 = "a" * 64

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = MODULE.prepare_manifest(
                source_dir=source_dir,
                output_root=Path(temp_dir),
                version="0.1.7",
                installer_url=installer_url,
                installer_sha256=installer_sha256,
                release_date="2026-07-20",
            )

            manifests = list(output_dir.glob("*.yaml"))
            self.assertEqual(len(manifests), 3)
            combined = "\n".join(manifest.read_text(encoding="utf-8") for manifest in manifests)
            self.assertNotIn("0.1.6", combined)
            self.assertIn("PackageVersion: 0.1.7", combined)
            self.assertIn(f"  InstallerUrl: {installer_url}", combined)
            self.assertIn(f"  InstallerSha256: {installer_sha256.upper()}", combined)
            self.assertIn("ReleaseDate: 2026-07-20", combined)

    def test_rejects_invalid_sha256(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.prepare_manifest(
                source_dir=Path("unused"),
                output_root=Path("unused"),
                version="0.1.7",
                installer_url="https://example.com/installer.msi",
                installer_sha256="invalid",
                release_date="2026-07-20",
            )


if __name__ == "__main__":
    unittest.main()
