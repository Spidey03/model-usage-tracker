"""Tests for summarize() and the menu bar title. Run from the project root:

    python3 -m unittest discover -s tests          # summarize only
    .venv/bin/python -m unittest discover -s tests  # also runs menu bar tests
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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


class CalibrateTests(unittest.TestCase):
    def setUp(self):
        self._orig_sources = cu.SOURCES
        cu.SOURCES = {}

    def tearDown(self):
        cu.SOURCES = self._orig_sources

    def test_records_ceiling_for_active_window(self):
        data = make_data([entry("anthropic", 30, inp=4000), entry("anthropic", 10, inp=1000)])
        total, ceiling = cu.calibrate(data, "anthropic", 50, NOW)
        self.assertEqual(total, 5000)
        self.assertAlmostEqual(ceiling, 10000.0)
        (cal,) = data["calibrations"]
        self.assertEqual(cal["provider"], "anthropic")
        self.assertEqual(cal["percent"], 50)
        self.assertEqual(cal["window_start"], (NOW - timedelta(minutes=30)).isoformat())
        # the ceiling feeds back into summarize()
        (row,) = cu.summarize(data, NOW)
        self.assertAlmostEqual(row["percent"], 50.0)

    def test_no_active_window_raises_and_records_nothing(self):
        data = make_data([entry("openai", 6 * 60)])
        with self.assertRaises(ValueError):
            cu.calibrate(data, "openai", 40, NOW)
        self.assertEqual(data["calibrations"], [])

    def test_rejects_out_of_range_percent(self):
        data = make_data([entry("anthropic", 5)])
        for bad in (0, -5, 101):
            with self.assertRaises(ValueError):
                cu.calibrate(data, "anthropic", bad, NOW)
        self.assertEqual(data["calibrations"], [])


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self._orig_sources = cu.SOURCES
        cu.SOURCES = {}

    def tearDown(self):
        cu.SOURCES = self._orig_sources

    def test_buckets_sum_to_total_and_place_events_by_time(self):
        # Window starts at the first event (59 min ago). 5h / 10 buckets = 30 min per slice.
        # The 59-min-ago event is at offset 0 (slice 0); the 5-min-ago event is 54 min in (slice 1).
        data = make_data([entry("anthropic", 59, inp=100), entry("anthropic", 5, inp=300)])
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(len(row["timeline"]), cu.TIMELINE_BUCKETS)
        self.assertAlmostEqual(sum(row["timeline"]), row["total"])
        self.assertAlmostEqual(row["timeline"][0], 100)
        self.assertAlmostEqual(row["timeline"][1], 300)


class ModelUsageTests(unittest.TestCase):
    def setUp(self):
        self._orig_sources = cu.SOURCES
        cu.SOURCES = {}

    def tearDown(self):
        cu.SOURCES = self._orig_sources

    def test_family_and_weight_helpers(self):
        self.assertEqual(cu.model_family("claude-opus-5-5"), "opus")
        self.assertEqual(cu.model_family("gpt-x"), "other")
        prof = {"model_weights": {"opus": 5.0}}
        self.assertEqual(cu.model_weight(prof, "claude-opus-5-5"), 5.0)
        self.assertEqual(cu.model_weight(prof, "claude-haiku-5-5"), 1.0)

    def test_all_time_and_window_share(self):
        data = make_data(
            [entry("anthropic", 6 * 60, model="claude-opus-5-5", inp=500),     # its window already ended
             entry("anthropic", 30, model="claude-opus-5-5", inp=1000),         # in window
             entry("anthropic", 10, model="claude-haiku-5-5", inp=3000)],       # in window
            calibrations=[{"provider": "anthropic", "window_start": (NOW - timedelta(minutes=30)).isoformat(),
                           "ceiling": 8000.0}],
        )
        data["profiles"]["anthropic"] = dict(cu.DEFAULT_PROFILES["anthropic"],
                                             model_weights={"opus": 2.0, "sonnet": 1.0, "haiku": 1.0})
        rows = {r["model"]: r for r in cu.model_usage(data, NOW)}
        opus, haiku = rows["claude-opus-5-5"], rows["claude-haiku-5-5"]
        self.assertEqual(opus["requests"], 2)
        self.assertEqual(opus["tokens"], 1500)                 # all time, includes the old event
        self.assertEqual(opus["window_weighted"], 2000)        # 1000 tokens * weight 2
        self.assertEqual(haiku["window_weighted"], 3000)
        self.assertAlmostEqual(opus["window_share"], 40.0)
        self.assertAlmostEqual(opus["limit_pct"], 25.0)        # 2000 of 8000
        self.assertEqual(opus["weight"], 2.0)

    def test_no_window_means_no_window_figures(self):
        (row,) = cu.model_usage(make_data([entry("openai", 6 * 60, model="gpt-x")]), NOW)
        self.assertEqual(row["window_weighted"], 0.0)
        self.assertIsNone(row["window_share"])
        self.assertIsNone(row["limit_pct"])
        self.assertEqual(row["requests"], 1)


class ClaudeCacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name)
        self.projects = self.config / "projects" / "p"
        self.projects.mkdir(parents=True)
        self._env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.config)
        cu._CLAUDE_FILE_CACHE.clear()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._env
        cu._CLAUDE_FILE_CACHE.clear()
        self._tmp.cleanup()

    def write(self, name, *records):
        f = self.projects / name
        f.write_text("".join(json.dumps(r) + "\n" for r in records))
        return f

    @staticmethod
    def rec(msg_id, inp, ts="2026-11-15T11:00:00Z"):
        return {"timestamp": ts, "uuid": "u-" + msg_id,
                "message": {"id": msg_id, "model": "claude-sonnet-5-5",
                            "usage": {"input_tokens": inp, "output_tokens": 0}}}

    def test_unchanged_file_is_not_reparsed(self):
        self.write("a.jsonl", self.rec("m1", 10))
        first = cu.anthropic_claude_code({})
        with mock.patch.object(cu, "_parse_claude_file", side_effect=AssertionError("reparsed")):
            second = cu.anthropic_claude_code({})
        self.assertEqual(len(first), 1)
        self.assertEqual([e["input"] for e in second], [10])

    def test_changed_file_is_reparsed(self):
        f = self.write("a.jsonl", self.rec("m1", 10))
        cu.anthropic_claude_code({})
        self.write("a.jsonl", self.rec("m1", 10), self.rec("m2", 25))  # new line, new size
        self.assertEqual(sorted(e["input"] for e in cu.anthropic_claude_code({})), [10, 25])

    def test_deleted_file_is_dropped_from_cache(self):
        f = self.write("a.jsonl", self.rec("m1", 10))
        cu.anthropic_claude_code({})
        f.unlink()
        self.assertEqual(cu.anthropic_claude_code({}), [])
        self.assertEqual(cu._CLAUDE_FILE_CACHE, {})

    def test_duplicate_message_across_files_counted_once(self):
        self.write("a.jsonl", self.rec("m1", 10))
        self.write("b.jsonl", self.rec("m1", 10), self.rec("m2", 5))
        self.assertEqual(sorted(e["input"] for e in cu.anthropic_claude_code({})), [5, 10])


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

    def test_sparkline_scales_to_busiest_slice(self):
        self.assertEqual(menubar.sparkline([0, 0, 0]), "▁▁▁")
        self.assertEqual(menubar.sparkline([0, 4, 8]), "▁▅█")
        self.assertEqual(menubar.sparkline([]), "")

    def test_model_title_with_and_without_limit(self):
        base = {"model": "claude-opus-5-5", "tokens": 1_500_000, "window_weighted": 0.0,
                "limit_pct": None}
        self.assertEqual(menubar.model_title(base), "claude-opus-5-5  ·  1.5M all time")
        self.assertEqual(menubar.model_title(dict(base, window_weighted=2000.0, limit_pct=25.0)),
                         "claude-opus-5-5  ·  1.5M all time  ·  25% of limit")
        self.assertEqual(menubar.model_title(dict(base, window_weighted=2000.0)),
                         "claude-opus-5-5  ·  1.5M all time  ·  2k this window")

    def test_icons_exist_for_every_family(self):
        for family in ("opus", "sonnet", "haiku", "other"):
            self.assertTrue(Path(menubar.model_icon(family)).exists(), family)
        self.assertTrue(Path(menubar.provider_icon("anthropic")).exists())
        self.assertTrue(Path(menubar.provider_icon("openai")).exists())

    def test_small_percent_keeps_one_decimal(self):
        self.assertEqual(menubar.format_title([self.row(percent=0.34)]), "anthropic 0.3%")

    def test_uncalibrated_shows_tokens_and_idle(self):
        rows = [self.row(total=1_500_000), self.row(provider="openai", active=False)]
        self.assertEqual(menubar.format_title(rows), "anthropic 1.5M  ·  openai idle")


if __name__ == "__main__":
    unittest.main()
