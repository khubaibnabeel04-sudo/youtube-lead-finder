import asyncio
import re
import os
import json
import tkinter as tk
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from google.oauth2 import service_account
from googleapiclient.discovery import build
from patchright.async_api import async_playwright

# --- CONFIG ---

SHEET_ID        = os.environ.get("GOOGLE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID_HERE")
SHEET_NAME      = '5. Needs Email'
KEY_PATH        = Path(__file__).parent / 'keys' / 'google-service-account.json'
SCOPES          = ['https://www.googleapis.com/auth/spreadsheets']

PROFILES_DIR    = os.environ.get("CHROME_PROFILES_DIR", "chrome_profiles")
RATE_LIMIT_TEXT = "you've reached today's access limit"
SETUP_FILE      = os.path.join(PROFILES_DIR, "setup_complete.json")

# Comma-separated in .env: the Gmail accounts whose Chrome profiles are used to reveal business emails
GMAIL_ACCOUNTS = [{"email": e.strip()} for e in os.environ.get("GMAIL_ACCOUNTS", "").split(",") if e.strip()]

EMAIL_BUTTON_PATTERNS = [
    r"view email address",
    r"for business inquiries",
    r"business inquiries",
    r"business inquiry",
    r"BUSINESS_INQUIRIES",
    r"businessEmail",
    r'"channelContactEmail"',
    r"reveal.*?email",
    r"show email",
    r"contact.*?email",
]

COL_EMAIL     = 0
COL_NAME      = 1
COL_CHANNELID = 2
COL_URL       = 3
COL_SUBS      = 4
COL_COUNTRY   = 5
COL_VIEWS     = 6
COL_BIZEMAIL  = 7
COL_STATUS    = 8

_sheets_service = None

def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    creds = service_account.Credentials.from_service_account_file(
        str(KEY_PATH), scopes=SCOPES
    )
    _sheets_service = build("sheets", "v4", credentials=creds)
    return _sheets_service

def read_sheet():
    """Return all rows from the Needs Email sheet (first element = header)."""
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!A:ZZ",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    return result.get("values", [])

def update_cell(row_index, col_index, value):
    """Update a single cell. row_index 0 = first data row (not header)."""
    service = get_sheets_service()
    sheet_row = row_index + 2
    col_letter = chr(65 + col_index)
    range_str = f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_str,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()

class FloatingButton:
    def __init__(self):
        self._event = asyncio.Event()
        self._root = None
        self._thread = None
        self._action = "grab"
        self._loop = asyncio.get_event_loop()

    def _build(self):
        self._root = tk.Tk()
        self._root.title("")
        self._root.attributes("-topmost", True)
        self._root.overrideredirect(True)
        self._root.geometry("230x145+20+20")
        self._root.configure(bg="#1a1a2e")

        tk.Label(
            self._root, text="YouTube Email Scraper",
            bg="#1a1a2e", fg="#a0a0c0", font=("Arial", 9)
        ).pack(pady=(8, 2))

        tk.Button(
            self._root,
            text=" Email Revealed - Grab It",
            bg="#00b894", fg="white",
            font=("Arial", 10, "bold"),
            relief="flat", cursor="hand2",
            padx=10, pady=6,
            command=self._on_grab
        ).pack(fill="x", padx=10, pady=2)

        tk.Button(
            self._root,
            text=" Skip This Channel",
            bg="#0984e3", fg="white",
            font=("Arial", 9),
            relief="flat", cursor="hand2",
            padx=10, pady=4,
            command=self._on_skip
        ).pack(fill="x", padx=10, pady=2)

        tk.Button(
            self._root,
            text=" Rate Limited - Switch Account",
            bg="#d63031", fg="white",
            font=("Arial", 9),
            relief="flat", cursor="hand2",
            padx=10, pady=4,
            command=self._on_rate_limit
        ).pack(fill="x", padx=10, pady=2)

        self._root.mainloop()

    def _on_grab(self):
        self._action = "grab"
        self._loop.call_soon_threadsafe(self._event.set)

    def _on_skip(self):
        self._action = "skip"
        self._loop.call_soon_threadsafe(self._event.set)

    def _on_rate_limit(self):
        self._action = "rate_limited"
        self._loop.call_soon_threadsafe(self._event.set)

    def show(self):
        self._event.clear()
        self._action = "grab"
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._build, daemon=True)
            self._thread.start()

    async def wait_for_click(self):
        await self._event.wait()
        return self._action

    def close(self):
        if self._root:
            try:
                self._root.quit()
                self._root.destroy()
            except Exception:
                pass
            self._root = None
            self._thread = None

def load_setup_state():
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE, "r") as f:
            return json.load(f)
    return {}

def mark_setup_complete(email):
    state = load_setup_state()
    state[email] = True
    with open(SETUP_FILE, "w") as f:
        json.dump(state, f, indent=2)

def is_setup_complete(email):
    return load_setup_state().get(email, False)

async def ensure_logged_in(page, email):
    await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
    await asyncio.sleep(1)
    already_in = await page.locator("button#avatar-btn").count()
    if already_in > 0:
        print(f"  [Login] Session active for {email}.")
        return True
    await page.goto("https://accounts.google.com/signin", wait_until="networkidle")
    print(f"\n  [Action Required] Please log into {email} in the browser.")
    print(f"  Make sure you end up on YouTube when done.")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "\n  >>> Press Enter once logged into YouTube: ")
    await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    signed_in = await page.locator("button#avatar-btn").count()
    if signed_in == 0:
        print("  [Warning] Still not signed in. Skipping.")
        return False
    print(f"  [Login] YouTube session confirmed for {email}.")
    return True

async def ensure_capsolver_installed(page, email):
    if is_setup_complete(email):
        print(f"  [Extension] Already set up for {email}, skipping.")
        return
    await page.goto("chrome://extensions", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    content = await page.content()
    if "capsolver" in content.lower() or "pgojnojmmhpofjgdmaebadhbocahppod" in content.lower():
        print(f"  [Extension] CapSolver found for {email}.")
        mark_setup_complete(email)
        return
    print(f"\n  [Action Required] Please install CapSolver extension for {email}.")
    print(f"  1. https://chromewebstore.google.com/detail/capsolver-captcha-solver/pgojnojmmhpofjgdmaebadhbocahppod")
    print(f"  2. Add to Chrome -> paste API key in settings -> Save")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "\n  >>> Press Enter once done: ")
    mark_setup_complete(email)
    print(f"  [Extension] Setup complete for {email}.")

def page_has_email_button(html):
    for pattern in EMAIL_BUTTON_PATTERNS:
        if re.search(pattern, html, re.IGNORECASE):
            return True
    return False

async def grab_email(page):
    try:
        dialog = page.locator("tp-yt-paper-dialog, ytd-popup-container")
        if await dialog.count() > 0:
            dialog_text = await dialog.first.inner_text()
            match = re.search(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                dialog_text
            )
            if match:
                email = match.group(0)
                if "youtube" not in email and "google" not in email:
                    return email
    except Exception:
        pass
    content = await page.content()
    for email in re.findall(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        content
    ):
        if "youtube" not in email and "google" not in email and "sentry" not in email:
            return email
    return None

async def scrape():
    os.makedirs(PROFILES_DIR, exist_ok=True)
    all_data = read_sheet()
    if not all_data or len(all_data) < 2:
        print("[Error] Sheet is empty or has no data rows.")
        return
    rows = all_data[1:]
    total = len(rows)
    print(f"[Start] {total} channels to process from sheet '{SHEET_NAME}'.\n")
    btn = FloatingButton()
    async with async_playwright() as p:
        for account in GMAIL_ACCOUNTS:
            print(f"\n{'='*50}")
            print(f"[Account] Switching to: {account['email']}")
            print(f"{'='*50}")
            profile_dir = os.path.join(
                PROFILES_DIR, account["email"].split("@")[0]
            )
            os.makedirs(profile_dir, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()
            login_ok = False
            try:
                login_ok = await ensure_logged_in(page, account["email"])
            except Exception as e:
                print(f"  [Login Error] {e}")
            if not login_ok:
                print(f"  [Skip] Could not confirm login for {account['email']}.")
                await context.close()
                continue
            await ensure_capsolver_installed(page, account["email"])
            await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            account_hit_limit = False
            for data_idx, row in enumerate(rows):
                while len(row) < COL_STATUS + 1:
                    row.append("")
                existing_email = (row[COL_EMAIL] or "").strip()
                status_val = (row[COL_STATUS] or "").strip().lower()
                if existing_email:
                    print(f"[{data_idx+1}/{total}] Already has email - skipping.")
                    continue
                if status_val in ("accepted", "rejected"):
                    print(f"[{data_idx+1}/{total}] Status is '{status_val}' - skipping.")
                    continue
                channel_url = (row[COL_URL] or "").strip()
                if not channel_url:
                    print(f"[{data_idx+1}/{total}] No URL - skipping.")
                    continue
                print(f"\n[{data_idx+1}/{total}] {channel_url}")
                about_url = channel_url.rstrip("/") + "/about"
                loaded = False
                for attempt in range(3):
                    try:
                        await page.goto(about_url, wait_until="domcontentloaded", timeout=15000)
                        loaded = True
                        break
                    except Exception:
                        print(f"  [Retry] Timeout, attempt {attempt + 1}/3...")
                        await asyncio.sleep(2)
                if not loaded:
                    print("  [Skip] Failed after 3 attempts.")
                    continue
                await asyncio.sleep(2)
                html = await page.content()
                if not page_has_email_button(html):
                    print("  [Skip] No email button in page source.")
                    continue
                print('  [Waiting] Click "View email address" then Submit.')
                print("  Then press the appropriate button on the overlay.")
                btn.show()
                action = await btn.wait_for_click()
                if action == "rate_limited":
                    print("  [Rate Limit] Switching account...")
                    account_hit_limit = True
                    break
                if action == "skip":
                    print("  [Skipped] Marking as Rejected.")
                    update_cell(data_idx, COL_STATUS, "Rejected")
                    rows[data_idx][COL_STATUS] = "Rejected"
                    continue
                email = await grab_email(page)
                if email:
                    print(f"  [Found] {email}")
                    update_cell(data_idx, COL_EMAIL, email)
                    update_cell(data_idx, COL_STATUS, "Accepted")
                    rows[data_idx][COL_EMAIL] = email
                    rows[data_idx][COL_STATUS] = "Accepted"
                else:
                    print("  [Not Found] Could not extract email.")
                await asyncio.sleep(1)
            await context.close()
            if not account_hit_limit:
                print("\n[Done] All channels processed!")
                break
    btn.close()
    print("\n[Finished] Scraping complete.")

if __name__ == "__main__":
    asyncio.run(scrape())
