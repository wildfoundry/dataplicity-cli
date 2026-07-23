from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


WIX_SOURCE = Path(__file__).parents[1] / "build" / "windows" / "DataplicityCLI.wxs"
WIX_NAMESPACE = {"wix": "http://schemas.microsoft.com/wix/2006/wi"}


class WindowsPackagingTest(unittest.TestCase):
    def test_msi_embeds_its_cabinet(self) -> None:
        root = ET.parse(WIX_SOURCE).getroot()
        media_template = root.find(".//wix:MediaTemplate", WIX_NAMESPACE)

        self.assertIsNotNone(media_template)
        self.assertEqual(media_template.get("EmbedCab"), "yes")


if __name__ == "__main__":
    unittest.main()
