# Architecture

Bloomkeeper is a native macOS app that shows a local web dashboard (see the [README](../README.md) for what it does). It has three parts: a small Swift shell, a Python backend (the "collector") running on a Python runtime bundled in the app, and a React web UI. Everything runs on the user's Mac. The phone view is the same web UI, reached through the user's own Tailscale network.

```
+---------------------------- Bloomkeeper.app ----------------------------+
| Swift shell: App.swift, Updates.swift, CachePermission.swift                |
|   window (WKWebView) --- HTTP + bloom_session cookie ---+                   |
|   starts and stops the collector, menus, Sparkle        |                   |
|                                                         v                   |
| Python collector: runtime-entry.py -> collector.py + native/*.py            |
|   127.0.0.1:8765  Mac dashboard and /api/*                                  |
|   127.0.0.1:8766  phone listener, behind Tailscale Serve                    |
|   history.sqlite3, optimizer, pollers, telemetry helper                     |
+------+------------------------+-------------------------+-------------------+
       | reads files, runs      | HTTPS                   | Tailscale Serve
       | the darkbloom CLI      v                         v (tailnet only)
       v                 api.darkbloom.dev         phone browser at
  ~/.darkbloom/          earnings, public data     <mac>.<tailnet>.ts.net:8443
```

## Swift shell (`native/`)

- **`App.swift`** is the app. It opens the window (a `WKWebView`), starts the collector with the bundled `python3`, reads the dashboard URL the collector prints on stdout, and loads it. It restarts the collector if it exits (at most 3 times in 5 minutes) and stops it with SIGTERM on Quit. It also owns the menus, the menu-bar item (closing the window keeps monitoring running) and the optional "Connect Darkbloom" window, which loads the official console and passes allowlisted reputation fields to the collector (`reputation-bridge.js`). The page reaches Swift through WebKit message handlers (`bloomAccount`, `bloomDiagnostics`, `bloomUpdates`), and Swift replies with DOM events. The web view loads only the local dashboard; allowlisted links open in the default browser and all other navigation is blocked.
- **`Updates.swift`** wraps Sparkle 2, enabled only in beta-channel builds. Sparkle handles consent, and every install needs the user's approval. Before Sparkle quits the app to install, `BloomUpdateAdmission` takes a short lease from the collector (`/api/update/native`, `update_guard.py`) so an update never interrupts a model switch or warm-up.
- **`CachePermission.swift`** manages the optional cache-recovery permission. It runs an embedded script through the macOS administrator prompt that adds one sudoers rule, `/private/etc/sudoers.d/bloom-dashboard-cache`, allowing only `/usr/sbin/purge` with no arguments and no password; removal deletes only that rule. `cache_recovery.py` then runs `sudo -n /usr/sbin/purge` when file cache blocks a model load. `enable-cache-recovery.sh` and `remove-cache-recovery.sh` are identical copies of the scripts for manual use; keep them in sync.
- Helpers compiled by `build-app.sh`: `telemetry.m` (read-only sensor helper; one JSON line of CPU, GPU, memory and temperature readings a second) and `qr.swift` (QR code for the phone address).

`--setup-preview` launches an isolated first-run preview (temporary data, random ports, no provider control). Some Swift functions sit between `// BEGIN … // END` markers so that Python tests can compile them with `swiftc` (`swift_harness.py`).

## Python backend (`native/*.py`)

Standard library only, plus the pinned Web Push packages in `push-requirements.txt`. Development uses the system `python3` (3.9) and the app ships CPython 3.12, so code must run on both.

- **`collector.py`** is the entry point. It starts the background loops (a one-second sample of the provider's `daemon-state.json`, the telemetry helper and Darkbloom Monitor's activity file; earnings every 20 s; network, demand and notification loops) and two `ThreadingHTTPServer`s on loopback, 8765 for the Mac and 8766 for the phone. `Handler.do_GET`/`do_POST` route `/api/*` by path; other paths serve the built UI under a strict Content-Security-Policy. `/api/snapshot` is the main one-second payload.
- **`history.py`** owns `history.sqlite3` (WAL mode): one-second samples, confirmed credits, the public network series, and a key/value `cache` table for settings and small state. `optimizer_store.py` and the journals add their own tables to the same file; `retention.py` trims per-second samples and passive journals after 90 days.
- **`optimizer.py`** is the controller; together with `provider_control.py` it is the only code that runs `darkbloom` commands. It reads Darkbloom's LaunchAgent plist, runs `~/.darkbloom/bin/darkbloom start --model …` in a worker thread, verifies the new session and pre-warms it through Darkbloom's local engine (`prewarm.py`). `observe` is the default mode; "Optimizer on" sets `demand`, and `week`, `optimize` and `combo` are scheduled tests. Every switch rechecks fresh readings, this Mac's identity in the provider roster, fresh earnings and demand data, memory, idle, AC power and temperature. If Bloomkeeper quits mid-switch, the next launch pauses automation.
- **`demand_optimizer.py`** decides what demand mode should do but never runs a provider command. It estimates each model's pay from this Mac's own paid, warm minutes under similar network demand, and proposes confident switches and short trial runs of other models within the user's policy (minimum run, confirmation, improvement margin, daily switch limit, protect level, learning time). See [OPTIMIZER_REDESIGN.md](OPTIMIZER_REDESIGN.md).
- **`stall_recovery.py`** (pure logic) and **`stall_control.py`** (runs it, demand mode only) react when steady work suddenly stops: a small test request, then a restart on the same model, then a move to another model, then stop and tell the user. Each step is saved as an event, so it isn't repeated after a restart. The test request goes through Darkbloom's API, routed back to this Mac, only if the user stored an API key in the login Keychain; otherwise it goes to the local engine.
- **`live_earnings.py`** turns the account-earnings API into confirmed credits and computes the live "Pulse" pace for the current session. A pace estimate is never shown as paid money.

Other notable modules:

- Earnings and reporting: `forecast.py`, `daily_earnings.py`, `earnings_target.py`, `provider_sessions.py`, `workload.py`, `energy.py`, `reputation.py`.
- Network and demand: `network.py` (polls Darkbloom's public endpoints), `model_demand.py`, `network_weekly.py`, `traffic_pulse.py`, `demand_alerts.py`, `demand_curves.py` (shadow estimator, seeded from `shared-priors.json`).
- Model control: `optimizer_store.py` (evidence tables), `optimizer_control.py` (On/Off), `manual_selection.py` (manual start and switch), `model_readiness.py`, `update_guard.py`.
- Phone and fleet: `remote.py` (phone access), `machines.py` (My Macs: read-only summaries from up to ten of the user's Macs, fetched over the tailnet and told apart by a random per-install id), `web_push.py` (phone notifications).
- Support and optional sharing: `diagnostics.py` (full report, saved to a file only), `support_reports.py` (problem reports), `usage_reporting.py` and `usage_integration.py` (usage sharing), `user_contact.py`, `pay_sharing.py`.
- Plumbing: `runtime-entry.py` (entry point in the bundle), `setup.py` (first launch), `bloom_log.py`, `feature_discovery.py`, `release_notes.py`, `community_insights.py` (shows an optional local digest file).

### How the UI authenticates to the collector

The UI calls same-origin `/api/*`. The Mac listener accepts a request only with an exact `127.0.0.1:<port>` or `localhost:<port>` Host, a same-origin `Origin` if one is sent, no cross-site fetch metadata, and no `Tailscale-*` or `X-Forwarded-*` headers. Every write from the page is a small JSON `POST` with a route-specific `X-Bloom-Action` header, which a page on another site can't add without a CORS preflight, and the server rejects preflights.

At launch, `App.swift` creates two random per-launch secrets and passes them to the collector as environment variables:

- `BLOOM_SESSION_TOKEN` becomes an HttpOnly, SameSite=Strict `bloom_session` cookie in the window's cookie store, where page JavaScript can't read it. The collector requires it on every `POST` to port 8765 (except the two native routes below) and on `GET /api/contact`, so other local programs and macOS accounts can read the dashboard but can't change anything.
- `BLOOM_NATIVE_TOKEN` is sent only by Swift code, in an `X-Bloom-Native` header, for `/api/reputation/native` and `/api/update/native`.

With no `BLOOM_SESSION_TOKEN` (running `collector.py` from source, unit tests), the cookie isn't checked.

### Phone access

Phone access is off until the user turns it on from the Mac, and it needs Tailscale on the Mac and the phone. `remote.py` points Tailscale Serve (HTTPS port 8443) at `127.0.0.1:8766/<secret>`, so the phone opens `https://<mac>.<tailnet>.ts.net:8443`, reachable only inside the user's tailnet. Bloomkeeper never enables Funnel and won't take port 8443 from another service. Serve adds a `Tailscale-User-Login` header, and the phone listener answers only when it matches the Tailscale account that owns this Mac's node (rechecked every 5 seconds). Because any local program could connect to 8766 and set that header itself, the listener also requires the random path prefix that Serve adds to every request (kept in `remote-access.json`, 0600) and returns 404 without it; Bloomkeeper re-points an older Serve entry to the prefix on its own. The phone gets the same UI, including model and optimizer controls, notifications and problem reports. Setup, phone access, My Macs, and sharing and contact choices are Mac-only; the phone can only turn automatic problem reports off.

## Web UI (repo root)

Vite, React 19 and TypeScript. The app uses only `vite.local.config.ts`: `pnpm run build:local` writes `dist/local/`, which `build-app.sh` copies into the bundle, and `pnpm run dev:local` serves the UI on `127.0.0.1:5173` with `/api` proxied to port 8765.

- `index.html` → `local-entry.tsx` → **`app/page.tsx`**: navigation, screens, and the `/api/snapshot` poll (every second on the Mac tab, only while the page is visible).
- **`components/dashboard/*`**: one file per panel or screen (optimizer, earnings, network, phone access, My Macs, support, sharing settings). `components/ui/*` holds shadcn/Base UI primitives.
- **`lib/*`**: validators that check API responses before rendering (for example `optimizer-response.ts`, `network-response.ts`) and pure logic for polling, formatting and charts. `lib/*.test.mjs` and `native/test_*.mjs` test them, importing the `.ts` files directly under `node --test`.
- **`app/globals.css`**: the single stylesheet (Tailwind v4 imports plus hand-written classes).
- `public/`: icons, the web app manifest, and `push-sw.js`, the service worker for phone notifications.

## Build, test and release

```sh
./native/setup-dev.sh        # once: Web Push packages into native/push_vendor (gitignored)
pnpm install
pnpm run check               # TypeScript
/usr/bin/python3 -m unittest discover -s native -p "test_*.py"
node --test native/test_*.mjs lib/*.test.mjs   # `pnpm test` runs both suites
```

`CachePermissionTests.swift` runs inside the Python suite through `test_cache_permission_native.py`, like the other Swift harnesses.

To run from source, quit the installed app, then run `/usr/bin/python3 native/collector.py --dev` and `pnpm run dev:local`. The collector's default `--data` is the real database in `~/Library/Application Support/Bloom Dashboard/`; pass another path to keep test data out of it. See [CONTRIBUTING.md](../CONTRIBUTING.md) before opening a pull request.

To build a local app bundle (ad-hoc signed, no updater):

```sh
python3 native/prepare-runtime.py   # pinned, checksummed CPython + packages -> .build/portable-runtime
python3 native/prepare-sparkle.py   # pinned, checksummed Sparkle -> .build/sparkle
pnpm run build:local
./native/build-app.sh               # -> .build/Bloomkeeper.app
```

`build-app.sh` copies exactly the files listed in `native/bundle-resources.txt` into `Contents/Resources`, so list every new bundled module there; `test_bundle_resources.py` fails if a bundled module imports a local module that isn't listed. `release-manifest.py` sets the version, writes `Info.plist` and a file manifest, and stops the build if the bundle contains private files or personal data.

Releases are built on the maintainer's Mac, which keeps the signing identity, notarization profile and Sparkle signing key in its Keychain. `package-beta.sh` builds the beta app (own bundle id and data folder, updates on) and its DMG. `release_check.py` is the release gate: typecheck, the JS tests and all Python tests under the bundled runtime, an offline optimizer replay (`replay_optimizer.py`, `native/replay/fixtures/`), bundle and updater checks, a first launch from a relocated copy, and Playwright browser checks. `distribution.py` notarizes and staples the DMG; `update-appcast.py` signs it for Sparkle and writes `beta.xml`. Publishing is a separate manual step.

## Where data lives, and what leaves the Mac

Bloomkeeper's folder is `~/Library/Application Support/Bloom Dashboard/` (`Bloomkeeper Beta/` for beta builds), created private (0700):

- `history.sqlite3` (plus `-wal`, `-shm`): history, optimizer evidence, settings and most consent choices.
- `logs/bloom.log`: rotating backend log, designed to hold fixed messages and error types, not earnings, tokens or account IDs.
- `remote-access.json` (phone access), `web-push.json` (notification keys, phone subscriptions), and `usage/`, which appears only once usage sharing is turned on.

Bloomkeeper reads Darkbloom's files (`~/.darkbloom/auth_token`, `daemon-state.json`, `local.json`, the provider's LaunchAgent plist, Darkbloom Monitor's activity history) but never writes them; provider changes go through the `darkbloom` CLI. Outside its folder, Bloomkeeper keeps ordinary app preferences (window position, Sparkle settings), the Connect Darkbloom window's WebKit data (the console sign-in), and two optional extras: the sudoers rule above and a Keychain item (`bloom-darkbloom-api-key`) the user adds for the stall test request. The dashboard window uses a non-persistent WebKit store, so its cookie and page storage are gone after Quit.

By default, network traffic goes only to:

- `api.darkbloom.dev`: the account's earnings, using the provider's existing login token, and public network, capacity, pricing, model catalog and provider roster data.
- `bloomformac.com`, in beta builds only: Sparkle's update check (and any update the user approves), if the user allows automatic checks or chooses Check for Updates. No system profile is sent.

Everything else starts with a user action. These four go to fixed `bloomformac.com/api/*` endpoints, with no redirects or proxies:

- **Usage sharing** (off by default): daily yes/no flags (setup completed, dashboard opened, phone used, optimizer used), a coarse setup-error category, app and macOS versions, chip family and memory range, updated about every six hours.
- **Problem reports**: versions, chip family, memory range and status or failure codes, plus any note or contact the user types. Sent when the user taps Send, or automatically once they tick "Send these automatically". Never earnings, account IDs, model names or logs.
- **Contact details**: only what the user types, after ticking consent.
- **Pay summaries** (off by default): a weekly per-model pay-curve summary that improves the starting estimates in `shared-priors.json`.

Usage sharing, contact details and pay summaries each use their own random ID and secret, so turning one off deletes the website's copy. Also opt-in: phone notifications (encrypted Web Push through Apple, Google or Mozilla), the stall test request, the Connect Darkbloom window, and phone access and My Macs, which stay inside the user's tailnet. The full diagnostics report is never uploaded; the user saves or shares the file.
