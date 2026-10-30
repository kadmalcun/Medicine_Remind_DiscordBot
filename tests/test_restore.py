"""ログからの状態復元と設定の読み込みのテスト（python -m unittest discover tests）"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DISCORD_TOKEN", "dummy")
os.environ.setdefault("CHANNEL_ID", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import medicine_bot as mb  # noqa: E402

JST = timezone(timedelta(hours=9))
REMIND = "13:00"


def at(day, hm):
    """10/day の hh:mm（JST）"""
    h, m = map(int, hm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=JST)


def ev(ts, event, **detail):
    return {"ts": ts, "event": event, "detail": detail}


class CycleBoundsTest(unittest.TestCase):
    def test_after_remind_time(self):
        self.assertEqual(mb.cycle_bounds(at(30, "14:00"), REMIND), (at(30, "13:00"), at(31, "03:00")))

    def test_after_midnight_belongs_to_previous_day(self):
        self.assertEqual(mb.cycle_bounds(at(31, "01:00"), REMIND), (at(30, "13:00"), at(31, "03:00")))

    def test_before_remind_time(self):
        self.assertEqual(mb.cycle_bounds(at(31, "10:00"), REMIND), (at(30, "13:00"), at(31, "03:00")))

    def test_remind_time_before_rollover(self):
        self.assertEqual(mb.cycle_bounds(at(30, "02:00"), "01:00"), (at(30, "01:00"), at(30, "03:00")))


class RestoreStateTest(unittest.TestCase):
    def restore(self, events, now):
        return mb.restore_state(events, now, REMIND)

    def test_empty_log(self):
        r = self.restore([], at(30, "14:00"))
        self.assertEqual(r["missed_doses"], 0)
        self.assertFalse(r["is_taken"])
        self.assertTrue(r["in_period"])
        self.assertIsNone(r["pending_missed"])

    def test_taken(self):
        events = [ev(at(30, "13:00"), "reminder_sent", kind="scheduled"), ev(at(30, "13:05"), "taken", missed_doses=0)]
        self.assertTrue(self.restore(events, at(30, "14:00"))["is_taken"])
        # 日付をまたいでも締め（3時）までは同じ日
        self.assertTrue(self.restore(events, at(31, "01:00"))["is_taken"])

    def test_taken_then_undo(self):
        events = [ev(at(30, "13:05"), "taken"), ev(at(30, "13:05"), "undo_taken")]
        self.assertFalse(self.restore(events, at(30, "14:00"))["is_taken"])

    def test_previous_day_taken_does_not_carry_over(self):
        events = [ev(at(29, "13:05"), "taken")]
        self.assertFalse(self.restore(events, at(30, "14:00"))["is_taken"])

    def test_home_waiting(self):
        events = [ev(at(30, "15:00"), "home")]
        self.assertEqual(self.restore(events, at(30, "18:00"))["home_until"], at(30, "22:00"))

    def test_home_reminder_missed_while_down(self):
        events = [ev(at(30, "15:00"), "home")]
        self.assertIsNone(self.restore(events, at(30, "23:00"))["home_until"])

    def test_home_reminder_already_sent(self):
        events = [ev(at(30, "15:00"), "home"), ev(at(30, "22:00"), "reminder_sent", kind="home")]
        self.assertIsNone(self.restore(events, at(30, "21:00") + timedelta(hours=2))["home_until"])

    def test_home_undone(self):
        events = [ev(at(30, "15:00"), "home"), ev(at(30, "15:00"), "undo_home")]
        self.assertIsNone(self.restore(events, at(30, "18:00"))["home_until"])

    def test_snooze_pending(self):
        events = [ev(at(30, "14:00"), "snoozed", hours=2)]
        self.assertEqual(self.restore(events, at(30, "15:00"))["snooze_until"], (at(30, "16:00"), 2.0))

    def test_snooze_expired(self):
        events = [ev(at(30, "14:00"), "snoozed", hours=2)]
        self.assertIsNone(self.restore(events, at(30, "17:00"))["snooze_until"])

    def test_snooze_cancelled_by_home(self):
        events = [ev(at(30, "14:00"), "snoozed", hours=2), ev(at(30, "14:30"), "home")]
        r = self.restore(events, at(30, "15:00"))
        self.assertIsNone(r["snooze_until"])
        self.assertEqual(r["home_until"], at(30, "22:00"))

    def test_missed_while_down_before_remind_time(self):
        events = [ev(at(30, "13:00"), "reminder_sent", kind="scheduled")]
        r = self.restore(events, at(31, "10:00"))
        self.assertFalse(r["in_period"])
        self.assertEqual(r["pending_missed"], at(30, "13:00"))

    def test_missed_while_down_after_remind_time(self):
        events = [ev(at(29, "13:00"), "reminder_sent", kind="scheduled")]
        self.assertEqual(self.restore(events, at(30, "14:00"))["pending_missed"], at(29, "13:00"))

    def test_missed_already_logged(self):
        events = [
            ev(at(30, "13:00"), "reminder_sent", kind="scheduled"),
            ev(at(31, "03:00"), "missed", missed_doses=1, cycle="2026-10-30"),
        ]
        r = self.restore(events, at(31, "10:00"))
        self.assertIsNone(r["pending_missed"])
        self.assertEqual(r["missed_doses"], 1)

    def test_missed_logged_late_is_matched_by_cycle(self):
        # 29日分の「記録なし」を30日14時（30日の区切りの中）に記録していても、30日分とは扱わない
        events = [
            ev(at(29, "13:00"), "reminder_sent", kind="scheduled"),
            ev(at(30, "14:00"), "missed", missed_doses=1, cycle="2026-10-29"),
            ev(at(30, "14:00"), "reminder_sent", kind="restart"),
        ]
        self.assertEqual(self.restore(events, at(31, "10:00"))["pending_missed"], at(30, "13:00"))

    def test_no_missed_without_reminder(self):
        self.assertIsNone(self.restore([], at(31, "10:00"))["pending_missed"])

    def test_no_missed_when_taken(self):
        events = [ev(at(30, "13:00"), "reminder_sent", kind="scheduled"), ev(at(30, "23:00"), "taken")]
        self.assertIsNone(self.restore(events, at(31, "10:00"))["pending_missed"])

    def test_missed_doses_follow_last_event(self):
        events = [
            ev(at(29, "03:00"), "missed", missed_doses=1, cycle="2026-10-28"),
            ev(at(30, "03:00"), "missed", missed_doses=2, cycle="2026-10-29"),
        ]
        self.assertEqual(self.restore(events, at(30, "14:00"))["missed_doses"], 2)
        events.append(ev(at(30, "13:05"), "taken", missed_doses=0, prev_missed_doses=2))
        self.assertEqual(self.restore(events, at(30, "14:00"))["missed_doses"], 0)
        events.append(ev(at(30, "13:05"), "undo_taken", missed_doses=2))
        self.assertEqual(self.restore(events, at(30, "14:00"))["missed_doses"], 2)


class FileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_read_log_skips_broken_lines(self):
        with open(self.path("log.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-30T13:05:00+09:00", "event": "taken", "detail": {}}) + "\n")
            f.write("{broken\n")
            f.write(json.dumps({"event": "taken"}) + "\n")
            f.write(json.dumps({"ts": "2026-10-30T13:06:00+09:00", "event": "home", "detail": None}) + "\n")
        events = mb.read_log_events(self.path("log.jsonl"))
        self.assertEqual([e["event"] for e in events], ["taken", "home"])
        self.assertEqual(events[1]["detail"], {})

    def test_read_log_missing_file(self):
        self.assertEqual(mb.read_log_events(self.path("none.jsonl")), [])

    def test_normalize_time(self):
        self.assertEqual(mb.normalize_time("9:05"), "09:05")
        self.assertEqual(mb.normalize_time(" 21:30 "), "21:30")
        for bad in ("25:00", "12:60", "abc", "12"):
            with self.assertRaises(ValueError):
                mb.normalize_time(bad)

    def test_load_config(self):
        orig = mb.CONFIG_PATH
        self.addCleanup(setattr, mb, "CONFIG_PATH", orig)
        mb.CONFIG_PATH = self.path("config.json")
        default = mb.normalize_time(mb.DEFAULT_REMIND_TIME)

        self.assertEqual(mb.load_config(), {"remind_time": default, "retry_interval": 60})

        with open(mb.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"remind_time": "8:00", "retry_interval": 0}, f)
        self.assertEqual(mb.load_config(), {"remind_time": "08:00", "retry_interval": 1})

        for broken in ("{broken", '{"remind_time": "99:99"}', "[]"):
            with open(mb.CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(broken)
            self.assertEqual(mb.load_config()["remind_time"], default)


if __name__ == "__main__":
    unittest.main()
