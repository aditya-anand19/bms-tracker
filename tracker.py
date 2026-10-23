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
import subprocess
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

# Daily "still running" message at this hour (IST, 24h clock). None = off.
HEARTBEAT_HOUR = 12

# =====================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
# Set by the workflow when the phone has just (re)connected
JUST_CONNECTED = os.environ.get("JUST_CONNECTED") == "true"
IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_FILE = "heartbeat.txt"  # remembers the last day we sent it (kept by GitHub cache)


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


def phone_reachable():
    """Quick Tailscale ping to the phone (a few seconds). True if it answers."""
    exit_node = os.environ.get("EXIT_NODE", "")
    if not exit_node:
        return True  # not running on GitHub with Tailscale -> don't block
    try:
        r = subprocess.run(
            ["sudo", "tailscale", "ping", "--until-direct=false", "--c", "2",
             "--timeout", "5s", exit_node],
            capture_output=True, text=True, timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def maybe_send_heartbeat(status, msg, checks_this_run, force=False):
    """Tell the user everything is fine: once a day after HEARTBEAT_HOUR IST,
    or right away when force=True (phone just reconnected)."""
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    if not force:
        if HEARTBEAT_HOUR is None:
            return
        try:
            last_sent = open(HEARTBEAT_FILE).read().strip()
        except OSError:
            last_sent = ""
        if now.hour < HEARTBEAT_HOUR or last_sent == today:
            return

    if status in ("not_yet", "found", "unsure"):
        head = "Still running \u2705 Everything is working."
        tail = "Sit back and relax - you'll get BOOK NOW + the link the moment it opens."
    else:
        head = "Still running, but the last check had a problem \u26a0\ufe0f"
        tail = "If this repeats tomorrow, tell Claude."
    text = (f"{head}\n"
            f"Watching: {THEATRE_NAME or 'any theatre'} {FORMAT_KEYWORD}, {TARGET_DATE}\n"
            f"Last check: {now.strftime('%d %b %H:%M')} IST - {status}\n"
            f"({msg})\n"
            f"Checks in this session: {checks_this_run}\n"
            f"{tail}")
    if send_telegram(text):
        # Counts as today's daily message if it's already past the daily hour,
        # so you don't get a duplicate a minute later.
        if not force or (HEARTBEAT_HOUR is not None and now.hour >= HEARTBEAT_HOUR):
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(today)


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
        s in text for s in ["access denied", "verify you are human", "just a moment",
                            "request blocked", "you have been blocked"]
    ):
        return "blocked", f"BookMyShow blocked the check (HTTP {code})."

    if TARGET_DATE not in final_url:
        return "not_yet", "Date not open yet (redirected to another date)."

    # Read the row of dates on the page (e.g. "05 nov", "thu 06 nov", "nov 7").
    # BookMyShow only lists dates that are open for booking.
    dates_on_page = dates_listed(text)
    log(f"Dates listed on page: {', '.join(dates_on_page) or '(could not read)'}")
    target_label = f"{int(TARGET_DATE[6:8])} {MONTHS[int(TARGET_DATE[4:6]) - 1]}"
    if dates_on_page and target_label not in dates_on_page:
        return "not_yet", (f"Date not open yet ({target_label} isn't in the date list; "
                           f"open: {', '.join(dates_on_page)}).")

    if THEATRE_NAME:
        name = THEATRE_NAME.lower()
        if name not in text:
            return "not_yet", f"Date is open, but {THEATRE_NAME} isn't listed yet."
        if FORMAT_KEYWORD:
            fmt = FORMAT_KEYWORD.lower()
            starts = [m.start() for m in re.finditer(re.escape(name), text)]
            if not any(fmt in text[i:i + 700] for i in starts):
                return "not_yet", f"{THEATRE_NAME} is listed, but no {FORMAT_KEYWORD} shows yet."
            found_msg = f"{FORMAT_KEYWORD} shows open at {THEATRE_NAME} on {target_label}!"
        else:
            found_msg = f"Shows open at {THEATRE_NAME} on {target_label}!"
    else:
        found_msg = f"Shows are open on {target_label}!"

    if not dates_on_page:
        # Couldn't read the date row, so we can't be sure -> "unsure", not "found".
        return "unsure", found_msg + " (couldn't confirm the date on the page)"
    return "found", found_msg + f" (dates open: {', '.join(dates_on_page)})"


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def dates_listed(text):
    """Find dates like '05 nov', 'thu\n05\nnov', 'nov 5' in the page text."""
    m = "|".join(MONTHS)
    found = []
    for d, mon in re.findall(rf"(?<![:.\d])\b(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+({m})[a-z]*\b", text):
        found.append(f"{int(d)} {mon}")
    for mon, d in re.findall(rf"\b({m})[a-z]*\s+(\d{{1,2}})\b(?![:.]\d)", text):
        found.append(f"{int(d)} {mon}")
    out = []
    for x in found:
        if 1 <= int(x.split()[0]) <= 31 and x not in out:
            out.append(x)
    return out


def main():
    target_url = build_target_url(BOOKING_URL, TARGET_DATE)
    log(f"Watching: {target_url}")
    stop_at = time.time() + RUN_FOR_MINUTES * 60
    test_results = []
    checks = 0
    unsure_sent = False
    errors_in_a_row = 0
    blocked_in_a_row = 0

    with sync_playwright() as p:
        phone_misses = 0
        while time.time() < stop_at:
            # Is the phone still there? If not 3 times in a row (~1 min), stop
            # so the workflow can tell you right away.
            if not phone_reachable():
                phone_misses += 1
                log(f"Phone not reachable ({phone_misses}/3)")
                if phone_misses >= 3:
                    log("Phone disconnected - stopping.")
                    sys.exit(3)
                time.sleep(15)
                continue
            phone_misses = 0

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
            checks += 1
            if checks == 1 and JUST_CONNECTED:
                # Phone just (re)connected: confirm the first real check worked.
                maybe_send_heartbeat(status, msg, checks, force=True)
            else:
                maybe_send_heartbeat(status, msg, checks)

            # Manual test run: report after 3 checks, so you know repeated
            # checks get through too (not just the first one).
            if MANUAL_RUN and len(test_results) < 3:
                test_results.append(status)
                if len(test_results) == 3:
                    ok = all(s in ("not_yet", "found", "unsure") for s in test_results)
                    send_telegram(f"Tracker test: {'OK' if ok else 'PROBLEM'} (via your phone)\n"
                                  f"First 3 checks: {', '.join(test_results)}\n"
                                  f"Latest: {msg}\n{target_url}")

            if status == "found":
                send_telegram(f"BOOK NOW!!! {msg}\n{target_url}")
            elif status == "unsure" and not unsure_sent:
                send_telegram(f"MAYBE OPEN - please check manually.\n{msg}\n{target_url}")
                unsure_sent = True

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
