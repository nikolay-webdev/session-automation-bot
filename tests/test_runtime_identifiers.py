import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


os.environ.update(
    {
        "BOT_TOKEN": "unit-test-token",
        "ALLOWED_CHAT_ID": "123",
        "DEFAULT_URL": "https://example.invalid/target",
        "DEFAULT_LOGIN": "unit-test-login",
        "DEFAULT_PASSWORD": "unit-test-password",
    }
)

import main
import watcher


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RuntimeIdentifierTests(unittest.TestCase):
    def test_python_identifiers_are_neutral(self):
        self.assertEqual(main.WAKE_TASK_NAME, "Session Automation Bot Wake")
        self.assertEqual(main.BOT_MUTEX_NAME, r"Local\SessionAutomationBotTelegramBot")
        self.assertEqual(watcher.WATCHER_MUTEX_NAME, r"Local\SessionAutomationBotWatcher")

    def test_wake_task_registration_and_removal_use_same_name(self):
        wake_at = datetime(2026, 11, 6, 8, 10, 0)

        with patch("main.subprocess.run") as run:
            main.register_wake_task(wake_at)
            register_command = " ".join(run.call_args.args[0])

            main.remove_wake_task()
            remove_command = " ".join(run.call_args.args[0])

        self.assertIn(
            f"Register-ScheduledTask -TaskName '{main.WAKE_TASK_NAME}'",
            register_command,
        )
        self.assertIn(
            f"Unregister-ScheduledTask -TaskName '{main.WAKE_TASK_NAME}'",
            remove_command,
        )
        self.assertIn("start_watcher_once.ps1", register_command)

    def test_watcher_install_script_uses_neutral_task_name(self):
        script = (PROJECT_ROOT / "install_bot_watcher_task.ps1").read_text(encoding="utf-8")
        self.assertIn('[string]$TaskName = "Session Automation Bot Watcher"', script)

    def test_wake_helper_script_uses_neutral_mutex(self):
        script = (PROJECT_ROOT / "start_watcher_once.ps1").read_text(encoding="utf-8")
        self.assertIn(r'"Local\SessionAutomationBotWakeHelper"', script)

    @patch("main.ctypes.get_last_error", return_value=0)
    @patch("main.ctypes.WinDLL")
    def test_bot_mutex_creation_uses_neutral_identifier(self, win_dll, _last_error):
        kernel32 = MagicMock()
        kernel32.CreateMutexW.return_value = 101
        win_dll.return_value = kernel32
        main.BOT_MUTEX_HANDLE = None

        try:
            self.assertTrue(main.acquire_bot_singleton())
            kernel32.CreateMutexW.assert_called_once_with(
                None,
                True,
                main.BOT_MUTEX_NAME,
            )
        finally:
            main.release_bot_singleton()

    @patch("watcher.ctypes.get_last_error", return_value=0)
    @patch("watcher.ctypes.WinDLL")
    def test_watcher_mutex_creation_uses_neutral_identifier(self, win_dll, _last_error):
        kernel32 = MagicMock()
        kernel32.CreateMutexW.return_value = 202
        win_dll.return_value = kernel32
        watcher.WATCHER_MUTEX_HANDLE = None

        try:
            self.assertTrue(watcher.acquire_watcher_singleton())
            kernel32.CreateMutexW.assert_called_once_with(
                None,
                True,
                watcher.WATCHER_MUTEX_NAME,
            )
        finally:
            watcher.release_watcher_singleton()


if __name__ == "__main__":
    unittest.main()
