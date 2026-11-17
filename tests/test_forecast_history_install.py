"""Tests for forecasts, auto-calibration, history and the installer.

    python3 -m unittest discover -s tests
"""
import json
import plistlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_usage as cu  # noqa: E402
import installer  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def entry(minutes_ago, inp=1000, model="claude-sonnet-5-5", project=None):
    return {"ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "provider": "anthropic",
            "surface": "web", "model": model, "input": inp, "output": 0,
            "cache_creation": 0, "cache_read": 0, "project": project}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.limits = self.dir / "claude_limits.json"
        self._orig = (cu.LIMITS_FILE, cu.SOURCES, cu.LOCAL_TZ, cu.DATA_FILE)
        cu.LIMITS_FILE, cu.SOURCES, cu.LOCAL_TZ = self.limits, {}, timezone.utc
        cu.DATA_FILE = self.dir / "data.json"

    def tearDown(self):
        cu.LIMITS_FILE, cu.SOURCES, cu.LOCAL_TZ, cu.DATA_FILE = self._orig
        self._tmp.cleanup()

    def data(self, entries, calibrations=()):
        return {"entries": entries, "calibrations": list(calibrations),
                "profiles": dict(cu.DEFAULT_PROFILES), "history": {}}

    def write_limits(self, five=None, five_resets=None, week=None, week_resets=None, captured=NOW):
        body = {"captured_at": captured.isoformat()}
        if five is not None:
            body["five_hour"] = {"used_percentage": five, "resets_at": int(five_resets.timestamp())}
        if week is not None:
            body["seven_day"] = {"used_percentage": week, "resets_at": int(week_resets.timestamp())}
        self.limits.write_text(json.dumps(body))


class ForecastTests(Base):
    START = NOW - timedelta(hours=2)
    END = NOW + timedelta(hours=3)

    def test_average_pace_reaches_limit_before_reset(self):
        fc = cu.forecast(60, self.START, self.END, NOW)   # 30%/h, 40% to go
        self.assertAlmostEqual(fc["projected"], 150)
        self.assertEqual(fc["hits_at"], NOW + timedelta(minutes=80))

    def test_slow_pace_stays_under(self):
        fc = cu.forecast(20, self.START, self.END, NOW)
        self.assertAlmostEqual(fc["projected"], 50)
        self.assertIsNone(fc["hits_at"])

    def test_recent_share_follows_current_pace(self):
        # Half of the window's usage fell in the last hour, so the pace now is 20%/h.
        fc = cu.forecast(40, self.START, self.END, NOW, recent_share=0.5)   # 20%/h now
        self.assertAlmostEqual(fc["projected"], 100)
        idle = cu.forecast(40, self.START, self.END, NOW, recent_share=0.0)
        self.assertAlmostEqual(idle["projected"], 40)
        self.assertIsNone(idle["hits_at"])

    def test_too_early_or_no_percent_gives_none(self):
        self.assertIsNone(cu.forecast(5, NOW - timedelta(minutes=3), self.END, NOW))
        self.assertIsNone(cu.forecast(None, self.START, self.END, NOW))

    def test_already_over_hits_now(self):
        self.assertEqual(cu.forecast(100, self.START, self.END, NOW)["hits_at"], NOW)

    def test_summary_carries_five_hour_and_weekly_forecast(self):
        self.write_limits(five=60, five_resets=NOW + timedelta(hours=3),
                          week=50, week_resets=NOW + timedelta(days=5))
        (row,) = cu.summarize(self.data([entry(110), entry(100)]), NOW)   # nothing in the last hour
        self.assertAlmostEqual(row["forecast_percent"], 60)
        self.assertIsNone(row["forecast_hits_at"])
        self.assertAlmostEqual(row["weekly_forecast_percent"], 175)       # 50% in 2 of 7 days
        self.assertEqual(row["weekly_hits_at"], NOW + timedelta(days=2))

    def test_weekly_forecast_waits_for_half_a_day(self):
        self.write_limits(week=2, week_resets=NOW + timedelta(days=7) - timedelta(hours=1))
        (row,) = cu.summarize(self.data([entry(5)]), NOW)
        self.assertEqual(row["weekly_percent"], 2)
        self.assertIsNone(row["weekly_forecast_percent"])


class FreshnessTests(Base):
    def test_live_stale_and_estimate(self):
        data = self.data([entry(5)])
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(row["freshness"], "estimate")
        self.write_limits(five=30, five_resets=NOW + timedelta(hours=2))
        self.assertEqual(cu.summarize(data, NOW)[0]["freshness"], "live")
        self.write_limits(five=30, five_resets=NOW + timedelta(hours=2),
                          captured=NOW - timedelta(minutes=45))
        self.assertEqual(cu.summarize(data, NOW)[0]["freshness"], "stale")


class AutoCalibrateTests(Base):
    def test_live_reading_sets_the_ceiling(self):
        self.write_limits(five=25, five_resets=NOW + timedelta(hours=2))
        data = self.data([entry(30, inp=4000), entry(5, inp=1000)])
        self.assertTrue(cu.auto_calibrate(data, NOW))
        (cal,) = data["calibrations"]
        self.assertTrue(cal["auto"])
        self.assertAlmostEqual(cal["ceiling"], 20000)
        self.assertFalse(cu.auto_calibrate(data, NOW))                    # same reading: no write

    def test_new_reading_replaces_the_window_entry(self):
        data = self.data([entry(30, inp=4000)])
        self.write_limits(five=20, five_resets=NOW + timedelta(hours=2))
        cu.auto_calibrate(data, NOW)
        self.write_limits(five=40, five_resets=NOW + timedelta(hours=2))
        self.assertTrue(cu.auto_calibrate(data, NOW))
        (cal,) = data["calibrations"]
        self.assertAlmostEqual(cal["ceiling"], 10000)

    def test_skips_tiny_stale_and_log_only_readings(self):
        data = self.data([entry(30, inp=4000)])
        self.assertFalse(cu.auto_calibrate(data, NOW))                    # no provider figure
        self.write_limits(five=2, five_resets=NOW + timedelta(hours=2))
        self.assertFalse(cu.auto_calibrate(data, NOW))                    # under 5%
        self.write_limits(five=50, five_resets=NOW + timedelta(hours=2),
                          captured=NOW - timedelta(hours=1))
        self.assertFalse(cu.auto_calibrate(data, NOW))                    # stale
        self.assertEqual(data["calibrations"], [])

    def test_estimate_uses_the_learned_ceiling_after_the_window_ends(self):
        self.write_limits(five=25, five_resets=NOW + timedelta(hours=2))
        data = self.data([entry(30, inp=5000)])
        cu.auto_calibrate(data, NOW)                                      # ceiling 20000
        self.limits.unlink()
        (row,) = cu.summarize(data, NOW)
        self.assertEqual(row["source"], "estimate")
        self.assertAlmostEqual(row["percent"], 25.0)

    def test_keeps_a_bounded_number(self):
        data = self.data([entry(30)])
        data["calibrations"] = [{"provider": "anthropic", "window_start": str(i), "percent": 10,
                                 "ceiling": 1.0, "auto": True} for i in range(cu.AUTO_CAL_KEEP + 5)]
        data["calibrations"].append({"provider": "anthropic", "window_start": "m", "ceiling": 2.0})
        self.write_limits(five=25, five_resets=NOW + timedelta(hours=2))
        cu.auto_calibrate(data, NOW)
        autos = [c for c in data["calibrations"] if c.get("auto")]
        self.assertEqual(len(autos), cu.AUTO_CAL_KEEP)
        self.assertEqual(len(data["calibrations"]), cu.AUTO_CAL_KEEP + 1)  # manual one kept


class HistoryTests(Base):
    def test_project_name(self):
        self.assertEqual(cu.project_name("/Users/x/Projects/tracker"), "tracker")
        self.assertEqual(cu.project_name("/Users/x/.t3/worktrees/Nexus/t3-1234"), "Nexus")
        self.assertIsNone(cu.project_name(None))

    def test_rows_cover_every_day_and_split_by_model_and_project(self):
        data = self.data([entry(60, inp=100, project="a"),
                          entry(60 * 24, inp=300, model="claude-opus-5-5", project="b"),
                          entry(60 * 24 + 5, inp=50, project="a")])
        rows = cu.history_rows(data, NOW, days=3)
        self.assertEqual([r["day"] for r in rows], ["2026-11-13", "2026-11-14", "2026-11-15"])
        self.assertEqual([r["tokens"] for r in rows], [0, 350, 100])
        self.assertEqual(rows[1]["by_model"], {"claude-opus-5-5": 300, "claude-sonnet-5-5": 50})
        self.assertEqual(cu.history_totals(rows, "by_project"), [("b", 300), ("a", 150)])

    def test_finished_days_survive_log_deletion(self):
        data = self.data([entry(60 * 24, inp=300), entry(10, inp=100)])
        self.assertTrue(cu.update_history(data, NOW))
        self.assertEqual(list(data["history"]), ["2026-11-14"])            # today is not stored yet
        self.assertFalse(cu.update_history(data, NOW))
        data["entries"] = []                                               # logs gone
        self.assertFalse(cu.update_history(data, NOW))                     # never shrinks
        rows = cu.history_rows(data, NOW, days=2)
        self.assertEqual([r["tokens"] for r in rows], [300, 0])

    def test_maintain_saves_only_when_something_changed(self):
        data = self.data([entry(60 * 24, inp=300)])
        self.assertTrue(cu.maintain(data, NOW))
        self.assertIn("2026-11-14", json.loads(cu.DATA_FILE.read_text())["history"])
        self.assertFalse(cu.maintain(data, NOW))


class InstallerTests(Base):
    def setUp(self):
        super().setUp()
        self.settings = self.dir / "settings.json"
        self.cmd = installer.statusline_command(python="/usr/bin/python3")

    def test_command_points_at_the_capture_script(self):
        self.assertIn("statusline_capture.py", self.cmd)
        self.assertTrue(self.cmd.startswith("/usr/bin/python3 ") or self.cmd.startswith("env "))

    def test_installs_into_missing_file(self):
        status, backup = installer.install_statusline(self.settings, self.cmd)
        self.assertEqual((status, backup), ("installed", None))
        saved = json.loads(self.settings.read_text())
        self.assertEqual(saved["statusLine"], {"type": "command", "command": self.cmd})

    def test_keeps_other_settings_and_backs_up(self):
        self.settings.write_text('{"theme": "dark"}')
        status, backup = installer.install_statusline(self.settings, self.cmd)
        self.assertEqual(status, "installed")
        self.assertEqual(json.loads(backup.read_text()), {"theme": "dark"})
        self.assertEqual(json.loads(self.settings.read_text())["theme"], "dark")
        self.assertEqual(installer.install_statusline(self.settings, self.cmd), ("unchanged", None))

    def test_refuses_to_replace_someone_elses_status_line(self):
        other = {"statusLine": {"type": "command", "command": "my-line.sh"}}
        self.settings.write_text(json.dumps(other))
        with self.assertRaises(installer.InstallError):
            installer.install_statusline(self.settings, self.cmd)
        self.assertEqual(json.loads(self.settings.read_text()), other)
        status, backup = installer.install_statusline(self.settings, self.cmd, force=True)
        self.assertEqual(status, "replaced")
        self.assertEqual(json.loads(backup.read_text()), other)

    def test_invalid_json_is_never_overwritten(self):
        self.settings.write_text("{broken")
        with self.assertRaises(installer.InstallError):
            installer.install_statusline(self.settings, self.cmd)
        self.assertEqual(self.settings.read_text(), "{broken")

    def test_uninstall_removes_only_ours(self):
        self.settings.write_text('{"statusLine": {"type": "command", "command": "my-line.sh"}}')
        self.assertFalse(installer.uninstall_statusline(self.settings))
        installer.install_statusline(self.settings, self.cmd, force=True)
        self.assertEqual(installer.statusline_state(self.settings), "ours")
        self.assertTrue(installer.uninstall_statusline(self.settings))
        self.assertEqual(installer.statusline_state(self.settings), "none")

    def test_plist_has_real_paths_and_env(self):
        plist = plistlib.loads(installer.render_plist({"CLAUDE_USAGE_DATA": "/x/data.json"}))
        self.assertEqual(plist["Label"], installer.LABEL)
        self.assertEqual(plist["ProgramArguments"][1], str(installer.PROJECT / "menubar.py"))
        self.assertEqual(plist["EnvironmentVariables"], {"CLAUDE_USAGE_DATA": "/x/data.json"})
        self.assertNotIn(b"__PROJECT_DIR__", installer.render_plist())


try:
    import menubar  # needs rumps
except ImportError:
    menubar = None


@unittest.skipIf(menubar is None, "rumps not installed (see requirements-menubar.txt)")
class TitleMarkTests(unittest.TestCase):
    def row(self, freshness):
        return {"provider": "anthropic", "active": True, "total": 1, "percent": 42.0,
                "freshness": freshness}

    def test_estimate_and_stale_are_marked(self):
        self.assertEqual(menubar.format_title([self.row("live")]), "anthropic 42%")
        self.assertEqual(menubar.format_title([self.row("estimate")]), "anthropic ~42%")
        self.assertEqual(menubar.format_title([self.row("stale")]), "anthropic 42%?")

    def test_title_marks_uncalibrated_estimate(self):
        row = dict(self.row("estimate"), percent=None, total=2_400_000)
        self.assertEqual(menubar.format_title([row]), "anthropic ~2.4M")


if __name__ == "__main__":
    unittest.main()
