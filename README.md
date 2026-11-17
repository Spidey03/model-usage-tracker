# claude-usage-tracker

Tracks AI usage across providers and surfaces, grouped into each provider's rolling
limit windows. The CLI uses Python 3 standard library only. The optional macOS menu
bar app needs `rumps`.

## Setup

    python3 claude_usage.py install      # status line hook + menu bar login item (macOS)
    python3 claude_usage.py doctor       # check every part; exits 1 if something is broken

`install` registers the status line hook in Claude Code's `settings.json`. When it changes
that file, it first keeps a timestamped backup next to it; if the hook is already registered,
it leaves the file alone. It will not replace another status line unless you pass
`--force`. It also creates `.venv` with `rumps` if needed and loads the login item. Use
`--no-menubar` for the hook only, and `uninstall` to remove both. Send one message in Claude
Code afterwards, then run `doctor` to confirm the first reading arrived.

## Quick start

    python3 claude_usage.py report                 # current window per provider
    python3 claude_usage.py add --model claude-sonnet-5-5 --input 2000 --output 900 --surface web
    python3 claude_usage.py calibrate --provider anthropic --percent 40   # % shown in /usage
    python3 claude_usage.py models                 # every model: all-time use, share of limit
    python3 claude_usage.py history                # tokens per day; --by model|project, --days N
    python3 claude_usage.py providers              # adapters, profiles, data file

## How it works

- **Adapters** read provider data and emit common events. Claude Code logs are read
  automatically from `~/.claude/projects` (override with `CLAUDE_CONFIG_DIR`).
- **Manual entries** cover anything without an adapter, such as claude.ai and Desktop
  chat, or other providers' web apps. Use any `--provider` name.
- **Limit profiles** set window length, token weights, and model weights per provider.
  Unknown providers use the `default` profile.
- **Windows** start at the first event after the previous window ends, matching the
  rolling 5-hour rule. Each provider has its own windows.
- **Ceilings** are estimated from calibration. The report shows an alert at 70% and 90%.
  While the provider's own figure is live, the tracker calibrates itself from it (one
  automatic entry per window), so the log estimate needs no manual calibration.
- **Forecasts** project the current pace to the reset. The 5-hour forecast follows the last
  hour of logged usage. The weekly forecast uses the week's average and starts after 12 hours.
- **History** keeps daily totals per model and project in the data file, so they survive
  Claude Code deleting old session logs.

Data is stored in `~/.claude-usage/data.json` (override with `CLAUDE_USAGE_DATA`).

## Menu bar (macOS)

The title shows every provider's current window, for example `anthropic 42%  ·  openai ~7%`.
A plain figure is the provider's own and current. `~` marks an estimate from the logs, and a
trailing `?` marks a provider figure with no update for 30 minutes. Providers without a
ceiling show weighted tokens, and providers with no active window show `idle`.

`install` sets this up. To run it by hand instead:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements-menubar.txt
    .venv/bin/python menubar.py

Each provider also shows a forecast ("At this pace: limit reached 15:40, before the reset"),
and a **Last 7 days** submenu shows tokens per day and the busiest projects. If the status
line hook is missing, the menu offers **Enable live limits from Claude Code…**.

The menu shows reset time, an Activity sparkline (usage across the current window in 10
slices), and a per-surface/model breakdown for each provider. Claude Code logs are cached by
file mtime, so refreshes only re-read logs that changed. A **Models** submenu lists each model seen in the logs, with all-time tokens and its share of
the current window's limit. Providers and model families have their own icons. Calibrate…
records the % the provider's usage page shows for the active window, the same as the CLI
`calibrate` command. It refreshes
every 60 seconds. It sends a notification when a window crosses 70% or 90%, once per threshold
per window, and the same for the weekly limit.

`install` loads the login item. To do it by hand, copy the LaunchAgent template and fill in
the project path:

    sed "s|__PROJECT_DIR__|$PWD|g" launchagent/local.claude-usage-menubar.plist.template \
        > ~/Library/LaunchAgents/local.claude-usage-menubar.plist
    launchctl load ~/Library/LaunchAgents/local.claude-usage-menubar.plist

LaunchAgents don't inherit shell variables. If you set `CLAUDE_CONFIG_DIR` or
`CLAUDE_USAGE_DATA`, add them to an `EnvironmentVariables` dict in the copied plist.
Logs go to `/tmp/claude-usage-menubar.log`. To stop, run `launchctl unload` on the same file.

## Provider-reported limits (Claude Code status line)

For claude.ai Pro and Max subscribers, Claude Code passes the provider's own 5-hour and
7-day usage (percent used and reset time) to the status line. The tracker uses those numbers
when they are live, so claude.ai and Desktop usage is counted too. Otherwise it falls back to
the log-based estimate.

`install` registers the capture script. The entry it writes to `~/.claude/settings.json`
looks like this:

    "statusLine": {
      "type": "command",
      "command": "/abs/path/to/model-usage-tracker/.venv/bin/python /abs/path/to/model-usage-tracker/statusline_capture.py"
    }

It saves `~/.claude-usage/claude_limits.json` and prints a short status line (for example
`5h 24% · 7d 41%`). Limits are reported only after the first response in a session, and they
appear only for Pro, Max and Ultra. Claude Code updates them after each assistant message. The
menu bar marks them as stale after 30 minutes without an update. This uses the documented
status line interface and makes no network calls.

## Tests

    python3 -m unittest discover -s tests            # CLI logic and limits
    .venv/bin/python -m unittest discover -s tests   # also menu bar title formatting

## Adding a provider adapter

1. Write a function that takes the data dict and returns a list of events:

        {"ts": datetime_utc, "provider": "openai", "surface": "cli", "model": "gpt-x",
         "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

2. Register it in `SOURCES` in `claude_usage.py`.
3. Add a profile under `DEFAULT_PROFILES` if the provider's window or weights differ
   from the default.

Verify the provider's log or usage format before writing the adapter.

## Limits

- Subscription caps are not published as token counts. Ceilings come from calibration
  and are estimates.
- Limits are per provider. Models are not limited separately, so the model list shows
  each model's share of the shared window rather than its own cap.
- Provider figures arrive only while Claude Code is in use. Usage on claude.ai or Desktop
  alone is not seen until the next Claude Code message, and the figure is marked stale
  meanwhile.
- Automatic ceilings err low, because the provider's percentage includes claude.ai and
  Desktop usage that the logs cannot see. The log estimate therefore errs high.
- Forecasts assume the current pace continues.
