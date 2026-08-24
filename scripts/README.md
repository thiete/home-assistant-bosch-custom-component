# scripts/

Standalone maintenance utilities, separate from the integration itself.
Nothing in this folder gets loaded by Home Assistant — `custom_components/bosch/`
is the actual integration; this is tooling for operating it.

## capture_oauth_redirect_playwright.py

Captures the Bosch POINTT OAuth redirect automatically, on macOS, Linux, or
Windows, with no Windows VM and no OS-level URL scheme registration.

### Why this exists

The OAuth flow redirects the browser to a mobile-app-only custom URI scheme
(`com.bosch.tt.dashtt.pointt://...`) that desktop browsers can't follow on
their own — see `FIXES.md`'s EasyControl/CT200 section for the full story.
The original workaround registers a fake protocol handler with the OS to
intercept that redirect, which works on Windows but hit a wall on macOS:
Gatekeeper rejects an unsigned `.app` bundle even after ad-hoc `codesign`.

This script sidesteps the OS entirely. [Playwright](https://playwright.dev/)
drives a real, scriptable Chromium browser, and the script listens on the
*browser's own event stream* for the redirect — via both a redirect
response's `Location` header and the browser's own attempted navigation to
the custom scheme. Neither depends on the OS ever successfully handling the
URL, so there's nothing to register, sign, or install beyond the browser
itself.

Adapted from
[JoniVR/home-assistant-bosch-custom-component](https://github.com/JoniVR/home-assistant-bosch-custom-component)'s
`scripts/pointt_oauth_playwright.py`, which was built for that fork's
separate hourly-energy POINTT feature — only the capture mechanism is
pulled in here, not that feature.

### Prerequisites

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # one-time, if you don't have uv
uv run --with playwright python -m playwright install chromium   # one-time
```

`uv run` (used by the wrapper script below) fetches `playwright` and
`playwright-stealth` automatically — no separate `pip install` needed, and
nothing gets installed into your regular Python environment.

### Usage

```bash
./scripts/run_capture_oauth_redirect.sh [--timeout SECONDS]
```

A real Chromium window opens on the Bosch SingleKey ID login page. Log in
with your Bosch account as normal. The script detects the redirect the
moment it happens and prints the captured URL — no need to find it in an
address bar or a separate helper's web page.

Paste that captured URL into whichever flow you're actually using to
finish authentication:
- **Home Assistant's own config flow** — "Add Integration" → EasyControl,
  or redo it for a device that's already configured (an existing entry's
  tokens get updated in place rather than erroring out — see Fix 9 in
  `FIXES.md`), or
- **[`refresh_bosch_oauth.py`](#refresh_bosch_oauthpy)** below, which
  exchanges the code and patches an existing config entry's stored tokens
  directly, without going through the HA UI at all.

If it can't capture the redirect (login page changed, bot detection, etc.),
it falls back to printing the same auth URL for you to open manually —
some browsers show the failed-navigation URL in the address bar even with
nothing registered to handle it, which is worth trying before reaching for
the Windows VM route.

## refresh_bosch_oauth.py

Redoes the POINTT OAuth2 login for the EasyControl/CT200 flow and writes the
resulting tokens directly into the *existing* config entry, instead of
removing and re-adding the integration through the UI.

### Why this exists

Removing the Bosch integration also deletes its entity registry rows —
friendly names, custom entity_ids, assigned areas, history graphs. Re-adding
it creates a brand-new config entry; you get the *default* computed names
back (since each entity's `unique_id` is derived from the device itself, not
the config entry), but anything you manually customized is gone.

Since the only thing that actually goes stale is three fields inside the
entry's stored data (`access_token`, `refresh_token`, `token_expires_at`),
this script redoes just the login and patches those fields in place. The
entry_id, and every entity registry row, is never touched.

See [`FIXES.md`](../FIXES.md) for the fuller story of why the OAuth token
can go stale in the first place (Fix 5 and Fix 7).

### Prerequisites

- Python 3 (stdlib only, no extra packages needed).
- A way to complete the OAuth login and capture the redirect. Two options:
  - **[`capture_oauth_redirect_playwright.py`](#capture_oauth_redirect_playwrightpy)
    above** (recommended, works on macOS/Linux/Windows) — run it on any
    machine with a browser, it prints the captured URL for you to paste
    below.
  - The original Windows VM + oauth-helper setup from initial config,
    if you'd rather not install `uv`/Playwright (see `FIXES.md`'s
    EasyControl/CT200 section for why the redirect needs external
    capture at all).
- Home Assistant stopped before running it, or as close to that as your
  setup allows (see [Timing caveat](#timing-caveat) below).

### Usage

```bash
python3 refresh_bosch_oauth.py [--storage-path PATH] [--yes]
```

- `--storage-path` — path to `core.config_entries`. Defaults to
  `/config/.storage/core.config_entries`; override this if your HA
  `/config` volume is mounted somewhere else (e.g. `/home/home/config/...`).
- `--yes` — skip the "is Home Assistant stopped?" confirmation prompt.

Walkthrough:

1. Stop Home Assistant.
2. Run the script. It prints an authorization URL.
3. Capture the redirect: either run
   `./scripts/run_capture_oauth_redirect.sh` in a separate terminal (any
   machine with a browser) and let it drive the login automatically, or
   open the printed URL yourself on the Windows VM with the oauth-helper
   running, and log in with your SingleKey ID credentials.
4. Copy the captured redirect URL.
5. Paste it back into this script when prompted.
6. It exchanges the code for fresh tokens and writes them into
   `core.config_entries`, backing up the original first
   (`core.config_entries.bak-<timestamp>`, alongside the original).
7. Restart Home Assistant.

### Getting the script onto the server without corruption

If your only access to the target machine is a web-based console (Proxmox's
noVNC terminal, etc.), avoid pasting the file contents directly — large
pastes through those can silently truncate mid-file, which produces a
script that still runs (and even prints some output) but never reaches
its own entry point. `wget`/`curl` the raw file from GitHub instead:

```bash
wget -O refresh_bosch_oauth.py https://raw.githubusercontent.com/<your-fork>/<branch>/scripts/refresh_bosch_oauth.py
wc -l refresh_bosch_oauth.py   # sanity-check the line count matches the source
```

### Timing caveat

Home Assistant's config-entry storage write is debounced by ~1 second, but
flushed on a clean shutdown (`EVENT_HOMEASSISTANT_FINAL_WRITE`). So a normal
UI-triggered restart is safe. The risk is specifically an *unclean* shutdown
(power loss, `docker kill`, OOM) landing inside that 1-second window right
after a token write — in which case the pending write is lost and HA comes
back up with the previous (possibly stale) tokens. Not a concern for a
routine graceful restart.

### If something looks wrong afterward

The script never modifies `core.config_entries` without first copying it to
`core.config_entries.bak-<timestamp>` in the same directory. To roll back:

```bash
cp core.config_entries.bak-<timestamp> core.config_entries
```
