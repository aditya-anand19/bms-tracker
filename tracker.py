"""
BookMyShow Show Tracker - GitHub Actions + your phone (Tailscale exit node)
--------------------------------------------------------------------------
GitHub runs this in the cloud; all its internet traffic goes out through your
phone, so BookMyShow sees a normal mobile connection instead of a cloud server.

- Silent while shows aren't open.
- When Dolby Cinema opens at your theatre on your date: "BOOK NOW" + link
  on Telegram every CHECK_EVERY_SECONDS until you disable the workflow.
"""

import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

# ======================= SETTINGS (edit these) =======================

BOOKING_URL = (
    "https://in.bookmyshow.com/movies/hyderabad/namit-malhotras-ramayana/"
    "buytickets/ET00523251/20261105"
    "?etCodes=ET00523251&language=hindi&refEventCode=ET00523251"
)

# Show date you want, YYYYMMDD
TARGET_DATE = "20261119"

# The ONE theatre you want ("" = any theatre)
THEATRE_NAME = "Allu Cinemas"

# Format you want at that theatre ("" = any format)
FORMAT_KEYWORD = "Dolby Cinema"

# How often to check, in seconds (don't go below 60)
CHECK_EVERY_SECONDS = 60

# How long one GitHub run keeps checking (GitHub's limit is 6 hours)
RUN_FOR_MINUTES = 340

# =====================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
IST = timezone(timedelta(hours=5, minutes=30))


def log(msg):
    print(f"[{datetime.now(IST).strftime('%d %b %H:%M:%S IST')}] {msg}", flush=True)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram secrets missing.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
        if r.ok:
            return True
        log(f"Telegram error: {r.text}")
    except Exception as e:
        log(f"Telegram failed: {e}")
    return False


def build_target_url(url, date):
    """Put the target date into the URL, keeping ?language=... etc."""
    path, _, query = url.strip().partition("?")
    path = path.rstrip("/")
    if re.search(r"/\d{8}$", path):
        path = re.sub(r"/\d{8}$", f"/{date}", path)
    else:
        path = f"{path}/{date}"
    return f"{path}?{query}" if query else path


def check(page, target_url):
    """Returns (status, message). status: found / not_yet / blocked."""
    resp = page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)  # let the theatre list load
    final_url = page.url
    text = page.inner_text("body").lower()
    page.screenshot(path="last_check.png", full_page=True)
    code = resp.status if resp else 0
    log(f"HTTP {code} | landed on: {final_url}")

    if code in (403, 429) or any(
        s in text for s in ["access denied", "verify you are human", "just a moment", "request blocked"]
    ):
        return "blocked", f"BookMyShow blocked the check (HTTP {code})."

    if TARGET_DATE not in final_url:
        return "not_yet", "Date not open yet (redirected to another date)."

    if THEATRE_NAME:
        name = THEATRE_NAME.lower()
        if name not in text:
            return "not_yet", f"Date is open, but {THEATRE_NAME} isn't listed yet."
        if FORMAT_KEYWORD:
            fmt = FORMAT_KEYWORD.lower()
            starts = [m.start() for m in re.finditer(re.escape(name), text)]
            if not any(fmt in text[i:i + 700] for i in starts):
                return "not_yet", f"{THEATRE_NAME} is listed, but no {FORMAT_KEYWORD} shows yet."
            return "found", f"{FORMAT_KEYWORD} shows open at {THEATRE_NAME} on {TARGET_DATE}!"
        return "found", f"Shows open at {THEATRE_NAME} on {TARGET_DATE}!"

    return "found", f"Shows are open on {TARGET_DATE}!"


def main():
    target_url = build_target_url(BOOKING_URL, TARGET_DATE)
    log(f"Watching: {target_url}")
    stop_at = time.time() + RUN_FOR_MINUTES * 60
    test_results = []
    errors_in_a_row = 0
    blocked_in_a_row = 0

    with sync_playwright() as p:
        while time.time() < stop_at:
            # A brand-new browser with NO cookies for every check.
            # (BookMyShow allows the first visit but flags the cookie it sets,
            #  so reusing one browser got every later check blocked.)
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                )
                page = context.new_page()
                status, msg = check(page, target_url)
                errors_in_a_row = 0
            except Exception as e:
                status, msg = "error", f"Check failed: {e}"
                errors_in_a_row += 1
            finally:
                browser.close()
            log(f"{status.upper()}: {msg}")

            # Manual test run: report after 3 checks, so you know repeated
            # checks get through too (not just the first one).
            if MANUAL_RUN and len(test_results) < 3:
                test_results.append(status)
                if len(test_results) == 3:
                    ok = all(s in ("not_yet", "found") for s in test_results)
                    send_telegram(f"Tracker test: {'OK' if ok else 'PROBLEM'} (via your phone)\n"
                                  f"First 3 checks: {', '.join(test_results)}\n"
                                  f"Latest: {msg}\n{target_url}")

            if status == "found":
                send_telegram(f"BOOK NOW!!! {msg}\n{target_url}")

            # Phone unreachable for ~5 checks -> fail the run so the workflow
            # warns you (it sends that warning directly, not via the phone).
            if errors_in_a_row >= 5:
                log("Too many failed checks in a row - is the phone online?")
                sys.exit(1)

            blocked_in_a_row = blocked_in_a_row + 1 if status == "blocked" else 0
            if blocked_in_a_row == 3:
                send_telegram("WARNING: BookMyShow is blocking checks even through "
                              "your phone. Slowing down to every 5 min.")
            wait = CHECK_EVERY_SECONDS * (5 if blocked_in_a_row >= 3 else 1)
            time.sleep(wait + random.randint(0, 15))

    log("Run finished; the schedule will start the next one.")


if __name__ == "__main__":
    main()
