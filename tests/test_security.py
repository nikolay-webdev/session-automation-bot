import os
import unittest


os.environ.update(
    {
        "BOT_TOKEN": "unit-test-token",
        "ALLOWED_CHAT_ID": "123",
        "DEFAULT_URL": "https://example.invalid/target",
        "DEFAULT_LOGIN": "unit-test-login",
        "DEFAULT_PASSWORD": "unit-test-password",
    }
)

from config import redact_sensitive_text


class SecurityTests(unittest.TestCase):
    def test_sensitive_diagnostics_are_redacted(self):
        token = "123" + "456" + ":" + ("A" * 24)
        password = "unit" + "-password"
        bearer = "private" + "-bearer"
        raw_text = (
            f"https://api.telegram.org/bot{token}/sendMessage "
            f"BOT_TOKEN={token} DEFAULT_LOGIN=student DEFAULT_PASSWORD={password} "
            "Cookie: sessionid=private-session "
            f"Authorization: Bearer {bearer} "
            r"C:\Users\ExampleUser\Downloads\Projects\Abgemeldet\main.py"
        )

        safe_text = redact_sensitive_text(raw_text)

        self.assertNotIn(token, safe_text)
        self.assertNotIn(password, safe_text)
        self.assertNotIn("private-session", safe_text)
        self.assertNotIn(bearer, safe_text)
        self.assertNotIn("ExampleUser", safe_text)
        self.assertIn("[REDACTED]", safe_text)

