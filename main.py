import asyncio
import json
import threading
import time
import subprocess
import psutil
import requests
import sys
import ctypes
from datetime import datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

from config import (
    ALLOWED_CHAT_ID,
    BOT_TOKEN,
    DEFAULT_LOGIN,
    DEFAULT_PASSWORD,
    DEFAULT_URL,
    WATCHDOG_EVERY_SECONDS,
    redact_sensitive_text,
)


LOGIN_SELECTOR = "#username"
PASSWORD_SELECTOR = "#password"
LOGIN_BUTTON_SELECTOR = "#loginbtn"
POLICY_CONTINUE_XPATH = (
    "//*[self::button or self::a or self::input]"
    "[normalize-space(.)='Fortsetzen' or @value='Fortsetzen']"
)

AFTER_LOGIN_BUTTON_SELECTOR = ".drawer-right-toggle .btn.icon-no-margin"

TARGET_BUTTON_SELECTOR_TEST = ".drawer.drawer-right .drawertoggle"  # menu click test
TARGET_BUTTON_SELECTOR_REAL = ".desktop-only a[href*='stoppen=1']"  # link um auszulogen
CHECKIN_RADIO_SELECTOR = "#flexRadioDefault2"
CHECKIN_SUBMIT_SELECTOR = "form[action='?starten=1'] input[type='submit']"
CHECKIN_SUCCESS_LINK_SELECTOR = ".desktop-only a[href*='stoppen=1']"
BASE_DIR = Path(__file__).resolve().parent
BOT_LOG_FILE = BASE_DIR / "bot_console.log"
WAKE_STATE_FILE = BASE_DIR / "sleep_wake_state.json"
NATIVE_WAKE_TIMER_STATE_FILE = BASE_DIR / "wake_timer_state.json"
NATIVE_WAKE_TIMER_SCRIPT = "wake_timer.py"
EARLY_WAKE_GRACE_SECONDS = 10 * 60
TELEGRAM_RETRY_SECONDS = 60
WAKE_TASK_NAME = "GFN Bot Wake"
WAKE_START_SCRIPT = "start_watcher_once.ps1"
WAKE_PREPARE_MINUTES = 10
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
BOT_MUTEX_NAME = r"Local\GFNAbgemeldetTelegramBot"
BOT_MUTEX_HANDLE = None



def console_log(text: str) -> None:
    safe_text = redact_sensitive_text(text)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {safe_text}"
    try:
        print(line)
    except UnicodeEncodeError:
        console_encoding = sys.stdout.encoding or "utf-8"
        safe_line = line.encode(console_encoding, errors="backslashreplace").decode(console_encoding)
        print(safe_line)
    try:
        with BOT_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_bot_message(text: str) -> None:
    console_log(f"BOT -> Telegram: {text}")


async def reply_user(update: Update, text: str, parse_mode: str | None = None) -> None:
    safe_text = redact_sensitive_text(text)
    log_bot_message(safe_text)
    await update.message.reply_text(safe_text, parse_mode=parse_mode)

CURRENT_DRIVER = None
CURRENT_STOP_EVENT = None
CURRENT_RUN_ID = 0
EXPECTED_WAKE_DT = None
EARLY_WAKE_RESLEEP_IN_PROGRESS = False
LAST_STATUS = "Bot läuft."
LAST_ERROR = ""
LAST_ACTION_TIME = ""


def set_status(status: str, error: str = "") -> None:
    global LAST_STATUS
    global LAST_ERROR
    global LAST_ACTION_TIME

    LAST_STATUS = redact_sensitive_text(status)
    LAST_ERROR = redact_sensitive_text(error)
    LAST_ACTION_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stop_current_worker() -> None:
    global CURRENT_STOP_EVENT

    if CURRENT_STOP_EVENT is not None:
        CURRENT_STOP_EVENT.set()
        CURRENT_STOP_EVENT = None


def is_run_cancelled(run_id: int | None = None, stop_event: threading.Event | None = None) -> bool:
    if stop_event is not None and stop_event.is_set():
        return True
    if run_id is not None and run_id != CURRENT_RUN_ID:
        return True
    return False


def cancel_current_run() -> None:
    global CURRENT_RUN_ID

    CURRENT_RUN_ID += 1
    stop_current_worker()
    close_current_driver()


def close_current_driver() -> None:
    global CURRENT_DRIVER

    if CURRENT_DRIVER is not None:
        try:
            CURRENT_DRIVER.quit()
        except Exception:
            pass

        CURRENT_DRIVER = None


def click_element(driver, selector: str, timeout: int = 30) -> None:
    element = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
    )

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.5)

    try:
        WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
        ).click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def accept_optional_alert(driver, timeout: int = 3) -> tuple[bool, str]:
    try:
        alert = WebDriverWait(driver, timeout).until(EC.alert_is_present())
        alert_text = alert.text
        alert.accept()
        return True, alert_text
    except Exception:
        return False, ""


def accept_optional_policy(driver, timeout: int = 5) -> bool:
    def find_continue_button(current_driver):
        try:
            body_text = current_driver.find_element(By.TAG_NAME, "body").text.lower()
            if "nutzungsrichtlinie" not in body_text:
                return False

            for element in current_driver.find_elements(By.XPATH, POLICY_CONTINUE_XPATH):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            return False

        return False

    try:
        continue_button = WebDriverWait(driver, timeout).until(find_continue_button)
        try:
            continue_button.click()
        except Exception:
            continue_button = WebDriverWait(driver, timeout).until(find_continue_button)
            driver.execute_script("arguments[0].click();", continue_button)

        console_log("LOGIN: Nutzungsrichtlinie bestaetigt (Fortsetzen).")

        try:
            WebDriverWait(driver, 10).until(lambda current_driver: not find_continue_button(current_driver))
        except TimeoutException:
            pass

        return True
    except TimeoutException:
        return False


def fill_login_fields(driver, login: str, password: str, timeout: int = 30) -> None:
    last_error = None

    for _ in range(3):
        try:
            login_input = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_SELECTOR))
            )
            password_input = driver.find_element(By.CSS_SELECTOR, PASSWORD_SELECTOR)

            login_input.clear()
            login_input.send_keys(login)
            password_input.clear()
            password_input.send_keys(password)
            return
        except StaleElementReferenceException as error:
            last_error = error
            time.sleep(0.5)

    raise last_error


def get_checkin_startzeit(driver, timeout: int = 30) -> str:
    success_link = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, CHECKIN_SUCCESS_LINK_SELECTOR))
    )

    try:
        container = success_link.find_element(
            By.XPATH,
            "./ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' desktop-only ')][1]"
        )
        result_text = container.text.strip()
    except Exception:
        result_text = success_link.text.strip()

    for line in result_text.splitlines():
        if "Startzeit" in line:
            return line.strip()

    return result_text or "Startzeit wurde gefunden."


def get_desktop_time_line(driver, label: str, timeout: int = 30) -> str:
    def find_time_line(active_driver):
        try:
            for container in active_driver.find_elements(By.CSS_SELECTOR, ".desktop-only"):
                if not container.is_displayed():
                    continue

                for line in container.text.strip().splitlines():
                    normalized_line = " ".join(line.split())
                    if label in normalized_line:
                        return normalized_line
        except StaleElementReferenceException:
            return False

        return False

    return WebDriverWait(driver, timeout).until(find_time_line)


def get_visible_checkin_state(driver):
    try:
        if any(
            element.is_displayed()
            for element in driver.find_elements(By.CSS_SELECTOR, CHECKIN_SUCCESS_LINK_SELECTOR)
        ):
            return "success"
        if any(
            element.is_displayed()
            for element in driver.find_elements(By.CSS_SELECTOR, CHECKIN_RADIO_SELECTOR)
        ):
            return "form"
    except StaleElementReferenceException:
        pass

    return False


def wait_for_checkin_form_or_success(driver, timeout: int = 30) -> str:
    return WebDriverWait(driver, timeout).until(get_visible_checkin_state)


def wait_for_checkin_state(driver, timeout: int = 60) -> str:
    console_log(f"CHECK-IN: Oeffne rechte Blockleiste auf Zielseite: {driver.current_url}")

    if not get_visible_checkin_state(driver):
        click_element(driver, AFTER_LOGIN_BUTTON_SELECTOR, timeout=30)
        console_log("CHECK-IN: Rechte Blockleiste wurde geoeffnet.")

    console_log("CHECK-IN: Suche sichtbare Starten-Form oder aktive Startzeit.")
    return wait_for_checkin_form_or_success(driver, timeout=timeout)


def get_page_diagnostics(driver, max_text_length: int = 500) -> str:
    details = []

    try:
        details.append(f"URL: {redact_sensitive_text(driver.current_url)}")
    except Exception:
        pass

    return " | ".join(details) or "Keine Seitendetails verfuegbar."


def wait_for_post_login_page(driver, timeout: int = 60) -> None:
    def get_login_state(active_driver):
        try:
            body_text = active_driver.find_element(By.TAG_NAME, "body").text.lower()
            if "ungueltige anmeldung" in body_text or "ungültige anmeldung" in body_text:
                return "invalid"

            ready_selectors = (
                AFTER_LOGIN_BUTTON_SELECTOR,
                CHECKIN_SUCCESS_LINK_SELECTOR,
                CHECKIN_RADIO_SELECTOR,
            )
            if any(
                active_driver.find_elements(By.CSS_SELECTOR, selector)
                for selector in ready_selectors
            ):
                return "ready"
        except StaleElementReferenceException:
            return False

        return False

    try:
        login_state = WebDriverWait(driver, timeout).until(get_login_state)
    except TimeoutException as error:
        raise TimeoutException(
            "Die Zielseite wurde nach dem Login nicht rechtzeitig geladen. "
            f"{get_page_diagnostics(driver)}"
        ) from error

    if login_state == "invalid":
        raise RuntimeError(
            "Die Webanwendung hat die Anmeldung abgelehnt. "
            f"{get_page_diagnostics(driver)}"
        )


def open_site_and_checkin(
    url: str,
    login: str,
    password: str,
    run_id: int | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[bool, str]:
    global CURRENT_DRIVER

    driver = None

    try:
        set_status("Check-in: Browser wird geoeffnet...")

        options = Options()
        options.enable_bidi = True
        options.add_argument("-headless")

        driver = webdriver.Firefox(options=options)
        CURRENT_DRIVER = driver

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        driver.set_window_size(1280, 900)
        driver.get(url)

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        if accept_optional_policy(driver, timeout=8):
            set_status("Check-in: Nutzungsrichtlinie wurde bestaetigt.")

        set_status("Check-in: Login-Seite geoeffnet. Ich suche das Login-Feld...")
        fill_login_fields(driver, login, password)

        set_status("Check-in: Login-Daten wurden eingetragen. Ich klicke auf Login...")

        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, LOGIN_BUTTON_SELECTOR))
        ).click()

        time.sleep(1)

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        alert_accepted, alert_text = accept_optional_alert(driver, timeout=5)
        if alert_accepted:
            console_log(f"CHECK-IN: Alert bestaetigt: {alert_text}")

        if accept_optional_policy(driver, timeout=8):
            set_status("Check-in: Nutzungsrichtlinie wurde bestaetigt.")

        set_status("Check-in: Ich warte auf die Zielseite...")
        wait_for_post_login_page(driver, timeout=60)

        set_status("Check-in: Ich suche die Starten-Form auf der Zielseite...")
        checkin_state = wait_for_checkin_state(driver, timeout=90)

        if checkin_state == "success":
            startzeit = get_checkin_startzeit(driver)
            now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_status(f"Check-in bereits aktiv. {startzeit}")
            return True, (
                "Check-in ist bereits aktiv.\n"
                f"Aktuelle Zeit: {now_label}\n"
                f"{startzeit}"
            )

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        radio = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, CHECKIN_RADIO_SELECTOR))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", radio)
        if not radio.is_selected():
            radio.click()

        set_status("Check-in: Standort wurde ausgewaehlt. Ich sende die Form...")

        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, CHECKIN_SUBMIT_SELECTOR))
        ).click()

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        startzeit = get_checkin_startzeit(driver, timeout=60)
        now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        set_status(f"Check-in erfolgreich. {startzeit}")
        return True, (
            "Check-in erfolgreich.\n"
            f"Aktuelle Zeit: {now_label}\n"
            f"{startzeit}"
        )

    except TimeoutException as e:
        error_text = (
            f"Timeout bei Schritt: {LAST_STATUS}. "
            f"{get_page_diagnostics(driver) if driver is not None else 'Browser nicht verfuegbar.'}"
        )
        now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        console_log(f"CHECK-IN: {error_text}")
        set_status("Fehler beim Check-in.", error_text)
        return False, (
            "Check-in fehlgeschlagen.\n"
            f"Aktuelle Zeit: {now_label}\n"
            f"Fehler: {error_text}"
        )

    except Exception as e:
        error_text = str(e).strip() or e.__class__.__name__
        now_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        set_status("Fehler beim Check-in.", error_text)
        return False, (
            "Check-in fehlgeschlagen.\n"
            f"Aktuelle Zeit: {now_label}\n"
            f"Fehler: {error_text}"
        )

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

        if CURRENT_DRIVER is driver:
            CURRENT_DRIVER = None


def open_site_and_login(
    url: str,
    login: str,
    password: str,
    target_selector: str = TARGET_BUTTON_SELECTOR_TEST,
    run_id: int | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[bool, str]:
    global CURRENT_DRIVER

    driver = None

    try:
        set_status("Browser wird geöffnet...")

        options = Options()
        options.enable_bidi = True
        options.add_argument("-headless")

        driver = webdriver.Firefox(options=options)
        CURRENT_DRIVER = driver

        if is_run_cancelled(run_id, stop_event):
            driver.quit()
            CURRENT_DRIVER = None
            return False, "Vorgang wurde abgebrochen."

        driver.set_window_size(1280, 900)
        driver.get(url)

        if is_run_cancelled(run_id, stop_event):
            driver.quit()
            CURRENT_DRIVER = None
            return False, "Vorgang wurde abgebrochen."

        set_status("Login-Seite geöffnet. Ich suche das Login-Feld...")

        if accept_optional_policy(driver, timeout=8):
            set_status("Nutzungsrichtlinie wurde bestaetigt.")

        if is_run_cancelled(run_id, stop_event):
            driver.quit()
            CURRENT_DRIVER = None
            return False, "Vorgang wurde abgebrochen."

        fill_login_fields(driver, login, password)

        set_status("Login-Daten wurden eingetragen. Ich klicke auf Login...")

        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, LOGIN_BUTTON_SELECTOR))
        ).click()

        time.sleep(1)

        if is_run_cancelled(run_id, stop_event):
            driver.quit()
            CURRENT_DRIVER = None
            return False, "Vorgang wurde abgebrochen."

        set_status("Login wurde gesendet. Ich suche die Schaltfläche nach dem Login...")

        alert_accepted, alert_text = accept_optional_alert(driver, timeout=5)
        if alert_accepted:
            console_log(f"LOGIN: Alert bestaetigt: {alert_text}")

        if accept_optional_policy(driver, timeout=8):
            set_status("Nutzungsrichtlinie wurde bestaetigt.")

        set_status("Login wurde gesendet. Ich warte auf die Zielseite...")
        wait_for_post_login_page(driver, timeout=60)

        if target_selector == TARGET_BUTTON_SELECTOR_REAL:
            set_status("Login wurde gesendet. Ich suche die Beenden-Schaltflaeche auf der Zielseite...")
            checkin_state = wait_for_checkin_state(driver, timeout=90)

            if checkin_state != "success":
                set_status("Anmeldung erfolgreich, aber keine Beenden-Schaltflaeche gefunden.")
                driver.quit()
                CURRENT_DRIVER = None
                return False, (
                    "Anmeldung erfolgreich, aber Abmelden ist nicht bereit.\n"
                    "Es wurde keine aktive Startzeit/Beenden-Schaltflaeche gefunden."
                )

            CURRENT_DRIVER = driver
            set_status("Anmeldung erfolgreich. Beenden-Schaltflaeche gefunden.")
            return True, "Browser geoeffnet. Anmeldung erfolgreich. Die Beenden-Schaltflaeche wurde gefunden."

        click_element(driver, AFTER_LOGIN_BUTTON_SELECTOR, timeout=60)

        set_status("Zwischen-Schaltfläche wurde geklickt. Ich suche die Ziel-Schaltfläche...")

        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, TARGET_BUTTON_SELECTOR_TEST))
        )

        if is_run_cancelled(run_id, stop_event):
            driver.quit()
            CURRENT_DRIVER = None
            return False, "Vorgang wurde abgebrochen."

        CURRENT_DRIVER = driver

        set_status("Anmeldung erfolgreich. Ziel-Schaltfläche gefunden.")
        return True, "Browser geöffnet. Anmeldung erfolgreich. Die Ziel-Schaltfläche wurde gefunden."

    except Exception as e:
        error_text = str(e)

        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

        if CURRENT_DRIVER is driver:
            CURRENT_DRIVER = None

        if is_run_cancelled(run_id, stop_event):
            return False, "Vorgang wurde abgebrochen."

        set_status("Fehler bei Anmeldung oder Vorbereitung.", error_text)
        return False, f"Fehler bei der Anmeldung oder beim Finden der Schaltfläche: {error_text}"

def build_target_datetime(target_time_str: str) -> datetime:
    try:
        return datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    now = datetime.now()
    target = datetime.strptime(target_time_str, "%H:%M").replace(
        year=now.year,
        month=now.month,
        day=now.day,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def serialize_target_datetime(target_dt: datetime) -> str:
    return target_dt.strftime("%Y-%m-%d %H:%M:%S")


def build_fixed_time_today_or_tomorrow(hour: int, minute: int, second: int = 0) -> datetime:
    now = datetime.now()
    target_dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    return target_dt


def build_next_workday_datetime(hour: int, minute: int, now: datetime | None = None) -> datetime:
    current_dt = now or datetime.now()
    target_dt = (current_dt + timedelta(days=1)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    while target_dt.weekday() >= 5:
        target_dt += timedelta(days=1)

    return target_dt


def build_today_datetime(hour: int, minute: int, now: datetime | None = None) -> datetime:
    current_dt = now or datetime.now()
    target_dt = current_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target_dt <= current_dt:
        raise ValueError("Diese Uhrzeit ist heute bereits vorbei.")

    return target_dt


def format_target_datetime(target_dt: datetime) -> str:
    if target_dt.date() == datetime.now().date():
        return target_dt.strftime("%H:%M:%S")
    return target_dt.strftime("%Y-%m-%d %H:%M")


def format_remaining(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours > 0:
        return f"{hours} Std. {minutes} Min. {secs} Sek."
    if minutes > 0:
        return f"{minutes} Min. {secs} Sek."
    return f"{secs} Sek."


def get_next_report_delay_seconds(remaining_seconds: float) -> float:
    if remaining_seconds > 60 * 60:
        return min(30 * 60, remaining_seconds - 60 * 60)
    if remaining_seconds > 30 * 60:
        return min(10 * 60, remaining_seconds - 30 * 60)
    if remaining_seconds > 5 * 60:
        return min(5 * 60, remaining_seconds - 5 * 60)
    return min(60, remaining_seconds)


def get_target_status_text(target_time_str: str) -> tuple[str, str]:
    if not target_time_str or target_time_str == "-":
        return "-", "-"

    try:
        target_dt = build_target_datetime(target_time_str)
    except Exception:
        return target_time_str, "-"

    remaining = (target_dt - datetime.now()).total_seconds()
    return format_target_datetime(target_dt), format_remaining(remaining)


def build_wake_datetime(wake_code: str) -> datetime:
    if wake_code == "wake_plus_5":
        return datetime.now() + timedelta(minutes=5)

    if wake_code == "wake_1615_today":
        return build_today_datetime(16, 15)

    fixed_times = {
        "wake_0810": (8, 10),
        "wake_0815": (8, 15),
        "wake_0820": (8, 20),
    }

    if wake_code not in fixed_times:
        raise ValueError(f"Unknown wake time: {wake_code}")

    hour, minute = fixed_times[wake_code]
    return build_next_workday_datetime(hour, minute)


def build_timer_wake_datetime(wake_code: str, ready_dt: datetime) -> datetime:
    if wake_code == "wake_plus_5":
        return ready_dt
    return ready_dt - timedelta(minutes=WAKE_PREPARE_MINUTES)


def save_expected_wake(wake_dt: datetime) -> None:
    global EXPECTED_WAKE_DT

    EXPECTED_WAKE_DT = wake_dt
    payload = {
        "expected_wake_at": wake_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        WAKE_STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console_log(f"SLEEP_WAKE: Erwartete Weckzeit gespeichert: {payload['expected_wake_at']}.")
    except Exception as e:
        console_log(f"SLEEP_WAKE: Erwartete Weckzeit konnte nicht gespeichert werden: {e}")


def clear_expected_wake(reason: str = "") -> None:
    global EXPECTED_WAKE_DT

    EXPECTED_WAKE_DT = None

    try:
        if WAKE_STATE_FILE.exists():
            WAKE_STATE_FILE.unlink()
    except Exception as e:
        console_log(f"SLEEP_WAKE: Wake-State konnte nicht geloescht werden: {e}")

    if reason:
        console_log(f"SLEEP_WAKE: Wake-State geloescht. Grund: {reason}")


def load_expected_wake() -> datetime | None:
    global EXPECTED_WAKE_DT

    if EXPECTED_WAKE_DT is not None:
        return EXPECTED_WAKE_DT

    try:
        if not WAKE_STATE_FILE.exists():
            return None

        data = json.loads(WAKE_STATE_FILE.read_text(encoding="utf-8"))
        raw_value = data.get("expected_wake_at", "")
        EXPECTED_WAKE_DT = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
        return EXPECTED_WAKE_DT
    except Exception as e:
        console_log(f"SLEEP_WAKE: Wake-State konnte nicht gelesen werden: {e}")
        clear_expected_wake("ungueltiger Wake-State")
        return None


def send_telegram_sync(text: str, retry_seconds: int = TELEGRAM_RETRY_SECONDS) -> bool:
    safe_text = redact_sensitive_text(text)
    log_bot_message(safe_text)
    deadline = time.monotonic() + retry_seconds
    last_error = ""

    while True:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ALLOWED_CHAT_ID, "text": safe_text},
                timeout=5,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            last_error = redact_sensitive_text(str(e))
            if time.monotonic() >= deadline:
                console_log(f"Telegram Sync Send fehlgeschlagen: {last_error}")
                return False
            time.sleep(5)


def handle_unexpected_early_wake() -> bool:
    global EARLY_WAKE_RESLEEP_IN_PROGRESS

    if EARLY_WAKE_RESLEEP_IN_PROGRESS:
        return False

    expected_wake = load_expected_wake()
    if expected_wake is None:
        return False

    now = datetime.now()
    if now >= expected_wake - timedelta(seconds=EARLY_WAKE_GRACE_SECONDS):
        clear_expected_wake("erwartetes Weckfenster erreicht")
        return False

    EARLY_WAKE_RESLEEP_IN_PROGRESS = True
    expected_label = expected_wake.strftime("%Y-%m-%d %H:%M:%S")
    now_label = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        message = (
            "Laptop ist unerwartet vor der geplanten Weckzeit aufgewacht.\n"
            f"Aktuelle Zeit: {now_label}\n"
            f"Geplante Weckzeit: {expected_label}\n"
            "Ich versetze ihn jetzt wieder in den Standby-Modus."
        )
        set_status("Unerwartetes fruehes Aufwachen erkannt. Gehe wieder in den Standby-Modus.")
        send_telegram_sync(message, retry_seconds=TELEGRAM_RETRY_SECONDS)
        console_log("SLEEP_WAKE: Fruehes Aufwachen erkannt. Standby wird erneut ausgefuehrt.")
        put_windows_to_sleep(mode="suspend")
        return True
    except Exception as e:
        set_status("Fehler beim erneuten Standby nach fruehem Aufwachen.", str(e))
        console_log(f"SLEEP_WAKE: Fehler beim erneuten Standby: {e}")
        return False
    finally:
        EARLY_WAKE_RESLEEP_IN_PROGRESS = False


def register_wake_task(wake_dt: datetime) -> None:
    script_path = str((Path(__file__).resolve().parent / WAKE_START_SCRIPT))
    python_path = sys.executable
    start_boundary = wake_dt.strftime("%Y-%m-%d %H:%M:%S")

    ps_command = (
        f"$Action = New-ScheduledTaskAction -Execute '{POWERSHELL_EXE}' "
        f"-Argument '-NoProfile -ExecutionPolicy Bypass -File \"{script_path}\" -PythonExe \"{python_path}\"'; "
        f"$At = [datetime]::ParseExact('{start_boundary}', 'yyyy-MM-dd HH:mm:ss', $null); "
        f"$Trigger = New-ScheduledTaskTrigger -Once -At $At; "
        f"$Settings = New-ScheduledTaskSettingsSet "
        f"-WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName '{WAKE_TASK_NAME}' "
        f"-Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null"
    )

    subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        check=True,
    )


def remove_wake_task() -> None:
    ps_command = (
        f"Unregister-ScheduledTask -TaskName '{WAKE_TASK_NAME}' "
        "-Confirm:$false -ErrorAction SilentlyContinue"
    )
    subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        check=False,
        capture_output=True,
        text=True,
    )


def stop_native_wake_timer() -> None:
    try:
        if not NATIVE_WAKE_TIMER_STATE_FILE.exists():
            return

        state = json.loads(NATIVE_WAKE_TIMER_STATE_FILE.read_text(encoding="utf-8"))
        pid = int(state.get("pid", 0))
        if pid > 0 and psutil.pid_exists(pid):
            process = psutil.Process(pid)
            command_line = " ".join(process.cmdline()).lower()
            if NATIVE_WAKE_TIMER_SCRIPT.lower() in command_line:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
    except Exception as error:
        console_log(f"SLEEP_WAKE: Alter nativer Wake-Timer konnte nicht beendet werden: {error}")
    finally:
        try:
            NATIVE_WAKE_TIMER_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def register_native_wake_timer(wake_dt: datetime, timeout: int = 10) -> int:
    stop_native_wake_timer()

    script_path = BASE_DIR / NATIVE_WAKE_TIMER_SCRIPT
    wake_label = wake_dt.strftime("%Y-%m-%d %H:%M:%S")
    process = subprocess.Popen(
        [
            sys.executable,
            str(script_path),
            "--wake-at",
            wake_label,
            "--state-file",
            str(NATIVE_WAKE_TIMER_STATE_FILE),
        ],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if NATIVE_WAKE_TIMER_STATE_FILE.exists():
            state = json.loads(NATIVE_WAKE_TIMER_STATE_FILE.read_text(encoding="utf-8"))
            status = state.get("status")
            if status == "ready" and int(state.get("pid", 0)) == process.pid:
                console_log(
                    f"SLEEP_WAKE: Nativer Wake-Timer bestaetigt fuer {wake_label}. PID={process.pid}"
                )
                return process.pid
            if status == "error":
                raise RuntimeError(state.get("error", "Unbekannter Fehler im nativen Wake-Timer."))

        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"Nativer Wake-Timer wurde mit Code {exit_code} beendet.")

        time.sleep(0.2)

    process.terminate()
    raise TimeoutError("Nativer Wake-Timer hat die Registrierung nicht bestaetigt.")


def send_bot_message(loop, bot, chat_id: int, text: str) -> None:
    try:
        safe_text = redact_sensitive_text(text)
        log_bot_message(safe_text)

        future = asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=safe_text),
            loop
        )
        future.result(timeout=60)
    except Exception as e:
        console_log(f"Fehler beim Senden einer Telegram-Nachricht: {e}")


def stop_persistent_keep_awake() -> None:
    wake_start_path = str((Path(__file__).resolve().parent / WAKE_START_SCRIPT))
    escaped_wake_start_path = wake_start_path.replace("'", "''")
    ps_command = (
        f"$wakeStartScript = '{escaped_wake_start_path}'; "
        "$currentPid = $PID; "
        "$matches = @(Get-CimInstance Win32_Process | "
        "Where-Object { "
        "$_.ProcessId -ne $currentPid -and "
        "$_.Name -match 'powershell|pwsh' -and "
        "$_.CommandLine -like ('*' + $wakeStartScript + '*') "
        "}); "
        "foreach ($process in $matches) { "
        "Stop-Process -Id $process.ProcessId -Force "
        "}; "
        "$matches.Count"
    )

    try:
        result = subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", ps_command],
            check=True,
            capture_output=True,
            text=True,
        )
        stopped_count = result.stdout.strip() or "0"
        console_log(f"POWER: Persistent keep-awake gestoppt. Prozesse: {stopped_count}")
    except Exception as e:
        console_log(f"POWER: Persistent keep-awake konnte nicht gestoppt werden: {e}")


def send_bot_document(loop, bot, chat_id: int, file_path: str, caption: str) -> None:
    try:
        safe_caption = redact_sensitive_text(caption)
        log_bot_message(f"Datei wird gesendet: {Path(file_path).name} | {safe_caption}")

        with open(file_path, "rb") as f:
            future = asyncio.run_coroutine_threadsafe(
                bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(f, filename=Path(file_path).name),
                    caption=safe_caption
                ),
                loop
            )
            future.result(timeout=120)
    except Exception as e:
        console_log(f"Fehler beim Senden einer Telegram-Datei: {e}")


def wait_and_click_worker(
    loop,
    bot,
    chat_id: int,
    target_time_str: str,
    user_data: dict,
    run_id: int | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    global CURRENT_DRIVER
    global CURRENT_STOP_EVENT

    if stop_event is None:
        stop_event = CURRENT_STOP_EVENT
    if stop_event is None or is_run_cancelled(run_id, stop_event):
        return

    try:
        target_dt = build_target_datetime(target_time_str)

        remaining = (target_dt - datetime.now()).total_seconds()

        if remaining > 0:
            send_bot_message(
                loop,
                bot,
                chat_id,
                f"Status: Der Browser ist geöffnet und verbunden. "
                f"Ich warte bis {format_target_datetime(target_dt)}. "
                f"Verbleibende Zeit: {format_remaining(remaining)}."
            )
        else:
            send_bot_message(
                loop,
                bot,
                chat_id,
                f"Hinweis: Die Zielzeit {target_time_str} ist bereits erreicht oder überschritten. "
                f"Ich versuche jetzt sofort zu klicken."
            )

        while not stop_event.is_set():
            remaining = (target_dt - datetime.now()).total_seconds()

            if remaining <= 0:
                break

            sleep_seconds = get_next_report_delay_seconds(remaining)
            time.sleep(sleep_seconds)

            if is_run_cancelled(run_id, stop_event):
                return

            remaining = (target_dt - datetime.now()).total_seconds()

            if remaining > 0:
                send_bot_message(
                    loop,
                    bot,
                    chat_id,
                    f"Zwischenbericht: Noch {format_remaining(remaining)} bis zur Ausführung."
                )

        if is_run_cancelled(run_id, stop_event):
            return

        if CURRENT_DRIVER is None:
            send_bot_message(
                loop,
                bot,
                chat_id,
                "Fehler: Kein aktiver Browser gefunden."
            )
            return

        send_bot_message(
            loop,
            bot,
            chat_id,
            "Aktion: Der Klick auf die Ziel-Schaltflaeche erfolgt jetzt."
        )

        target_selector = user_data.get("target_selector", TARGET_BUTTON_SELECTOR_TEST)
        click_element(CURRENT_DRIVER, target_selector, timeout=30)  

        endzeit = ""
        if target_selector == TARGET_BUTTON_SELECTOR_REAL:
            try:
                endzeit = get_desktop_time_line(CURRENT_DRIVER, "Endzeit", timeout=30)
            except TimeoutException:
                console_log("ABMELDEN: Endzeit wurde nach dem Klick nicht gefunden.")
        else:
            time.sleep(5)

        if endzeit:
            send_bot_message(
                loop,
                bot,
                chat_id,
                "Abmelden erfolgreich.\n"
                f"{endzeit}"
            )
        else:
            send_bot_message(
                loop,
                bot,
                chat_id,
                "Fertig: Die Ziel-Schaltflaeche wurde erfolgreich geklickt."
            )

        send_bot_message(
            loop,
            bot,
            chat_id,
            "Der Browser wird jetzt geschlossen."
        )

    except Exception as e:
        if not is_run_cancelled(run_id, stop_event):
            send_bot_message(
                loop,
                bot,
                chat_id,
                f"Fehler beim Klick auf die Ziel-Schaltflaeche: {e}"
            )

    finally:
        if not is_run_cancelled(run_id, stop_event):
            try:
                if CURRENT_DRIVER is not None:
                    CURRENT_DRIVER.quit()
            except Exception:
                pass

            CURRENT_DRIVER = None
            CURRENT_STOP_EVENT = None

            user_data["state"] = "finished"
            user_data.pop("action", None)
            user_data.pop("target_time", None)

            set_status("Vorgang abgeschlossen. Bereit fuer neuen Start.")

            send_bot_message(
                loop,
                bot,
                chat_id,
                "Bereit fuer neuen Start. Sende /start."
            )


def is_driver_alive() -> bool:
    if CURRENT_DRIVER is None:
        return False

    try:
        _ = CURRENT_DRIVER.current_url
        return True
    except Exception:
        return False


def watchdog_worker() -> None:
    while True:
        time.sleep(WATCHDOG_EVERY_SECONDS)

        try:
            if handle_unexpected_early_wake():
                continue

            browser_alive = is_driver_alive()

            if CURRENT_DRIVER is not None and not browser_alive:
                console_log("WATCHDOG: Browser scheint nicht mehr erreichbar zu sein. Räume auf.")
                close_current_driver()
                stop_current_worker()
                set_status("Watchdog: Browser war nicht erreichbar. Bereinigt.")

            else:
                console_log(
                    "WATCHDOG: Bot läuft. "
                    f"Browser={'ja' if browser_alive else 'nein'}, "
                    f"Warteprozess={'ja' if CURRENT_STOP_EVENT is not None else 'nein'}, "
                    f"Letzte Aktion={LAST_STATUS}"
                )

        except Exception as e:
            console_log(f"WATCHDOG Fehler: {e}")
            set_status("Watchdog-Fehler.", str(e))


def start_watchdog() -> None:
    watchdog = threading.Thread(target=watchdog_worker, daemon=True)
    watchdog.start()
    console_log("WATCHDOG: gestartet.")

def get_battery_status() -> str:
    try:
        battery = psutil.sensors_battery()

        if battery is None:
            return "Akku: keine Akku-Informationen verfügbar"

        percent = round(battery.percent)
        power_status = "am Netzteil" if battery.power_plugged else "im Akkubetrieb"

        if battery.secsleft == psutil.POWER_TIME_UNLIMITED:
            time_left = "unbegrenzt / Netzbetrieb"
        elif battery.secsleft == psutil.POWER_TIME_UNKNOWN:
            time_left = "unbekannt"
        else:
            hours = battery.secsleft // 3600
            minutes = (battery.secsleft % 3600) // 60
            time_left = f"{hours} Std. {minutes} Min."

        return (
            f"Akku: {percent}%\n"
            f"Stromversorgung: {power_status}\n"
            f"Restlaufzeit: {time_left}"
        )

    except Exception as e:
        return f"Akku: Fehler beim Lesen ({e})"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    cancel_current_run()

    context.user_data.clear()
    context.user_data["state"] = "choose_action"

    keyboard = [
        [InlineKeyboardButton("Check-in", callback_data="checkin")],
        [InlineKeyboardButton("Abmelden Test", callback_data="abmelden_test")],
        [InlineKeyboardButton("Abmelden Real", callback_data="abmelden_real")]
    ]

    log_bot_message("Was soll ausgeführt werden?")

    await update.message.reply_text(
        "Was soll ausgeführt werden?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    browser_status = "geöffnet" if CURRENT_DRIVER is not None else "nicht geöffnet"
    browser_alive = "ja" if is_driver_alive() else "nein"
    worker_status = "läuft" if CURRENT_STOP_EVENT is not None else "läuft nicht"

    state = context.user_data.get("state", "-")
    action = context.user_data.get("action", "-")
    target_time = context.user_data.get("target_time", "-")
    target_label, remaining_label = get_target_status_text(target_time)

    if LAST_ACTION_TIME:
        try:
            last_dt = datetime.strptime(LAST_ACTION_TIME, "%Y-%m-%d %H:%M:%S")
            seconds_ago = int((datetime.now() - last_dt).total_seconds())
            last_activity = f"{LAST_ACTION_TIME} ({seconds_ago} Sek. zuvor)"
        except Exception:
            last_activity = LAST_ACTION_TIME
    else:
        last_activity = "-"

    ready_text = "ja" if CURRENT_DRIVER is None and CURRENT_STOP_EVENT is None else "nein"

    battery_status = get_battery_status()

    await reply_user(
        update,
        "Statusbericht:\n"
        f"Bot: läuft\n"
        f"Browser: {browser_status}\n"
        f"Browser erreichbar: {browser_alive}\n"
        f"Watchdog: aktiv\n"
        f"{battery_status}\n"
        f"Warteprozess: {worker_status}\n"
        f"Bereit für neuen Start: {ready_text}\n"
        f"Aktion: {action}\n"
        f"State: {state}\n"
        f"Zielzeit: {target_label}\n"
        f"Verbleibende Zeit: {remaining_label}\n"
        f"Letzte Aktion: {LAST_STATUS}\n"
        f"Letzte Aktivität: {last_activity}\n"
        f"Letzter Fehler: {LAST_ERROR or '-'}"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await query.edit_message_text("Kein Zugriff.")
        return

    action = query.data

    if action in ["abmelden_test", "abmelden_real"]:
        context.user_data["action"] = action

        if action == "abmelden_test":
            context.user_data["target_selector"] = TARGET_BUTTON_SELECTOR_TEST
            action_title = "Abmelden Test"
        else:
            context.user_data["target_selector"] = TARGET_BUTTON_SELECTOR_REAL
            action_title = "Abmelden Real"
        context.user_data["state"] = "choose_time"

        keyboard = [
            [
                InlineKeyboardButton("1 Min.", callback_data="time_plus_1"),
                InlineKeyboardButton("2 Min.", callback_data="time_plus_2"),
                InlineKeyboardButton("5 Min.", callback_data="time_plus_5"),
            ],
            [
                InlineKeyboardButton("16:30:05", callback_data="time_fixed_1630")
            ]
        ]

        await query.edit_message_text(
            f"{action_title} ausgewählt.\n\n"
            "Wann soll die Aktion ausgeführt werden?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if action == "checkin":
        context.user_data["action"] = "checkin"
        context.user_data["state"] = "checkin_in_progress"

        await query.edit_message_text(
            "Check-in ausgewaehlt.\n\n"
            "Ich starte jetzt den Browser und melde dich an..."
        )

        await start_checkin_process(query, context)
        return

    if action in ["time_plus_1", "time_plus_2", "time_plus_5"]:
        minutes_map = {
            "time_plus_1": 1,
            "time_plus_2": 2,
            "time_plus_5": 5,
        }

        minutes = minutes_map[action]
        target_dt = datetime.now() + timedelta(minutes=minutes)
        target_time = serialize_target_datetime(target_dt)
        target_label = format_target_datetime(target_dt)

        context.user_data["target_time"] = target_time
        context.user_data["state"] = "login_in_progress"

        await query.edit_message_text(
            f"Zeit gewaehlt: in {minutes} Min. ({target_label})\n\n"
            "Ich starte jetzt den Browser und die Anmeldung..."
        )

        await start_login_process(query, context, target_time)
        return

    if action == "time_fixed_1630":
        target_dt = build_fixed_time_today_or_tomorrow(16, 30, 5)
        target_time = serialize_target_datetime(target_dt)
        target_label = format_target_datetime(target_dt)

        context.user_data["target_time"] = target_time
        context.user_data["state"] = "login_in_progress"

        await query.edit_message_text(
            f"Zeit gewaehlt: {target_label}\n\n"
            "Ich starte jetzt den Browser und die Anmeldung..."
        )

        await start_login_process(query, context, target_time)
        return

    if action == "sleep_yes":
        await query.edit_message_text(
            "OK. Der Laptop wird jetzt in den Ruhezustand versetzt."
        )
        console_log("SLEEP: Benutzer hat Standby bestätigt.")
        put_windows_to_sleep()
        return

    if action == "sleep_no":
        await query.edit_message_text(
            "Abgebrochen. Der Laptop bleibt eingeschaltet."
        )
        console_log("SLEEP: Benutzer hat Standby abgebrochen.")
        return

    if action == "sleep_wake_yes":
        keyboard = [
            [
                InlineKeyboardButton("08:10 Arbeitstag", callback_data="wake_0810"),
                InlineKeyboardButton("08:15 Arbeitstag", callback_data="wake_0815"),
                InlineKeyboardButton("08:20 Arbeitstag", callback_data="wake_0820"),
            ],
            [
                InlineKeyboardButton("16:15 Heute", callback_data="wake_1615_today"),
            ],
            [
                InlineKeyboardButton("in 5 Min.", callback_data="wake_plus_5"),
                InlineKeyboardButton("Abbrechen", callback_data="sleep_wake_cancel"),
            ],
        ]

        await query.edit_message_text(
            "Wann soll der Laptop spaetestens bereit sein?\n"
            "08-Uhr-Zeiten gelten fuer den naechsten Arbeitstag. "
            "16:15 gilt fuer heute. Feste Zeiten werden "
            "10 Minuten frueher geweckt.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if action == "sleep_wake_no" or action == "sleep_wake_cancel":
        await query.edit_message_text(
            "Abgebrochen. Der Laptop bleibt eingeschaltet."
        )
        console_log("SLEEP_WAKE: Benutzer hat abgebrochen.")
        return

    if action in ["wake_0810", "wake_0815", "wake_0820", "wake_1615_today", "wake_plus_5"]:
        try:
            ready_dt = build_wake_datetime(action)
            timer_dt = build_timer_wake_datetime(action, ready_dt)
            if timer_dt <= datetime.now():
                raise ValueError(
                    "Die technische Weckzeit liegt bereits in der Vergangenheit. "
                    "Bitte waehle eine spaetere Zeit."
                )
            register_native_wake_timer(timer_dt)
            register_wake_task(timer_dt)
        except Exception as e:
            stop_native_wake_timer()
            remove_wake_task()
            safe_error = redact_sensitive_text(str(e))
            await query.edit_message_text(
                f"Fehler: Der Weck-Timer konnte nicht erstellt werden.\n{safe_error}"
            )
            console_log(f"SLEEP_WAKE Fehler: {e}")
            return

        save_expected_wake(timer_dt)

        if timer_dt == ready_dt:
            timing_text = f"Weck-Timer gesetzt: {format_target_datetime(timer_dt)}."
        else:
            timing_text = (
                f"Geplante Bereitschaft: {format_target_datetime(ready_dt)}.\n"
                f"Technisches Aufwecken: {format_target_datetime(timer_dt)}."
            )

        await query.edit_message_text(
            f"{timing_text}\n"
            "Nativer Timer und Windows-Aufgabe wurden bestaetigt.\n"
            "Der Laptop wird jetzt in den Standby-Modus versetzt."
        )
        console_log(
            "SLEEP_WAKE: Native- und Task-Timer gesetzt fuer "
            f"{timer_dt.strftime('%Y-%m-%d %H:%M:%S')}. "
            f"Bereit-Zeit: {ready_dt.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        put_windows_to_sleep(mode="suspend")
        return

async def reset_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    cancel_current_run()
    context.user_data.clear()
    set_status("Manueller Reset ausgeführt. Bereit für neuen Start.")

    await reply_user(
        update,
        "Reset ausgeführt.\n"
        "Browser geschlossen, Warteprozess gestoppt.\n"
        "Bereit für neuen Start. Sende /start."
    )

async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    keyboard = [
        [
            InlineKeyboardButton("Ja", callback_data="sleep_yes"),
            InlineKeyboardButton("Nein", callback_data="sleep_no")
        ]
    ]

    log_bot_message("Laptop in den Standby-Modus versetzen?")

    await update.message.reply_text(
        "Laptop in den Standby-Modus versetzen?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )



async def sleep_wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    keyboard = [
        [
            InlineKeyboardButton("Ja", callback_data="sleep_wake_yes"),
            InlineKeyboardButton("Nein", callback_data="sleep_wake_no")
        ]
    ]

    log_bot_message("Laptop in den Standby-Modus versetzen und spaeter automatisch wecken?")

    await update.message.reply_text(
        "Laptop in den Standby-Modus versetzen und spaeter automatisch wecken?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def put_windows_to_sleep(mode: str = "hibernate") -> None:
    if mode == "suspend":
        console_log("POWER: Standby wird angefordert fuer geplantes Wake.")
    else:
        console_log("POWER: Ruhezustand wird angefordert.")

    stop_persistent_keep_awake()

    suspend_command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$ok = [System.Windows.Forms.Application]::SetSuspendState("
        "[System.Windows.Forms.PowerState]::Suspend, $false, $false); "
        "if (-not $ok) { throw 'SetSuspendState Suspend returned false.' }"
    )

    if mode == "suspend":
        subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", suspend_command],
            check=True
        )
        return

    hibernate_command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$ok = [System.Windows.Forms.Application]::SetSuspendState("
        "[System.Windows.Forms.PowerState]::Hibernate, $false, $false); "
        "if (-not $ok) { throw 'SetSuspendState Hibernate returned false.' }"
    )

    try:
        subprocess.run(
            [POWERSHELL_EXE, "-NoProfile", "-Command", hibernate_command],
            check=True
        )
        return
    except Exception as e:
        console_log(f"POWER: Ruhezustand fehlgeschlagen, versuche Standby. Fehler: {e}")

    subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-Command",
            suspend_command
        ],
        check=True
    )

async def start_checkin_process(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CURRENT_STOP_EVENT
    global CURRENT_RUN_ID

    cancel_current_run()
    CURRENT_RUN_ID += 1
    run_id = CURRENT_RUN_ID
    stop_event = threading.Event()
    CURRENT_STOP_EVENT = stop_event

    loop = asyncio.get_running_loop()
    chat_id = ALLOWED_CHAT_ID
    bot = context.bot

    def checkin_worker():
        global CURRENT_STOP_EVENT

        ok, message = open_site_and_checkin(
            DEFAULT_URL,
            DEFAULT_LOGIN,
            DEFAULT_PASSWORD,
            run_id=run_id,
            stop_event=stop_event
        )

        if is_run_cancelled(run_id, stop_event):
            return

        send_bot_message(loop, bot, chat_id, message)

        if ok:
            context.user_data["state"] = "finished"
            set_status("Check-in abgeschlossen. Bereit fuer neuen Start.")
        else:
            context.user_data["state"] = "checkin_error"

        context.user_data.pop("action", None)

        if run_id == CURRENT_RUN_ID:
            CURRENT_STOP_EVENT = None

    threading.Thread(target=checkin_worker, daemon=True).start()


async def start_login_process(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_time: str) -> None:
    global CURRENT_STOP_EVENT
    global CURRENT_RUN_ID

    cancel_current_run()
    CURRENT_RUN_ID += 1
    run_id = CURRENT_RUN_ID
    stop_event = threading.Event()
    CURRENT_STOP_EVENT = stop_event
    context.user_data["target_time"] = target_time

    loop = asyncio.get_running_loop()
    chat_id = ALLOWED_CHAT_ID
    bot = context.bot

    def login_worker():
        global CURRENT_STOP_EVENT

        ok, message = open_site_and_login(
            DEFAULT_URL,
            DEFAULT_LOGIN,
            DEFAULT_PASSWORD,
            target_selector=context.user_data.get("target_selector", TARGET_BUTTON_SELECTOR_TEST),
            run_id=run_id,
            stop_event=stop_event
        )

        if is_run_cancelled(run_id, stop_event):
            return

        send_bot_message(loop, bot, chat_id, message)

        if not ok:
            context.user_data["state"] = "login_error"
            if run_id == CURRENT_RUN_ID:
                CURRENT_STOP_EVENT = None
            return

        context.user_data["state"] = "ready_to_wait"

        worker = threading.Thread(
            target=wait_and_click_worker,
            args=(
                loop,
                bot,
                chat_id,
                context.user_data["target_time"],
                context.user_data,
                run_id,
                stop_event
            ),
            daemon=True
        )
        worker.start()

        send_bot_message(
            loop,
            bot,
            chat_id,
            "Der Warteprozess wurde gestartet. "
            "Zwischenberichte kommen automatisch: alle 30, 10, 5 oder 1 Minute(n), je nach Restzeit."
        )

    threading.Thread(target=login_worker, daemon=True).start()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CURRENT_STOP_EVENT

    if update.effective_chat.id != ALLOWED_CHAT_ID:
        await reply_user(update, "Kein Zugriff.")
        return

    await reply_user(update, "Sende /start")
    return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error_text = redact_sensitive_text(str(context.error))

    console_log(f"ERROR: {error_text}")

    try:
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"Fehler im Bot:\n{error_text}"
        )
    except Exception:
        console_log("Fehler konnte nicht an Telegram gesendet werden.")


def acquire_bot_singleton() -> bool:
    global BOT_MUTEX_HANDLE

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p

        handle = kernel32.CreateMutexW(None, True, BOT_MUTEX_NAME)
        last_error = ctypes.get_last_error()

        if not handle:
            console_log("BOT: Singleton-Mutex konnte nicht erstellt werden. Starte trotzdem.")
            return True

        if last_error == 183:
            console_log("BOT: Zweite Bot-Instanz erkannt. Diese Instanz beendet sich.")
            return False

        BOT_MUTEX_HANDLE = handle
        return True
    except Exception as e:
        console_log(f"BOT: Singleton-Pruefung fehlgeschlagen. Starte trotzdem. Fehler: {e}")
        return True


def release_bot_singleton() -> None:
    global BOT_MUTEX_HANDLE

    if not BOT_MUTEX_HANDLE:
        return

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex(ctypes.c_void_p(BOT_MUTEX_HANDLE))
        kernel32.CloseHandle(ctypes.c_void_p(BOT_MUTEX_HANDLE))
    except Exception as e:
        console_log(f"BOT: Singleton-Mutex konnte nicht freigegeben werden: {e}")
    finally:
        BOT_MUTEX_HANDLE = None


def main() -> None:
    if not acquire_bot_singleton():
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reset", reset_bot))
    application.add_handler(CommandHandler("sleep", sleep_command))
    application.add_handler(CommandHandler("sleep_wake", sleep_wake_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(error_handler)

    start_watchdog()

    print("Bot gestartet. Zum Beenden Ctrl+C drücken.")
    try:
        application.run_polling()
    finally:
        release_bot_singleton()


if __name__ == "__main__":
    main()
