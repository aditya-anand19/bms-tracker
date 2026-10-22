"""
BookMyShow Show Tracker - GitHub Actions version
------------------------------------------------
GitHub starts this in the cloud, so your laptop can be off. Each run stays
alive for ~5h45m, checking every CHECK_EVERY_SECONDS, and sends a Telegram
message when shows open. When a run ends, the schedule starts a new one.

Edit the SETTINGS block below. Telegram details go in GitHub Secrets
(not in this file), so they stay private even in a public repo.
"""

import os
import re
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

# ======================= SETTINGS (edit these) =======================

# Your BookMyShow booking URL (city + movie + language/format chosen).
# Copy it from the address bar after clicking "Book tickets".
BOOKING_URL = "PASTE_YOUR_BOOKMYSHOW_URL_HERE"

# Show date you want, YYYYMMDD (5 Nov 2026 = 20261105)
TARGET_DATE = "20261105"

# Only alert if one of these words is on the page, e.g. ["PVR", "IMAX"].
# Leave [] to alert for ANY theatre.
THEATRE_KEYWORDS = []

# How often to check, in seconds. 60 is a good balance; going much lower
# makes BookMyShow more likely to block you.
CHECK_EVERY_SECONDS = 60

# How long one run keeps checking (GitHub's hard limit is 6 hours).
RUN_FOR_MINUTES = 345

# =====================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# True when you click "Run workflow" by hand -> always sends a status report
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

IST = timezone(timedelta(hours=5, minutes=30))


def log(msg):
    print(f"[{datetime.now(IST).strftime('%H:%M:%S IST')}] {msg}", flush=True)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram secrets missing. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
        log("Telegram sent." if r.ok else f"Telegram error: {r.text}")
    except Exception as e:
        log(f"Telegram failed: {e}")


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
    """Returns (status, message). status is 'found', 'not_yet' or 'blocked'."""
    resp = page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    final_url = page.url
    text = page.inner_text("body").lower()
    page.screenshot(path="last_check.png", full_page=True)

    code = resp.status if resp else 0
    log(f"HTTP {code} | landed on: {final_url}")

    if code in (403, 429) or any(
        s in text for s in ["access denied", "verify you are human", "just a moment", "request blocked"]
    ):
        return "blocked", f"BookMyShow blocked the cloud check (HTTP {code})."

    if TARGET_DATE not in final_url:
        return "not_yet", "Date not open yet (redirected to another date)."

    if THEATRE_KEYWORDS:
        hits = [k for k in THEATRE_KEYWORDS if k.lower() in text]
        if not hits:
            return "not_yet", "Date is open, but your theatre/format isn't listed yet."
        return "found", f"Shows open on {TARGET_DATE} with: {', '.join(hits)}"

    return "found", f"Shows are open on {TARGET_DATE}!"


def main():
    if "PASTE" in BOOKING_URL:
        log("Paste your BookMyShow URL into BOOKING_URL in tracker.py first.")
        sys.exit(1)

    target_url = build_target_url(BOOKING_URL, TARGET_DATE)
    log(f"Watching: {target_url}")
    stop_at = time.time() + RUN_FOR_MINUTES * 60
    first = True
    blocked_streak = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
        )

        while time.time() < stop_at:
            try:
                status, msg = check(page, target_url)
            except Exception as e:
                status, msg = "error", f"Check failed: {e}"
            log(msg)

            if status == "found":
                send_telegram(f"🎬 BOOK NOW! {msg}\n{target_url}")
                break  # next scheduled run will remind you again

            if first and MANUAL_RUN:
                # Manual test run: report once, so you know the setup works.
                send_telegram(f"Tracker test run ✅\nStatus: {status}\n{msg}\n{target_url}")
            first = False

            # If blocked many times in a row, back off instead of hammering.
            blocked_streak = blocked_streak + 1 if status == "blocked" else 0
            wait = CHECK_EVERY_SECONDS * (5 if blocked_streak >= 3 else 1)
            time.sleep(wait + random.randint(0, 15))

        browser.close()
    log("Run finished.")


if __name__ == "__main__":
    main()
