from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


WIX_SOURCE = Path(__file__).parents[1] / "build" / "windows" / "DataplicityCLI.wxs"
WIX_NAMESPACE = {"wix": "http://schemas.microsoft.com/wix/2006/wi"}


class WindowsPackagingTest(unittest.TestCase):
    def test_msi_targets_64_bit_program_files(self) -> None:
        root = ET.parse(WIX_SOURCE).getroot()
        package = root.find(".//wix:Package", WIX_NAMESPACE)
        install_root = root.find(".//wix:Directory[@Id='ProgramFiles64Folder']", WIX_NAMESPACE)
        executable_component = root.find(".//wix:Component[@Id='MainExecutable']", WIX_NAMESPACE)

        self.assertIsNotNone(package)
        self.assertEqual(package.get("Platform"), "x64")
        self.assertIsNotNone(install_root)
        self.assertIsNotNone(executable_component)
        self.assertEqual(executable_component.get("Win64"), "yes")

    def test_msi_embeds_its_cabinet(self) -> None:
        root = ET.parse(WIX_SOURCE).getroot()
        media_template = root.find(".//wix:MediaTemplate", WIX_NAMESPACE)

        self.assertIsNotNone(media_template)
        self.assertEqual(media_template.get("EmbedCab"), "yes")


if __name__ == "__main__":
    unittest.main()
