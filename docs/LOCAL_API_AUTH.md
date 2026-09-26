# Local API authentication (review item 21): plan

Status: **A is done** (personal 1.36.45, Sep 25). Verified live: an outside request gets 403 while reads stay open, and a real WKWebView check confirms the cookie arrives on page loads and POSTs but isn't visible to page JavaScript. `pnpm run dev:local` against the installed app is now read-only. **B is done on `main` (Sep 26) as the secret path prefix (B3)**; the Unix socket (B2) can't work, see B1 results below.

## The gap

- **Mac UI (port 8765).** Anything that can reach 127.0.0.1 can call every route: another macOS user account on the same Mac, or any local process. Today's checks (`Handler.permitted` in `native/collector.py`: exact `Host`, same-origin `Origin`, `X-Bloom-Action` headers) stop web pages in a browser from forging requests. They don't stop a program that sets those headers itself. The POST routes change state, including `/api/optimizer/control`, `/api/model-control`, `/api/setup`, `/api/support/send`, `/api/contact` and `/api/machines`.
- **Phone listener (port 8766).** It trusts a `Tailscale-User-Login` header, plus `Host` and `X-Forwarded-Proto`, because Tailscale Serve sets those headers. A local program that connects to 127.0.0.1:8766 directly can send the same headers and get owner access.

Native-only routes (`/api/reputation/native`, `/api/update/native`) already require the per-launch `BLOOM_NATIVE_TOKEN` that `App.swift` passes to the backend.

## A. Mac UI: a per-launch session cookie (recommended; no web UI changes)

1. At launch, `App.swift` generates a second random token, `sessionToken`, and passes it as `BLOOM_SESSION_TOKEN`. The existing `nativeToken` stays for native routes.
2. Before loading the dashboard, the app sets it as a cookie in the web view's own store: `WKWebsiteDataStore.httpCookieStore.setCookie` with name `bloom_session`, domain `127.0.0.1`, path `/`, **HttpOnly** and **SameSite=Strict**. The page's JavaScript can't read it, and every same-origin `fetch` sends it automatically, so no changes are needed in `components/` or `lib/`.
3. The backend requires the cookie, compared in constant time, on every local POST (the support and diagnostics previews are POSTs too). It also requires it on `GET /api/contact`, which returns saved contact details. Without the cookie a request gets 403 with "Open Bloomkeeper to change settings", and read-only views keep working.
4. **Development and tests.** With no `BLOOM_SESSION_TOKEN` set (`pnpm run dev:local`, unit tests), nothing is enforced. That way the collector tests and the preview keep working. New tests cover enforcement when a token is set. The release browser tests add the cookie with Playwright's `context.addCookies`.

**Trade-off:** opening http://127.0.0.1:8765 in Safari or Chrome on the Mac becomes read-only. If that matters, a menu item "Open in Browser" could hand over a one-time link that sets the cookie. Not recommended unless Andrew uses a browser on the Mac.

**Effort:** about half a day, including tests. The risk is low because the web UI doesn't change.

## B. Phone: serve the phone listener over a Unix socket

The Tailscale on this Mac is the standalone version (`io.tailscale.ipn.macsys` 1.102.3, network system extension). Its `tailscale serve` accepts `unix:/path.sock` targets.

1. **Test first (needs Andrew's OK; it changes Tailscale Serve config, and is reversible).** Run a throwaway server on a socket under `~/Library/Application Support/Bloom Dashboard/phone/` (a 0700 directory). Serve it on a spare HTTPS port such as 8444, fetch it from the phone, then run `tailscale serve --https=8444 off`. This shows whether the system extension can reach a socket in the user's folder.
2. If it works, the phone `ThreadingHTTPServer` moves to that socket, as a `UnixStreamServer` subclass, instead of 127.0.0.1:8766. `native/remote.py` then points Serve at `unix:<path>`. The existing enable flow already detects and replaces Bloomkeeper's own Serve slot (`serve_slot`), so current users are migrated the next time Bloomkeeper checks the slot. Other accounts can't open the socket because of the 0700 directory. Only this user's processes and root (Tailscale) can.
3. If the extension can't reach the socket, **fall back** to keeping TCP and adding a per-launch secret path prefix to the Serve target (`http://127.0.0.1:8766/<random>/`). The listener then accepts only requests under that prefix. The secret lives only in the Serve config, which only root and this user can read, so another account can't learn it.

**Effort:** about a day, most of it testing on the real phone.

### B results (Sep 26)

- **B1: the socket is unreachable.** Serve on a spare port pointed at `unix:` sockets in `~/Library/Application Support/Bloom Dashboard/`, the per-user temp folder and `/private/tmp` all returned 502; Tailscale's log says `dial unix …: connect: operation not permitted`. The standalone Tailscale's network extension runs as root but sandboxed, and its entitlements only allow `/Library/Tailscale/`, root's group containers and `/private/var/db/mds/`, none of which Bloomkeeper can write without admin rights. The App Store Tailscale is sandboxed too. The test entries were removed.
- **B3 shipped instead.** `Remote` keeps a random 32-character secret in `remote-access.json` (0600, 0700 folder) and points Serve at `http://127.0.0.1:8766/<secret>`. Serve prepends that path to every request (checked: `/api/x?y=1` arrives as `/<secret>/api/x?y=1`, `..` forms stay after the prefix, redirects pass through unchanged), so the web UI needs no changes. The phone listener's `parse_request` returns 404 unless the path starts with `/<secret>/` (constant-time compare), then strips it before any other check. The secret is kept across restarts, replaced on disable, and never appears in API responses.
- **Existing setups move automatically.** `serve_slot` reports `stale` for Bloomkeeper's own entry pointing at the listener with no or an older secret; when phone access is on for the same host and account, the 5-second refresh re-points it (at most one attempt a minute if Tailscale refuses). Disable removes a stale entry too. Phone URLs don't change.
- **Who can read the secret:** this user, root, and admin accounts through Tailscale's LocalAPI (`/Library/Tailscale/sameuserproof-*` is `root:admin 0640`; admins can become root anyway). Standard accounts and network-only programs can't. With a Homebrew/open-source `tailscaled`, other local accounts may be able to read Serve config; a Unix socket would work there, but it isn't worth a second code path for that setup.
- **Verified (personal 1.36.54, Sep 26):** Serve was re-pointed automatically after install; through Tailscale 200, a forged direct request to 127.0.0.1:8766 404; Andrew's phone works through Tailscale.

## Not in scope

A local process running as **the same user** can already read `~/.darkbloom/auth_token` and Bloomkeeper's database, so neither change defends against that. Both close the path for other accounts on the Mac, and for programs that can only reach the network.

## Order

1. A, then install and check the Mac UI.
2. B1 test (with Andrew present to check the phone), then B2 or B3.
