#!/usr/bin/env python3
"""Claude Code status line hook. Saves the provider's rate limits for the usage tracker.

Claude Code runs this after each assistant message and passes session JSON on stdin.
For claude.ai Pro/Max subscribers that JSON includes rate_limits.five_hour and
rate_limits.seven_day (used_percentage, resets_at). This script saves them to
claude_limits.json next to the usage data, then prints a short status line.

Register it in ~/.claude/settings.json (use the absolute paths of your checkout):

    "statusLine": {
      "type": "command",
      "command": "/abs/path/.venv/bin/python /abs/path/statusline_capture.py"
    }

Stdlib only. It never raises or exits non-zero, so a failure cannot break the status bar.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import claude_usage as cu


def extract(payload):
    """The rate_limits block to save, or None if the payload has none (non-subscriber, or
    before the first API response)."""
    rl = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(rl, dict):
        return None
    windows = {k: rl[k] for k in ("five_hour", "seven_day") if isinstance(rl.get(k), dict)}
    return windows or None


def save(windows, path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(windows, captured_at=now.isoformat())
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".limits-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(body, fh)
    os.replace(tmp, path)  # atomic: the menu bar never reads a half-written file


def status_line(windows):
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = windows.get(key) if windows else None
        if w and w.get("used_percentage") is not None:
            parts.append(f"{label} {float(w['used_percentage']):.0f}%")
    return " · ".join(parts)


def capture(text, path=None, now=None):
    """Save what the payload carries and return the status line text."""
    now = now or datetime.now(timezone.utc)
    path = path or cu.LIMITS_FILE
    try:
        windows = extract(json.loads(text))
    except ValueError:
        return ""
    if windows:
        save(windows, path, now)
    return status_line(windows)


def main():
    try:
        out = capture(sys.stdin.read())
    except Exception:  # never let a bad write break the status bar
        out = ""
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
