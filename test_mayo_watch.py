"""Offline tests for parsing, alert de-duplication and failure handling."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import mayo_watch as w

T0 = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
ENGLISH = "14:00～【Mayoterrace】English Tour"
JAPANESE = "14:00～【マヨテラス】小学生以上対象"
DATES = {"2026-12-03"}


def slot(text, tour=ENGLISH, timestamp="2026/12/03 14:00", url="https://x/add"):
    return w.Slot(tour, timestamp, text, w.parse_status(text), url)


class Parsing(unittest.TestCase):
    def test_status(self):
        self.assertEqual(w.parse_status("残り3人"), 3)
        self.assertEqual(w.parse_status("残り 12 人"), 12)
        self.assertEqual(w.parse_status("残り0人"), 0)
        for closed in ("Not Open", "受付前", "満席", "開催なし", "休館日", "休業日", "", None):
            self.assertIsNone(w.parse_status(closed))

    def test_header_year_rollover(self):
        self.assertEqual(w.header_date("12/01(火)", date(2026, 11, 3)), date(2026, 12, 1))
        self.assertEqual(w.header_date("01/05(火)", date(2026, 12, 20)), date(2027, 1, 5))
        self.assertIsNone(w.header_date("", date(2026, 11, 3)))

    def test_relative_url_becomes_absolute(self):
        week = {"rows": [{"tour": ENGLISH, "cells": [
            {"timestamp": "2026/12/03 14:00", "url": "/reservations/add?event_id=1719", "text": "残り3人"},
            {"timestamp": "2026/12/04 14:00", "url": None, "text": "満席"}]}]}
        slots = w.slots_from_week(week)
        self.assertEqual(slots[0].url, "https://reservation.kewpie.co.jp/reservations/add?event_id=1719")
        self.assertEqual(slots[1].url, w.CALENDAR_URL)

    def test_stale_table_is_detected_after_a_week_change(self):
        cells = [{"timestamp": "2026/11/30 14:00", "url": "", "text": ""}]
        rows = [{"tour": ENGLISH, "cells": cells}]
        self.assertTrue(w.week_is_consistent({"headers": ["11/30(月)"], "rows": rows}))
        self.assertFalse(w.week_is_consistent({"headers": ["12/01(火)"], "rows": rows}))
        self.assertFalse(w.week_is_consistent({"headers": ["12/01(火)"], "rows": []}))

    def test_english_filter_and_hit_threshold(self):
        slots = [slot("残り2人"), slot("残り1人", timestamp="2026/12/03 10:30"),
                 slot("残り9人", JAPANESE), slot("残り9人", timestamp="2026/12/04 14:00")]
        english = w.select_slots(slots, DATES, any_tour=False)
        self.assertEqual(len(english), 2)
        self.assertEqual([h.left for h in w.find_hits(english, 2)], [2])
        self.assertEqual(len(w.select_slots(slots, DATES, any_tour=True)), 3)

    def test_message_format_matches_spec(self):
        self.assertEqual(w.build_message([slot("残り3人")], 2),
                         "Mayo Terrace English tour: 2 places — Thu 3 Dec 14:00 (3 left)")

    def test_japanese_hit_is_labelled(self):
        message = w.build_message([slot("残り3人", JAPANESE)], 1)
        self.assertIn("Mayo Terrace tour: 1 place", message)
        self.assertIn(JAPANESE, message)

    def test_unknown_status_is_flagged(self):
        self.assertEqual(w.unknown_statuses([slot("Not Open"), slot("満席"), slot("あと3名")]), {"あと3名"})


class Scenario(unittest.TestCase):
    FREE = [slot("残り3人")]
    GONE = [slot("満席")]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state.json"
        self.send = mock.Mock()

    def run_check(self, slots, now=T0, error=None, send=None, any_tour=False):
        return w.run_once(DATES, 2, any_tour, self.path, "topic", False,
                          fetch=lambda d, t: (slots, error), send=send or self.send, now=now)

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_new_hit_alerts_once_then_again_after_two_hours(self):
        self.run_check(self.FREE)
        self.assertEqual(self.send.call_count, 1)
        self.run_check(self.FREE, T0 + timedelta(minutes=15))
        self.run_check(self.FREE, T0 + timedelta(minutes=119))
        self.assertEqual(self.send.call_count, 1)
        self.run_check(self.FREE, T0 + timedelta(hours=2))
        self.assertEqual(self.send.call_count, 2)

    def test_gone_then_back_alerts_immediately(self):
        self.run_check(self.FREE)
        self.run_check(self.GONE, T0 + timedelta(minutes=15))
        self.run_check(self.FREE, T0 + timedelta(minutes=30))
        self.assertEqual(self.send.call_count, 2)

    def test_all_hits_share_one_message_and_click_is_slot_url(self):
        self.run_check([slot("残り3人", url="https://x/a"),
                        slot("残り4人", timestamp="2026/12/03 15:30", url="https://x/b")])
        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertEqual(args[1].count("\n"), 1)
        self.assertEqual(kwargs["click"], "https://x/a")
        self.assertIn("https://x/b", kwargs["actions"])

    def test_japanese_slots_ignored_by_default(self):
        self.run_check([slot("残り9人", JAPANESE), slot("満席")])
        self.send.assert_not_called()

    def test_one_broken_alert_after_four_failures(self):
        for _ in range(3):
            self.run_check([], error="timeout")
        self.send.assert_not_called()
        for _ in range(5):
            self.run_check([], error="timeout")
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("Mayo watcher broken", self.send.call_args[0][1])

    def test_zero_slots_counts_as_failure_and_recovery_rearms(self):
        for _ in range(4):
            self.run_check([])
        self.assertEqual(self.send.call_count, 1)
        self.run_check(self.GONE)
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertFalse(self.state()["broken_alert_sent"])
        for _ in range(4):
            self.run_check([])
        self.assertEqual(self.send.call_count, 2)

    def test_only_japanese_rows_counts_as_failure(self):
        # If "English" disappears from the titles, the watcher must not go quiet.
        self.run_check([slot("残り9人", JAPANESE)])
        self.assertEqual(self.state()["consecutive_failures"], 1)

    def test_hits_read_before_a_failure_still_alert(self):
        self.run_check(self.FREE, error="week 5 timed out")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.state()["consecutive_failures"], 1)

    def test_failed_check_does_not_cause_a_duplicate_alert(self):
        self.run_check(self.FREE)
        self.run_check([], T0 + timedelta(minutes=15), error="timeout")
        self.run_check(self.FREE, T0 + timedelta(minutes=30))
        self.assertEqual(self.send.call_count, 1)

    def test_failed_send_is_retried_next_run(self):
        code = self.run_check(self.FREE, send=mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["active"], {})
        self.run_check(self.FREE, T0 + timedelta(minutes=15))
        self.assertEqual(self.send.call_count, 1)

    def test_dry_run_sends_and_saves_nothing(self):
        w.run_once(DATES, 2, False, self.path, None, True,
                   fetch=lambda d, t: (self.FREE, None), send=self.send)
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())


class Stop(unittest.TestCase):
    def test_stops_after_last_date_without_loading_anything(self):
        with mock.patch.object(w, "run_once") as run:
            code = w.main(["--dry-run", "--dates", "2020-01-01"])
        self.assertEqual(code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
