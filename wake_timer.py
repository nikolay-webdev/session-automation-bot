import argparse
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
WAKE_LOG_FILE = BASE_DIR / "wake_log.txt"
WINDOWS_EPOCH_OFFSET = 116444736000000000
ERROR_NOT_SUPPORTED = 50
WAIT_OBJECT_0 = 0
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
CREATE_NO_WINDOW = 0x08000000
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with WAKE_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def write_state(path: Path, payload: dict) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def to_windows_filetime(local_wake_at: datetime) -> int:
    utc_wake_at = local_wake_at.astimezone(timezone.utc)
    return int(utc_wake_at.timestamp() * 10_000_000) + WINDOWS_EPOCH_OFFSET


def launch_wake_helper() -> None:
    script_path = BASE_DIR / "start_watcher_once.ps1"
    subprocess.Popen(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-PythonExe",
            sys.executable,
        ],
        cwd=BASE_DIR,
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )


def run_wake_timer(wake_at: datetime, state_path: Path, launch_helper: bool = True) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateWaitableTimerW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateWaitableTimerW.restype = wintypes.HANDLE
    kernel32.SetWaitableTimer.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.LONG,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    kernel32.SetWaitableTimer.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    timer_handle = kernel32.CreateWaitableTimerW(None, True, None)
    if not timer_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        due_time = ctypes.c_longlong(to_windows_filetime(wake_at))
        ctypes.set_last_error(0)
        timer_set = kernel32.SetWaitableTimer(
            timer_handle,
            ctypes.byref(due_time),
            0,
            None,
            None,
            True,
        )
        last_error = ctypes.get_last_error()

        if not timer_set:
            raise ctypes.WinError(last_error)
        if last_error == ERROR_NOT_SUPPORTED:
            raise OSError("Windows or firmware does not support wakeable timers.")

        wake_label = wake_at.strftime("%Y-%m-%d %H:%M:%S")
        write_state(
            state_path,
            {
                "status": "ready",
                "pid": int(kernel32.GetCurrentProcessId()),
                "wake_at": wake_label,
                "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        log(f"Nativer Wake-Timer registriert für {wake_label}. PID={kernel32.GetCurrentProcessId()}")

        wait_result = kernel32.WaitForSingleObject(timer_handle, INFINITE)
        if wait_result == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if wait_result != WAIT_OBJECT_0:
            raise OSError(f"Unexpected wait result: {wait_result}")

        fired_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_state(
            state_path,
            {
                "status": "fired",
                "pid": int(kernel32.GetCurrentProcessId()),
                "wake_at": wake_label,
                "fired_at": fired_at,
            },
        )
        log(f"Nativer Wake-Timer ausgelöst. Geplant={wake_label}, Tatsächlich={fired_at}")

        if launch_helper:
            launch_wake_helper()
            log("Nativer Wake-Timer hat start_watcher_once.ps1 gestartet.")
    finally:
        kernel32.CloseHandle(timer_handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wake-at", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--no-wake-action", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state_file).resolve()

    try:
        wake_at = datetime.strptime(args.wake_at, "%Y-%m-%d %H:%M:%S").astimezone()
        run_wake_timer(wake_at, state_path, launch_helper=not args.no_wake_action)
        return 0
    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        try:
            write_state(
                state_path,
                {
                    "status": "error",
                    "error": error_text,
                    "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            log(f"Native wakeable timer failed: {error_text}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
