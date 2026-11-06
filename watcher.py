import ctypes
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

from config import ALLOWED_CHAT_ID, BOT_TOKEN


CHECK_INTERVAL = 10
BASE_DIR = Path(__file__).resolve().parent
MAIN_SCRIPT = BASE_DIR / "main.py"
WATCHER_MUTEX_NAME = r"Local\SessionAutomationBotWatcher"
WATCHER_MUTEX_HANDLE = None


def send_telegram(text: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": ALLOWED_CHAT_ID,
                "text": text,
            },
            timeout=5,
        )
    except Exception:
        pass


def acquire_watcher_singleton() -> bool:
    global WATCHER_MUTEX_HANDLE

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p

        handle = kernel32.CreateMutexW(None, True, WATCHER_MUTEX_NAME)
        last_error = ctypes.get_last_error()

        if not handle:
            return True

        if last_error == 183:
            return False

        WATCHER_MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def release_watcher_singleton() -> None:
    global WATCHER_MUTEX_HANDLE

    if not WATCHER_MUTEX_HANDLE:
        return

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex(ctypes.c_void_p(WATCHER_MUTEX_HANDLE))
        kernel32.CloseHandle(ctypes.c_void_p(WATCHER_MUTEX_HANDLE))
    except Exception:
        pass
    finally:
        WATCHER_MUTEX_HANDLE = None


def start_bot():
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, str(MAIN_SCRIPT)],
        cwd=BASE_DIR,
        creationflags=creationflags,
    )


def is_current_process(process: psutil.Process) -> bool:
    return process.pid == psutil.Process().pid


def is_project_main_process(process: psutil.Process) -> bool:
    try:
        if is_current_process(process):
            return False

        info = process.as_dict(attrs=["name", "cmdline"])
        name = (info.get("name") or "").lower()
        if "python" not in name:
            return False

        cmdline = info.get("cmdline") or []
        if not any(Path(str(arg)).name.lower() == "main.py" for arg in cmdline):
            return False

        for arg in cmdline:
            try:
                if Path(str(arg)).resolve() == MAIN_SCRIPT:
                    return True
            except Exception:
                pass

        try:
            return Path(process.cwd()).resolve() == BASE_DIR
        except Exception:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def find_running_bot() -> psutil.Process | None:
    for process in psutil.process_iter():
        if is_project_main_process(process):
            return process
    return None


def main() -> None:
    if not acquire_watcher_singleton():
        return

    try:
        send_telegram("Watcher gestartet. Ich ueberwache den Bot.")

        process = find_running_bot()
        if process is None:
            process = start_bot()
        else:
            send_telegram("Bot laeuft bereits. Kein zweiter Start.")

        while True:
            time.sleep(CHECK_INTERVAL)

            running_process = find_running_bot()
            if running_process is not None:
                process = running_process
                continue

            send_telegram("Bot ist abgestuerzt. Starte neu...")
            process = start_bot()
            send_telegram("Bot wurde neu gestartet.")
    finally:
        release_watcher_singleton()


if __name__ == "__main__":
    main()
