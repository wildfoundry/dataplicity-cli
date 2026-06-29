from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataplicity_cli.config import (
    DEFAULT_BASE_URL,
    LEGACY_DEFAULT_BASE_URLS,
    Config,
    default_config_path,
)


class ConfigTest(unittest.TestCase):
    def test_load_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = Config.load(Path(temp_dir) / "missing.json")
        self.assertEqual(cfg.base_url, DEFAULT_BASE_URL)
        self.assertIsNone(cfg.auth_method)
        self.assertIsNone(cfg.access_token)

    def test_load_invalid_json_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cli.json"
            path.write_text("{not-valid-json", encoding="utf-8")
            cfg = Config.load(path)
        self.assertEqual(cfg.base_url, DEFAULT_BASE_URL)
        self.assertIsNone(cfg.api_key)

    def test_load_upgrades_legacy_default_base_url(self) -> None:
        legacy_url = next(iter(LEGACY_DEFAULT_BASE_URLS))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cli.json"
            path.write_text(json.dumps({"base_url": legacy_url}), encoding="utf-8")
            cfg = Config.load(path)
        self.assertEqual(cfg.base_url, DEFAULT_BASE_URL)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "cli.json"
            cfg = Config(
                base_url="https://example.test",
                auth_method="jwt",
                access_token="a",
                refresh_token="r",
                api_key="k",
                last_email="test@example.com",
                preferred_login_method="sso",
            )
            cfg.save(path)
            loaded = Config.load(path)
        self.assertEqual(loaded.to_dict(), cfg.to_dict())

    def test_clear_tokens_resets_jwt_mode_only(self) -> None:
        cfg = Config(auth_method="jwt", access_token="a", refresh_token="r")
        cfg.clear_tokens()
        self.assertIsNone(cfg.auth_method)
        self.assertIsNone(cfg.access_token)
        self.assertIsNone(cfg.refresh_token)

    def test_clear_api_key_resets_api_key_mode_only(self) -> None:
        cfg = Config(auth_method="api_key", api_key="secret")
        cfg.clear_api_key()
        self.assertIsNone(cfg.auth_method)
        self.assertIsNone(cfg.api_key)

    def test_default_config_path_uses_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"DATAPLICITY_CONFIG_DIR": temp_dir}, clear=False):
                self.assertEqual(default_config_path(), Path(temp_dir) / "cli.json")


if __name__ == "__main__":
    unittest.main()
