"""Setup for the usage tracker: status line hook, menu bar login item, and health checks.

    python3 claude_usage.py install      # register both
    python3 claude_usage.py doctor       # check every part
    python3 claude_usage.py uninstall    # remove both

install edits Claude Code's settings.json. It keeps a timestamped backup next to the file
and refuses to replace someone else's status line unless --force is given.
"""
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import claude_usage as cu

PROJECT = Path(__file__).resolve().parent
CAPTURE_SCRIPT = PROJECT / "statusline_capture.py"
VENV_PYTHON = PROJECT / ".venv" / "bin" / "python"
PLIST_TEMPLATE = PROJECT / "launchagent" / "local.claude-usage-menubar.plist.template"
LABEL = "local.claude-usage-menubar"
PASSED_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_USAGE_DATA")


class InstallError(Exception):
    pass


def settings_path():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def passed_env():
    return {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}


def statusline_command(python=None):
    """The command Claude Code runs. Carries over path overrides set in this shell."""
    parts = [f"{k}={shlex.quote(v)}" for k, v in passed_env().items()]
    if parts:
        parts.insert(0, "env")
    parts += [shlex.quote(str(python or sys.executable)), shlex.quote(str(CAPTURE_SCRIPT))]
    return " ".join(parts)


def is_ours(status_line):
    return isinstance(status_line, dict) and CAPTURE_SCRIPT.name in str(status_line.get("command", ""))


def read_settings(path):
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text())
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}). Fix it first; nothing was changed.")
    if not isinstance(settings, dict):
        raise InstallError(f"{path} does not hold a JSON object. Nothing was changed.")
    return settings


def write_settings(path, settings, now=None):
    """Write atomically. Returns the backup path, or None if there was no file to back up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return backup


def install_statusline(path, command, force=False, now=None):
    """Register the capture script. Returns ("unchanged" | "installed" | "replaced", backup)."""
    settings = read_settings(path)
    existing = settings.get("statusLine")
    wanted = {"type": "command", "command": command}
    if existing == wanted:
        return "unchanged", None
    if existing and not is_ours(existing) and not force:
        raise InstallError(
            f"{path} already has a status line:\n    {existing.get('command', existing)}\n"
            "Run install --force to replace it (a backup is kept), or call "
            f"{CAPTURE_SCRIPT} from your own status line script.")
    settings["statusLine"] = wanted
    backup = write_settings(path, settings, now)
    return ("replaced" if existing else "installed"), backup


def uninstall_statusline(path):
    """Remove the status line only if it is ours. Returns True if it was removed."""
    settings = read_settings(path)
    if not is_ours(settings.get("statusLine")):
        return False
    del settings["statusLine"]
    write_settings(path, settings)
    return True


def render_plist(env=None):
    text = PLIST_TEMPLATE.read_text().replace("__PROJECT_DIR__", str(PROJECT))
    plist = plistlib.loads(text.encode())
    if env:
        plist["EnvironmentVariables"] = dict(env)
    return plistlib.dumps(plist)


def has_rumps(python=VENV_PYTHON):
    if not Path(python).exists():
        return False
    return subprocess.run([str(python), "-c", "import rumps"], capture_output=True).returncode == 0


def ensure_venv():
    """Create .venv with rumps if it is missing. Needs network access for pip."""
    if has_rumps():
        return
    if not VENV_PYTHON.exists():
        subprocess.run([sys.executable, "-m", "venv", str(PROJECT / ".venv")], check=True)
    subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-q", "-r",
                    str(PROJECT / "requirements-menubar.txt")], check=True)


def agent_loaded():
    return subprocess.run(["launchctl", "list", LABEL], capture_output=True).returncode == 0


def install_launchagent():
    dest = plist_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
    dest.write_bytes(render_plist(passed_env()))
    done = subprocess.run(["launchctl", "load", str(dest)], capture_output=True, text=True)
    if done.returncode != 0 or not agent_loaded():
        raise InstallError(f"launchctl could not load {dest}: {done.stderr.strip() or 'unknown error'}")
    return dest


def uninstall_launchagent():
    dest = plist_path()
    if not dest.exists():
        return False
    subprocess.run(["launchctl", "unload", str(dest)], capture_output=True)
    dest.unlink()
    return True


# ---------- checks ----------

def statusline_state(path=None):
    """"ours", "other" or "none". "none" also covers an unreadable settings file."""
    try:
        existing = read_settings(path or settings_path()).get("statusLine")
    except InstallError:
        return "none"
    if not existing:
        return "none"
    return "ours" if is_ours(existing) else "other"


def checks(now=None):
    """[(level, text)] where level is "ok", "warn" or "fail"."""
    now = now or datetime.now(timezone.utc)
    out = []
    events = cu.anthropic_claude_code({})
    if events:
        latest = max(e["ts"] for e in events)
        out.append(("ok", f"Claude Code logs: {len(events):,} requests, latest {cu.local(latest)}"))
    else:
        out.append(("warn", "Claude Code logs: none found. Usage appears after you run Claude Code."))

    state = statusline_state()
    if state == "ours":
        out.append(("ok", f"Status line: registered in {settings_path()}"))
    elif state == "other":
        out.append(("fail", "Status line: another command is registered. Run install --force, "
                            "or call statusline_capture.py from it."))
    else:
        out.append(("fail", "Status line: not registered. Run: python3 claude_usage.py install"))

    limits = cu.load_limits()
    if limits is None:
        hint = ("Send a message in Claude Code to get the first one." if state == "ours"
                else "They arrive once the status line is registered.")
        out.append(("warn", f"Provider limits: no reading yet. {hint}"))
    else:
        captured = limits.get("captured_at")
        age = (now - cu.parse_ts(captured)).total_seconds() / 60 if captured else None
        five = cu._live_window(limits, "five_hour", now)
        week = cu._live_window(limits, "seven_day", now)
        figures = ", ".join(f"{label} {w['used_percent']:.0f}%" for label, w in
                            (("5-hour", five), ("weekly", week)) if w)
        when = f"{age:.0f} min ago" if age is not None else "at an unknown time"
        if not figures:
            out.append(("warn", f"Provider limits: last reading {when}; its windows have ended. "
                                "Send a message in Claude Code to refresh."))
        elif age is None or age > cu.LIMITS_STALE_MINUTES:
            out.append(("warn", f"Provider limits: {figures}, read {when} (stale)."))
        else:
            out.append(("ok", f"Provider limits: {figures}, read {when}"))

    if sys.platform == "darwin":
        if not has_rumps():
            out.append(("warn", "Menu bar: rumps is not installed in .venv. Run install to set it up."))
        elif agent_loaded():
            out.append(("ok", "Menu bar: login item loaded"))
        elif plist_path().exists():
            out.append(("fail", f"Menu bar: {plist_path()} exists but is not loaded. Run install again."))
        else:
            out.append(("warn", "Menu bar: no login item. Run install, or start menubar.py by hand."))
    out.append(("ok", f"Data file: {cu.DATA_FILE}"))
    return out


# ---------- commands ----------

MARKS = {"ok": "✓", "warn": "!", "fail": "✗"}


def run_doctor():
    results = checks()
    for level, text in results:
        print(f"{MARKS[level]} {text}")
    return 1 if any(level == "fail" for level, _ in results) else 0


def run_install(menubar=True, force=False):
    try:
        status, backup = install_statusline(settings_path(), statusline_command(), force=force)
        print(f"Status line: {status} in {settings_path()}")
        if backup:
            print(f"  Backup of the previous settings: {backup}")
        if menubar and sys.platform == "darwin":
            ensure_venv()
            print(f"Menu bar: login item loaded from {install_launchagent()}")
        elif menubar:
            print("Menu bar: skipped, macOS only.")
    except InstallError as exc:
        print(exc, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Setting up .venv failed: {exc}", file=sys.stderr)
        return 1
    print("\nNext: send one message in Claude Code, then run: python3 claude_usage.py doctor")
    return 0


def run_uninstall():
    try:
        removed = uninstall_statusline(settings_path())
    except InstallError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Status line: removed" if removed else "Status line: not ours or not set; left as is")
    if sys.platform == "darwin":
        print("Menu bar: login item removed" if uninstall_launchagent() else "Menu bar: no login item")
    print(f"Usage data is kept in {cu.DATA_FILE.parent}")
    return 0
