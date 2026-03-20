"""Bambu Lab cloud token extractor.

Uses Selenium + system Chromium to log into bambulab.com and extract the
MQTT-compatible accessToken JWT from the login network response.

The browser cookie 'token' is a web session token (not a JWT) and is
rejected by the Bambu MQTT broker.  This script captures the actual
accessToken (eyJ...) from the login POST response body via Chrome's
network performance log — that is the token required for MQTT.

Usage on Raspberry Pi (called automatically by install.sh on Pi 4+):
    python scripts/get_cloud_token.py --headless --output-file /tmp/token.txt

Usage on a desktop PC:
    pip install selenium
    python scripts/get_cloud_token.py

If Bambu sends a verification email, enter the code at the prompt — the
script fills it into the browser automatically.

Security: the token is written to --output-file or printed once to stdout only.
"""

import argparse
import getpass
import json
import os
import sys
import time

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException, TimeoutException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("selenium is not installed.  Run:  pip install selenium")
    sys.exit(1)

BAMBU_URL = "https://bambulab.com/en-gb"
POLL_SECONDS = 120  # wait up to 2 min for login + any verification step

# System Chromium paths (Raspberry Pi OS Bookworm / Bullseye)
_CHROMIUM_CANDIDATES = [
    "/usr/bin/chromium",          # Pi OS Bookworm
    "/usr/bin/chromium-browser",  # Pi OS Bullseye / Ubuntu
]
_CHROMEDRIVER_CANDIDATES = [
    "/usr/bin/chromedriver",
    "/usr/bin/chromium-driver",
]


def _make_driver(headless: bool) -> webdriver.Chrome:
    """Build a Chrome WebDriver, preferring the system Chromium on Pi."""
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")           # required when running as root
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Enable network performance logging so we can extract the accessToken JWT
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    # Use system Chromium + chromedriver if available (Pi)
    chromium_bin = next((p for p in _CHROMIUM_CANDIDATES if os.path.exists(p)), None)
    chromedriver_bin = next((p for p in _CHROMEDRIVER_CANDIDATES if os.path.exists(p)), None)

    if chromedriver_bin:
        if chromium_bin:
            options.binary_location = chromium_bin
        return webdriver.Chrome(service=Service(chromedriver_bin), options=options)

    # Desktop fallback — let Selenium Manager find/download chromedriver
    return webdriver.Chrome(options=options)


def _get_jwt_from_network_logs(driver: webdriver.Chrome) -> str | None:
    """Scan Chrome performance logs for the accessToken JWT in the login response.

    The Bambu MQTT broker requires the accessToken JWT (eyJ...) from the login
    POST response, not the browser session cookie which is a different format.
    """
    try:
        logs = driver.get_log("performance")
    except Exception:
        return None

    for entry in reversed(logs):  # scan most recent first
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            params = msg.get("params", {})
            response = params.get("response", {})
            url = response.get("url", "")
            status = response.get("status", 0)

            # Look for a successful login endpoint response
            if status != 200:
                continue
            if "sign-in" not in url and "login" not in url:
                continue

            request_id = params.get("requestId", "")
            if not request_id:
                continue

            body_resp = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = json.loads(body_resp.get("body", "{}"))
            token = body.get("accessToken") or body.get("access_token")
            if token and token.startswith("eyJ"):
                return token

        except Exception:
            continue

    return None


def _get_cookie_token(driver: webdriver.Chrome) -> str | None:
    """Return the Bambu session cookie value, or None.

    This is the web session token — it works for the REST API but is NOT
    accepted by the MQTT broker.  Used as a fallback / login-complete signal.
    """
    for cookie in driver.get_cookies():
        if cookie.get("name") == "token":
            val = cookie.get("value", "")
            if len(val) > 20:
                return val
    return None


def extract_token(email: str, password: str, headless: bool) -> str:
    """Open bambulab.com, log in, and return the MQTT-compatible accessToken JWT."""
    driver = _make_driver(headless)
    wait = WebDriverWait(driver, 15)

    try:
        # ── Navigate ─────────────────────────────────────────────────────────
        print("Navigating to bambulab.com …")
        driver.get(BAMBU_URL)

        # ── Click the account / login icon (top-right, next to Store) ────────
        print("Opening login form …")
        try:
            el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
                'a[href*="sign-in"], a[href*="login"], '
                'button[aria-label*="account" i], button[aria-label*="profile" i], '
                'a[class*="account" i], a[class*="login" i]'
            )))
            el.click()
        except TimeoutException:
            # Fallback: the icon sits immediately before the Store button
            try:
                store = driver.find_element(By.XPATH,
                    '//*[self::a or self::button][normalize-space()="Store"]')
                driver.execute_script(
                    "arguments[0].previousElementSibling.click()", store)
            except Exception:
                print("Could not locate the login button automatically.")
                print("If a browser window is visible, click the login icon manually.")

        # ── Wait for email field ──────────────────────────────────────────────
        print("Waiting for login form …")
        try:
            email_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR,
                'input[placeholder*="Email" i], input[type="email"]'
            )))
        except TimeoutException:
            raise RuntimeError(
                "Login form did not appear. The site layout may have changed.")

        # ── Fill credentials ──────────────────────────────────────────────────
        email_el.clear()
        email_el.send_keys(email)

        pw_el = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
        pw_el.send_keys(password)

        # Tick the Terms of Use checkbox if present and unchecked
        try:
            cb = driver.find_element(By.CSS_SELECTOR, 'input[type="checkbox"]')
            if not cb.is_selected():
                cb.click()
        except NoSuchElementException:
            pass

        # ── Submit ────────────────────────────────────────────────────────────
        try:
            driver.find_element(By.XPATH,
                '//button[@type="submit"] | //button[normalize-space()="Log In"]'
            ).click()
        except NoSuchElementException:
            pass

        # ── Accept Terms / Notice popup if it appears ─────────────────────────
        try:
            agree = WebDriverWait(driver, 5).until(EC.element_to_be_clickable(
                (By.XPATH, '//button[normalize-space()="Agree"]')
            ))
            agree.click()
            print("Accepted Terms of Use notice.")
        except TimeoutException:
            pass  # no popup — that's fine

        # ── Poll for login completion, handling email verification if needed ──
        print(f"Waiting for login to complete (up to {POLL_SECONDS}s) …")
        if headless:
            print("If a verification code was emailed to you, enter it at the prompt below.")

        code_submitted = False
        for _ in range(POLL_SECONDS):
            # Login is complete when the session cookie appears
            if _get_cookie_token(driver):
                break

            # Detect verification code input field and prompt in terminal
            if not code_submitted:
                try:
                    code_el = driver.find_element(By.CSS_SELECTOR,
                        'input[placeholder*="code" i], input[placeholder*="verif" i], '
                        'input[placeholder*="verification" i]'
                    )
                    print("\nEmail verification required.")
                    print("Check your email for a code from Bambu Lab.")
                    code = input("Enter verification code: ").strip()
                    code_el.clear()
                    code_el.send_keys(code)
                    try:
                        driver.find_element(By.XPATH,
                            '//button[@type="submit"] | '
                            '//button[contains(normalize-space(),"Confirm")] | '
                            '//button[contains(normalize-space(),"Verify")]'
                        ).click()
                    except NoSuchElementException:
                        pass
                    code_submitted = True
                except NoSuchElementException:
                    pass

            time.sleep(1)
        else:
            raise RuntimeError(
                "Login timed out. Check your credentials and try again.")

        # ── Extract the MQTT-compatible JWT accessToken ───────────────────────
        # The browser session cookie 'token' is NOT a JWT and is rejected by
        # the MQTT broker.  We capture the real accessToken from the login
        # POST response via Chrome's network performance log.
        print("Extracting access token …")
        jwt_token = _get_jwt_from_network_logs(driver)
        if jwt_token:
            return jwt_token

        # Fallback warning — cookie token will work for REST API but not MQTT
        cookie_token = _get_cookie_token(driver)
        if cookie_token:
            print(
                "\nWarning: could not find JWT accessToken in network logs.\n"
                "The cookie token may not work for MQTT. Try logging in again\n"
                "or use the requests-based script to get a proper JWT token."
            )
            return cookie_token

        raise RuntimeError("No token found after login.")

    finally:
        driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Bambu Lab MQTT accessToken via browser automation"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser headlessly (auto-detected when DISPLAY is unset on Linux)"
    )
    parser.add_argument(
        "--output-file", metavar="PATH",
        help="Write token to this file instead of stdout"
    )
    args = parser.parse_args()

    # Auto-detect headless: no DISPLAY on Linux means no GUI available
    headless: bool = args.headless or (
        sys.platform == "linux" and not os.environ.get("DISPLAY")
    )

    print("\nBambu Lab Cloud Token Extractor")
    print("=" * 38)
    if headless:
        print("Running headlessly — Chromium will not show a window.")
        print("If a verification email is sent, you will be prompted here.\n")
    else:
        print("A browser window will open. Log in and the token is extracted")
        print("automatically. Enter any verification code in the browser window.\n")

    email = input("Bambu Lab account email: ").strip()
    password = getpass.getpass("Password: ")

    print("\nStarting Chromium …")
    try:
        token = extract_token(email, password, headless)
    except Exception as exc:
        print(f"\nError: {exc}")
        sys.exit(1)

    if args.output_file:
        with open(args.output_file, "w") as fh:
            fh.write(token)
        print(f"Token written to {args.output_file}")
    else:
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
