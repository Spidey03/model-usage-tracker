#!/usr/bin/env python3
"""Multi-provider usage tracker.

Architecture
- Source adapters turn provider-specific data into one common event format.
  Adapters live in the SOURCES registry (see the "Adapters" section). Add a provider
  by writing one function that yields events and registering it.
- Manual entries cover any provider or surface without an adapter (web apps, etc).
- Limit profiles define each provider's window length, weights, and default ceiling
  behaviour. Each provider gets its own rolling windows.
- Ceilings are estimated by calibration against the % a provider's UI shows.
  Exact caps are only known where a provider publishes them.

Data lives in ~/.claude-usage/data.json (override with CLAUDE_USAGE_DATA).
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_FILE = Path(os.environ.get("CLAUDE_USAGE_DATA", Path.home() / ".claude-usage" / "data.json"))
ALERT_LEVELS = (70, 90)
TOKEN_FIELDS = ("input", "output", "cache_creation", "cache_read")

# ---------- limit profiles ----------
# window_hours: rolling window length. Weights scale tokens toward the limit.
# Defaults are neutral except for cache reads, which are usually cheaper.
# Calibrate model_weights against your own observed burn rate.
DEFAULT_PROFILES = {
    "anthropic": {
        "window_hours": 5,
        "token_weights": {"input": 1.0, "output": 1.0, "cache_creation": 1.0, "cache_read": 0.1},
        "model_weights": {"opus": 1.0, "sonnet": 1.0, "haiku": 1.0},
    },
    # Generic fallback for providers without a profile yet.
    "default": {
        "window_hours": 5,
        "token_weights": {"input": 1.0, "output": 1.0, "cache_creation": 0.0, "cache_read": 0.0},
        "model_weights": {},
    },
}


# ---------- storage ----------

def load_data():
    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    data.setdefault("entries", [])
    data.setdefault("calibrations", [])
    data.setdefault("profiles", {})
    data.setdefault("history", {})
    for name, prof in DEFAULT_PROFILES.items():
        data["profiles"].setdefault(name, prof)
    return data


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".data-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, DATA_FILE)  # atomic: a crash or a second writer never leaves half a file


def parse_ts(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def profile_for(data, provider):
    return data["profiles"].get(provider, data["profiles"]["default"])


# ---------- adapters ----------
# Each adapter returns a list of events:
#   {"ts": datetime, "provider": str, "surface": str, "model": str,
#    "input": int, "output": int, "cache_creation": int, "cache_read": int}

def project_name(cwd):
    """Short project label from a session's working directory. Worktrees map to their repo."""
    if not cwd:
        return None
    parts = Path(cwd).parts
    if "worktrees" in parts[:-1]:
        return parts[parts.index("worktrees") + 1]
    return parts[-1] if parts else None


_CLAUDE_FILE_CACHE = {}  # path -> ((mtime_ns, size), [(dedupe_key, event), ...])


def _parse_claude_file(path):
    """Parse one session log into (dedupe_key, event) pairs, in file order."""
    out = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            ts = rec.get("timestamp")
            model = msg.get("model") or "unknown"
            if not usage or not ts or model == "<synthetic>":
                continue
            key = msg.get("id") or rec.get("uuid")
            out.append((key, {
                "ts": parse_ts(ts), "provider": "anthropic", "surface": "claude-code",
                "model": model, "project": project_name(rec.get("cwd")),
                "input": usage.get("input_tokens", 0) or 0,
                "output": usage.get("output_tokens", 0) or 0,
                "cache_creation": usage.get("cache_creation_input_tokens", 0) or 0,
                "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
            }))
    return out


def _cached_claude_file(path):
    """Re-parse a log only when its mtime or size changed. Keeps the menu bar cheap on refresh."""
    st = path.stat()
    sig = (st.st_mtime_ns, st.st_size)
    hit = _CLAUDE_FILE_CACHE.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    records = _parse_claude_file(path)
    _CLAUDE_FILE_CACHE[path] = (sig, records)
    return records


def anthropic_claude_code(_data):
    """Claude Code session logs in ~/.claude/projects, deduplicated by message id."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    base = config_dir / "projects"
    if not base.exists():
        return []
    files = list(base.rglob("*.jsonl"))
    for stale in set(_CLAUDE_FILE_CACHE) - set(files):  # drop deleted logs
        del _CLAUDE_FILE_CACHE[stale]
    seen, events = set(), []
    for f in files:
        for key, ev in _cached_claude_file(f):
            if key:
                if key in seen:
                    continue
                seen.add(key)
            events.append(ev)
    return events


# Register adapters here. Key = adapter name. Add new providers as new functions.
SOURCES = {
    "anthropic-claude-code": anthropic_claude_code,
    # "openai-codex": openai_codex,   # TODO: verify Codex CLI log format first
}


def manual_events(data):
    return [{
        "ts": parse_ts(e["ts"]), "provider": e.get("provider", "anthropic"),
        "surface": e.get("surface", "web"), "model": e.get("model", "unknown"),
        "project": e.get("project"),
        "input": e.get("input", 0), "output": e.get("output", 0),
        "cache_creation": e.get("cache_creation", 0), "cache_read": e.get("cache_read", 0),
    } for e in data["entries"]]


def collect(data):
    events = manual_events(data)
    for name, fn in SOURCES.items():
        events += fn(data)
    return events


# ---------- math ----------

def model_weight(prof, model):
    name = model.lower()
    return next((w for key, w in prof["model_weights"].items() if key in name), 1.0)


def weighted(e, prof):
    tw = prof["token_weights"]
    raw = sum(e[k] * tw.get(k, 1.0) for k in TOKEN_FIELDS)
    return raw * model_weight(prof, e["model"])


MODEL_FAMILIES = ("opus", "sonnet", "haiku")


def model_family(model):
    name = model.lower()
    return next((f for f in MODEL_FAMILIES if f in name), "other")


def build_windows(events, window):
    """Each window starts at the first event after the previous one ended."""
    windows, cur = [], None
    for e in sorted(events, key=lambda e: e["ts"]):
        if cur is None or e["ts"] >= cur["end"]:
            cur = {"start": e["ts"], "end": e["ts"] + window, "events": []}
            windows.append(cur)
        cur["events"].append(e)
    return windows


def active_window(windows, now):
    return windows[-1] if windows and windows[-1]["end"] > now else None


def ceiling_estimate(data, provider, window_start):
    cals = [c for c in data["calibrations"] if c["provider"] == provider]
    same = [c["ceiling"] for c in cals if c["window_start"] == window_start.isoformat()]
    vals = same or [c["ceiling"] for c in cals]
    return statistics.median(vals) if vals else None


def providers_in(events):
    return sorted({e["provider"] for e in events})


# ---------- provider-reported limits ----------
# Claude Code's status line receives rate_limits from the provider for Pro/Max subscribers.
# statusline_capture.py saves them here. They replace the local estimate while live.
LIMITS_FILE = DATA_FILE.parent / "claude_limits.json"
LIMITS_STALE_MINUTES = 30


def load_limits():
    try:
        return json.loads(LIMITS_FILE.read_text())
    except (OSError, ValueError):
        return None


def _live_window(limits, key, now):
    w = (limits or {}).get(key)
    if not isinstance(w, dict) or w.get("used_percentage") is None or w.get("resets_at") is None:
        return None
    end = datetime.fromtimestamp(float(w["resets_at"]), timezone.utc)
    if end <= now:  # the provider's window has ended; Claude Code drops it too
        return None
    return {"end": end, "used_percent": float(w["used_percentage"])}


def current_window(data, prov, evs, now):
    """The window that counts right now for one provider, or None.

    Returns dict(start, end, events, source, used_percent, captured_at). source is
    "provider" when the provider's own 5-hour reset is live, else "estimate" from the logs.
    """
    prof = profile_for(data, prov)
    window = timedelta(hours=prof["window_hours"])
    limits = load_limits() if prov == "anthropic" else None
    live = _live_window(limits, "five_hour", now)
    if live:
        start = live["end"] - window
        captured = (limits or {}).get("captured_at")
        return {"start": start, "end": live["end"],
                "events": [e for e in evs if start <= e["ts"] < live["end"]],
                "source": "provider", "used_percent": live["used_percent"],
                "captured_at": parse_ts(captured) if captured else None}
    win = active_window(build_windows(evs, window), now)
    if not win:
        return None
    return dict(win, source="estimate", used_percent=None, captured_at=None)


def provider_weekly(now):
    """The provider's 7-day window, if live. Returns (used_percent, resets_at) or None."""
    live = _live_window(load_limits(), "seven_day", now)
    return (live["used_percent"], live["end"]) if live else None


RECENT_MINUTES = 60          # the pace of the last hour drives the 5-hour forecast
WEEKLY_MIN_FORECAST_HOURS = 12


def forecast(percent, start, end, now, recent_share=None, min_minutes=10):
    """Project a window's percent to its reset. Returns dict(projected, hits_at) or None.

    recent_share is the fraction of the window's logged usage that fell in the last
    RECENT_MINUTES. With it the forecast follows the current pace; without it, the
    window's average pace. hits_at is when 100% is reached, or None if the reset comes first.
    """
    if percent is None:
        return None
    elapsed = (now - start).total_seconds() / 60
    left = (end - now).total_seconds() / 60
    if elapsed < min_minutes or left <= 0:
        return None
    if recent_share is not None:
        rate = percent * recent_share / min(RECENT_MINUTES, elapsed)
    else:
        rate = percent / elapsed
    projected = percent + rate * left
    hits_at = None
    if percent >= 100:
        hits_at = now
    elif projected >= 100:
        hits_at = now + timedelta(minutes=(100 - percent) / rate)
    return {"projected": projected, "hits_at": hits_at}


def recent_share(win, prof, now):
    """Fraction of the window's logged usage from the last RECENT_MINUTES, or None if no usage."""
    total = sum(weighted(e, prof) for e in win["events"])
    if total <= 0:
        return None
    cutoff = now - timedelta(minutes=RECENT_MINUTES)
    return sum(weighted(e, prof) for e in win["events"] if e["ts"] >= cutoff) / total


def freshness(win, now):
    """"live" (provider figure, recent), "stale" (provider figure, old) or "estimate" (logs)."""
    if win["source"] != "provider":
        return "estimate"
    captured = win["captured_at"]
    if captured and (now - captured).total_seconds() / 60 <= LIMITS_STALE_MINUTES:
        return "live"
    return "stale"


AUTO_CAL_MIN_PERCENT = 5   # below this the provider's rounding dominates the ceiling
AUTO_CAL_KEEP = 30


def auto_calibrate(data, now, events=None):
    """Calibrate the log estimate from a live provider reading. Returns True if data changed.

    Keeps one automatic calibration per provider window (the latest reading wins), so the
    fallback estimate tracks the provider without manual calibration. The provider's figure
    includes usage the logs cannot see (claude.ai, Desktop), so these ceilings err low and
    the estimate errs high.
    """
    events = collect(data) if events is None else events
    changed = False
    for prov in providers_in(events):
        prof = profile_for(data, prov)
        win = current_window(data, prov, [e for e in events if e["provider"] == prov], now)
        if not win or freshness(win, now) != "live":
            continue
        pct = win["used_percent"]
        total = sum(weighted(e, prof) for e in win["events"])
        if not AUTO_CAL_MIN_PERCENT <= pct <= 100 or total <= 0:
            continue
        key = win["start"].isoformat()
        cals = data["calibrations"]
        old = next((c for c in cals if c.get("auto") and c["provider"] == prov
                    and c["window_start"] == key), None)
        if old and abs(old["percent"] - pct) < 1:
            continue
        if old:
            cals.remove(old)
        cals.append({"at": now.isoformat(), "provider": prov, "window_start": key,
                     "percent": pct, "weighted_total": total, "ceiling": total / (pct / 100),
                     "auto": True})
        autos = [c for c in cals if c.get("auto") and c["provider"] == prov]
        for extra in autos[:-AUTO_CAL_KEEP]:
            cals.remove(extra)
        changed = True
    return changed


def active_windows_by_provider(data, events, now):
    out = {}
    for prov in providers_in(events):
        prof = profile_for(data, prov)
        evs = [e for e in events if e["provider"] == prov]
        win = current_window(data, prov, evs, now)
        if win:
            out[prov] = (win, prof)
    return out


def summarize(data, now):
    """One row per provider with events. Shared by the CLI report and the menu bar.

    Each row: provider, active, window_start, window_end, minutes_left, total (weighted),
    surfaces [(surface, model, weighted)] sorted by size, ceiling, percent (None if no
    ceiling), alert (highest ALERT_LEVELS threshold reached, or None).
    """
    events = collect(data)
    rows = []
    for prov in providers_in(events):
        prof = profile_for(data, prov)
        evs = [e for e in events if e["provider"] == prov]
        win = current_window(data, prov, evs, now)
        row = {"provider": prov, "active": win is not None, "window_start": None,
               "window_end": None, "minutes_left": None, "total": 0.0, "surfaces": [],
               "ceiling": None, "percent": None, "alert": None, "source": None,
               "captured_at": None, "weekly_percent": None, "weekly_resets_at": None,
               "estimate_total": None, "estimate_percent": None, "freshness": None,
               "forecast_percent": None, "forecast_hits_at": None,
               "weekly_forecast_percent": None, "weekly_hits_at": None}
        if win:
            per = {}
            for e in win["events"]:
                k = (e["surface"], e["model"])
                per[k] = per.get(k, 0.0) + weighted(e, prof)
            total = sum(per.values())
            ceiling = ceiling_estimate(data, prov, win["start"])
            row.update(
                window_start=win["start"], window_end=win["end"],
                minutes_left=int((win["end"] - now).total_seconds() // 60), total=total,
                surfaces=sorted(((s, m, v) for (s, m), v in per.items()), key=lambda r: -r[2]),
                ceiling=ceiling, source=win["source"], captured_at=win["captured_at"],
                freshness=freshness(win, now),
            )
            if win["used_percent"] is not None:  # provider's own number
                pct = win["used_percent"]
            elif ceiling:
                pct = total / ceiling * 100
            else:
                pct = None
            if pct is not None:
                row["percent"] = pct
                hit = [lvl for lvl in ALERT_LEVELS if pct >= lvl]
                row["alert"] = max(hit) if hit else None
                fc = forecast(pct, win["start"], win["end"], now, recent_share(win, prof, now))
                if fc:
                    row["forecast_percent"], row["forecast_hits_at"] = fc["projected"], fc["hits_at"]
            row["timeline"] = window_timeline(win, prof)
        # Log-based estimate, always shown. It can differ from the provider's window.
        local = active_window(build_windows(evs, timedelta(hours=prof["window_hours"])), now)
        if local:
            est_total = sum(weighted(e, prof) for e in local["events"])
            est_ceiling = ceiling_estimate(data, prov, local["start"])
            row["estimate_total"] = est_total
            row["estimate_percent"] = est_total / est_ceiling * 100 if est_ceiling else None
        if prov == "anthropic":
            weekly = provider_weekly(now)
            if weekly:
                row["weekly_percent"], row["weekly_resets_at"] = weekly
                fc = forecast(weekly[0], weekly[1] - timedelta(days=7), weekly[1], now,
                              min_minutes=WEEKLY_MIN_FORECAST_HOURS * 60)
                if fc:
                    row["weekly_forecast_percent"], row["weekly_hits_at"] = fc["projected"], fc["hits_at"]
        rows.append(row)
    return rows


TIMELINE_BUCKETS = 10


def window_timeline(win, prof):
    """Weighted usage per equal slice of the window, oldest first. Used for the menu sparkline."""
    width = timedelta(hours=prof["window_hours"]) / TIMELINE_BUCKETS
    buckets = [0.0] * TIMELINE_BUCKETS
    for e in win["events"]:
        idx = min(int((e["ts"] - win["start"]) / width), TIMELINE_BUCKETS - 1)
        buckets[idx] += weighted(e, prof)
    return buckets


def model_usage(data, now):
    """One row per provider and model seen in the logs.

    All-time: requests and raw tokens. Current window: weighted use and its share of the
    provider's estimated limit (None without a calibrated ceiling). Limits are per provider;
    models are not limited separately, so the share shows how much of the shared window each uses.
    """
    events = collect(data)
    rows = []
    for prov in providers_in(events):
        prof = profile_for(data, prov)
        evs = [e for e in events if e["provider"] == prov]
        win = current_window(data, prov, evs, now)
        per = {}
        for e in evs:
            r = per.get(e["model"])
            if r is None:
                r = per[e["model"]] = {
                    "provider": prov, "model": e["model"], "family": model_family(e["model"]),
                    "weight": model_weight(prof, e["model"]), "requests": 0, "tokens": 0,
                    "last_seen": None, "window_weighted": 0.0, "window_share": None,
                    "limit_pct": None,
                }
            r["requests"] += 1
            r["tokens"] += sum(e[k] for k in TOKEN_FIELDS)
            if r["last_seen"] is None or e["ts"] > r["last_seen"]:
                r["last_seen"] = e["ts"]
        if win:
            for e in win["events"]:
                per[e["model"]]["window_weighted"] += weighted(e, prof)
            window_total = sum(r["window_weighted"] for r in per.values())
            ceiling = ceiling_estimate(data, prov, win["start"])
            for r in per.values():
                if window_total:
                    r["window_share"] = r["window_weighted"] / window_total * 100
                if ceiling:
                    r["limit_pct"] = r["window_weighted"] / ceiling * 100
        rows.extend(sorted(per.values(), key=lambda r: -r["tokens"]))
    return rows


# ---------- history ----------
# Daily totals per provider, model and project. Claude Code deletes old session logs, so
# completed days are copied into data["history"] and survive that.
LOCAL_TZ = None  # None = system timezone. Tests pin it.


def daily_usage(data, events):
    """{day: {"provider|model|project": {"requests", "tokens", "weighted"}}} from events."""
    days = {}
    for e in events:
        day = e["ts"].astimezone(LOCAL_TZ).date().isoformat()
        key = f"{e['provider']}|{e['model']}|{e.get('project') or ''}"
        cell = days.setdefault(day, {}).setdefault(key, {"requests": 0, "tokens": 0, "weighted": 0.0})
        cell["requests"] += 1
        cell["tokens"] += sum(e[k] for k in TOKEN_FIELDS)
        cell["weighted"] += weighted(e, profile_for(data, e["provider"]))
    return days


def _merge_days(stored, live):
    """Per cell, keep whichever side saw more tokens. Logs only shrink, by deletion."""
    out = {day: dict(cells) for day, cells in stored.items()}
    for day, cells in live.items():
        target = out.setdefault(day, {})
        for key, cell in cells.items():
            if key not in target or cell["tokens"] > target[key]["tokens"]:
                target[key] = cell
    return out


def update_history(data, now, events=None):
    """Store completed days in data["history"]. Returns True if data changed."""
    events = collect(data) if events is None else events
    today = now.astimezone(LOCAL_TZ).date().isoformat()
    done = {d: c for d, c in daily_usage(data, events).items() if d < today}
    merged = _merge_days(data.setdefault("history", {}), done)
    if merged == data["history"]:
        return False
    data["history"] = merged
    return True


def history_rows(data, now, days=14, events=None):
    """One row per day, oldest first, for the last `days` days including today.

    Each row: day, requests, tokens, weighted, by_model {model: tokens}, by_project {project: tokens}.
    """
    events = collect(data) if events is None else events
    merged = _merge_days(data.get("history", {}), daily_usage(data, events))
    today = now.astimezone(LOCAL_TZ).date()
    rows = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        row = {"day": day, "requests": 0, "tokens": 0, "weighted": 0.0, "by_model": {}, "by_project": {}}
        for key, cell in merged.get(day, {}).items():
            _prov, model, project = key.split("|", 2)
            project = project or "(none)"
            row["requests"] += cell["requests"]
            row["tokens"] += cell["tokens"]
            row["weighted"] += cell["weighted"]
            row["by_model"][model] = row["by_model"].get(model, 0) + cell["tokens"]
            row["by_project"][project] = row["by_project"].get(project, 0) + cell["tokens"]
        rows.append(row)
    return rows


def history_totals(rows, field):
    """Sum by_model or by_project across rows, largest first: [(name, tokens)]."""
    totals = {}
    for r in rows:
        for name, tokens in r[field].items():
            totals[name] = totals.get(name, 0) + tokens
    return sorted(totals.items(), key=lambda kv: -kv[1])


def maintain(data, now):
    """Housekeeping after reading: auto-calibrate and store finished days. Saves if needed."""
    events = collect(data)
    changed = auto_calibrate(data, now, events)
    changed = update_history(data, now, events) or changed
    if changed:
        save_data(data)
    return changed


# ---------- commands ----------

def local(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def forecast_text(pct, hits_at, reset_word="reset"):
    if pct is None:
        return None
    if hits_at:
        return f"At this pace: limit reached {local(hits_at)}, before the {reset_word}"
    return f"At this pace: about {min(pct, 100):.0f}% by the {reset_word}"


def cmd_report(args):
    data = load_data()
    now = datetime.now(timezone.utc)
    maintain(data, now)
    rows = summarize(data, now)
    if not rows:
        print("No usage found. Run Claude Code, or log usage with: add --model ...")
        return
    active = [r for r in rows if r["active"] and (not args.provider or r["provider"] == args.provider)]
    if not active:
        print("No active windows. The next message from any tracked provider starts one.")
        return
    for r in active:
        prov = r["provider"]
        print(f"== {prov} ==")
        print(f"Window started: {local(r['window_start'])}")
        print(f"Resets at:      {local(r['window_end'])}  ({r['minutes_left']} min left)")
        print(f"  {'surface':<14}{'model':<26}{'weighted':>14}")
        for surface, model, v in r["surfaces"]:
            print(f"  {surface:<14}{model:<26}{v:>14,.0f}")
        print(f"  {'TOTAL':<40}{r['total']:>14,.0f}")
        if r["source"] == "provider":
            stale = "  STALE: no update for over 30 min" if r["freshness"] == "stale" else ""
            print(f"Used: {r['percent']:.0f}%  (provider, from Claude Code status line, "
                  f"updated {local(r['captured_at']) if r['captured_at'] else 'unknown'}){stale}")
            if r["alert"]:
                print(f"ALERT: at or above {r['alert']}% of the provider's limit.")
            if r["estimate_total"] is not None:
                print(f"Log estimate: {r['estimate_total']:,.0f} weighted"
                      + (f"  ({r['estimate_percent']:.0f}%)" if r["estimate_percent"] is not None else ""))
        elif r["ceiling"]:
            print(f"Estimated used: {r['percent']:.0f}%  (ceiling ~{r['ceiling']:,.0f}, from calibration)")
            if r["alert"]:
                print(f"ALERT: at or above {r['alert']}% of estimated limit.")
        else:
            print("No ceiling calibrated. For live limits, run `install` once, then send a "
                  "message in Claude Code. Or read the % on the provider's usage page, then: "
                  f"calibrate --provider {prov} --percent N")
        line = forecast_text(r["forecast_percent"], r["forecast_hits_at"])
        if line:
            print(line)
        if r["weekly_percent"] is not None:
            print(f"Weekly: {r['weekly_percent']:.0f}%  (resets {local(r['weekly_resets_at'])})")
            line = forecast_text(r["weekly_forecast_percent"], r["weekly_hits_at"], "weekly reset")
            if line:
                print("  " + line)
        print()


def cmd_models(_args):
    data = load_data()
    rows = model_usage(data, datetime.now(timezone.utc))
    if not rows:
        print("No usage found. Run Claude Code, or log usage with: add --model ...")
        return
    print(f"{'provider':<11}{'model':<26}{'requests':>9}{'tokens':>14}{'window':>12}"
          f"{'of limit':>10}{'weight':>8}  last seen")
    for r in rows:
        window = f"{r['window_weighted']:,.0f}" if r["window_weighted"] else "-"
        limit = f"{r['limit_pct']:.1f}%" if r["limit_pct"] is not None else "-"
        print(f"{r['provider']:<11}{r['model']:<26}{r['requests']:>9,}{r['tokens']:>14,}"
              f"{window:>12}{limit:>10}{r['weight']:>8.2f}  {local(r['last_seen'])}")
    print()
    print("tokens: all time, all four fields. window: weighted use in the current window.")
    print("of limit: share of the provider's estimated limit (needs calibration). weight: model weight.")


def fmt_count(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:.0f}"


def bar(value, top, width=24):
    return "█" * round(value / top * width) if top > 0 else ""


def cmd_history(args):
    data = load_data()
    now = datetime.now(timezone.utc)
    maintain(data, now)
    rows = history_rows(data, now, days=args.days)
    if args.by == "day":
        top = max(r["tokens"] for r in rows)
        print(f"{'day':<12}{'requests':>9}{'tokens':>9}")
        for r in rows:
            print(f"{r['day']:<12}{r['requests']:>9,}{fmt_count(r['tokens']):>9}  {bar(r['tokens'], top)}")
    else:
        totals = history_totals(rows, "by_model" if args.by == "model" else "by_project")
        top = totals[0][1] if totals else 0
        print(f"{args.by:<34}{'tokens':>9}")
        for name, tokens in totals:
            print(f"{name[:32]:<34}{fmt_count(tokens):>9}  {bar(tokens, top)}")
    print(f"\nLast {args.days} days. Tokens are raw, all four fields.")


def cmd_install(args):
    import installer
    sys.exit(installer.run_install(menubar=not args.no_menubar, force=args.force))


def cmd_uninstall(_args):
    import installer
    sys.exit(installer.run_uninstall())


def cmd_doctor(_args):
    import installer
    sys.exit(installer.run_doctor())


def cmd_add(args):
    data = load_data()
    ts = parse_ts(args.at) if args.at else datetime.now(timezone.utc)
    data["entries"].append({
        "ts": ts.isoformat(), "provider": args.provider, "surface": args.surface,
        "model": args.model, "input": args.input, "output": args.output,
        "cache_creation": args.cache_creation, "cache_read": args.cache_read,
    })
    save_data(data)
    print(f"Added {args.provider}/{args.surface}/{args.model} at {ts.isoformat()}.")


def calibrate(data, provider, percent, now):
    """Record the % a provider's UI shows for its active window. Shared by CLI and menu bar.

    Mutates data (caller saves). Returns (weighted_total, ceiling). Raises ValueError.
    """
    events = collect(data)
    active = active_windows_by_provider(data, events, now)
    if provider not in active:
        raise ValueError(f"No active window for '{provider}'. Send a message there first.")
    if not 0 < percent <= 100:
        raise ValueError("Percent must be greater than 0 and at most 100.")
    win, prof = active[provider]
    total = sum(weighted(e, prof) for e in win["events"])
    ceiling = total / (percent / 100)
    data["calibrations"].append({
        "at": now.isoformat(), "provider": provider,
        "window_start": win["start"].isoformat(), "percent": percent,
        "weighted_total": total, "ceiling": ceiling,
    })
    return total, ceiling


def cmd_calibrate(args):
    data = load_data()
    try:
        total, ceiling = calibrate(data, args.provider, args.percent, datetime.now(timezone.utc))
    except ValueError as exc:
        sys.exit(str(exc))
    save_data(data)
    print(f"{args.provider}: {args.percent}% at {total:,.0f} weighted -> ceiling ~{ceiling:,.0f}.")


def cmd_providers(_args):
    data = load_data()
    print("Adapters:", ", ".join(SOURCES) or "(none)")
    print("Manual entry: any provider name works with add --provider")
    print("Profiles:", ", ".join(sorted(data["profiles"])))
    print(f"Data file: {DATA_FILE}")


def main():
    p = argparse.ArgumentParser(description="Track usage across AI providers and surfaces.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="Show current window for each provider")
    r.add_argument("--provider", help="Limit output to one provider")
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("add", help="Log usage by hand (any provider or surface)")
    a.add_argument("--provider", default="anthropic")
    a.add_argument("--surface", default="web", help="e.g. web, desktop, app, api, cli")
    a.add_argument("--model", required=True)
    a.add_argument("--input", type=int, default=0)
    a.add_argument("--output", type=int, default=0)
    a.add_argument("--cache-creation", type=int, default=0)
    a.add_argument("--cache-read", type=int, default=0)
    a.add_argument("--at", help="ISO timestamp; default now")
    a.set_defaults(fn=cmd_add)

    c = sub.add_parser("calibrate", help="Record the % a provider's UI shows right now")
    c.add_argument("--provider", default="anthropic")
    c.add_argument("--percent", type=float, required=True)
    c.set_defaults(fn=cmd_calibrate)

    sub.add_parser("models", help="List models with all-time usage and share of limit").set_defaults(fn=cmd_models)
    h = sub.add_parser("history", help="Usage per day, model or project")
    h.add_argument("--days", type=int, default=14)
    h.add_argument("--by", choices=("day", "model", "project"), default="day")
    h.set_defaults(fn=cmd_history)

    i = sub.add_parser("install", help="Register the status line hook and the menu bar login item")
    i.add_argument("--no-menubar", action="store_true", help="Only register the status line")
    i.add_argument("--force", action="store_true", help="Replace an existing status line command")
    i.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="Remove the status line hook and login item").set_defaults(fn=cmd_uninstall)
    sub.add_parser("doctor", help="Check that every part of the setup works").set_defaults(fn=cmd_doctor)

    sub.add_parser("providers", help="List adapters, profiles, and data file").set_defaults(fn=cmd_providers)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
