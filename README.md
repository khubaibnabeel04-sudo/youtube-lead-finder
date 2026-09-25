# YouTube Lead Finder

A lead-generation engine that discovers YouTube creators in high-RPM niches, qualifies them automatically, and extracts their business contact emails, ready for cold outreach. A browser crawler "hops" through YouTube search results and recommendation sidebars to harvest channels. A FastAPI backend validates each one against the YouTube Data API (subscriber range, upload activity, country, niche fit) and de-duplicates it against every lead I've already contacted. Qualified channels land in a React review queue. I built it because finding the right creators by hand was the slowest part of my outreach. It now keeps my cold-email pipeline supplied with fresh, pre-qualified leads every day.

## Results

- Supplies the lead pipeline behind **~2,000 new leads / month** and **~100 cold emails / day**, at a **~4.5% reply rate**
- Replaces hours of manual YouTube browsing, spreadsheet checking and copy-pasting. It's part of a system that saves me **10–20 hours / week**

## Tech stack

- **Backend:** Python, FastAPI, Uvicorn, httpx (async), pandas / openpyxl
- **Browser automation:** Patchright / Playwright driving persistent Chrome profiles
- **Computer vision:** OpenCV + the YuNet face-detection model, which scores thumbnails (creator-led videos show a face)
- **AI:** Groq API (`gpt-oss-20b`), which picks the next video to hop to for the most topic novelty
- **Google APIs:** YouTube Data API v3 (multi-key rotation with quota tracking), Google Sheets API (service account)
- **Frontend:** React 18, axios
- **Other:** yt-dlp, psutil

## How it works

```
  keywords.txt
       │
       ▼
 ┌─────────────────────────┐   channel IDs / @handles / video IDs
 │  Crawler (Chrome)       │ ─────────────────────────────────────┐
 │  hop_scraper.py  manual │                                      │
 │  hop_assist.py   you pick from an in-page overlay              │
 │  auto_hop.py     fully automatic:                              │
 │     text niche scoring + face detection + Groq picker          │
 └─────────────────────────┘                                      ▼
                                              ┌──────────────────────────────┐
     Google Sheet (all past leads) ─────────► │  FastAPI backend  :8000      │
          dedup by ID / URL / name            │  • validation queue          │
                                              │  • YouTube Data API checks   │
                                              │  • hard filters + niche score│
                                              │  • API-key rotation          │
                                              └──────────────┬───────────────┘
                                                             │ qualified / borderline
                                                             ▼
                                              ┌──────────────────────────────┐
                                              │  React review UI  :3000      │
                                              │  accept / reject, stats, logs│
                                              └──────────────┬───────────────┘
                                                             │ accepted
                                                             ▼
                                   Email scrapers (logged-in Chrome profiles)
                                   reveal the "business inquiries" email
                                   → written back to the Google Sheet / Excel export
```

1. **Discover.** Starting from a keyword list, the crawler harvests every channel on search pages and watch-page sidebars. In AI mode it only considers organic results (never ads), rejects off-niche and non-English titles, boosts thumbnails with a creator's face, and asks an LLM which candidate opens the most new topic territory. That stops YouTube from recycling the same channels.
2. **Qualify.** The backend resolves each handle or video to a channel and pulls stats for 3–4 API units per channel. It then applies hard filters: 8K–5M subscribers, an upload in the last 60 days, and an English-speaking market (US/UK/CA/AU/NZ/IE). A weighted niche score finishes the job. Channels that pass everything except niche go to a "borderline" list instead of being thrown away.
3. **De-duplicate.** Every candidate is checked against all tabs of the master Google Sheet, so nobody gets contacted twice.
4. **Review.** Qualified channels appear in the React dashboard for a quick accept or reject.
5. **Enrich.** For accepted channels, the email scraper (`auto_email_scraper.py`) opens each channel's About page in a real logged-in Chrome profile, grabs a plain-text email or reveals the "View email address" one, rotates accounts when YouTube's daily limit hits, and writes results back to the sheet.

## Setup

**Prerequisites:** Python 3.11+, Node.js 18+, Google Chrome, and a Google Cloud project with the YouTube Data v3 and Sheets APIs enabled.

```bash
git clone https://github.com/<your-username>/youtube-lead-finder.git
cd youtube-lead-finder

python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r backend/requirements.txt
patchright install chromium

cd frontend && npm install && cd ..

cp .env.example .env             # then fill in your keys
```

Credentials:

1. **YouTube and Groq keys, Sheet ID:** put these in `.env`.
2. **Service account (Sheets):** save the JSON key as `backend/keys/google-service-account.json` (gitignored) and share your sheet with the service account's email.
3. **Email scraper (optional):** set `GMAIL_ACCOUNTS` and `CHROME_PROFILES_DIR`. Log in to each account once in its Chrome profile.

Run it:

```bash
# Windows: double-click "Start Outreach.bat", or:
bash start_servers.sh
```

This starts the backend at http://localhost:8000 and the dashboard at http://localhost:3000. Start a crawl from the dashboard, or run `python backend/hop_scraper.py` for manual mode.

## Project structure

```
backend/main.py                 FastAPI app: validation queue, filters, dedup, exports
backend/hop_scraper.py          Manual crawler
backend/hop_assist.py           Assisted crawler + in-page picker (hop_overlay.py)
backend/auto_hop.py             Fully automatic crawler (scoring, face detection)
backend/hop_groq.py             LLM "next video" picker
backend/auto_email_scraper.py   Automatic business-email extraction
backend/business_email_scraper.py  Semi-manual version of the above
backend/email_checker.py        Checks whether a channel exposes a business email
backend/recover_rejected.py     One-off maintenance scripts
backend/dedup_recovered.py
backend/keywords.txt            Seed search keywords
backend/models/                 YuNet face-detection model (OpenCV Zoo, MIT)
frontend/                       React review dashboard
```

## Notes

- All scraped data, caches and browser profiles stay local and are gitignored.
- Built for personal use. Respect YouTube's Terms of Service and API quotas if you adapt it.
