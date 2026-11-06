import os
import re
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required setting: {name}. Add it to .env.")
    return value


def _required_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Setting {name} must be a number.") from exc


def _optional_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Setting {name} must be a number.") from exc


def _required_url(name: str) -> str:
    value = _required(name)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Setting {name} must be a valid HTTP(S) URL.")
    return value


def redact_sensitive_text(text: str) -> str:
    """Remove credentials, session data and personal Windows paths from diagnostics."""
    safe_text = str(text)

    patterns = (
        (r"(?i)(https?://api\.telegram\.org/bot)[^/\s\"']+", r"\1[REDACTED]"),
        (r"(?i)\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_TELEGRAM_TOKEN]"),
        (
            r"(?i)(\b(?:BOT_TOKEN|DEFAULT_LOGIN|DEFAULT_PASSWORD|PASSWORD|SECRET|API_KEY)\b\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)(\b(?:Cookie|Set-Cookie|Authorization)\s*:\s*)[^\r\n]+", r"\1[REDACTED]"),
        (
            r"(?i)([?&](?:session(?:_?id)?|jsessionid|phpsessid|token)=)[^&\s]+",
            r"\1[REDACTED]",
        ),
        (r"(?i)([A-Z]:\\Users\\)[^\\/:*?\"<>|]+", r"\1[REDACTED]"),
    )

    for pattern, replacement in patterns:
        safe_text = re.sub(pattern, replacement, safe_text)

    return safe_text


_load_env_file()

BOT_TOKEN = _required("BOT_TOKEN")
ALLOWED_CHAT_ID = _required_int("ALLOWED_CHAT_ID")
DEFAULT_URL = _required_url("DEFAULT_URL")
DEFAULT_LOGIN = _required("DEFAULT_LOGIN")
DEFAULT_PASSWORD = _required("DEFAULT_PASSWORD")

WATCHDOG_EVERY_SECONDS = _optional_int("WATCHDOG_EVERY_SECONDS", 60)
