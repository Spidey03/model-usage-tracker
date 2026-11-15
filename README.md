# claude-usage-tracker

Tracks AI usage across providers and surfaces, grouped into each provider's rolling
limit windows. The CLI uses Python 3 standard library only. The optional macOS menu
bar app needs `rumps`.

## Quick start

    python3 claude_usage.py report                 # current window per provider
    python3 claude_usage.py add --model claude-sonnet-5-5 --input 2000 --output 900 --surface web
    python3 claude_usage.py calibrate --provider anthropic --percent 40   # % shown in /usage
    python3 claude_usage.py models                 # every model: all-time use, share of limit
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

Data is stored in `~/.claude-usage/data.json` (override with `CLAUDE_USAGE_DATA`).

## Menu bar (macOS)

A read-only menu bar view. The title shows every provider's current window, for example
`anthropic 42%  ·  openai 7%`. Providers without a calibrated ceiling show weighted tokens,
and providers with no active window show `idle`.

    python3 -m venv .venv
    .venv/bin/pip install -r requirements-menubar.txt
    .venv/bin/python menubar.py

The menu shows reset time, an Activity sparkline (usage across the current window in 10
slices), and a per-surface/model breakdown for each provider. Claude Code logs are cached by
file mtime, so refreshes only re-read logs that changed. A **Models** submenu lists each model seen in the logs, with all-time tokens and its share of
the current window's limit. Providers and model families have their own icons. Calibrate…
records the % the provider's usage page shows for the active window, the same as the CLI
`calibrate` command. It refreshes
every 60 seconds. It sends a notification when a window crosses 70% or 90%, once per threshold
per window. Percentages are estimates from calibration.

To start at login, copy the LaunchAgent template and fill in the project path:

    sed "s|__PROJECT_DIR__|$PWD|g" launchagent/local.claude-usage-menubar.plist.template \
        > ~/Library/LaunchAgents/local.claude-usage-menubar.plist
    launchctl load ~/Library/LaunchAgents/local.claude-usage-menubar.plist

LaunchAgents don't inherit shell variables. If you set `CLAUDE_CONFIG_DIR` or
`CLAUDE_USAGE_DATA`, add them to an `EnvironmentVariables` dict in the copied plist.
Logs go to `/tmp/claude-usage-menubar.log`. To stop, run `launchctl unload` on the same file.

## Tests

    python3 -m unittest discover -s tests            # CLI logic
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
- Web and Desktop usage appears only through manual entries.
- Weekly caps are not modelled yet.
