"""Tests for provider-reported limits (Claude Code status line) and the capture script.

    python3 -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_usage as cu  # noqa: E402
import statusline_capture as sc  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def epoch(dt):
    return int(dt.timestamp())


def entry(minutes_ago, inp=1000, model="claude-sonnet-5-5"):
    return {"ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "provider": "anthropic",
            "surface": "web", "model": model, "input": inp, "output": 0,
            "cache_creation": 0, "cache_read": 0}


class LimitsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.limits = Path(self._tmp.name) / "claude_limits.json"
        self._orig_limits = cu.LIMITS_FILE
        self._orig_sources = cu.SOURCES
        cu.LIMITS_FILE = self.limits
        cu.SOURCES = {}

    def tearDown(self):
        cu.LIMITS_FILE = self._orig_limits
        cu.SOURCES = self._orig_sources
        self._tmp.cleanup()

    def write_limits(self, five_used=None, five_resets=None, week_used=None, week_resets=None,
                     captured=NOW):
        body = {"captured_at": captured.isoformat()}
        if five_used is not None:
            body["five_hour"] = {"used_percentage": five_used, "resets_at": epoch(five_resets)}
        if week_used is not None:
            body["seven_day"] = {"used_percentage": week_used, "resets_at": epoch(week_resets)}
        self.limits.write_text(json.dumps(body))

    def data(self, entries):
        return {"entries": entries, "calibrations": [], "profiles": dict(cu.DEFAULT_PROFILES)}


class ProviderWindowTests(LimitsTestCase):
    def test_live_provider_window_replaces_estimate(self):
        resets = NOW + timedelta(minutes=90)
        self.write_limits(five_used=23.5, five_resets=resets, week_used=41.2,
                          week_resets=NOW + timedelta(days=3))
        data = self.data([entry(30, inp=4000), entry(5, inp=1000)])
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(row["source"], "provider")
        self.assertAlmostEqual(row["percent"], 23.5)
        self.assertEqual(row["window_end"], resets.replace(microsecond=0))
        self.assertEqual(row["window_start"], resets - timedelta(hours=5))
        self.assertEqual(row["total"], 5000)               # local logs still give the breakdown
        self.assertAlmostEqual(row["weekly_percent"], 41.2)
        self.assertEqual(row["captured_at"], NOW)

    def test_provider_window_only_counts_events_inside_it(self):
        # Provider window started 2h ago, so an event 3h ago is outside it.
        self.write_limits(five_used=10, five_resets=NOW + timedelta(hours=3))
        data = self.data([entry(180, inp=9000), entry(10, inp=1000)])
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(row["total"], 1000)

    def test_ended_provider_window_falls_back_to_estimate(self):
        self.write_limits(five_used=99, five_resets=NOW - timedelta(minutes=1))
        (row,) = cu.summarize(self.data([entry(30)]), NOW)
        self.assertEqual(row["source"], "estimate")
        self.assertIsNone(row["percent"])  # no ceiling calibrated, and no provider number

    def test_no_limits_file_uses_estimate(self):
        (row,) = cu.summarize(self.data([entry(30)]), NOW)
        self.assertEqual(row["source"], "estimate")

    def test_provider_percent_drives_alerts(self):
        self.write_limits(five_used=93, five_resets=NOW + timedelta(hours=2))
        (row,) = cu.summarize(self.data([entry(5)]), NOW)
        self.assertEqual(row["alert"], 90)

    def test_model_usage_uses_provider_window(self):
        self.write_limits(five_used=20, five_resets=NOW + timedelta(hours=2))
        data = self.data([entry(200, inp=500, model="claude-opus-5-5"),
                          entry(10, inp=1000, model="claude-haiku-5-5")])
        rows = {r["model"]: r for r in cu.model_usage(data, NOW)}
        self.assertEqual(rows["claude-haiku-5-5"]["window_weighted"], 1000)
        self.assertEqual(rows["claude-opus-5-5"]["window_weighted"], 0)  # outside provider window

    def test_calibration_uses_the_same_window(self):
        self.write_limits(five_used=20, five_resets=NOW + timedelta(hours=2))
        data = self.data([entry(200, inp=9000), entry(10, inp=1000)])
        total, ceiling = cu.calibrate(data, "anthropic", 50, NOW)
        self.assertEqual(total, 1000)
        self.assertEqual(ceiling, 2000)

    def test_corrupt_limits_file_is_ignored(self):
        self.limits.write_text("{not json")
        (row,) = cu.summarize(self.data([entry(30)]), NOW)
        self.assertEqual(row["source"], "estimate")


class CaptureTests(LimitsTestCase):
    def payload(self, **rate):
        return json.dumps({"session_id": "s", "rate_limits": rate} if rate else {"session_id": "s"})

    def test_saves_windows_and_prints_status_line(self):
        resets = epoch(NOW + timedelta(hours=1))
        text = self.payload(five_hour={"used_percentage": 23.5, "resets_at": resets},
                            seven_day={"used_percentage": 41.2, "resets_at": resets})
        out = sc.capture(text, path=self.limits, now=NOW)
        self.assertEqual(out, "5h 24% · 7d 41%")
        saved = json.loads(self.limits.read_text())
        self.assertEqual(saved["five_hour"]["resets_at"], resets)
        self.assertEqual(saved["captured_at"], NOW.isoformat())

    def test_non_subscriber_prints_nothing_and_keeps_file(self):
        self.write_limits(five_used=10, five_resets=NOW + timedelta(hours=1))
        before = self.limits.read_text()
        self.assertEqual(sc.capture(self.payload(), path=self.limits, now=NOW), "")
        self.assertEqual(self.limits.read_text(), before)

    def test_partial_window_prints_what_is_there(self):
        text = self.payload(five_hour={"used_percentage": 7, "resets_at": epoch(NOW)})
        self.assertEqual(sc.capture(text, path=self.limits, now=NOW), "5h 7%")

    def test_bad_input_never_raises(self):
        self.assertEqual(sc.capture("", path=self.limits, now=NOW), "")
        self.assertEqual(sc.capture("[1, 2]", path=self.limits, now=NOW), "")
        self.assertFalse(self.limits.exists())

    def test_round_trip_into_summary(self):
        text = self.payload(five_hour={"used_percentage": 64, "resets_at": epoch(NOW + timedelta(hours=2))})
        sc.capture(text, path=self.limits, now=NOW)
        (row,) = cu.summarize(self.data([entry(5)]), NOW)
        self.assertEqual(row["source"], "provider")
        self.assertAlmostEqual(row["percent"], 64.0)


if __name__ == "__main__":
    unittest.main()
