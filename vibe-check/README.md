# vibe-check

Writes the dated editorial copy that gives each Daily Brief section its voice
(`vibe.json`). It is a small Node program that runs hourly, asks a low-cost
OpenRouter model for the section copy, prunes messages older than the retention
window, then commits and pushes `vibe.json` so GitHub Pages redeploys.

## What it reads

- Published documents at the repo root: `weather.json`, `calendar.json`,
  `events.json`, `news.json`, `geeknews.json`.
- The **programmatic** almanac/sky values the page computes, imported directly
  from `almanac-calc.mjs` (`computeAlmanacDay`) and `on-this-date.mjs`
  (`getOnThisDate`). Using the modules rather than a data file keeps the copy
  and the rendered UI on one source of truth.

The `(section, role)` set is defined once in `generate.mjs` (`SECTIONS`). The
`masthead/eyebrow` line is generated deterministically (date + location); the
rest come from the model.

## Usage

```sh
cp .env.example .env         # then paste OPENROUTER_API_KEY
./run.sh                     # generate + write + commit + push
VIBE_NO_PUSH=1 ./run.sh      # generate + write, no git
VIBE_DRY_RUN=1 ./run.sh      # call the model, print, write nothing
node generate.mjs --context-only   # show the exact model input
```

`run.sh` serializes itself with `/tmp/dailybrief-vibe.lock`, keeps the Mac
awake with `caffeinate`, waits (bounded) for `/tmp/dailybrief-collect.lock` so
it never reads a half-written document, and only ever stages `vibe.json`.
Output goes to `vibe.log`; each run's model input is kept in `cache/` for audit.

Env knobs: `VIBE_NODE`, `VIBE_NO_PUSH`, `VIBE_DRY_RUN`, `VIBE_WAIT_LIMIT`,
`VIBE_MIN_INTERVAL`, `VIBE_SKIP_COLLECT_WAIT`, `OPENROUTER_VIBE_MODEL`,
`VIBE_RETENTION_HOURS`.

`VIBE_MIN_INTERVAL` (default 50 minutes) skips a run when `vibe.json` was
refreshed more recently than that. This lets the collector chain and the
standalone launchd timer coexist without duplicate API calls or commit churn.

## Model

Defaults to `google/gemini-2.5-flash-lite` (~$0.0004/run, ~1.5s, JSON-schema
capable). Override with `OPENROUTER_VIBE_MODEL`. The response is validated for
exact section coverage, length, and plain-text before anything is written.

## Scheduled run

`launchd/com.dailybrief.vibe.plist` runs `run.sh` at :40 every hour, after the
hourly collection (`com.dailybrief.collect`). Install:

```sh
cp launchd/com.dailybrief.vibe.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dailybrief.vibe.plist
launchctl print gui/$(id -u)/com.dailybrief.vibe | head
```

Remove with `launchctl bootout gui/$(id -u)/com.dailybrief.vibe`.