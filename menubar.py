#!/usr/bin/env python3
"""macOS menu bar view of claude_usage.

Needs rumps: pip install -r requirements-menubar.txt
Run: .venv/bin/python menubar.py

The title shows every provider's current window. Usage numbers come from
claude_usage.summarize(), the same code the CLI report uses.
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import rumps

import claude_usage as cu
import installer

REFRESH_SECONDS = 60
APP_NAME = "Claude Usage"
ASSETS = Path(__file__).resolve().parent / "assets"
ICON_PATH = str(ASSETS / "menubarTemplate.png")
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def icon_file(name):
    return str(ASSETS / "icons" / f"{name}.png")


def provider_icon(provider):
    return icon_file("provider-anthropic" if provider == "anthropic" else "provider-default")


def model_icon(family):
    return icon_file(f"model-{family}" if family in cu.MODEL_FAMILIES else "model-other")


def sparkline(values):
    """Text bar chart. Scaled to the window's busiest slice; all-zero shows flat."""
    top = max(values) if values else 0
    if top <= 0:
        return SPARK_CHARS[0] * len(values)
    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int(v / top * (len(SPARK_CHARS) - 1) + 0.5))]
                   for v in values)


fmt_tokens = cu.fmt_count


def fmt_percent(pct):
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def title_part(row):
    name = row["provider"]
    if not row["active"]:
        return f"{name} idle"
    # "~" marks a log estimate, "?" a provider figure that has gone stale.
    mark = {"estimate": ("~", ""), "stale": ("", "?")}.get(row.get("freshness"), ("", ""))
    if row["percent"] is not None:
        return f"{name} {mark[0]}{fmt_percent(row['percent'])}{mark[1]}"
    return f"{name} {mark[0]}{fmt_tokens(row['total'])}{mark[1]}"


def format_title(rows):
    if not rows:
        return "No usage"
    return "  ·  ".join(title_part(r) for r in rows)


def source_label(row):
    return "from Claude Code" if row["source"] == "provider" else "estimate"


def provider_header(row):
    name = row["provider"]
    if not row["active"]:
        return f"{name}: no active window"
    if row["percent"] is not None:
        return f"{name}: {row['percent']:.0f}% used ({source_label(row)})"
    return f"{name}: {fmt_tokens(row['total'])} weighted (no ceiling)"


def freshness_text(row, now):
    captured = row["captured_at"]
    if captured is None:
        return None
    age = (now - captured).total_seconds() / 60
    stamp = cu.local(captured)
    if age > cu.LIMITS_STALE_MINUTES:
        return f"Last update from Claude Code: {stamp} (stale; send a message to refresh)"
    return f"Live from Claude Code · updated {stamp}"


def model_title(r):
    title = f"{r['model']}  ·  {fmt_tokens(r['tokens'])} all time"
    if r["limit_pct"] is not None and r["window_weighted"]:
        title += f"  ·  {fmt_percent(r['limit_pct'])} of limit"
    elif r["window_weighted"]:
        title += f"  ·  {fmt_tokens(r['window_weighted'])} this window"
    return title


def provider_items(row, on_calibrate, models, now):
    header = rumps.MenuItem(provider_header(row), icon=provider_icon(row["provider"]), template=True)
    items = [header]
    if row["weekly_percent"] is not None:
        items.append(rumps.MenuItem(
            f"Weekly: {row['weekly_percent']:.0f}%  (resets {cu.local(row['weekly_resets_at'])})"))
        line = cu.forecast_text(row["weekly_forecast_percent"], row["weekly_hits_at"], "weekly reset")
        if line:
            items.append(rumps.MenuItem(f"Weekly, {line[0].lower()}{line[1:]}"))
    if not row["active"]:
        return items
    items.append(rumps.MenuItem(
        f"Resets {cu.local(row['window_end'])}  ({row['minutes_left']} min left)"))
    line = cu.forecast_text(row["forecast_percent"], row["forecast_hits_at"])
    if line:
        items.append(rumps.MenuItem(line))
    if row["source"] == "provider":
        items.append(rumps.MenuItem(freshness_text(row, now) or "Live from Claude Code"))
        if row["estimate_total"] is not None:
            est = f"Log estimate: {fmt_tokens(row['estimate_total'])} weighted"
            if row["estimate_percent"] is not None:
                est += f" ({fmt_percent(row['estimate_percent'])})"
            items.append(rumps.MenuItem(est))
    items.append(rumps.MenuItem(f"Activity  {sparkline(row['timeline'])}"))
    if row["source"] != "provider":
        if row["ceiling"] is None:
            items.append(rumps.MenuItem("No ceiling calibrated yet"))
        items.append(rumps.MenuItem(
            "Calibrate…", callback=lambda _, p=row["provider"]: on_calibrate(p)))
    breakdown = rumps.MenuItem("Breakdown")
    for surface, model, v in row["surfaces"]:
        breakdown.add(rumps.MenuItem(f"{surface} · {model}: {v:,.0f}"))
    items.append(breakdown)
    if models:
        submenu = rumps.MenuItem("Models")
        for r in models:
            submenu.add(rumps.MenuItem(model_title(r), icon=model_icon(r["family"]), template=True))
        items.append(submenu)
    return items


def history_item(rows):
    """Submenu: one bar per day, then the busiest projects over the same days."""
    menu = rumps.MenuItem(f"Last {len(rows)} days")
    top = max((r["tokens"] for r in rows), default=0)
    for r in rows:
        day = datetime.strptime(r["day"], "%Y-%m-%d").strftime("%a %d")
        menu.add(rumps.MenuItem(f"{day}  {fmt_tokens(r['tokens']):>6}  {cu.bar(r['tokens'], top, 12)}"))
    projects = cu.history_totals(rows, "by_project")[:5]
    if projects:
        menu.add(rumps.separator)
        for name, tokens in projects:
            menu.add(rumps.MenuItem(f"{name}  ·  {fmt_tokens(tokens)}"))
    return menu


def weekly_alert_level(row):
    pct = row.get("weekly_percent")
    hit = [lvl for lvl in cu.ALERT_LEVELS if pct is not None and pct >= lvl]
    return max(hit) if hit else None


class UsageMenuBar(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="Claude Usage", icon=ICON_PATH, template=True,
                         quit_button=None)
        self._notified = {}  # (provider, window_start iso) -> highest threshold already alerted
        self.timer = rumps.Timer(self.refresh, REFRESH_SECONDS)
        self.timer.start()
        self.refresh()

    def refresh(self, _=None):
        try:
            data = cu.load_data()
            now = datetime.now(timezone.utc)
            cu.maintain(data, now)
            rows = cu.summarize(data, now)
            models = cu.model_usage(data, now)
            history = cu.history_rows(data, now, days=7)
        except Exception as exc:  # keep the menu bar alive on bad data
            self.title = "Usage ⚠"
            self._rebuild_menu([rumps.MenuItem(f"Error: {exc}")])
            return
        self.title = format_title(rows)
        self._rebuild_menu(self._build_items(rows, models, now, history))
        self._alert(rows)

    def _build_items(self, rows, models, now, history=()):
        items = []
        if not rows:
            items.append(rumps.MenuItem("No usage found. Run Claude Code or use the CLI add command."))
        for i, row in enumerate(rows):
            if i:
                items.append(rumps.separator)
            mine = [m for m in models if m["provider"] == row["provider"]]
            items.extend(provider_items(row, self.calibrate, mine, now))
        if any(r["tokens"] for r in history):
            items += [rumps.separator, history_item(history)]
        if installer.statusline_state() == "none":
            items += [rumps.separator,
                      rumps.MenuItem("Enable live limits from Claude Code…", callback=self.enable_live)]
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
                    f"{row['provider']} at {row['percent']:.0f}% ({source_label(row)})",
                    f"Crossed {level}%. Resets {cu.local(row['window_end'])}.",
                )
            weekly = weekly_alert_level(row)
            if weekly:
                key = (row["provider"] + " weekly", row["weekly_resets_at"].isoformat())
                active_keys.add(key)
                if weekly > self._notified.get(key, 0):
                    self._notified[key] = weekly
                    rumps.notification(
                        APP_NAME, f"{row['provider']} weekly limit at {row['weekly_percent']:.0f}%",
                        f"Crossed {weekly}%. Resets {cu.local(row['weekly_resets_at'])}.")
        self._notified = {k: v for k, v in self._notified.items() if k in active_keys}

    def enable_live(self, _=None):
        """Register the status line hook, so Claude Code reports the provider's own figures."""
        try:
            installer.install_statusline(installer.settings_path(), installer.statusline_command())
        except installer.InstallError as exc:
            rumps.alert(APP_NAME, str(exc))
            return
        rumps.alert(APP_NAME, "Done. Live limits appear after your next message in Claude Code.")
        self.refresh()

    def calibrate(self, provider):
        """Ask for the % the provider's UI shows, then record it for the active window."""
        win = rumps.Window(
            message=f"Enter the % {provider} shows for its current window in its usage page.",
            title=f"Calibrate {provider}",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(160, 24),
        )
        resp = win.run()
        if not resp.clicked:
            return
        try:
            pct = float(resp.text.strip().rstrip("%"))
        except ValueError:
            rumps.alert(APP_NAME, "Enter a number such as 40.")
            return
        data = cu.load_data()
        try:
            cu.calibrate(data, provider, pct, datetime.now(timezone.utc))
        except ValueError as exc:
            rumps.alert(APP_NAME, str(exc))
            return
        cu.save_data(data)
        self.refresh()

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
