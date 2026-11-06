import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


os.environ.update(
    {
        "BOT_TOKEN": "unit-test-token",
        "ALLOWED_CHAT_ID": "123",
        "DEFAULT_URL": "https://example.invalid/target",
        "DEFAULT_LOGIN": "unit-test-login",
        "DEFAULT_PASSWORD": "unit-test-password",
    }
)

import config


class ConfigTests(unittest.TestCase):
    def test_load_env_file_reads_values_without_overwriting_environment(self):
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "UNIT_TEST_VALUE=from-file\n"
                "UNIT_TEST_QUOTED='quoted value'\n"
                "UNIT_TEST_EXISTING=from-file\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"UNIT_TEST_EXISTING": "from-environment"}, clear=False):
                os.environ.pop("UNIT_TEST_VALUE", None)
                os.environ.pop("UNIT_TEST_QUOTED", None)
                with patch.object(config, "ENV_FILE", env_file):
                    config._load_env_file()

                self.assertEqual(os.environ["UNIT_TEST_VALUE"], "from-file")
                self.assertEqual(os.environ["UNIT_TEST_QUOTED"], "quoted value")
                self.assertEqual(os.environ["UNIT_TEST_EXISTING"], "from-environment")

    def test_required_and_optional_integer_validation(self):
        with patch.dict(os.environ, {"UNIT_TEST_INT": "42"}, clear=False):
            self.assertEqual(config._required_int("UNIT_TEST_INT"), 42)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UNIT_TEST_MISSING", None)
            self.assertEqual(config._optional_int("UNIT_TEST_MISSING", 7), 7)

            with self.assertRaisesRegex(RuntimeError, "Missing required setting"):
                config._required("UNIT_TEST_MISSING")

    def test_invalid_integer_raises_clear_error(self):
        with patch.dict(os.environ, {"UNIT_TEST_INT": "not-a-number"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must be a number"):
                config._required_int("UNIT_TEST_INT")

    def test_missing_default_url_raises_clear_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFAULT_URL", None)
            with self.assertRaisesRegex(RuntimeError, "Missing required setting: DEFAULT_URL"):
                config._required_url("DEFAULT_URL")

    def test_invalid_default_url_raises_clear_error_without_echoing_value(self):
        with patch.dict(os.environ, {"DEFAULT_URL": "not-a-url-with-secret"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, r"valid HTTP\(S\) URL") as error:
                config._required_url("DEFAULT_URL")
            self.assertNotIn("secret", str(error.exception))

