#!/usr/bin/env python3
"""Refresh the Bosch integration's OAuth tokens in place, without removing
and re-adding the integration -- preserves entry_id, all entity registry
rows, names, customizations, and history.

STOP HOME ASSISTANT BEFORE RUNNING THIS. If HA is running, it holds its
own in-memory copy of the config entry and will silently overwrite this
script's edit the next time anything triggers a config-entries save.

Usage:
    1. Stop Home Assistant.
    2. Run this script: python3 refresh_bosch_oauth.py
       (adjust STORAGE_PATH below first if it isn't at that path)
    3. It prints an authorization URL. Open it in a browser on the Windows
       VM with the oauth-helper running (same as initial setup).
    4. Log in. Copy the captured redirect URL from the oauth-helper page.
    5. Paste it back into this script when prompted.
    6. Restart Home Assistant.
"""

import sys

# Unbuffered output no matter how this gets invoked (docker exec, a
# non-tty pipe, etc.) -- print() should never get lost in a buffer.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

print(f"[startup] python {sys.version.split()[0]}, argv={sys.argv}", flush=True)

import json
import os
import re
import shutil
import traceback
import urllib.error
import urllib.parse
import urllib.request
import hashlib
import base64
from collections import namedtuple
from datetime import datetime, timedelta, timezone

STORAGE_PATH = "/home/home/config/.storage/core.config_entries"

DOMAIN = "bosch"
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
TOKEN_EXPIRES_AT = "token_expires_at"

CLIENT_ID = "762162C0-FA2D-4540-AE66-6489F189FADC"
REDIRECT_URI = "com.bosch.tt.dashtt.pointt://app/login"
CODE_VERIFIER = "abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklm"
TOKEN_URL = "https://singlekey-id.com/auth/connect/token"
SCOPES = [
    "openid", "email", "profile", "offline_access",
    "pointt.gateway.claiming", "pointt.gateway.removal",
    "pointt.gateway.list", "pointt.gateway.users",
    "pointt.gateway.resource.dashapp",
    "pointt.castt.flow.token-exchange", "bacon",
]


def build_auth_url():
    Components = namedtuple("Components", ["scheme", "netloc", "url", "path", "query", "fragment"])

    challenge = hashlib.sha256(CODE_VERIFIER.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode("utf-8").replace("=", "")

    query_params = {
        "redirect_uri": urllib.parse.quote_plus(REDIRECT_URI),
        "client_id": CLIENT_ID,
        "response_type": "code",
        "prompt": "login",
        "state": "_yUmSV3AjUTXfn6DSZQZ-g",
        "nonce": "5iiIvx5_9goDrYwxxUEorQ",
        "scope": urllib.parse.quote(" ".join(SCOPES)),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "style_id": "tt_bsch",
        "suppressed_prompt": "login",
    }
    query_params_encoded = urllib.parse.unquote(urllib.parse.urlencode(query_params))
    query = urllib.parse.quote(query_params_encoded)
    query_params_new = urllib.parse.quote_plus("/auth/connect/authorize/callback?")
    query_full = "ReturnUrl=" + query_params_new + query

    return urllib.parse.urlunparse(Components(
        scheme="https", netloc="singlekey-id.com",
        query=query_full, path="", url="/auth/en-us/login", fragment="",
    ))


def extract_code(redirect_url):
    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    if not code:
        # Custom scheme sometimes doesn't parse cleanly as a URL.
        match = re.search(r"[?&]code=([^&]+)", redirect_url)
        code = match.group(1) if match else None
    return code


def exchange_code_for_tokens(code):
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "scope": " ".join(SCOPES),
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": CODE_VERIFIER,
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        print(f"Token exchange failed: HTTP {err.code}\n{err.read().decode('utf-8', 'replace')}", flush=True)
        sys.exit(1)
    except urllib.error.URLError as err:
        print(f"Token exchange failed: network error: {err}", flush=True)
        sys.exit(1)

    if "access_token" not in body or "refresh_token" not in body:
        print(f"Missing tokens in response: {body}", flush=True)
        sys.exit(1)

    expires_in = body.get("expires_in", 3600)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    return body["access_token"], body["refresh_token"], expires_at


def patch_storage(access_token, refresh_token, token_expires_at):
    if not os.path.exists(STORAGE_PATH):
        print(f"STORAGE_PATH does not exist: {STORAGE_PATH}", flush=True)
        sys.exit(1)

    backup_path = f"{STORAGE_PATH}.bak-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(STORAGE_PATH, backup_path)
    print(f"Backed up current storage to {backup_path}", flush=True)

    with open(STORAGE_PATH, "r", encoding="utf-8") as f:
        store = json.load(f)

    entries = store.get("data", {}).get("entries", [])
    matches = [e for e in entries if e.get("domain") == DOMAIN]

    if not matches:
        print(f"No config entry with domain={DOMAIN!r} found in {STORAGE_PATH}. Nothing changed.", flush=True)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Found {len(matches)} entries with domain={DOMAIN!r}, refusing to guess which one. Nothing changed.", flush=True)
        sys.exit(1)

    entry = matches[0]
    print(f"Updating entry_id={entry.get('entry_id')} title={entry.get('title')!r}", flush=True)
    entry["data"][ACCESS_TOKEN] = access_token
    entry["data"][REFRESH_TOKEN] = refresh_token
    entry["data"][TOKEN_EXPIRES_AT] = token_expires_at

    tmp_path = f"{STORAGE_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp_path, STORAGE_PATH)
    print("Storage updated in place. Entity registry untouched.", flush=True)


def prompt(text):
    """input() but with a clear error instead of a silent/ambiguous failure
    if there's no interactive terminal attached (e.g. docker exec without
    -it)."""
    try:
        return input(text)
    except EOFError:
        print(
            "\n[error] No interactive input available (got EOF on stdin).\n"
            "If you're running this via `docker exec`, make sure to include\n"
            "both -i and -t, e.g.:\n"
            "    docker exec -it <container> python3 " + os.path.abspath(__file__) + "\n",
            flush=True,
        )
        sys.exit(1)


def main():
    print(f"[startup] STORAGE_PATH = {STORAGE_PATH}", flush=True)
    print(f"[startup] STORAGE_PATH exists: {os.path.exists(STORAGE_PATH)}", flush=True)
    print("=" * 60, flush=True)
    print("Bosch OAuth token refresh (in-place, no re-add needed)", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)
    print("Make sure Home Assistant is STOPPED before continuing.", flush=True)
    if prompt("Is Home Assistant stopped? [y/N] ").strip().lower() != "y":
        print("Aborting -- stop Home Assistant first.", flush=True)
        sys.exit(1)

    print(flush=True)
    print("Open this URL in a browser on the Windows VM (with the", flush=True)
    print("oauth-helper running to capture the redirect):", flush=True)
    print(flush=True)
    print(build_auth_url(), flush=True)
    print(flush=True)
    redirect_url = prompt("Paste the captured redirect URL here: ").strip()

    code = extract_code(redirect_url)
    if not code:
        print("Could not find an authorization code in that URL.", flush=True)
        sys.exit(1)

    print("Exchanging authorization code for tokens...", flush=True)
    access_token, refresh_token, token_expires_at = exchange_code_for_tokens(code)
    print(f"Got fresh tokens. New expiry: {token_expires_at}", flush=True)

    patch_storage(access_token, refresh_token, token_expires_at)
    print(flush=True)
    print("Done. Restart Home Assistant now.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        print("\n[fatal] Unhandled exception:", flush=True)
        traceback.print_exc()
        sys.exit(1)
