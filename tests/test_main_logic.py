import os
import threading
import unittest
from datetime import datetime
from unittest.mock import patch


os.environ.update(
    {
        "BOT_TOKEN": "unit-test-token",
        "ALLOWED_CHAT_ID": "123",
        "DEFAULT_LOGIN": "unit-test-login",
        "DEFAULT_PASSWORD": "unit-test-password",
    }
)

import main


class SchedulingTests(unittest.TestCase):
    def test_format_remaining(self):
        self.assertEqual(main.format_remaining(3661), "1 Std. 1 Min. 1 Sek.")
        self.assertEqual(main.format_remaining(59), "59 Sek.")

    def test_build_target_datetime_accepts_serialized_datetime(self):
        target = main.build_target_datetime("2026-11-05 08:30:00")
        self.assertEqual(target, datetime(2026, 11, 5, 8, 30))

    def test_next_workday_skips_weekend(self):
        friday = datetime(2026, 11, 6, 12, 0)
        target = main.build_next_workday_datetime(8, 10, now=friday)
        self.assertEqual(target, datetime(2026, 11, 9, 8, 10))

    def test_today_datetime_rejects_time_in_the_past(self):
        current = datetime(2026, 11, 5, 12, 0)
        with self.assertRaisesRegex(ValueError, "bereits vorbei"):
            main.build_today_datetime(11, 59, now=current)

    def test_wake_timer_preparation_time(self):
        ready_at = datetime(2026, 11, 9, 8, 10)
        self.assertEqual(
            main.build_timer_wake_datetime("wake_0810", ready_at),
            datetime(2026, 11, 9, 8, 0),
        )
        self.assertEqual(
            main.build_timer_wake_datetime("wake_plus_5", ready_at),
            ready_at,
        )

    def test_report_intervals_become_more_frequent(self):
        self.assertEqual(main.get_next_report_delay_seconds(7201), 1800)
        self.assertEqual(main.get_next_report_delay_seconds(3600), 600)
        self.assertEqual(main.get_next_report_delay_seconds(1800), 300)
        self.assertEqual(main.get_next_report_delay_seconds(300), 60)

    def test_run_cancellation_checks_event_and_run_id(self):
        stop_event = threading.Event()
        with patch.object(main, "CURRENT_RUN_ID", 3):
            self.assertFalse(main.is_run_cancelled(run_id=3, stop_event=stop_event))
            self.assertTrue(main.is_run_cancelled(run_id=2, stop_event=stop_event))
            stop_event.set()
            self.assertTrue(main.is_run_cancelled(run_id=3, stop_event=stop_event))

