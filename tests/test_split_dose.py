"""2錠以上の日に1錠ずつ飲む機能のテスト（設計書 docs/split-dose-design.md）"""
import os
import sys
import unittest

os.environ.setdefault("DISCORD_TOKEN", "dummy")
os.environ.setdefault("CHANNEL_ID", "1")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import medicine_bot as mb  # noqa: E402
from test_restore import REMIND, at, ev  # noqa: E402

START, ROLLOVER = at(30, "13:00"), at(31, "03:00")


class NextDoseTimeTest(unittest.TestCase):
    def nxt(self, taken_at, gap=6, deadline="23:00"):
        return mb.next_dose_time(taken_at, gap, deadline, START, ROLLOVER)

    def test_design_table(self):
        # 設計書 4.3 の表
        self.assertEqual(self.nxt(at(30, "13:05")), at(30, "19:05"))
        self.assertEqual(self.nxt(at(30, "17:30")), at(30, "23:00"))
        self.assertEqual(self.nxt(at(30, "22:40")), at(30, "23:00"))
        self.assertIsNone(self.nxt(at(30, "23:20")))

    def test_at_deadline_and_after_midnight(self):
        self.assertIsNone(self.nxt(at(30, "23:00")))
        self.assertIsNone(self.nxt(at(31, "01:00")))

    def test_deadline_after_midnight(self):
        # 締め切りを 01:00 にすると、その日の夜中の 01:00 が上限になる
        self.assertEqual(self.nxt(at(30, "22:00"), deadline="01:00"), at(31, "01:00"))

    def test_never_after_rollover(self):
        self.assertIsNone(self.nxt(at(30, "22:00"), deadline="05:00"))

    def test_settings_are_used(self):
        self.assertEqual(self.nxt(at(30, "13:00"), gap=2), at(30, "15:00"))
        self.assertEqual(self.nxt(at(30, "13:00"), deadline="14:00"), at(30, "14:00"))


def dose(ts, count=1, remaining=1, next_at=None, user="kaede"):
    e = ev(ts, "dose_taken", count=count, remaining=remaining, next_at=next_at.isoformat() if next_at else None)
    e["user_name"] = user
    return e


class RestoreSplitDoseTest(unittest.TestCase):
    """前日分を1錠持ち越して、今日は2錠の日"""
    carried = [ev(at(30, "03:00"), "missed", missed_doses=1, cycle="2026-10-29")]

    def restore(self, events, now):
        return mb.restore_state(self.carried + events, now, REMIND)

    def test_waiting_for_next_dose(self):
        events = [ev(START, "reminder_sent", kind="scheduled"), dose(at(30, "13:05"), next_at=at(30, "19:05"))]
        r = self.restore(events, at(30, "15:00"))
        self.assertFalse(r["is_taken"])
        self.assertEqual(r["dose_until"], at(30, "19:05"))
        self.assertEqual([(d[1], d[2]) for d in r["doses"]], [("kaede", 1)])
        self.assertEqual(r["missed_doses"], 1)  # 途中の記録では持ち越しの値を変えない

    def test_next_dose_time_passed_while_down(self):
        events = [ev(START, "reminder_sent", kind="scheduled"), dose(at(30, "13:05"), next_at=at(30, "19:05"))]
        r = self.restore(events, at(30, "20:00"))
        self.assertIsNone(r["dose_until"])
        self.assertFalse(r["dose_wait_rollover"])
        self.assertEqual(len(r["doses"]), 1)

    def test_next_dose_already_notified(self):
        events = [
            ev(START, "reminder_sent", kind="scheduled"),
            dose(at(30, "13:05"), next_at=at(30, "19:05")),
            ev(at(30, "19:05"), "reminder_sent", kind="dose"),
        ]
        self.assertIsNone(self.restore(events, at(30, "19:30"))["dose_until"])

    def test_no_more_notice_today(self):
        events = [ev(START, "reminder_sent", kind="scheduled"), dose(at(30, "23:20"))]
        r = self.restore(events, at(31, "01:00"))
        self.assertTrue(r["dose_wait_rollover"])
        self.assertIsNone(r["dose_until"])

    def test_undo_dose(self):
        events = [
            ev(START, "reminder_sent", kind="scheduled"),
            dose(at(30, "13:05"), next_at=at(30, "19:05")),
            ev(at(30, "13:06"), "undo_dose", remaining=2),
        ]
        r = self.restore(events, at(30, "15:00"))
        self.assertIsNone(r["dose_until"])
        self.assertEqual(r["doses"], [])

    def test_finished_with_second_dose(self):
        events = [
            ev(START, "reminder_sent", kind="scheduled"),
            dose(at(30, "13:05"), next_at=at(30, "19:05")),
            ev(at(30, "19:05"), "reminder_sent", kind="dose"),
            ev(at(30, "19:10"), "taken", missed_doses=0, prev_missed_doses=1, count=1),
        ]
        r = self.restore(events, at(30, "20:00"))
        self.assertTrue(r["is_taken"])
        self.assertEqual(r["missed_doses"], 0)
        self.assertEqual([d[2] for d in r["doses"]], [1, 1])

    def test_undo_final_dose_keeps_first(self):
        events = [
            ev(START, "reminder_sent", kind="scheduled"),
            dose(at(30, "13:05"), next_at=at(30, "19:05")),
            ev(at(30, "19:05"), "reminder_sent", kind="dose"),
            ev(at(30, "19:10"), "taken", missed_doses=0, prev_missed_doses=1, count=1),
            ev(at(30, "19:11"), "undo_taken", missed_doses=1, remaining=1),
        ]
        r = self.restore(events, at(30, "20:00"))
        self.assertFalse(r["is_taken"])
        self.assertEqual(r["missed_doses"], 1)
        self.assertEqual(len(r["doses"]), 1)
        self.assertIsNone(r["dose_until"])

    def test_partial_day_closed_while_down(self):
        events = [ev(START, "reminder_sent", kind="scheduled"), dose(at(30, "13:05"), next_at=at(30, "19:05"))]
        r = self.restore(events, at(31, "10:00"))
        self.assertEqual(r["pending_missed"], START)
        self.assertEqual([d[2] for d in r["pending_doses"]], [1])

    def test_missed_logged_late_counts_for_today(self):
        # 締めのときに止まっていて、29日分の「記録なし」を30日14時に記録した日
        events = [
            ev(at(30, "14:00"), "missed", missed_doses=2, cycle="2026-10-29"),
            ev(at(30, "14:00"), "reminder_sent", kind="restart"),
            ev(at(30, "14:10"), "taken", missed_doses=0, prev_missed_doses=2),
        ]
        self.assertEqual([d[2] for d in self.restore(events, at(30, "15:00"))["doses"]], [3])

    def test_old_taken_without_count_means_all(self):
        # 1錠ずつ飲む機能より前のログ: count がない taken は残りすべて
        events = [ev(START, "reminder_sent", kind="scheduled"), ev(at(30, "13:05"), "taken", missed_doses=0, prev_missed_doses=1)]
        r = self.restore(events, at(30, "14:00"))
        self.assertTrue(r["is_taken"])
        self.assertEqual([d[2] for d in r["doses"]], [2])


if __name__ == "__main__":
    unittest.main()
