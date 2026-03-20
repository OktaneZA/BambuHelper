"""Bambu Lab cloud token extractor — browser automation method.

Opens a Chromium browser window, navigates to bambulab.com, and logs you
in using your credentials. The session token is then extracted from cookies.

If Bambu sends a verification email, the browser window stays open so you
can enter the code directly — no script changes needed.

Usage:
    pip install playwright
    playwright install chromium
    python scripts/get_cloud_token.py

Run this on your Windows/Mac/Linux PC (not the Pi). Copy the token that
appears at the end into the BambuHelper web portal under Connection → Cloud Token.

Tokens are valid for approximately 3 months.
Security: the token is printed ONCE to stdout only.
"""

import getpass
import sys

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is not installed. Run:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

BAMBU_URL = "https://bambulab.com/en-gb"
COOKIE_NAME = "token"
LOGIN_POLL_SECONDS = 120  # wait up to 2 minutes for login + any verification


def _find_token(cookies: list) -> str | None:
    """Return the Bambu token cookie value if present."""
    for cookie in cookies:
        if cookie.get("name") == COOKIE_NAME and "bambulab.com" in cookie.get("domain", ""):
            return cookie.get("value")
    return None


def main() -> None:
    print("\nBambu Lab Cloud Token Extractor")
    print("=" * 38)
    print("A browser window will open. Log in and the token will be")
    print("extracted automatically. If asked for a verification code,")
    print("enter it in the browser — the script will wait.\n")

    email = input("Bambu Lab account email: ").strip()
    password = getpass.getpass("Password: ")

    print("\nLaunching browser …")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # ── Navigate to bambulab.com ────────────────────────────────────────
        page.goto(BAMBU_URL, wait_until="domcontentloaded")

        # ── Click the account/profile icon (top-right, next to Store) ───────
        print("Clicking account icon …")
        try:
            # The icon is an <a> or <button> near the Store button
            page.click(
                'header a[href*="sign-in"], '
                'header a[href*="login"], '
                'header button[aria-label*="account" i], '
                'header button[aria-label*="profile" i], '
                'header [class*="user" i] a, '
                'header svg[class*="user" i]',
                timeout=8000,
            )
        except PlaywrightTimeout:
            # Fallback: look for a person/account icon near the Store button
            try:
                store_btn = page.locator('a:has-text("Store"), button:has-text("Store")').first
                store_btn.locator("xpath=preceding-sibling::*[1]").click(timeout=5000)
            except Exception:
                print("Could not find the account icon automatically.")
                print("Please click the account icon in the browser window to open the login form.")

        # ── Wait for login modal / email field ───────────────────────────────
        print("Waiting for login form …")
        try:
            page.wait_for_selector(
                'input[placeholder*="Email" i], input[type="email"]',
                timeout=15000,
            )
        except PlaywrightTimeout:
            print("Login form did not appear. Please click the login button in the browser.")
            page.wait_for_selector(
                'input[placeholder*="Email" i], input[type="email"]',
                timeout=60000,
            )

        # ── Fill credentials ─────────────────────────────────────────────────
        print("Entering credentials …")
        email_input = page.locator('input[placeholder*="Email" i], input[type="email"]').first
        email_input.fill(email)

        pw_input = page.locator('input[type="password"]').first
        pw_input.fill(password)

        # Accept Terms of Use checkbox if present and unchecked
        try:
            checkbox = page.locator('input[type="checkbox"]').first
            if not checkbox.is_checked():
                checkbox.check(timeout=2000)
        except Exception:
            pass

        # Click Log In button
        page.locator('button[type="submit"], button:has-text("Log In")').first.click()

        # ── Handle "Notice" / Terms popup if it appears ──────────────────────
        try:
            page.wait_for_selector('text="Agree"', timeout=4000)
            page.locator('button:has-text("Agree")').click()
            print("Accepted Terms of Use notice.")
        except PlaywrightTimeout:
            pass  # no notice popup — that's fine

        # ── Poll for token cookie ────────────────────────────────────────────
        print(
            f"\nWaiting for login to complete (up to {LOGIN_POLL_SECONDS}s) …"
            "\nIf a verification email was sent, enter the code in the browser window."
        )
        token = None
        for _ in range(LOGIN_POLL_SECONDS):
            cookies = context.cookies()
            token = _find_token(cookies)
            if token:
                break
            page.wait_for_timeout(1000)

        browser.close()

    if not token:
        print("\nToken not found after waiting. Possible causes:")
        print("  - Login failed (wrong password)")
        print("  - Verification step not completed in time")
        print("  - Bambu changed their cookie name")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS — Your Bambu Lab Cloud Token:")
    print("=" * 60)
    print(token)
    print("=" * 60)
    print("\nCopy the token above and paste it into:")
    print("  BambuHelper web portal → Connection → Cloud Token")
    print("Token is valid for ~3 months. Run this script again to refresh.\n")


if __name__ == "__main__":
    main()
