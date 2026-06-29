from __future__ import annotations

import runpy
import unittest
from unittest.mock import patch


class EntrypointTest(unittest.TestCase):
    def test_main_module_invokes_cli_app(self) -> None:
        with patch("dataplicity_cli.cli.app") as mock_app:
            runpy.run_module("dataplicity_cli.__main__", run_name="__main__")
        mock_app.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
