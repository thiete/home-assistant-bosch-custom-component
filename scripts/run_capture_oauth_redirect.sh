#!/bin/bash
# Capture the Bosch POINTT OAuth redirect via a real, automated browser --
# no Windows VM, no OS URL-scheme registration. See
# capture_oauth_redirect_playwright.py for how this works and why.
#
# First run only:
#   curl -LsSf https://astral.sh/uv/install.sh | sh
#   uv run --with playwright python -m playwright install chromium
set -e
cd "$(dirname "$0")"
uv run --with playwright --with playwright-stealth python capture_oauth_redirect_playwright.py "$@"
