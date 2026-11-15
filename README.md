# claude-usage-tracker

Tracks AI usage across providers and surfaces, grouped into each provider's rolling
limit windows. Python 3, standard library only.

## Quick start

    python3 claude_usage.py report                 # current window per provider
    python3 claude_usage.py add --model claude-sonnet-5-5 --input 2000 --output 900 --surface web
    python3 claude_usage.py calibrate --provider anthropic --percent 40   # % shown in /usage
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
- Web and Desktop usage appears only through manual entries.
- Weekly caps are not modelled yet.
