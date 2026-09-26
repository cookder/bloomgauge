# Agent notes

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how the pieces fit) and [CONTRIBUTING.md](CONTRIBUTING.md) (setup, tests, formatting and ground rules).

**Maintainer checkout.** If `../../AGENTS.md` exists, this folder sits in the maintainer's workspace: read that file and `../../PROJECT_CONTEXT.md` first, then `docs/HANDOFF.md`, and `docs/IMPLEMENTATION_NOTES.md` for detailed history. These notes are private and not part of the public repository. Update the handoff and the root context after material work.

- Build into staging (`.build/`), then install. Never build into an installed app or a shortcut to one.
- New `native/*.py` modules must be listed in `native/bundle-resources.txt` (`test_bundle_resources.py` checks this).
- Never run Python inside a signed `.app`. It writes `__pycache__` and breaks the code signature.
- The installed app's data is in `~/Library/Application Support/Bloom Dashboard/`. Open its database read-only, and analyse a `.backup` copy rather than the live file. Never write to `~/.darkbloom`; provider changes go through the `darkbloom` CLI.
- Tests: `pnpm test` runs `node --test` and the Python suite. Save the full output to a file and judge the run by its final line.
