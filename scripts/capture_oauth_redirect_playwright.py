#!/usr/bin/env python3
"""Capture the Bosch POINTT OAuth redirect automatically via Playwright --
no Windows VM, no OS-level URL scheme registration.

The OAuth flow redirects to a mobile-app-only custom URI scheme
(com.bosch.tt.dashtt.pointt://...) that desktop browsers can't follow on
their own. The original workaround (see FIXES.md's EasyControl/CT200
section) registers a fake protocol handler with the OS to intercept it --
straightforward on Windows, but blocked by Gatekeeper on macOS (an
unsigned .app bundle gets rejected even after ad-hoc codesign).

This script sidesteps the OS entirely: Playwright drives a real, scriptable
Chromium browser and listens on the browser's own event stream for the
redirect, via two independent hooks so a change in exactly how the
redirect fires (a 3xx Location header vs. the browser's own navigation
attempt) doesn't break capture:
  - page.on("response"): a redirect response whose Location header
    targets the custom scheme
  - page.on("request"):  the browser itself trying to navigate there

Neither depends on the OS ever successfully "opening" the URL -- Playwright
sees the attempt regardless of whether anything is registered to handle it.
Works identically on macOS, Linux, and Windows.

Adapted from JoniVR/home-assistant-bosch-custom-component's
scripts/pointt_oauth_playwright.py (MIT, same upstream project family) --
same OAuth constants as bosch_thermostat_client.connectors.oauth2 and this
repo's own scripts/refresh_bosch_oauth.py.

First-time setup:
    curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
    uv run --with playwright python -m playwright install chromium

Run:
    ./scripts/run_capture_oauth_redirect.sh

    Prints the captured redirect URL. Paste it into whichever flow you're
    using to finish authentication:
      - Home Assistant's own config flow ("Add Integration" -> EasyControl,
        or redoing it for an existing device -- see config_flow.py's
        _abort_if_unique_id_configured(updates=...) in FIXES.md's Fix 9),
      - or scripts/refresh_bosch_oauth.py, which exchanges the code and
        patches an existing config entry's tokens in place.
"""

import argparse
import asyncio
import base64
import hashlib
import logging
import sys
import urllib.parse
from urllib.parse import unquote, urlencode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s  %(message)s")
log = logging.getLogger("capture_oauth_redirect")

# Mirrors bosch_thermostat_client.connectors.oauth2.Oauth2Connector and
# scripts/refresh_bosch_oauth.py -- keep these three in sync if Bosch ever
# changes the client registration.
CLIENT_ID = "762162C0-FA2D-4540-AE66-6489F189FADC"
REDIRECT_URI = "com.bosch.tt.dashtt.pointt://app/login"
CODE_VERIFIER = "abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklm"
SCOPES = [
    "openid", "email", "profile", "offline_access",
    "pointt.gateway.claiming", "pointt.gateway.removal",
    "pointt.gateway.list", "pointt.gateway.users",
    "pointt.gateway.resource.dashapp",
    "pointt.castt.flow.token-exchange", "bacon",
]


def build_auth_url() -> str:
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(CODE_VERIFIER.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
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
    query = unquote(urlencode(params))
    encoded_query = urllib.parse.quote(query)
    return_url = urllib.parse.quote_plus("/auth/connect/authorize/callback?")
    return f"https://singlekey-id.com/auth/en-us/login?ReturnUrl={return_url}{encoded_query}"


async def capture_callback_url(timeout_seconds: int) -> str | None:
    """Open a real browser on the Bosch login page, wait for the user to
    log in, and return the captured OAuth redirect URL."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit(
            "playwright isn't installed in this environment.\n"
            "Run this via ./scripts/run_capture_oauth_redirect.sh (uses `uv run`\n"
            "to fetch dependencies automatically), or:\n"
            "    pip install playwright playwright-stealth\n"
            "    playwright install chromium"
        )

    try:
        from playwright_stealth import Stealth
        stealth_ctx = Stealth().use_async(async_playwright())
    except ImportError:
        # playwright-stealth helps avoid bot-detection on the SingleKey ID
        # login page but isn't strictly required -- fall back without it.
        log.warning("playwright-stealth not installed; login may hit bot detection.")
        stealth_ctx = async_playwright()

    auth_url = build_auth_url()
    captured: list[str] = []
    done = asyncio.Event()

    async with stealth_ctx as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        def on_response(response):
            location = response.headers.get("location", "")
            if location.startswith(REDIRECT_URI.split("://")[0] + "://"):
                log.info("Captured redirect (response Location header): %s...", location[:80])
                captured.append(location)
                done.set()

        def on_request(request):
            if request.url.startswith(REDIRECT_URI.split("://")[0] + "://"):
                log.info("Captured redirect (navigation attempt): %s...", request.url[:80])
                captured.append(request.url)
                done.set()

        page.on("response", on_response)
        page.on("request", on_request)

        log.info("Opening Bosch SingleKey ID login page...")
        await page.goto(auth_url, wait_until="domcontentloaded", timeout=20_000)

        print()
        print("  Browser window is open -- log in with your Bosch account.")
        print("  This will continue automatically once you're logged in.")
        print()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.error("Timed out waiting for login (%ds).", timeout_seconds)
        finally:
            await browser.close()

    return captured[0] if captured else None


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="Seconds to wait for login before giving up (default: %(default)s)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    callback_url = await capture_callback_url(args.timeout)

    if not callback_url:
        sys.exit(
            "\nCould not capture the redirect URL automatically.\n"
            "Fall back to the manual method: open the URL below yourself, log in,\n"
            "and copy the failed-navigation URL from your browser's address bar\n"
            "(some browsers show it even without any handler registered):\n\n"
            + build_auth_url()
        )

    print()
    print("=" * 60)
    print("Captured redirect URL -- paste this wherever you're completing auth:")
    print()
    print(callback_url)
    print()
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
