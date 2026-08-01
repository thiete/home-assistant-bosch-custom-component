#!/usr/bin/env python3
"""Refresh the Bosch integration's OAuth tokens in place.

Redoes the POINTT OAuth2 login and writes the resulting tokens directly
into the existing config entry's stored data, instead of removing and
re-adding the integration through the config flow UI. That matters
because removing the integration also deletes its entity registry rows
-- friendly names, custom entity_ids, assigned areas, history links --
and re-adding it only gets the *default* names back, not anything you
customized. This script touches only the token fields and leaves the
entry_id and every entity registry row completely untouched.

See scripts/README.md for the full walkthrough (why this exists, how to
run it, and what to do if it goes wrong).
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from datetime import datetime, timedelta, timezone

DOMAIN = "bosch"
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
TOKEN_EXPIRES_AT = "token_expires_at"

# Mirrors bosch_thermostat_client.connectors.oauth2.Oauth2Connector.
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
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        # The custom URI scheme doesn't always parse cleanly as a URL.
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
        sys.exit(f"Token exchange failed: HTTP {err.code}\n{err.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as err:
        sys.exit(f"Token exchange failed: network error: {err}")

    if "access_token" not in body or "refresh_token" not in body:
        sys.exit(f"Missing tokens in response: {body}")

    expires_in = body.get("expires_in", 3600)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    return body["access_token"], body["refresh_token"], expires_at


def patch_storage(storage_path, access_token, refresh_token, token_expires_at):
    backup_path = f"{storage_path}.bak-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(storage_path, backup_path)
    print(f"Backed up current storage to {backup_path}")

    with open(storage_path, "r", encoding="utf-8") as f:
        store = json.load(f)

    entries = store.get("data", {}).get("entries", [])
    matches = [e for e in entries if e.get("domain") == DOMAIN]

    if not matches:
        sys.exit(f"No config entry with domain={DOMAIN!r} found in {storage_path}. Nothing changed.")
    if len(matches) > 1:
        sys.exit(f"Found {len(matches)} entries with domain={DOMAIN!r}, refusing to guess which one. Nothing changed.")

    entry = matches[0]
    print(f"Updating entry_id={entry.get('entry_id')} title={entry.get('title')!r}")
    entry["data"][ACCESS_TOKEN] = access_token
    entry["data"][REFRESH_TOKEN] = refresh_token
    entry["data"][TOKEN_EXPIRES_AT] = token_expires_at

    # Write to a temp file and rename over the original, so a crash
    # mid-write can never leave core.config_entries half-written.
    tmp_path = f"{storage_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp_path, storage_path)
    print("Storage updated in place. Entity registry untouched.")


def prompt(text):
    """input(), but with a clear error instead of an ambiguous silent exit
    if there's no interactive terminal attached (e.g. `docker exec`
    without `-it`)."""
    try:
        return input(text)
    except EOFError:
        sys.exit(
            "\nNo interactive input available (got EOF on stdin).\n"
            "If you're running this via `docker exec`, include both -i and -t:\n"
            f"    docker exec -it <container> python3 {os.path.abspath(__file__)}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-path",
        default="/config/.storage/core.config_entries",
        help="Path to Home Assistant's core.config_entries file "
             "(default: %(default)s -- override if your /config volume is mounted elsewhere)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the 'is Home Assistant stopped?' confirmation prompt.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("Bosch OAuth token refresh (in-place, no re-add needed)")
    print("=" * 60)
    print(f"Storage file: {args.storage_path}")
    if not os.path.exists(args.storage_path):
        sys.exit(f"That path doesn't exist. Pass --storage-path if /config lives elsewhere.")
    print()

    if not args.yes:
        print("Home Assistant must be STOPPED before continuing -- it holds its own")
        print("in-memory copy of this file and will overwrite this edit otherwise.")
        if prompt("Is Home Assistant stopped? [y/N] ").strip().lower() != "y":
            sys.exit("Aborting -- stop Home Assistant first.")

    print()
    print("Open this URL in a browser on the Windows VM (with the")
    print("oauth-helper running to capture the redirect):")
    print()
    print(build_auth_url())
    print()
    redirect_url = prompt("Paste the captured redirect URL here: ").strip()

    code = extract_code(redirect_url)
    if not code:
        sys.exit("Could not find an authorization code in that URL.")

    print("Exchanging authorization code for tokens...")
    access_token, refresh_token, token_expires_at = exchange_code_for_tokens(code)
    print(f"Got fresh tokens. New expiry: {token_expires_at}")

    patch_storage(args.storage_path, access_token, refresh_token, token_expires_at)
    print()
    print("Done. Restart Home Assistant now.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        print("\nUnhandled exception:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
