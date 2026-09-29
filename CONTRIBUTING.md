# Contributing

Thanks for helping improve BloomGauge. Bug reports, reliability fixes and test cases from different Mac models are the most useful contributions right now.

## Reporting a problem

Open an issue that includes:

- your BloomGauge version, Mac chip, memory size and exact macOS version
- the Darkbloom provider version (`darkbloom status`)
- what you expected, what happened, and the steps to reproduce it

A diagnostics report (More → Help & feedback) is useful. Review it before attaching it. Never include credentials, auth tokens, account or device IDs, private Tailscale addresses or raw logs.

## Making changes

1. Run `./native/setup-dev.sh` once, then make sure the backend tests pass before you start:
   `/usr/bin/python3 -m unittest discover -s native -p 'test_*.py'` (the vendored packages are built for the system Python)
2. Keep changes focused, and add or update tests with them. The optimizer's safety checks (minimum runs, switch limits, memory, idle, power and temperature) have deterministic tests. Keep them passing, and add a replay fixture in `native/replay/fixtures/` for any new decision behavior.
3. Run `pnpm run check` if you changed the UI. `pnpm test` runs the JavaScript and backend tests; pull requests run both, plus the typecheck, in GitHub Actions.
4. Format before you commit: `pnpm run format` (TypeScript, JavaScript and JSON, settings in `.oxfmtrc.json`) and `ruff format` (Python, settings in `ruff.toml`). Swift is formatted by hand.
5. Never run a development collector and the installed app against the same provider at the same time.

## Ground rules

- Observe mode stays the default. No change may start automation, switch models or send data without an explicit user action.
- Keep confirmed money, estimates and forecasts clearly separate in the UI.
- Don't add network destinations, telemetry or credentials handling without discussing it in an issue first.
