"""
auto_email_scraper.py — "Email Scraper with AI"
================================================
Fully automatic version of business_email_scraper.py. Same accounts, same
Google Sheet, same Chrome profiles — but nobody has to click anything:

  1. Opens each channel's About page and waits for it to fully load.
  2. If the channel's description already shows the email in plain text,
     grabs it straight from there — no button, no captcha.
  3. Otherwise finds the "View email address" button by scanning the live
     DOM text (scrolling the About panel between attempts, since the button
     is often below the fold or lazy-rendered).
  4. Clicks it. The CapSolver extension solves the reCAPTCHA on its own.
  5. Keeps checking that the About popup is actually still open (a stray
     click outside it closes it while the URL keeps saying /about, so the
     DOM is asked, not the URL). If it got closed, the page is refreshed
     and the whole click flow is retried.
  6. Watches for the solved tick two ways: the checkbox's aria-checked
     attribute, and (fallback) an OpenCV green-pixel check on a screenshot
     of the checkbox iframe — in case the DOM doesn't expose it.
  7. Clicks Submit, extracts the revealed email, saves it to the sheet
     (what the "Email Revealed - Grab It" overlay button did).
  8. Detects YouTube's "reached today's access limit" message and switches
     to the next Gmail account automatically (what the "Rate Limited -
     Switch Account" overlay button did).

Progress is written to auto_email_status.json for the app UI.
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import cv2
import numpy as np
from patchright.async_api import async_playwright

# Reuse the manual scraper's config + sheet helpers (no import-time side
# effects — it only runs under __main__).
from business_email_scraper import (
    GMAIL_ACCOUNTS, PROFILES_DIR, RATE_LIMIT_TEXT,
    COL_EMAIL, COL_URL, COL_STATUS,
    read_sheet, update_cell, page_has_email_button, grab_email,
)

STATUS_FILE = os.path.join(os.path.dirname(__file__), "auto_email_status.json")

PAGE_SETTLE_WAIT   = (4.0, 6.0)  # after opening the About page
BUTTON_TRIES       = 6           # scroll-and-look attempts for the email button
CAPTCHA_WAIT_MAX   = 90          # seconds to wait for CapSolver's tick
MAX_CAPTCHA_FAILS  = 3           # consecutive unsolved captchas -> switch account
ABOUT_REOPEN_TRIES = 2           # refresh-and-retry attempts if the About popup got closed

# ──────────────────────────────────────────────
# STATUS FILE (read by /api/email-ai/status)
# ──────────────────────────────────────────────

_status = {
    "phase": "starting",
    "account": "",
    "account_idx": 0,
    "total_accounts": len(GMAIL_ACCOUNTS),
    "row": 0,
    "total_rows": 0,
    "emails_found": 0,
    "last_email": "",
    "last_channel": "",
    "message": "",
}

def write_status(**kw):
    _status.update(kw)
    _status["updated_at"] = datetime.now().isoformat()
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_status, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass

def log(msg):
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass

# ──────────────────────────────────────────────
# PAGE HELPERS
# ──────────────────────────────────────────────

# Find a visible clickable element whose text contains the needle, scroll it
# into view (works inside the About dialog too), and return its center point.
# The elementFromPoint check makes sure the click would actually land on the
# button — never on the dimmed backdrop, which would close the About popup.
LOCATE_TEXT_JS = """(needle) => {
  const els = document.querySelectorAll(
    'button, tp-yt-paper-button, a, yt-button-shape, ytd-button-renderer'
  );
  for (const el of els) {
    const t = (el.innerText || '').trim().toLowerCase();
    if (t && t.length < 80 && t.includes(needle)) {
      el.scrollIntoView({block: 'center'});
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) {
        const cx = r.x + r.width / 2;
        const cy = r.y + r.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        if (hit && (el.contains(hit) || hit.contains(el))) {
          return {x: cx, y: cy};
        }
      }
    }
  }
  return null;
}"""

# Scroll whatever is scrollable inside the About dialog (fall back to the
# page itself) so lazy-rendered content below the fold shows up.
SCROLL_PANEL_JS = """() => {
  const dialog = document.querySelector(
    'tp-yt-paper-dialog, ytd-engagement-panel-section-list-renderer'
  );
  const root = dialog || document.body;
  let scrolled = false;
  for (const el of root.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 40) {
      el.scrollTop += 400;
      scrolled = true;
    }
  }
  if (!scrolled) window.scrollBy(0, 400);
}"""

async def find_and_click_text(page, needle, tries=BUTTON_TRIES):
    """Look for a button containing `needle`; scroll and retry if hidden."""
    for _ in range(tries):
        try:
            pos = await page.evaluate(LOCATE_TEXT_JS, needle)
        except Exception:
            pos = None
        if pos:
            await asyncio.sleep(0.4)  # let scrollIntoView settle
            try:
                # Re-locate after the settle; never click stale coordinates —
                # a re-render can move the button and a blind click on old
                # coords is exactly what closes the About popup.
                fresh = await page.evaluate(LOCATE_TEXT_JS, needle)
                if fresh:
                    await page.mouse.click(fresh["x"], fresh["y"])
                    return True
            except Exception:
                return False
        try:
            await page.evaluate(SCROLL_PANEL_JS)
        except Exception:
            pass
        await asyncio.sleep(1.2)
    return False

# Is the About popup (or the captcha dialog it spawns) actually visible?
# Clicking outside the popup closes it but the URL keeps saying /about, so
# the DOM is asked instead of the URL. When YouTube closes the popup it
# hides it (display:none / zero-size rect), which this catches.
ABOUT_OPEN_JS = """() => {
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  };
  const sel = [
    'ytd-about-channel-renderer',
    'ytd-engagement-panel-section-list-renderer',
    'ytd-popup-container tp-yt-paper-dialog',
    'iframe[src*="recaptcha"][src*="anchor"]',
  ].join(', ');
  for (const el of document.querySelectorAll(sel)) {
    if (visible(el)) return true;
  }
  return false;
}"""

async def about_is_open(page):
    try:
        return bool(await page.evaluate(ABOUT_OPEN_JS))
    except Exception:
        return False

ABOUT_TEXT_JS = """() => {
  const el = document.querySelector('ytd-about-channel-renderer');
  return el ? el.innerText : '';
}"""

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

async def email_in_description(page):
    """Some channels write their email straight into the About description —
    no captcha needed then. Returns the email if it's there, else None."""
    try:
        text = await page.evaluate(ABOUT_TEXT_JS) or ""
    except Exception:
        return None
    for email in EMAIL_RE.findall(text):
        low = email.lower()
        if "youtube" not in low and "google" not in low and "sentry" not in low:
            return email
    return None

async def hit_rate_limit(page):
    try:
        text = await page.evaluate("() => document.body.innerText.toLowerCase()")
        return "access limit" in text or RATE_LIMIT_TEXT.lower() in text
    except Exception:
        return False

async def captcha_tick_dom(page):
    """Code check: the reCAPTCHA checkbox reports aria-checked=true."""
    for frame in page.frames:
        if "recaptcha" in frame.url and "anchor" in frame.url:
            try:
                el = await frame.query_selector("#recaptcha-anchor")
                if el and (await el.get_attribute("aria-checked")) == "true":
                    return True
            except Exception:
                pass
    return False

async def captcha_tick_visual(page):
    """Picture check: screenshot the checkbox iframe and look for the green
    tick with OpenCV — covers cases where the DOM doesn't expose the state."""
    try:
        el = await page.query_selector('iframe[src*="recaptcha"][src*="anchor"]')
        if not el:
            return False
        png = await el.screenshot()
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (40, 80, 80), (85, 255, 255))
        return int(green.sum() / 255) > 150
    except Exception:
        return False

async def wait_for_captcha(page):
    """Wait until CapSolver produces the tick. Returns 'solved',
    'rate_limited', 'already_revealed', 'about_closed', or 'timeout'."""
    deadline = time.monotonic() + CAPTCHA_WAIT_MAX
    while time.monotonic() < deadline:
        if await hit_rate_limit(page):
            return "rate_limited"
        # A stray click may have closed the About popup — the captcha keeps
        # solving in the hidden iframe but the tick/Submit are unreachable,
        # so bail out early and let the caller refresh + retry.
        if not await about_is_open(page):
            return "about_closed"
        # Occasionally the email shows without a captcha at all
        if not await page.query_selector('iframe[src*="recaptcha"][src*="anchor"]'):
            if await grab_email(page):
                return "already_revealed"
        if await captcha_tick_dom(page) or await captcha_tick_visual(page):
            return "solved"
        await asyncio.sleep(1.0)
    return "timeout"

async def extract_email_with_retries(page, tries=5):
    for _ in range(tries):
        email = await grab_email(page)
        if email:
            return email
        await asyncio.sleep(1.5)
    return None

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

async def load_about_page(page, about_url):
    for attempt in range(3):
        try:
            await page.goto(about_url, wait_until="domcontentloaded", timeout=15000)
            return True
        except Exception:
            log(f"  [Retry] Timeout, attempt {attempt + 1}/3...")
            await asyncio.sleep(2)
    return False

def save_email(data_idx, email, rows):
    update_cell(data_idx, COL_EMAIL, email)
    update_cell(data_idx, COL_STATUS, "Accepted")
    rows[data_idx][COL_EMAIL] = email
    rows[data_idx][COL_STATUS] = "Accepted"

async def process_row(page, data_idx, total, row, rows):
    """Handle one channel. Returns 'saved', 'skipped', 'failed',
    'captcha_timeout', or 'rate_limited'."""
    channel_url = (row[COL_URL] or "").strip()
    about_url = channel_url.rstrip("/") + "/about"
    log(f"\n[{data_idx+1}/{total}] {channel_url}")
    write_status(row=data_idx + 1, last_channel=channel_url, message="")

    if not await load_about_page(page, about_url):
        log("  [Skip] Page failed to load after 3 attempts.")
        return "failed"
    # Let YouTube fully render the channel + About dialog before touching it
    await asyncio.sleep(random.uniform(*PAGE_SETTLE_WAIT))

    # First pass + up to ABOUT_REOPEN_TRIES refreshes if the popup got closed
    for attempt in range(1 + ABOUT_REOPEN_TRIES):
        if attempt:
            log("  [Reopen] About popup got closed — refreshing and retrying...")
            write_status(message="about popup closed, refreshing...")
            if not await load_about_page(page, about_url):
                return "failed"
            await asyncio.sleep(random.uniform(*PAGE_SETTLE_WAIT))

        if not await about_is_open(page):
            continue  # popup never showed up -> refresh and try again

        # Email written straight into the description? Then no captcha needed.
        email = await email_in_description(page)
        if email:
            log(f"  [Found] {email} (straight from the description, no captcha)")
            save_email(data_idx, email, rows)
            return "saved"

        html = await page.content()
        if not page_has_email_button(html):
            log("  [Skip] No email button in page source.")
            return "skipped"

        write_status(message="looking for View email address button...")
        if not await find_and_click_text(page, "view email"):
            log("  [Fail] Could not find the View email address button.")
            return "failed"
        log("  [Clicked] View email address — waiting for CapSolver...")

        await asyncio.sleep(2.5)
        write_status(message="waiting for captcha tick...")
        result = await wait_for_captcha(page)

        if result == "rate_limited":
            return "rate_limited"
        if result == "about_closed":
            continue  # refresh and run the whole click flow again
        if result == "timeout":
            log("  [Fail] Captcha never got solved (90s).")
            return "captcha_timeout"

        if result == "solved":
            # Last sanity check before Submit — popup must still be on screen
            if not await about_is_open(page):
                continue
            log("  [Tick] Captcha solved — clicking Submit...")
            write_status(message="captcha solved, submitting...")
            if not await find_and_click_text(page, "submit", tries=3):
                log("  [Fail] Could not find the Submit button.")
                return "failed"
            await asyncio.sleep(random.uniform(2.0, 3.0))
            if await hit_rate_limit(page):
                return "rate_limited"

        email = await extract_email_with_retries(page)
        if not email:
            log("  [Not Found] Could not extract email after submit.")
            return "failed"

        log(f"  [Found] {email}")
        save_email(data_idx, email, rows)
        return "saved"

    log(f"  [Fail] About popup kept closing ({ABOUT_REOPEN_TRIES} refreshes tried).")
    return "failed"

async def main():
    write_status(phase="starting")
    all_data = read_sheet()
    if not all_data or len(all_data) < 2:
        write_status(phase="error", message="Sheet is empty or has no data rows")
        log("[Error] Sheet is empty or has no data rows.")
        return
    rows = all_data[1:]
    total = len(rows)
    write_status(total_rows=total)
    log(f"[Start] {total} channels to process (fully automatic).")

    emails_found = 0
    done_idx = set()  # rows finished this run (any terminal outcome)

    async with async_playwright() as p:
        for acc_idx, account in enumerate(GMAIL_ACCOUNTS):
            email_acc = account["email"]
            log(f"\n{'='*50}\n[Account] {email_acc}\n{'='*50}")
            write_status(phase="logging_in", account=email_acc,
                         account_idx=acc_idx, message="")

            profile_dir = os.path.join(PROFILES_DIR, email_acc.split("@")[0])
            os.makedirs(profile_dir, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    # Chrome stops painting + throttles JS when the window is
                    # minimized or covered — which stalls CapSolver, the tick
                    # detection, and lazy rendering. These keep it fully alive
                    # in the background:
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
                ],
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            try:
                await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
            except Exception:
                pass
            # Give Chrome + the CapSolver extension time to fully come up
            await asyncio.sleep(random.uniform(4.0, 6.0))

            if await page.locator("button#avatar-btn").count() == 0:
                log(f"  [Skip] {email_acc} is not signed in — next account.")
                write_status(message=f"{email_acc} not signed in — skipped")
                await context.close()
                continue
            log(f"  [Login] Session active for {email_acc}.")

            write_status(phase="scraping")
            account_hit_limit = False
            captcha_fails = 0

            for data_idx, row in enumerate(rows):
                if data_idx in done_idx:
                    continue
                while len(row) < COL_STATUS + 1:
                    row.append("")
                if (row[COL_EMAIL] or "").strip():
                    done_idx.add(data_idx)
                    continue
                if (row[COL_STATUS] or "").strip().lower() in ("accepted", "rejected"):
                    done_idx.add(data_idx)
                    continue
                if not (row[COL_URL] or "").strip():
                    done_idx.add(data_idx)
                    continue

                outcome = await process_row(page, data_idx, total, row, rows)

                if outcome == "rate_limited":
                    log("  [Rate Limit] Access limit reached — switching account.")
                    account_hit_limit = True
                    break  # this row is NOT marked done -> next account retries it
                if outcome == "captcha_timeout":
                    captcha_fails += 1
                    if captcha_fails >= MAX_CAPTCHA_FAILS:
                        log(f"  [Switch] {MAX_CAPTCHA_FAILS} unsolved captchas in a row — "
                            "treating as limited, switching account.")
                        account_hit_limit = True
                        break
                    # row stays unfinished -> retried later by another account
                    continue
                captcha_fails = 0
                done_idx.add(data_idx)
                if outcome == "saved":
                    emails_found += 1
                    write_status(emails_found=emails_found,
                                 last_email=rows[data_idx][COL_EMAIL])
                await asyncio.sleep(random.uniform(1.0, 2.0))

            await context.close()
            if not account_hit_limit:
                log("\n[Done] All channels processed!")
                break

    write_status(phase="finished", message=f"{emails_found} emails saved")
    log(f"\n[Finished] {emails_found} emails saved to the sheet.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        write_status(phase="error", message=str(e))
        raise
