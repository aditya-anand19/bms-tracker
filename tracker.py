"""
BookMyShow Show Tracker - GitHub Actions + your phone (Tailscale)
-----------------------------------------------------------------
GitHub runs this in the cloud. Everything Chrome loads goes through a local proxy
that the workflow routes through your phone; Telegram and everything else use
GitHub's own internet.

- Phone online : checks BookMyShow every minute (silent while not open).
- Phone offline: one "disconnected" message, then looks for the phone every
                 minute; one "reconnected" message when it's back.
- Shows found  : "BOOK NOW" + link every 30 seconds.
- Daily ~12 PM : one "Still running" message while connected.
- Every message goes to Telegram and (if set up) to email; BOOK NOW is
  emailed once per opening (Telegram repeats it every 30 s).

The workflow also uses this file to send its own messages:
    python3 tracker.py notify connected | disconnected | routing_failed
(that part needs only Python's built-in modules, no extra installs).
"""

import html
import json
import os
import random
import re
import smtplib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

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

# How often to repeat BOOK NOW once shows are open, in seconds
BOOK_NOW_EVERY_SECONDS = 30

# How long one GitHub run keeps checking (GitHub's limit is 6 hours)
RUN_FOR_MINUTES = 330

# Daily "still running" message at this hour (IST, 24h clock). None = off.
HEARTBEAT_HOUR = 12

# =====================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Optional email (Gmail + App Password). Leave the secrets empty to turn email off.
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "").strip()
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip() or EMAIL_ADDRESS
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
# Set by the workflow when the phone has just (re)connected
JUST_CONNECTED = os.environ.get("JUST_CONNECTED") == "true"
# Local proxy that the workflow routes through your phone (Chrome uses it)
BMS_PROXY = os.environ.get("BMS_PROXY", "")
IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_FILE = "heartbeat.txt"  # remembers the last day we sent it (kept by GitHub cache)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def log(msg):
    print(f"[{datetime.now(IST).strftime('%d %b %H:%M:%S IST')}] {msg}", flush=True)


# ============================ helpers ============================

def esc(s):
    """Make any text safe to put inside a Telegram HTML message."""
    return html.escape(str(s), quote=False)


def now_str():
    dt = datetime.now(IST)
    return f"{dt:%d %b %Y}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'} IST"


def target_date_pretty():
    try:
        return datetime.strptime(TARGET_DATE, "%Y%m%d").strftime("%a, %d %b %Y")
    except ValueError:
        return TARGET_DATE


def language():
    q = urllib.parse.parse_qs(urllib.parse.urlparse(BOOKING_URL).query)
    return (q.get("language", [""])[0] or "any language").capitalize()


def build_target_url(url, date):
    """Put the target date into the URL, keeping ?language=... etc."""
    path, _, query = url.strip().partition("?")
    path = path.rstrip("/")
    if re.search(r"/\d{8}$", path):
        path = re.sub(r"/\d{8}$", f"/{date}", path)
    else:
        path = f"{path}/{date}"
    return f"{path}?{query}" if query else path


TARGET_URL = build_target_url(BOOKING_URL, TARGET_DATE)


def _http_get_json(url, proxy=None, timeout=15):
    """GET a JSON url. proxy=None -> direct (GitHub's internet); proxy='http://..' -> via it."""
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _describe_network(j):
    org = re.sub(r"^AS\d+\s+", "", str(j.get("org", "")).strip()) or "unknown network"
    country = str(j.get("country", "")).strip() or "?"
    return f"{org} ({country})"


_sender_cache = {"value": None}


def sender_network():
    """Network of the GitHub server that sends the Telegram messages (direct, not via phone)."""
    if _sender_cache["value"] is None:
        try:
            _sender_cache["value"] = _describe_network(_http_get_json("https://ipinfo.io/json"))
        except Exception:
            _sender_cache["value"] = "network unknown"
    return _sender_cache["value"]


_bms_cache = {"value": None, "at": 0.0}


def proxy_running():
    m = re.match(r"https?://([^:/]+):(\d+)", BMS_PROXY or "")
    if not m:
        return False
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=3):
            return True
    except OSError:
        return False


def bms_view(max_age=600):
    """What BookMyShow sees: asked through the SAME proxy Chrome uses (i.e. via your phone)."""
    if _bms_cache["value"] and time.time() - _bms_cache["at"] < max_age:
        return _bms_cache["value"]
    if not BMS_PROXY or not proxy_running():
        return "unknown (Chrome's proxy isn't running)"
    try:
        value = _describe_network(_http_get_json("https://ipinfo.io/json", proxy=BMS_PROXY, timeout=20))
    except Exception:
        return "unknown (couldn't reach the internet through your phone)"
    _bms_cache.update(value=value, at=time.time())
    return value


def run_link():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        return f'<a href="{esc(f"{server}/{repo}/actions/runs/{run_id}")}">run log</a>'
    return ""


def watching_line():
    return (f"🎬 <b>Watching:</b> {esc(THEATRE_NAME or 'Any theatre')} · "
            f"{esc(FORMAT_KEYWORD or 'any format')} · {esc(target_date_pretty())} · {esc(language())}")


def footer(bms):
    link = run_link()
    return ("\n\n"
            f"📡 <b>BookMyShow sees:</b> {esc(bms)}\n"
            f"✉️ <b>Sent from:</b> GitHub server · {esc(sender_network())} — not your phone\n"
            f"🕒 {esc(now_str())}" + (f" · {link}" if link else ""))


# ============================ Telegram ============================

def send_telegram(text):
    """Send an HTML-formatted message over GitHub's own internet. Never raises."""
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram secrets missing.")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    attempts = [
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
        # fallback: same message as plain text (if Telegram ever rejects the formatting)
        {"chat_id": CHAT_ID, "text": html.unescape(re.sub(r"<[^>]+>", "", text)),
         "disable_web_page_preview": "true"},
    ]
    for data in attempts:
        for _ in range(3):
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
                with direct.open(req, timeout=20) as r:
                    if json.loads(r.read().decode()).get("ok"):
                        return True
            except urllib.error.HTTPError as e:
                log(f"Telegram error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
                if e.code == 400:
                    break  # formatting problem -> try the plain-text version
                time.sleep(3)
            except Exception as e:
                log(f"Telegram failed: {e}")
                time.sleep(3)
    return False


def send_email(text):
    """Send the same message by email (Gmail SMTP, GitHub's own internet). Never raises."""
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD or not EMAIL_TO:
        return False  # email not set up -> silently skip
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    subject = plain.split("\n", 1)[0].strip()[:150] + " · BMS Tracker"
    body_html = ("<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5\">"
                 + re.sub(r"(https://in\.bookmyshow\.com/\S+)", r'<a href="\1">\1</a>', text).replace("\n", "<br>")
                 + "</div>")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"BMS Tracker <{EMAIL_ADDRESS}>"
    msg["To"] = EMAIL_TO
    msg.set_content(plain)
    msg.add_alternative(body_html, subtype="html")
    for _ in range(2):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
                smtp.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
                smtp.send_message(msg)
            return True
        except smtplib.SMTPAuthenticationError as e:
            log(f"Email login failed (check EMAIL_ADDRESS / EMAIL_APP_PASSWORD): {e}")
            return False
        except Exception as e:
            log(f"Email failed: {e}")
            time.sleep(3)
    return False


def send(text, email=True):
    """Send a message to Telegram and (optionally) email. Returns True if Telegram got it."""
    ok = send_telegram(text)
    if email:
        send_email(text)
    return ok


# ============================ messages ============================
# Every message: bold title, what happened, what happens next, then the footer
# (what BookMyShow sees, which server sent it, time, link to the run log).

STATUS_WORDS = {
    "not_yet": "Not open yet",
    "found": "OPEN!",
    "unsure": "Maybe open",
    "blocked": "Blocked by BookMyShow",
    "error": "Page failed to load",
}


def msg_connected(reconnected=False):
    title = ("✅ <b>Phone reconnected — tracker is running again</b>" if reconnected
             else "✅ <b>Phone connected — tracker is running</b>")
    return (f"{title}\n\n"
            f"{watching_line()}\n"
            "Checking BookMyShow every minute. The first check is running now; "
            "you'll get a confirmation in about a minute."
            + footer(bms_view(max_age=0)))


def msg_disconnected():
    return ("⏸️ <b>Phone disconnected — tracker paused</b>\n\n"
            "I can't reach your phone on Tailscale, so BookMyShow checks are paused.\n"
            "I'll keep looking for it and send <b>one</b> message when it's back. "
            "No more messages until then."
            + footer("nothing (phone offline)"))


def msg_routing_failed():
    reason = os.environ.get("ROUTE_FAIL_REASON", "").strip() or "Unknown problem."
    if "exit node" in reason.lower():  # all phone-side reasons mention the exit node
        hint = ("Fix: open Tailscale on your phone and make sure <b>Run as exit node</b> is ON.")
    else:
        hint = ("This is a problem on the GitHub side and usually fixes itself on the next run. "
                "If it keeps happening, tell Claude.")
    return ("⚠️ <b>Phone found, but BookMyShow checks couldn't start</b>\n\n"
            f"<b>Reason:</b> {esc(reason)}\n"
            f"{hint}\n\n"
            "I'm <b>not</b> checking BookMyShow right now. I'll retry every 5 minutes "
            "(this warning is sent once)."
            + footer(bms_view(max_age=0)))


def msg_status(kind, status, detail, checks):
    ok = status in ("not_yet", "found", "unsure")
    if not ok:
        title = "🟠 <b>Still running — but the last check had a problem</b>"
        tail = "I'll keep trying. If this keeps happening, tell Claude."
    elif kind == "first":
        title = "🟢 <b>First check done — all good</b>"
        tail = "Sit back and relax — you'll get BOOK NOW + the link the moment it opens."
    else:
        title = "🟢 <b>Daily check-in — still running</b>"
        tail = "Sit back and relax — you'll get BOOK NOW + the link the moment it opens."
    return (f"{title}\n\n"
            f"{watching_line()}\n"
            f"🔎 <b>Last check:</b> {esc(STATUS_WORDS.get(status, status))}\n"
            f"↳ {esc(detail)}\n"
            f"🔢 <b>Checks this session:</b> {checks}\n\n"
            f"{tail}"
            + footer(bms_view()))


def msg_book_now(detail, n):
    return ("🚨🎟️ <b>BOOK NOW! Shows are open</b>\n\n"
            f"{watching_line()}\n"
            f"✅ {esc(detail)}\n\n"
            f"👉 <b>Book here:</b> {esc(TARGET_URL)}\n\n"
            f"Alert #{n} · repeats every {BOOK_NOW_EVERY_SECONDS} seconds while the shows are listed. "
            "Turn Tailscale off to stop."
            + footer(bms_view()))


def msg_maybe_open(detail):
    return ("❓ <b>Maybe open — please check manually</b>\n\n"
            f"{watching_line()}\n"
            f"↳ {esc(detail)}\n\n"
            f"👉 <b>Check here:</b> {esc(TARGET_URL)}\n\n"
            "I found your theatre and format but couldn't confirm the date on the page. "
            "Sent once per session."
            + footer(bms_view()))


def msg_pages_failing(detail):
    return ("⚠️ <b>BookMyShow pages keep failing to load</b>\n\n"
            "Your phone is connected, but the last 5 checks failed.\n"
            f"Last error: <code>{esc(detail[:300])}</code>\n\n"
            "I'll keep trying. You'll only hear about this again if it recovers and then fails again."
            + footer(bms_view(max_age=0)))


def msg_blocked():
    return ("⚠️ <b>BookMyShow is blocking the checks</b>\n\n"
            "The last 3 checks were blocked, even through your phone.\n"
            "Slowing down to one check every 5 minutes. "
            "You'll only hear about this again if it recovers and then gets blocked again."
            + footer(bms_view(max_age=0)))


def msg_test(results, detail):
    ok = all(s in ("not_yet", "found", "unsure") for s in results)
    title = "🧪 <b>Test run: OK</b>" if ok else "🧪 <b>Test run: PROBLEM</b>"
    return (f"{title}\n\n"
            f"{watching_line()}\n"
            f"🔎 <b>First 3 checks:</b> {esc(', '.join(STATUS_WORDS.get(s, s) for s in results))}\n"
            f"↳ {esc(detail)}"
            + footer(bms_view()))


# ============================ phone & proxy ============================

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


def ensure_proxy():
    """Make sure Chrome's local proxy is running; restart it if it died."""
    if not BMS_PROXY or proxy_running():
        return
    log("Chrome's proxy wasn't running - restarting it.")
    try:
        # started by root, tinyproxy switches itself to the "bmsproxy" user (see config)
        subprocess.run(["sudo", "tinyproxy", "-c", "/etc/tinyproxy/tinyproxy.conf"],
                       capture_output=True, timeout=15)
        time.sleep(2)
    except Exception as e:
        log(f"Could not restart proxy: {e}")


def write_state(state):
    """Remember online/offline for the next run (the workflow saves this file)."""
    try:
        with open("state.txt", "w") as f:
            f.write(state)
    except OSError:
        pass


def maybe_send_heartbeat(status, msg, checks_this_run, force=False):
    """'Still running' message: once a day after HEARTBEAT_HOUR IST,
    or right away when force=True (phone just (re)connected)."""
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

    if send(msg_status("first" if force else "daily", status, msg, checks_this_run)):
        # Counts as today's daily message if it's already past the daily hour,
        # so you don't get a duplicate a minute later.
        if not force or (HEARTBEAT_HOUR is not None and now.hour >= HEARTBEAT_HOUR):
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(today)
            except OSError:
                pass


# ============================ reading the page ============================

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


def check(page, target_url):
    """Returns (status, message). status: found / not_yet / unsure / blocked."""
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
        return "not_yet", "Date not open yet (BookMyShow redirected to another date)."

    # Read the row of dates on the page (e.g. "05 nov", "thu 06 nov", "nov 7").
    # BookMyShow only lists dates that are open for booking.
    dates_on_page = dates_listed(text)
    shown = ", ".join(d.title() for d in dates_on_page)  # "5 Nov, 6 Nov" for messages
    log(f"Dates listed on page: {shown or '(could not read)'}")
    target_label = f"{int(TARGET_DATE[6:8])} {MONTHS[int(TARGET_DATE[4:6]) - 1]}"
    pretty_target = target_label.title()
    if dates_on_page and target_label not in dates_on_page:
        return "not_yet", (f"Date not open yet ({pretty_target} isn't in the date list; "
                           f"open dates: {shown}).")

    if THEATRE_NAME:
        name = THEATRE_NAME.lower()
        if name not in text:
            return "not_yet", f"Date is open, but {THEATRE_NAME} isn't listed yet."
        if FORMAT_KEYWORD:
            fmt = FORMAT_KEYWORD.lower()
            starts = [m.start() for m in re.finditer(re.escape(name), text)]
            if not any(fmt in text[i:i + 700] for i in starts):
                return "not_yet", f"{THEATRE_NAME} is listed, but no {FORMAT_KEYWORD} shows yet."
            found_msg = f"{FORMAT_KEYWORD} shows open at {THEATRE_NAME} on {pretty_target}!"
        else:
            found_msg = f"Shows open at {THEATRE_NAME} on {pretty_target}!"
    else:
        found_msg = f"Shows are open on {pretty_target}!"

    if not dates_on_page:
        # Couldn't read the date row, so we can't be sure -> "unsure", not "found".
        return "unsure", found_msg + " (couldn't confirm the date on the page)"
    return "found", found_msg + f" (open dates: {shown})"


# ============================ main loop ============================

def main():
    from playwright.sync_api import sync_playwright  # only needed for checking

    log(f"Watching: {TARGET_URL}")
    stop_at = time.time() + RUN_FOR_MINUTES * 60
    test_results = []
    checks = 0
    unsure_sent = False
    errors_in_a_row = 0
    blocked_in_a_row = 0
    book_now_count = 0

    with sync_playwright() as p:
        phone_misses = 0
        phone_online = True          # the workflow only starts us when the phone is online
        just_connected = JUST_CONNECTED
        checks_since_connect = 0
        error_warned = False
        while time.time() < stop_at:
            # ---------- Is the phone still there? ----------
            if not phone_reachable():
                phone_misses += 1
                log(f"Phone not reachable ({phone_misses}/3)" if phone_online
                    else "Phone still offline - checking again in 1 min")
                if phone_online and phone_misses >= 3:
                    phone_online = False
                    write_state("offline")
                    send(msg_disconnected())
                    log("Phone disconnected - waiting for it to come back.")
                time.sleep(15 if phone_online else 60)
                continue
            phone_misses = 0

            if not phone_online:
                # Phone came back during this run
                phone_online = True
                just_connected = True
                checks_since_connect = 0
                write_state("online")
                ensure_proxy()
                send(msg_connected(reconnected=True))
                log("Phone reconnected.")

            # ---------- Check BookMyShow ----------
            # A brand-new browser with NO cookies for every check.
            # (BookMyShow allows the first visit but flags the cookie it sets,
            #  so reusing one browser got every later check blocked.)
            ensure_proxy()
            browser = None
            try:
                launch_args = {"headless": False}
                if BMS_PROXY:
                    launch_args["proxy"] = {"server": BMS_PROXY}
                browser = p.chromium.launch(**launch_args)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                )
                page = context.new_page()
                status, msg = check(page, TARGET_URL)
                errors_in_a_row = 0
            except Exception as e:
                status, msg = "error", f"Check failed: {e}"
                errors_in_a_row += 1
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
            log(f"{status.upper()}: {msg}")
            checks += 1
            checks_since_connect += 1

            if checks_since_connect == 1 and just_connected:
                # Phone just (re)connected: confirm the first real check worked.
                maybe_send_heartbeat(status, msg, checks, force=True)
                just_connected = False
            else:
                maybe_send_heartbeat(status, msg, checks)

            # Manual test run: report after 3 checks, so you know repeated
            # checks get through too (not just the first one).
            if MANUAL_RUN and len(test_results) < 3:
                test_results.append(status)
                if len(test_results) == 3:
                    send(msg_test(test_results, msg))

            if status == "found":
                book_now_count += 1
                # Telegram every time; email only for the first alert of this opening
                send(msg_book_now(msg, book_now_count), email=(book_now_count == 1))
            else:
                book_now_count = 0
                if status == "unsure" and not unsure_sent:
                    send(msg_maybe_open(msg))
                    unsure_sent = True

            # Page keeps failing even though the phone answers: warn once, keep going.
            if errors_in_a_row >= 5 and not error_warned:
                send(msg_pages_failing(msg))
                error_warned = True
            if errors_in_a_row == 0:
                error_warned = False

            blocked_in_a_row = blocked_in_a_row + 1 if status == "blocked" else 0
            if blocked_in_a_row == 3:
                send(msg_blocked())

            if status == "found":
                # Shows are open: repeat BOOK NOW every 30 s, re-check the page every minute
                # (so the alerts stop by themselves if the shows disappear).
                time.sleep(BOOK_NOW_EVERY_SECONDS)
                book_now_count += 1
                send(msg_book_now(msg, book_now_count), email=False)
                time.sleep(BOOK_NOW_EVERY_SECONDS)
                continue

            wait = CHECK_EVERY_SECONDS * (5 if blocked_in_a_row >= 3 else 1)
            time.sleep(wait + random.randint(0, 15))

    log("Run finished; the schedule will start the next one.")


def notify(kind):
    """Messages sent by the workflow itself (no Chrome / extra installs needed)."""
    builders = {
        "connected": msg_connected,
        "disconnected": msg_disconnected,
        "routing_failed": msg_routing_failed,
    }
    if kind not in builders:
        log(f"Unknown notify kind: {kind}")
        return
    ok = send(builders[kind]())
    log(f"Sent '{kind}' message: {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "notify":
        try:
            notify(sys.argv[2])
        except Exception as e:  # a message must never break the workflow
            log(f"notify failed: {e}")
        sys.exit(0)
    main()
