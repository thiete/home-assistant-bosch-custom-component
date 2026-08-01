# scripts/

Standalone maintenance utilities, separate from the integration itself.
Nothing in this folder gets loaded by Home Assistant — `custom_components/bosch/`
is the actual integration; this is tooling for operating it.

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
- A way to complete the OAuth login and capture the redirect — same
  Windows VM + oauth-helper setup used for initial config, since the
  `com.bosch.tt.dashtt.pointt://` redirect still needs external capture
  (see `FIXES.md`'s EasyControl/CT200 section for why).
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
3. Open that URL in a browser on the machine running the oauth-helper
   (the Windows VM), and log in with your SingleKey ID credentials.
4. Copy the redirect URL the oauth-helper captures.
5. Paste it back into the script when prompted.
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
