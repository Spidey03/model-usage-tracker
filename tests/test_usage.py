"""Tests for summarize() and the menu bar title. Run from the project root:

    python3 -m unittest discover -s tests          # summarize only
    .venv/bin/python -m unittest discover -s tests  # also runs menu bar tests
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_usage as cu  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def entry(provider, minutes_ago, surface="web", model="claude-sonnet-5-5", inp=1000, out=0):
    return {"ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "provider": provider,
            "surface": surface, "model": model, "input": inp, "output": out,
            "cache_creation": 0, "cache_read": 0}


def make_data(entries, calibrations=()):
    return {"entries": entries, "calibrations": list(calibrations),
            "profiles": dict(cu.DEFAULT_PROFILES)}


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self._orig_sources = cu.SOURCES
        cu.SOURCES = {}  # no Claude Code logs during tests

    def tearDown(self):
        cu.SOURCES = self._orig_sources

    def test_no_events_returns_empty(self):
        self.assertEqual(cu.summarize(make_data([]), NOW), [])

    def test_active_window_with_calibrated_ceiling(self):
        data = make_data(
            [entry("anthropic", 30, inp=4000), entry("anthropic", 10, inp=1000, surface="cli")],
            calibrations=[{"provider": "anthropic", "window_start": (NOW - timedelta(minutes=30)).isoformat(),
                           "ceiling": 10000.0}],
        )
        (row,) = cu.summarize(data, NOW)
        self.assertTrue(row["active"])
        self.assertEqual(row["total"], 5000)
        self.assertEqual(row["minutes_left"], 270)  # 5h window started 30 min ago
        self.assertAlmostEqual(row["percent"], 50.0)
        self.assertEqual(row["alert"], None)
        self.assertEqual(row["surfaces"][0][0], "web")  # largest first

    def test_alert_reports_highest_threshold(self):
        data = make_data([entry("anthropic", 5, inp=9500)],
                         calibrations=[{"provider": "anthropic", "window_start": (NOW - timedelta(minutes=5)).isoformat(),
                                        "ceiling": 10000.0}])
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(row["alert"], 90)

    def test_uncalibrated_has_no_percent(self):
        (row,) = cu.summarize(make_data([entry("openai", 5)]), NOW)
        self.assertTrue(row["active"])
        self.assertIsNone(row["ceiling"])
        self.assertIsNone(row["percent"])

    def test_expired_window_is_inactive_but_listed(self):
        (row,) = cu.summarize(make_data([entry("openai", 6 * 60)]), NOW)
        self.assertFalse(row["active"])
        self.assertIsNone(row["window_end"])

    def test_one_row_per_provider_sorted(self):
        rows = cu.summarize(make_data([entry("openai", 5), entry("anthropic", 5)]), NOW)
        self.assertEqual([r["provider"] for r in rows], ["anthropic", "openai"])


try:
    import menubar  # needs rumps
except ImportError:
    menubar = None


@unittest.skipIf(menubar is None, "rumps not installed (see requirements-menubar.txt)")
class TitleTests(unittest.TestCase):
    def row(self, **kw):
        base = {"provider": "anthropic", "active": True, "total": 1500, "percent": None,
                "ceiling": None, "window_end": NOW, "minutes_left": 60, "surfaces": [],
                "alert": None, "window_start": NOW}
        base.update(kw)
        return base

    def test_empty(self):
        self.assertEqual(menubar.format_title([]), "No usage")

    def test_percent_for_every_provider(self):
        rows = [self.row(percent=42.4), self.row(provider="openai", percent=7.0)]
        self.assertEqual(menubar.format_title(rows), "anthropic 42%  ·  openai 7.0%")

    def test_small_percent_keeps_one_decimal(self):
        self.assertEqual(menubar.format_title([self.row(percent=0.34)]), "anthropic 0.3%")

    def test_uncalibrated_shows_tokens_and_idle(self):
        rows = [self.row(total=1_500_000), self.row(provider="openai", active=False)]
        self.assertEqual(menubar.format_title(rows), "anthropic 1.5M  ·  openai idle")


if __name__ == "__main__":
    unittest.main()
