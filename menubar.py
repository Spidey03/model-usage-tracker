#!/usr/bin/env python3
"""macOS menu bar view of claude_usage. Read-only (Phase 1).

Needs rumps: pip install -r requirements-menubar.txt
Run: .venv/bin/python menubar.py

The title shows every provider's current window. Usage numbers come from
claude_usage.summarize(), the same code the CLI report uses.
"""
import subprocess
from datetime import datetime, timezone

import rumps

import claude_usage as cu

REFRESH_SECONDS = 60
APP_NAME = "Claude Usage"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def fmt_percent(pct):
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def title_part(row):
    name = row["provider"]
    if not row["active"]:
        return f"{name} idle"
    if row["percent"] is not None:
        return f"{name} {fmt_percent(row['percent'])}"
    return f"{name} {fmt_tokens(row['total'])}"


def format_title(rows):
    if not rows:
        return "No usage"
    return "  ·  ".join(title_part(r) for r in rows)


def provider_header(row):
    name = row["provider"]
    if not row["active"]:
        return f"{name}: no active window"
    if row["percent"] is not None:
        return f"{name}: {row['percent']:.0f}% used (estimate)"
    return f"{name}: {fmt_tokens(row['total'])} weighted (no ceiling)"


def provider_items(row):
    items = [rumps.MenuItem(provider_header(row))]
    if not row["active"]:
        return items
    items.append(rumps.MenuItem(
        f"Resets {cu.local(row['window_end'])}  ({row['minutes_left']} min left)"))
    if row["ceiling"] is None:
        items.append(rumps.MenuItem("No ceiling calibrated yet (run the CLI calibrate command)"))
    breakdown = rumps.MenuItem("Breakdown")
    for surface, model, v in row["surfaces"]:
        breakdown.add(rumps.MenuItem(f"{surface} · {model}: {v:,.0f}"))
    items.append(breakdown)
    return items


class UsageMenuBar(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="Claude Usage", quit_button=None)
        self._notified = {}  # (provider, window_start iso) -> highest threshold already alerted
        self.timer = rumps.Timer(self.refresh, REFRESH_SECONDS)
        self.timer.start()
        self.refresh()

    def refresh(self, _=None):
        try:
            data = cu.load_data()
            rows = cu.summarize(data, datetime.now(timezone.utc))
        except Exception as exc:  # keep the menu bar alive on bad data
            self.title = "Usage ⚠"
            self._rebuild_menu([rumps.MenuItem(f"Error: {exc}")])
            return
        self.title = format_title(rows)
        self._rebuild_menu(self._build_items(rows))
        self._alert(rows)

    def _build_items(self, rows):
        items = []
        if not rows:
            items.append(rumps.MenuItem("No usage found. Run Claude Code or use the CLI add command."))
        for i, row in enumerate(rows):
            if i:
                items.append(rumps.separator)
            items.extend(provider_items(row))
        items += [
            rumps.separator,
            rumps.MenuItem(f"Updated {datetime.now().strftime('%H:%M')}"),
            rumps.MenuItem("Refresh", callback=self.refresh),
            rumps.MenuItem("Open data file", callback=self.open_data),
            rumps.separator,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        return items

    def _rebuild_menu(self, items):
        self.menu.clear()
        for item in items:
            self.menu.add(item)

    def _alert(self, rows):
        active_keys = set()
        for row in rows:
            if not row["active"]:
                continue
            key = (row["provider"], row["window_start"].isoformat())
            active_keys.add(key)
            level = row["alert"]
            if level and level > self._notified.get(key, 0):
                self._notified[key] = level
                rumps.notification(
                    APP_NAME,
                    f"{row['provider']} at {row['percent']:.0f}% (estimate)",
                    f"Crossed {level}%. Resets {cu.local(row['window_end'])}.",
                )
        self._notified = {k: v for k, v in self._notified.items() if k in active_keys}

    def open_data(self, _=None):
        target = cu.DATA_FILE if cu.DATA_FILE.exists() else cu.DATA_FILE.parent
        subprocess.run(["open", "-R", str(target)], check=False)


def hide_dock_icon():
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    except ImportError:
        return
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


if __name__ == "__main__":
    hide_dock_icon()
    UsageMenuBar().run()
