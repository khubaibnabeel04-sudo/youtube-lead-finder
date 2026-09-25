"""
YouTube Channel Scraper API — Backend
======================================
Receives channels from hop_scraper.py, validates via YouTube Data API v3,
surfaces only qualified channels to the UI for review.

Quota per channel:
  @handle   : 4 units  (resolve handle + channels.list + playlistItems + videos)
  UC... ID  : 3 units  (channels.list + playlistItems + videos)
  video_id  : 4 units  (resolve video + channels.list + playlistItems + videos)
"""

from fastapi import FastAPI, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import pandas as pd
import os
import re
import sys
import asyncio
import io
import csv
import json
import httpx
import subprocess
import time
import psutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Lock
from pydantic import BaseModel
from typing import List, Optional
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as SheetsHttpError
from email_checker import EmailChecker
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

app = FastAPI(title="YouTube Channel Scraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------
# CONFIG
# ----------------------------------------------

DATA_DIR       = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE     = os.path.join(DATA_DIR, "state.json")
LEADS_MASTER   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Leads_Master.xlsx")
os.makedirs(DATA_DIR, exist_ok=True)

MIN_SUBS          = 8_000
MAX_SUBS          = 5_000_000
ACTIVITY_DAYS     = 60
ALLOWED_COUNTRIES = {"US", "GB", "CA", "AU", "NZ", "IE"}
MIN_NICHE_SCORE   = 2

YT_API_BASE = "https://www.googleapis.com/youtube/v3"

# ----------------------------------------------
# GOOGLE SHEETS DEDUP CONFIG
# ----------------------------------------------

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID_HERE")

SHEET_NAMES = [
    "1. Old Leads (App)",
    "2. New Leads (App)",
    "Sent",
    "No Reply",
    "5. Needs Email",
    "new_leads_reason",
    "old_leads_reason",
]

class SheetDedupManager:
    """Reads all sheets from the Google Sheet to build a dedup set of known channels."""

    def __init__(self):
        self.service = None
        self.known_ids = set()       # channel IDs (UC...)
        self.known_names = set()     # channel names (lowercase)
        self.known_urls = set()      # channel URLs
        self.last_refresh = None
        self.error = None
        self._lock = Lock()

    def _get_service(self):
        if self.service:
            return self.service
        key_path = os.path.join(os.path.dirname(__file__), "keys", "google-service-account.json")
        try:
            creds = service_account.Credentials.from_service_account_file(
                key_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
            )
            self.service = build("sheets", "v4", credentials=creds)
        except Exception as e:
            print(f"  [Sheet] Failed to initialize Google Sheets service: {e}")
            self.error = str(e)
            raise
        return self.service

    def refresh(self):
        """Read all sheets and rebuild dedup sets."""
        try:
            service = self._get_service()
        except Exception as e:
            self.error = str(e)
            return {"error": str(e), "ids": 0, "names": 0, "urls": 0}

        new_ids = set()
        new_names = set()
        new_urls = set()

        for sheet_name in SHEET_NAMES:
            try:
                rows = self._read_sheet(service, sheet_name)
                if not rows or len(rows) < 2:
                    print(f"  [Sheet] {sheet_name}: empty or header only ({len(rows) if rows else 0} rows)")
                    continue

                headers = [str(h).strip().lower().replace(" ", "").replace("_", "") for h in rows[0]]
                data = rows[1:]
                print(f"  [Sheet] {sheet_name}: {len(data)} data rows, headers={headers[:6]}...")

                # Find relevant columns by normalized matching
                id_idx = self._find_column(headers, ["channelid", "channel_id"])
                name_idx = self._find_column(headers, ["channelname", "channel_name", "name"])
                url_idx = self._find_column(headers, ["channelurl", "channelurl", "channelurls", "channel_url"])
                email_idx = self._find_column(headers, ["channelemail", "channel_email", "email"])

                for row_idx, row in enumerate(data):
                    # Extract channel ID (column "channelId" or at index 2 for known sheets)
                    val = ""
                    if id_idx is not None and id_idx < len(row):
                        val = str(row[id_idx]).strip()
                    elif sheet_name in ("No Reply",) and len(row) > 2:
                        # No Reply uses column index 2 for channel_id
                        val = str(row[2]).strip()
                    if val and val not in ("none", "null", ""):
                        new_ids.add(val)

                    # Extract channel name
                    val = ""
                    if name_idx is not None and name_idx < len(row):
                        val = str(row[name_idx]).strip().lower()
                    elif sheet_name in ("No Reply",) and len(row) > 0:
                        val = str(row[0]).strip().lower()
                    if val and val not in ("none", "null", ""):
                        new_names.add(val)

                    # Extract URL
                    val = ""
                    if url_idx is not None and url_idx < len(row):
                        val = str(row[url_idx]).strip()
                    if val and val not in ("none", "null", ""):
                        new_urls.add(val)

                    # Use email as fallback name dedup for No Reply sheet
                    if sheet_name in ("No Reply",) and len(row) > 1:
                        email_val = str(row[1]).strip().lower()
                        if email_val and "@" in email_val:
                            new_names.add(email_val)

            except Exception as e:
                print(f"  [Sheet] Error reading {sheet_name}: {e}")
                continue

        with self._lock:
            self.known_ids = new_ids
            self.known_names = new_names
            self.known_urls = new_urls
            self.last_refresh = datetime.now()
            self.error = None

        print(f"  [Sheet] Refreshed: {len(new_ids)} IDs, {len(new_names)} names, {len(new_urls)} URLs")
        return {"ids": len(new_ids), "names": len(new_names), "urls": len(new_urls)}

    def _read_sheet(self, service, sheet_name):
        """Read all rows from a sheet, trying different range formats."""
        for template in ["'{0}'!A:ZZ", "{0}!A:ZZ"]:
            try:
                result = service.spreadsheets().values().get(
                    spreadsheetId=SHEET_ID,
                    range=template.format(sheet_name),
                    valueRenderOption="FORMATTED_VALUE"
                ).execute()
                vals = result.get("values", [])
                if vals:
                    return vals
            except Exception:
                continue
        raise Exception(f"Cannot read sheet: {sheet_name}")

    def _find_column(self, headers, candidates):
        """Find a column index by trying candidate names (headers already normalized)."""
        for i, h in enumerate(headers):
            h_clean = h.lower().replace(" ", "").replace("_", "").replace("-", "")
            for c in candidates:
                c_clean = c.lower().replace(" ", "").replace("_", "").replace("-", "")
                if h_clean == c_clean:
                    return i
        return None

    def is_known(self, channel_id=None, name=None, url=None):
        """Check if a channel is already known from any Google Sheet."""
        with self._lock:
            if channel_id and channel_id in self.known_ids:
                return True
            if name and name.lower() in self.known_names:
                return True
            if url and url in self.known_urls:
                return True
            return False

    def status(self):
        with self._lock:
            return {
                "ids_count": len(self.known_ids),
                "names_count": len(self.known_names),
                "urls_count": len(self.known_urls),
                "last_refresh": self.last_refresh.isoformat() if self.last_refresh else None,
                "error": self.error,
            }

    # ── Write helpers ──

    def _get_write_service(self):
        """Get a Sheets service with write scope."""
        key_path = os.path.join(os.path.dirname(__file__), "keys", "google-service-account.json")
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("sheets", "v4", credentials=creds)

    def append_to_sheet(self, sheet_name, rows):
        """Append rows to a specific sheet. Each row is a list of values."""
        if not rows:
            return
        try:
            service = self._get_write_service()
            # Try quoted name first
            for template in ["'{0}'!A:ZZ", "{0}!A:ZZ"]:
                try:
                    service.spreadsheets().values().append(
                        spreadsheetId=SHEET_ID,
                        range=template.format(sheet_name),
                        valueInputOption="USER_ENTERED",
                        insertDataOption="INSERT_ROWS",
                        body={"values": rows}
                    ).execute()
                    return
                except SheetsHttpError:
                    continue
                except Exception:
                    continue
            raise Exception(f"Cannot append to sheet: {sheet_name}")
        except Exception as e:
            print(f"  [Sheet] Append error for {sheet_name}: {e}")
            raise

sheet_dedup = SheetDedupManager()

# ----------------------------------------------
# API KEY MANAGER
# ----------------------------------------------

# Comma-separated in .env — the KeyManager rotates to the next key when one runs out of daily quota
API_KEYS = [k.strip() for k in os.environ.get("YOUTUBE_API_KEYS", "").split(",") if k.strip()]

YT_QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # YouTube Data API quota resets at midnight Pacific

class KeyManager:
    def __init__(self, keys):
        self.keys       = list(keys)
        self.current    = 0
        self.exhausted  = set()
        self.quota_used = {k: 0 for k in keys}
        self.reset_date = datetime.now(YT_QUOTA_TZ).date()
        self._lock      = asyncio.Lock()

    def _check_daily_reset(self):
        """Quota resets daily at midnight Pacific, but nothing was clearing our
        in-memory `exhausted` set to match — once all keys got marked exhausted
        on a given day, available_keys() stayed 0 forever, even after Google's
        quota had long since replenished. Only a full process restart fixed it.
        Checked lazily here instead of on a timer so it self-heals on next use."""
        today = datetime.now(YT_QUOTA_TZ).date()
        if today != self.reset_date:
            print(f"  [KEY] New quota day ({today}) — resetting exhausted keys.")
            self.exhausted  = set()
            self.quota_used = {k: 0 for k in self.keys}
            self.current    = 0
            self.reset_date = today

    def current_key(self):
        self._check_daily_reset()
        return self.keys[self.current]

    async def rotate(self, reason="quota_exceeded"):
        async with self._lock:
            self._check_daily_reset()
            self.exhausted.add(self.keys[self.current])
            print(f"  [KEY] Key {self.current+1} exhausted ({reason}). Rotating...")
            for i, k in enumerate(self.keys):
                if k not in self.exhausted:
                    self.current = i
                    print(f"  [KEY] Switched to key {i+1}")
                    return True
            print("  [KEY] All keys exhausted.")
            return False

    def add_usage(self, units):
        self.quota_used[self.keys[self.current]] = self.quota_used.get(self.keys[self.current], 0) + units

    def available_keys(self):
        self._check_daily_reset()
        return len(self.keys) - len(self.exhausted)

    def status(self):
        self._check_daily_reset()
        return {
            f"key_{i+1}": {"exhausted": k in self.exhausted, "quota_used": self.quota_used.get(k, 0)}
            for i, k in enumerate(self.keys)
        }

keys = KeyManager(API_KEYS)

# ----------------------------------------------
# FILTER LISTS
# ----------------------------------------------

NICHE_SIGNALS = [
    # ===== FINANCE & INVESTING =====
    "personal finance", "investing", "investment", "investor",
    "stock market", "stock trading", "trading", "trader",
    "dividend", "growth stock", "value investing",
    "index fund", "etf", "mutual fund", "portfolio",
    "retirement", "401k", "ira", "roth ira",
    "wealth management", "wealth building", "wealth",
    "financial independence", "fire movement", "financial freedom",
    "passive income", "side hustle", "multiple streams",
    "real estate investing", "real estate investor", "property investing",
    "rental property", "real estate agent", "realtor",
    "mortgage", "refinance", "home buying", "housing market",
    "commercial real estate", "cre",
    "crypto", "cryptocurrency", "bitcoin", "ethereum", "blockchain",
    "defi", "nft", "web3",
    "savings", "budgeting", "budget", "frugal",
    "credit card", "credit score", "debt payoff", "debt free",
    "financial literacy", "money management", "money tips",
    "tax", "taxes", "tax strategy", "accounting", "cpa",
    "bookkeeping", "audit",
    "banking", "savings account", "high yield",
    "insurance", "life insurance", "health insurance",
    "finance", "money", "financial advisor",
    "economics", "economy", "market analysis",
    "financial education", "financial planning",
    "millionaire", "net worth", "asset allocation",

    # ===== BUSINESS & ENTREPRENEURSHIP =====
    "entrepreneur", "entrepreneurship", "founder", "co-founder",
    "startup", "scale up", "growth hacking",
    "business owner", "small business", "business growth",
    "business strategy", "business advice", "business tips",
    "business development", "bd",
    "ceo", "executive", "leadership",
    "management", "manager", "operations",
    "productivity", "time management", "efficiency",
    "b2b", "b2c", "crm",
    "sales", "sales strategy", "sales tips", "cold calling",
    "cold email", "cold outreach", "outbound",
    "sales funnel", "conversion", "closing",
    "revenue", "profit", "margin", "mrr", "arr",
    "recurring revenue", "subscription",
    "valuation", "fundraising", "venture capital", "vc",
    "angel investor", "acquisition", "exit strategy",
    "pitch deck", "investor pitch",
    "hiring", "team building", "company culture",
    "supply chain", "logistics", "fulfillment",
    "franchise", "franchising",

    # ===== MARKETING & ADVERTISING =====
    "digital marketing", "marketing strategy", "marketing tips",
    "content marketing", "content strategy",
    "social media marketing", "social media strategy",
    "seo", "search engine optimization", "sem",
    "ppc", "google ads", "facebook ads", "instagram ads",
    "linkedin marketing", "tiktok marketing",
    "email marketing", "email list", "newsletter",
    "affiliate marketing", "affiliate",
    "influencer marketing", "influencer",
    "branding", "brand strategy", "personal brand",
    "copywriting", "copywriter",
    "landing page", "lead magnet", "lead generation",
    "funnel building", "sales funnel",
    "analytics", "data driven", "a/b testing",
    "growth marketing", "growth hacker",
    "cpo", "cpa", "cac", "ltv", "roas",
    "retargeting", "remarketing",
    "agency", "smma", "marketing agency",
    "media buyer", "media buying",

    # ===== SAAS & TECHNOLOGY =====
    "saas", "software as a service", "software",
    "app development", "mobile app", "web app",
    "product management", "product owner", "pm",
    "product design", "ux design", "ui design",
    "product led growth", "plg",
    "api", "integration", "platform",
    "devops", "engineering", "software engineer",
    "startup founder", "tech startup",
    "micro saas", "indie hacker", "maker",

    # ===== AI & MACHINE LEARNING =====
    "artificial intelligence", "ai", "machine learning", "ml",
    "deep learning", "neural network", "llm", "large language model",
    "gpt", "chatgpt", "openai",
    "prompt engineering", "ai agent", "ai automation",
    "computer vision", "natural language processing", "nlp",
    "data science", "data scientist", "analytics",
    "data engineering", "big data",
    "automation", "rpa", "workflow automation",
    "ai tools", "ai for business", "ai productivity",
    "generative ai", "gen ai", "ai generated",
    "vector database", "rag", "fine tuning",
    "machine learning engineer", "ml ops",
    "no code", "low code",

    # ===== REAL ESTATE & PROPERTY =====
    "real estate", "property", "realtor",
    "real estate investing", "rental income",
    "house hacking", "brrr", "fix and flip",
    "wholesaling", "real estate wholesaling",
    "property management", "property manager",
    "commercial real estate", "multifamily",
    "airbnb", "short term rental", "vacation rental",
    "real estate agent", "real estate broker",
    "title", "escrow", "appraisal",
    "home inspection", "contractor", "remodeling",
    "landlord", "tenant", "lease",

    # ===== HEALTH, WELLNESS & MEDICAL =====
    "health", "wellness", "fitness", "nutrition",
    "weight loss", "fat loss", "muscle building",
    "personal trainer", "fitness coach",
    "mental health", "mindfulness", "meditation",
    "biohacking", "longevity", "anti aging",
    "supplements", "vitamins", "health optimization",
    "physical therapy", "pt",
    "healthcare", "medical", "telehealth",
    "dental", "dentist", "orthodontist",
    "skincare", "dermatology", "cosmetic",
    "functional medicine", "hormone", "thyroid",
    "sleep", "recovery", "stress management",
    "nursing", "doctor", "physician",
    "pharmacy", "pharmaceutical",

    # ===== ONLINE EDUCATION & COURSES =====
    "online course", "digital course", "course creator",
    "online education", "elearning", "edtech",
    "coaching", "coach", "life coach",
    "consulting", "consultancy", "consultant",
    "mastermind", "group coaching",
    "membership", "subscription community",
    "digital product", "digital download",
    "worksheet", "template", "notion template",
    "webinar", "live training", "workshop",

    # ===== CAREER & PROFESSIONAL =====
    "career advice", "career growth", "career change",
    "job search", "job hunting", "resume", "interview",
    "linkedin", "networking", "professional development",
    "remote work", "work from home", "wfh",
    "freelance", "freelancer", "gig economy",
    "side hustle", "solopreneur",
    "salary negotiation", "raise", "promotion",
    "productivity", "efficiency", "focus",

    # ===== LEGAL =====
    "lawyer", "attorney", "legal advice",
    "business law", "corporate law",
    "contract law", "contract review",
    "intellectual property", "ip", "patent",
    "employment law", "real estate law",
    "estate planning", "trust", "will",
    "immigration", "visa",
    "legalzoom", "incorporation", "llc",

    # ===== SPECIFIC HIGH-VALUE SIGNALS =====
    "high ticket", "high-ticket", "high ticket sales",
    "scaling", "scale", "scaled to",
    "six figure", "seven figure", "eight figure",
    "$10k", "$20k", "$30k", "$50k", "$100k",
    "$1m", "$1 million", "$10m", "$100m",
    "case study", "real numbers", "income report",
    "behind the scenes", "playbook",
    "systemize", "systemize your", "sop",
    "gohighlevel", "go high level", "hubspot",
    "skool", "calendly", "clickfunnels",
    "instantly", "smartlead", "apollo", "clay",
    "notion", "make.com", "zapier",
    "retainer", "monthly recurring",
    "client acquisition", "client retention",
    "discovery call", "appointment setter",
    "client", "clients", "customer",
]

NEGATIVE_HARD = [
    # ===== GAMING & ESPORTS =====
    "gameplay", "gaming", "gamer", "let's play", "lets play",
    "playthrough", "speedrun", "walkthrough",
    "minecraft", "fortnite", "cod", "call of duty",
    "valorant", "league of legends", "lol", "dota",
    "pubg", "apex legends", "overwatch", "roblox",
    "twitch", "streamer", "streaming",
    "esports", "competitive gaming",
    "fifa", "nba 2k", "grand theft auto", "gta",
    "skyrim", "elden ring", "zelda", "pokemon",
    "nintendo", "playstation", "xbox", "pc gaming",

    # ===== MUSIC & ENTERTAINMENT =====
    "music", "musician", "singer", "song", "album",
    "lyrics", "music video", "official video",
    "rap", "hip hop", "pop music", "edm", "dj",
    "guitar", "piano", "instrumental",
    "concert", "tour", "live performance",
    "band", "artist", "record label",
    "spotify", "apple music", "soundcloud",
    "music production", "beat", "remix",

    # ===== MOVIES, TV & FILM =====
    "movie", "movies", "film", "films", "cinema",
    "tv show", "tv series", "netflix", "disney+",
    "hbo", "amazon prime", "hulu",
    "movie review", "film review", "trailer",
    "marvel", "dc", "star wars", "star trek",
    "hollywood", "bollywood", "anime",
    "oscar", "emmy", "grammy",
    "actor", "actress", "director",
    "behind the scenes film",

    # ===== SPORTS & ATHLETICS =====
    "sports", "sport", "athlete", "athletic",
    "nfl", "nba", "mlb", "nhl", "mls", "ufc",
    "football", "basketball", "baseball", "hockey",
    "soccer", "tennis", "golf", "boxing",
    "cricket", "rugby", "mma",
    "olympics", "world cup", "super bowl",
    "highlights", "sports news",
    "workout", "exercise", "bodybuilding",
    "yoga", "pilates", "crossfit",
    "running", "marathon", "triathlon",

    # ===== NEWS, POLITICS & CURRENT EVENTS =====
    "news", "breaking news", "current events",
    "politics", "political", "democrat", "republican",
    "election", "government", "president",
    "congress", "senate", "supreme court",
    "war", "ukraine", "russia", "conflict",
    "weather", "forecast", "natural disaster",
    "cnn", "fox news", "bbc news", "msnbc",
    "world news", "local news", "daily news",
    "opinion", "commentary politics",

    # ===== VLOGS & PERSONAL =====
    "vlog", "vlogger", "daily vlog",
    "day in my life", "day in the life",
    "morning routine", "night routine",
    "get ready with me", "grwm",
    "weekly vlog", "travel vlog",
    "family vlog", "mom vlogger", "dad vlogger",
    "lifestyle", "lifestyle blogger",
    "aesthetic", "cozy", "minimalist lifestyle",

    # ===== FOOD, COOKING & RECIPES =====
    "recipe", "recipes", "cooking", "baking",
    "food", "delicious", "tasty",
    "restaurant", "street food", "food review",
    "meal prep", "healthy recipe", "vegan recipe",
    "kitchen", "chef", "cookbook",

    # ===== FASHION, BEAUTY & MAKEUP =====
    "fashion", "style", "outfit", "ootd",
    "makeup", "makeup tutorial", "beauty",
    "skincare routine", "hairstyle", "hair tutorial",
    "nail art", "manicure", "pedicure",
    "cosmetics", "beauty product",
    "model", "modeling", "runway",
    "try on haul", "shopping haul",

    # ===== TRAVEL & ADVENTURE =====
    "travel", "tourism", "tourist",
    "travel guide", "travel tips",
    "hotel review", "flight review", "airport",
    "backpacking", "road trip",
    "destinations", "vacation", "holiday",
    "exploring", "adventure travel",

    # ===== KIDS & FAMILY CONTENT =====
    "kids", "children", "toddler", "baby",
    "parenting", "mom", "dad life",
    "cartoon", "disney", "animation",
    "nursery rhymes", "kids song",
    "educational kids", "kids learning",
    "unboxing toys", "toy review",
    "family friendly", "family channel",
    "kid's", "kid shows",
    "coComelon", "blippi", "peppa pig",

    # ===== DIY, CRAFTS & HOME IMPROVEMENT =====
    "diy", "do it yourself", "craft", "handmade",
    "home decor", "interior design", "decorating",
    "woodworking", "wood work", "carpentry",
    "sewing", "knitting", "crochet",
    "home renovation", "home improvement",
    "gardening", "landscaping", "backyard",
    "upcycling", "restoration",

    # ===== PETS & ANIMALS =====
    "pet", "pets", "dog", "cat", "puppy", "kitten",
    "dog training", "cat training",
    "cute animals", "funny animals",
    "dog groomer", "pet care",
    "horse", "bird", "fish", "aquarium",

    # ===== TECHNOLOGY REVIEWS & GADGETS =====
    "unboxing", "tech review", "gadget review",
    "smartphone", "iphone", "android", "samsung",
    "laptop review", "tablet", "apple watch",
    "tech tips", "tech news",
    "car review", "auto review", "test drive",

    # ===== REACTION & COMMENTARY =====
    "reaction", "react to", "reacting", "reacts",
    "commentary", "drama", "tea",
    "response", "responding to",

    # ===== ASMR, MUKBANG & WEIRD =====
    "asmr", "mukbang", "eating show",
    "oddly satisfying", "satisfying video",

    # ===== NON-ENGLISH LANGUAGE CONTENT =====
    "hindi", "hindi mein", "urdu", "urdu mein",
    "tagalog", "filipino", "spanish", "español",
    "french", "français", "portuguese", "português",
    "german", "deutsch", "italian", "italiano",
    "russian", "русский", "chinese", "中文",
    "japanese", "日本語", "korean", "한국어",
    "arabic", "العربية", "turkish", "türkçe",

    # ===== RELIGIOUS & SPIRITUALITY =====
    "sermon", "preacher", "preaching", "ministry",
    "gospel", "worship", "praise",
    "church", "bible study", "bible verse",
    "prayer", "pray", "god",
    "pastor", "evangelist", "missionary",
    "christian", "muslim", "islam", "hindu",
    "buddhist", "jewish", "faith",
    "religious", "spiritual",

    # ===== LOW-QUALITY / SCAMMY SIGNALS =====
    "first $1000", "first $100", "make your first",
    "easy money", "quick money", "fast money",
    "get rich quick", "no experience needed",
    "work from home and make money",
    "mlm", "network marketing", "multi-level",
    "pyramid", "downline", "upline",
    "day trading", "forex signals", "crypto signals",
    "pump and dump", "moon", "hodl", "altcoin",
    "trading bot", "trading robot",
    "dropship", "drop ship", "dropshipping",
    "shopify store from scratch", "aliexpress",
    "copy trading", "binary options",
    "get paid", "make money fast",

    # ===== GENERAL ENTERTAINMENT =====
    "comedy", "funny", "comedian",
    "prank", "challenge",
    "memes", "meme review",
    "fail", "epic fail",
    "try not to laugh",
    "cringe", "tiktok compilation",
    "funny moments", "best of",
    "stand up comedy",

    # ===== MISC LOW-RPM =====
    "podcast clip", "podcast episode",
    "trivia", "quiz",
    "magic", "magician",
    "motorcycle", "motorbike",
    "truck", "tractor",
    "farming", "farmer",
    "fishing", "hunting",
    "survival", "bushcraft",
    "camping", "outdoors",
    "weapon", "gun review",
    "military", "army",
    "history", "historical",
    "science experiment", "science project",
    "math", "mathematics",
    "lecture", "university course",
    "office hours",
]

# ----------------------------------------------
# STATE
# ----------------------------------------------

class AppState:
    def __init__(self):
        self.blocklist        = set()
        self.rejected         = set()
        self.all_seen         = set()
        self.channels_found   = []    # passed all filters — pending review
        self.borderline       = []    # passed hard filters, failed niche only
        self.accepted         = []    # user approved
        self.validation_queue = []
        self.reject_log       = {}
        self.log              = []
        self.lock             = Lock()
        self.next_keyword     = False
        self.next_keyword_lock = Lock()
        self.email_checked    = set()   # channel IDs that have already been email-checked
        self.recovery_checked = set()   # channel IDs/names already run through Recover Rejected — skip until reset
        self.email_check_queue = []     # channel dicts waiting for email check
        self.email_check_stats = {
            "status":       "idle",
            "total":        0,
            "completed":    0,
            "passed":       0,   # has email → saved to sheet
            "failed":       0,   # no email  → rejected
            "errors":       0,
            "last_result":  None,
        }
        self.recover_stats = {
            "status":       "idle",
            "total":        0,
            "processed":    0,
            "recovered":    0,
            "still_failed": 0,
            "current":      None,
            "details":      [],    # list of {id, name, reason}
        }
        self.stats = {
            "queue_size":       0,
            "validated_passed": 0,
            "validated_failed": 0,
            "borderline_count": 0,
            "total_accepted":   0,
            "total_rejected":   0,
            "status":           "idle",
            "last_channel":     None,
        }

    def log_msg(self, msg):
        ts    = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log.append(entry)
        if len(self.log) > 1000:
            self.log = self.log[-1000:]
        # Safe print — Windows console (cp1252) can't display emoji/CJK/Cyrillic/etc.
        # NOTE: encoding to utf-8 and decoding back (the old fallback here) does NOT
        # fix this — utf-8 round-trips virtually any Unicode string unchanged, so the
        # second print() call hit the exact same UnicodeEncodeError, uncaught, and
        # silently killed whatever asyncio task was running this (this is exactly
        # what killed validation_worker mid-recovery on a channel named "DenQ來了").
        # Fix: encode using the console's *actual* encoding with errors="replace" so
        # undisplayable characters become "?" instead of raising, and never let a
        # logging call itself take down a caller.
        try:
            print(entry)
        except Exception:
            try:
                enc = sys.stdout.encoding or "ascii"
                print(entry.encode(enc, errors="replace").decode(enc, errors="replace"))
            except Exception:
                pass

    def add_reject(self, reason):
        self.reject_log[reason] = self.reject_log.get(reason, 0) + 1

    def is_in_blocklist(self, channel_id=None, name=None, url=None):
        """Check if a channel is in the blocklist OR known from Google Sheets."""
        if channel_id and (channel_id in self.blocklist or channel_id in self.rejected):
            return True, "in_blocklist"
        if name and (name.lower() in self.blocklist or name.lower() in self.rejected):
            return True, "in_blocklist"
        if sheet_dedup.is_known(channel_id=channel_id, name=name, url=url):
            return True, "already_in_sheets"
        return False, None

state = AppState()

# ----------------------------------------------
# PERSISTENCE
# ----------------------------------------------

def save_state():
    """Write channels_found, accepted, rejected, all_seen to disk."""
    try:
        data = {
            "channels_found": state.channels_found,
            "borderline":     state.borderline,
            "accepted":       state.accepted,
            "rejected":       list(state.rejected),
            "all_seen":       list(state.all_seen),
            "reject_log":     state.reject_log,
            "email_checked":  list(state.email_checked),
            "recovery_checked": list(state.recovery_checked),
            "recover_details": state.recover_stats.get("details", []),
            "saved_at":       datetime.now().isoformat(),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)   # atomic write
    except Exception as e:
        print(f"  [Save Error] {e}")

def load_state():
    """Load persisted state on startup."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state.channels_found = data.get("channels_found", [])
        state.borderline     = data.get("borderline", [])
        state.accepted       = data.get("accepted", [])
        state.rejected       = set(data.get("rejected", []))
        state.all_seen       = set(data.get("all_seen", []))
        state.reject_log     = data.get("reject_log", {})
        state.email_checked  = set(data.get("email_checked", []))
        state.recovery_checked = set(data.get("recovery_checked", []))
        state.stats["validated_passed"] = len(state.channels_found) + len(state.accepted)
        state.stats["borderline_count"] = len(state.borderline)
        state.stats["total_accepted"]   = len(state.accepted)
        print(f"  [Load] Restored: {len(state.channels_found)} pending, "
              f"{len(state.accepted)} accepted, "
              f"{len(state.borderline)} borderline, "
              f"{len(state.rejected)} rejected")
    except Exception as e:
        print(f"  [Load Error] {e}")

# ----------------------------------------------
# WAITING QUEUE PERSISTENCE - channels found by the hop scrapers that are still
# waiting for validation (e.g. because every API key hit its daily quota). Kept on
# disk so a backend restart does not lose them.
# ----------------------------------------------

QUEUE_FILE = os.path.join(DATA_DIR, "validation_queue.json")
_queue_sig = None

def save_queue(force=False):
    global _queue_sig
    with state.lock:
        q = list(state.validation_queue)
    sig = (len(q), json.dumps(q[:1] + q[-1:], sort_keys=True, default=str))
    if not force and sig == _queue_sig:
        return
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": q, "saved_at": datetime.now().isoformat()}, f, ensure_ascii=False)
        os.replace(tmp, QUEUE_FILE)
        _queue_sig = sig
    except Exception as e:
        print(f"  [Queue Save Error] {e}")

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            items = json.load(f).get("items", [])
        with state.lock:
            have = {(i.get("id"), i.get("video_id")) for i in state.validation_queue}
            added = 0
            for it in items:
                k = (it.get("id"), it.get("video_id"))
                if k not in have:
                    state.validation_queue.append(it)
                    have.add(k)
                    added += 1
            state.stats["queue_size"] = len(state.validation_queue)
        print(f"  [Load] Restored {added} channel(s) waiting for validation")
    except Exception as e:
        print(f"  [Queue Load Error] {e}")

async def queue_saver():
    while True:
        await asyncio.sleep(3)
        try:
            save_queue()
        except Exception:
            pass

# ----------------------------------------------
# HELPERS
# ----------------------------------------------

def parse_excel_blocklist(file_bytes):
    blocklist = set()
    try:
        dfs = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
        for _, df in dfs.items():
            for col in df.columns:
                cl = str(col).lower()
                if "id" in cl and "channel" in cl:
                    for val in df[col].dropna().astype(str):
                        blocklist.add(val.strip())
                elif "url" in cl:
                    for val in df[col].dropna().astype(str):
                        m = re.search(r"channel/([\w-]+)", val)
                        if m: blocklist.add(m.group(1))
                        m = re.search(r"@([\w-]+)", val)
                        if m: blocklist.add(m.group(1))
                elif "name" in cl and "channel" in cl:
                    for val in df[col].dropna().astype(str):
                        blocklist.add(val.strip().lower())
    except Exception as e:
        print(f"Excel parse error: {e}")
    return blocklist

SHEET_REFRESH_TIMEOUT = 20.0

async def refresh_sheets_safe(timeout=SHEET_REFRESH_TIMEOUT):
    """Run sheet_dedup.refresh() (blocking network I/O, no built-in timeout) off
    the event loop with a hard timeout. Without this, a single stalled Google
    Sheets request freezes the entire app — every async worker, every endpoint —
    since refresh() used to be called directly inside async functions."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(sheet_dedup.refresh), timeout=timeout)
    except asyncio.TimeoutError:
        state.log_msg(f"[SHEETS] Refresh timed out after {timeout}s — keeping previous dedup data")
        return {"error": "timeout", "ids": 0, "names": 0, "urls": 0}

def parse_subs(val):
    if val is None: return None
    if isinstance(val, (int, float)): return int(val)
    s = str(val).lower().replace(",", "").strip()
    for suffix, mult in [("k", 1_000), ("m", 1_000_000), ("b", 1_000_000_000)]:
        if suffix in s:
            try: return int(float(s.replace(suffix, "")) * mult)
            except: return None
    try: return int(float(s))
    except: return None

def is_english(text):
    if not text or len(text.strip()) < 10: return False
    ascii_c = sum(1 for c in text if ord(c) < 128 and c.isalpha())
    total_c = sum(1 for c in text if c.isalpha())
    return total_c > 0 and (ascii_c / total_c) > 0.85

def is_recent(upload_date_str):
    if not upload_date_str: return False
    try:
        if len(upload_date_str) == 8:
            d = datetime.strptime(upload_date_str, "%Y%m%d")
        else:
            d = datetime.fromisoformat(upload_date_str.replace("Z", "+00:00").split("+")[0])
        return (datetime.now() - d) <= timedelta(days=ACTIVITY_DAYS)
    except: return False

def niche_score(haystack):
    return sum(1 for s in NICHE_SIGNALS if s in haystack)

def negative_hit(haystack):
    """Check for negative keywords using word-boundary matching.

    Uses \\b boundaries to avoid false positives from substring matches,
    e.g. 'cat' won't match 'education', 'news' won't match 'business'.
    Multi-word entries like 'call of duty' are matched as whole phrases
    with boundaries on both ends.
    """
    for n in NEGATIVE_HARD:
        # Escape the keyword and wrap with word boundaries
        escaped = re.escape(n)
        # For entries with only word/space chars, use \b boundaries
        if re.match(r'^[\w\s]+$', n):
            pattern = r'\b' + escaped + r'\b'
        else:
            pattern = escaped
        if re.search(pattern, haystack, re.IGNORECASE):
            return n
    return None

def extract_channel_id_from_url(url):
    """Extract channel ID or handle from various YouTube URL formats.
    Returns UC... ID, @handle, or None."""
    if not url: return None

    # Direct UC channel ID: youtube.com/channel/UCxxxx
    m = re.search(r"youtube\.com/channel/(UC[\w-]+)", url)
    if m: return m.group(1)

    # Handle: youtube.com/@handle (handles can have dots, underscores, hyphens)
    m = re.search(r"youtube\.com/@([\w.]+)", url)
    if m: return "@" + m.group(1)

    # Bare @handle typed by user (no youtube.com)
    m = re.match(r"^@([\w.]+)$", url.strip())
    if m: return "@" + m.group(1)

    # Legacy /c/ or /user/ — skip these, they don't reliably map to handles.
    # Let resolve_handle or raw fallthrough handle them.
    return None

# ----------------------------------------------
# YOUTUBE API CALLS
# ----------------------------------------------

async def yt_get(client: httpx.AsyncClient, endpoint: str, params: dict, cost: int = 1):
    for _ in range(len(API_KEYS) + 1):
        if keys.available_keys() == 0:
            state.log_msg("All API keys exhausted. Validation paused.")
            return None
        params["key"] = keys.current_key()
        try:
            resp = await client.get(f"{YT_API_BASE}/{endpoint}", params=params, timeout=15)
            data = resp.json()
            if resp.status_code == 200:
                keys.add_usage(cost)
                return data
            reason = ""
            try: reason = data["error"]["errors"][0]["reason"]
            except: reason = str(resp.status_code)
            if resp.status_code in (403, 429) or "quota" in reason.lower():
                if not await keys.rotate(reason): return None
                continue
            return None
        except Exception as e:
            state.log_msg(f"API error: {e}")
            return None
    return None

async def resolve_handle(client, handle: str) -> Optional[str]:
    """@handle -> UC channel ID. 1 unit."""
    data = await yt_get(client, "channels", {
        "part": "id", "forHandle": f"@{handle.lstrip('@')}", "maxResults": 1
    }, cost=1)
    if not data: return None
    items = data.get("items", [])
    return items[0]["id"] if items else None

async def resolve_video_ids(client, video_ids: list) -> dict:
    """video_id -> channel_id via videos.list. 1 unit per 50 videos."""
    result = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        data  = await yt_get(client, "videos", {
            "part": "snippet", "id": ",".join(batch), "maxResults": 50,
        }, cost=1)
        if not data: continue
        for item in data.get("items", []):
            cid = item.get("snippet", {}).get("channelId")
            if cid: result[item["id"]] = cid
    return result

async def fetch_channel_meta(client, channel_id: str) -> Optional[dict]:
    """channels.list for one channel. 1 unit."""
    data = await yt_get(client, "channels", {
        "part": "snippet,statistics,contentDetails",
        "id": channel_id, "maxResults": 1
    }, cost=1)
    if not data or not data.get("items"): return None
    item    = data["items"][0]
    snippet = item.get("snippet", {})
    stats   = item.get("statistics", {})
    content = item.get("contentDetails", {})
    thumbs  = snippet.get("thumbnails", {})
    thumb   = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
    return {
        "channel_id":       item["id"],
        "channel_name":     snippet.get("title", "Unknown"),
        "description":      snippet.get("description", ""),
        "country":          snippet.get("country", "").upper(),
        "channel_url":      f"https://www.youtube.com/channel/{item['id']}",
        "thumbnail":        thumb,
        "subscriber_count": parse_subs(stats.get("subscriberCount")),
        "uploads_playlist": content.get("relatedPlaylists", {}).get("uploads", ""),
    }

async def fetch_recent_video_ids(client, playlist_id: str) -> list:
    """playlistItems.list. 1 unit."""
    if not playlist_id: return []
    data = await yt_get(client, "playlistItems", {
        "part": "contentDetails", "playlistId": playlist_id, "maxResults": 10
    }, cost=1)
    if not data: return []
    return [
        i["contentDetails"]["videoId"]
        for i in data.get("items", [])
        if i.get("contentDetails", {}).get("videoId")
    ]

async def fetch_video_stats(client, video_ids: list) -> dict:
    """videos.list for publish dates. 1 unit per 50."""
    if not video_ids: return {}
    data = await yt_get(client, "videos", {
        "part": "snippet", "id": ",".join(video_ids[:50]), "maxResults": 50,
    }, cost=1)
    if not data: return {}
    return {
        item["id"]: {"published_at": item.get("snippet", {}).get("publishedAt", "")}
        for item in data.get("items", [])
    }

async def get_full_meta(client, channel_id: str) -> Optional[dict]:
    """Full metadata for a UC channel ID. 3 units total."""
    meta = await fetch_channel_meta(client, channel_id)
    if not meta: return None

    video_ids   = await fetch_recent_video_ids(client, meta["uploads_playlist"])
    last_upload = ""

    if video_ids:
        vstats = await fetch_video_stats(client, video_ids)
        for vd in vstats.values():
            pub = vd.get("published_at", "")
            if pub and (not last_upload or pub > last_upload):
                last_upload = pub

    if last_upload:
        try:
            dt          = datetime.fromisoformat(last_upload.replace("Z", "+00:00").split("+")[0])
            last_upload = dt.strftime("%Y%m%d")
        except: last_upload = ""

    meta["last_upload_date"] = last_upload
    return meta

# ----------------------------------------------
# FILTER STACK
# ----------------------------------------------

def run_filters(meta: dict, skip_rejected_check: bool = False):
    """
    Returns (result, reason, score) where result is:
      "pass"       — passed everything, add to channels_found
      "borderline" — passed hard filters, failed niche only → add to borderline
      "fail"       — failed a hard filter → discard

    skip_rejected_check: used by the recovery flow, which is re-checking channels
    that are, by definition, already in state.rejected — so that check is skipped.
    Everything else runs identically to normal validation.
    """
    if not meta: return "fail", "metadata_fetch_failed", 0

    cid      = meta.get("channel_id", "")
    name     = (meta.get("channel_name") or "").strip()
    name_l   = name.lower()
    desc     = meta.get("description", "") or ""
    haystack = f"{name_l} {desc.lower()}"

    if cid in state.blocklist or name_l in state.blocklist:
        return "fail", "in_blocklist", 0
    if not skip_rejected_check and (cid in state.rejected or name_l in state.rejected):
        return "fail", "previously_rejected", 0
    if sheet_dedup.is_known(channel_id=cid, name=name_l, url=meta.get("channel_url", "")):
        return "fail", "already_in_sheets", 0

    subs = meta.get("subscriber_count")
    if subs is None:    return "fail", "subs_unknown", 0
    if subs < MIN_SUBS: return "fail", f"too_small:{subs}subs", 0
    if subs > MAX_SUBS: return "fail", f"too_large:{subs}subs", 0

    country = meta.get("country", "")
    if country and country not in ALLOWED_COUNTRIES:
        return "fail", f"country:{country}", 0

    if not is_recent(meta.get("last_upload_date", "")):
        return "fail", "no_recent_uploads", 0

    if not is_english(desc):
        return "fail", "non_english", 0

    neg = negative_hit(haystack)
    if neg: return "fail", f"negative:{neg}", 0

    # All hard filters passed — now check niche
    score = niche_score(haystack)
    if score < MIN_NICHE_SCORE:
        return "borderline", f"low_niche_score:{score}", score

    return "pass", "passed", score

# ----------------------------------------------
# VALIDATION WORKER
# ----------------------------------------------

async def validation_worker():
    state.log_msg("Validation worker started")
    state.stats["status"] = "ready"

    async with httpx.AsyncClient() as client:
        while True:
            item = None
            with state.lock:
                if state.validation_queue:
                    item = state.validation_queue.pop(0)
                state.stats["queue_size"] = len(state.validation_queue)

            if not item:
                idle_status = _hop_scraper_idle_status()
                if state.stats["status"] != idle_status:
                    state.stats["status"] = idle_status
                await asyncio.sleep(2)
                continue

            if keys.available_keys() == 0:
                with state.lock:
                    state.validation_queue.insert(0, item)
                state.log_msg("All keys exhausted. Pausing 5 min...")
                state.stats["status"] = "keys_exhausted"
                await asyncio.sleep(300)
                continue

            cid         = item.get("id", "")
            name        = item.get("name", "Unknown")
            video_id    = item.get("video_id", "")
            is_recovery = item.get("source") == "recovered"

            state.stats["status"] = f"Validating: {(name or cid)[:40]}..."
            state.log_msg(f"[STEP] picked up {cid or video_id or '?'} (source={item.get('source', 'hop')}) — resolving...")

            # Resolve video_id -> channel_id
            if not cid and video_id:
                try:
                    resolved = await asyncio.wait_for(resolve_video_ids(client, [video_id]), timeout=20)
                except asyncio.TimeoutError:
                    state.log_msg(f"[STEP] video_id resolve TIMED OUT for {video_id} — skipping")
                    continue
                cid = resolved.get(video_id, "")
                if not cid:
                    continue

            # Resolve @handle -> UC ID
            if cid and not cid.startswith("UC"):
                try:
                    resolved_cid = await asyncio.wait_for(resolve_handle(client, cid), timeout=20)
                except asyncio.TimeoutError:
                    state.log_msg(f"[STEP] handle resolve TIMED OUT for {cid} — skipping")
                    continue
                if not resolved_cid: continue
                cid = resolved_cid

            # Dedup on real channel ID.
            # Recovery items are, by definition, channels we've already seen before
            # (that's why they ended up rejected) — so they'd otherwise be silently
            # skipped by the all_seen check below. We only lift that block for THIS
            # one item, right now, instead of stripping the whole batch out of
            # all_seen/rejected up front — that keeps at most one channel ever in a
            # "transitional" state, so a crash/restart mid-recovery can't wipe out
            # tracking for channels that haven't even been picked up yet.
            with state.lock:
                if is_recovery:
                    state.all_seen.discard(cid)
                    state.rejected.discard(cid)
                if cid in state.all_seen: continue
                if cid: state.all_seen.add(cid)

            state.log_msg(f"[STEP] {cid} — fetching metadata...")
            try:
                meta = await asyncio.wait_for(get_full_meta(client, cid), timeout=30)
                timed_out = False
            except asyncio.TimeoutError:
                state.log_msg(f"[STEP] {cid} — metadata fetch TIMED OUT after 30s, marking as failed and moving on")
                meta = None
                timed_out = True

            if not timed_out and keys.available_keys() == 0 and (not meta or not meta.get("last_upload_date")):
                # The keys ran out DURING this channel's checks - don't fail it for that.
                with state.lock:
                    state.all_seen.discard(cid)
                    state.validation_queue.insert(0, item)
                state.log_msg("API keys ran out mid-check - channel put back in the waiting queue")
                state.stats["status"] = "keys_exhausted"
                continue

            if meta and meta.get("channel_name"):
                name = meta["channel_name"]
                if is_recovery:
                    with state.lock:
                        state.rejected.discard(name.lower())

            state.log_msg(f"[STEP] {name or cid} — metadata OK, running filters..." if meta
                          else f"[STEP] {cid} — metadata fetch failed")

            result, reason, score = run_filters(meta, skip_rejected_check=is_recovery) if meta \
                else ("fail", "metadata_fetch_timeout" if timed_out else "metadata_fetch_failed", 0)

            channel_data = None
            if meta and result in ("pass", "borderline"):
                channel_data = {
                    "id":          meta["channel_id"],
                    "name":        meta["channel_name"],
                    "url":         meta["channel_url"],
                    "subscribers": meta["subscriber_count"],
                    "description": meta["description"][:250] + ("..." if len(meta["description"]) > 250 else ""),
                    "uploadDate":  meta["last_upload_date"],
                    "thumbnail":   meta.get("thumbnail"),
                    "source":      item.get("source", "hop"),
                    "niche_score": score,
                    "timestamp":   item.get("timestamp", datetime.now().isoformat()),
                    "selected":    False,
                }

            if result == "pass":
                with state.lock:
                    existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
                    if channel_data["id"] not in existing:
                        state.channels_found.append(channel_data)
                        state.stats["validated_passed"] += 1
                        state.stats["last_channel"]      = channel_data
                state.log_msg(f"PASS       {name} ({meta['subscriber_count']:,} subs | score={score})")
                save_state()

            elif result == "borderline":
                with state.lock:
                    existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
                    if channel_data["id"] not in existing:
                        state.borderline.append(channel_data)
                        state.stats["borderline_count"] += 1
                state.log_msg(f"BORDERLINE {name} ({meta['subscriber_count']:,} subs | score={score}) — needs review")
                save_state()

            else:
                with state.lock:
                    if cid: state.rejected.add(cid)
                    if name: state.rejected.add(name.lower())
                    state.stats["validated_failed"] += 1
                    state.stats["total_rejected"]   += 1
                    state.add_reject(reason)
                if not any(reason.startswith(p) for p in ("too_small", "previously_rejected", "in_blocklist")):
                    state.log_msg(f"FAIL       {name} -> {reason}")

            # Recovered channels are just regular queue items tagged with source="recovered" —
            # this only tracks progress for the recovery panel, the processing above is identical.
            if is_recovery:
                with state.lock:
                    state.recover_stats["processed"] += 1
                    if result in ("pass", "borderline"):
                        state.recover_stats["recovered"] += 1
                    else:
                        state.recover_stats["still_failed"] += 1
                    # Mark done regardless of outcome — a rejected channel that
                    # fails recovery again gets re-added to state.rejected above,
                    # but recovery_checked keeps it from being re-queued next run.
                    if cid: state.recovery_checked.add(cid)
                    if name: state.recovery_checked.add(name.lower())
                    state.recover_stats["current"] = {
                        "index": state.recover_stats["processed"],
                        "total": state.recover_stats["total"],
                        "name": name, "status": reason,
                    }
                    if state.recover_stats["processed"] >= state.recover_stats["total"]:
                        state.recover_stats["status"] = "idle"
                        state.recover_stats["current"] = None
                        state.log_msg(
                            f"[RECOVER] 🎉 Complete! Checked {state.recover_stats['processed']}, "
                            f"recovered {state.recover_stats['recovered']}, "
                            f"still failing {state.recover_stats['still_failed']}"
                        )
                save_state()

            await asyncio.sleep(0.2)


async def periodic_sheet_refresh():
    """Auto-refresh Google Sheet data every 24 hours."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            result = await refresh_sheets_safe()
            state.log_msg(f"Auto-refreshed Google Sheets: {result.get('ids', 0)} IDs, "
                         f"{result.get('names', 0)} names, {result.get('urls', 0)} URLs")
        except Exception as e:
            state.log_msg(f"Sheet auto-refresh error: {e}")

# ----------------------------------------------
# EMAIL CHECK WORKER
# ----------------------------------------------

async def email_check_worker():
    """Background worker that processes the email check queue."""
    ec = None
    state.email_check_stats["status"] = "idle"

    while True:
        item = None
        with state.lock:
            if state.email_check_queue:
                item = state.email_check_queue.pop(0)
                state.email_check_stats["total"] = len(state.email_check_queue) + 1
                state.email_check_stats["status"] = "running"

        if not item:
            with state.lock:
                state.email_check_stats["status"] = "idle"
            await asyncio.sleep(2)
            continue

        cid = item.get("id", "")
        name = item.get("name", "Unknown")

        if cid in state.email_checked:
            continue

        state.log_msg(f"[EMAIL] Checking {name} ({cid[:15]}...)")

        # Create checker on first use
        if ec is None:
            try:
                ec = EmailChecker()
                await ec.start()
                state.log_msg("[EMAIL] Browser started")
            except Exception as e:
                state.log_msg(f"[EMAIL] Failed to start browser: {e}")
                with state.lock:
                    state.email_check_queue.insert(0, item)
                    state.email_check_stats["status"] = "error"
                await asyncio.sleep(30)
                continue

        # Run the check (fully async)
        has_email = False
        check_error = None
        try:
            result = await ec.check(cid)
            has_email = result.get("has_email", False)
        except Exception as e:
            check_error = str(e)

        # --- Process result (outside try to avoid log_msg crashes faking failures) ---
        if check_error:
            state.log_msg(f"[EMAIL] Error checking {name}: {check_error[:80]}")
            with state.lock:
                state.email_check_stats["errors"] += 1
                state.email_check_stats["last_result"] = {
                    "name": name, "id": cid, "result": "error",
                    "message": check_error[:80]
                }
                state.email_check_queue.append(item)
            await asyncio.sleep(5)
            continue

        if has_email:
            # Write to Google Sheets "5. Needs Email"
            sheet_row = [
                "",                       # channelemail
                name,                     # channelname
                cid,                      # channelid
                item.get("url", f"https://www.youtube.com/channel/{cid}"),  # channelurl
                str(item.get("subscribers", "")),  # subscribers
                item.get("country", ""),   # country
            ]
            sheet_saved = False
            sheet_error = None
            try:
                sheet_dedup.append_to_sheet("5. Needs Email", [sheet_row])
                sheet_saved = True
            except Exception as e:
                sheet_error = str(e)

            if sheet_saved:
                state.log_msg(f"[EMAIL] PASS {name} - saved to Google Sheets")
                with state.lock:
                    state.email_check_stats["passed"] += 1
                    state.email_check_stats["last_result"] = {
                        "name": name, "id": cid, "result": "passed",
                        "message": "Saved to Google Sheets"
                    }
            else:
                state.log_msg(f"[EMAIL] PASS {name} - sheet write failed: {sheet_error[:80]}")
                with state.lock:
                    state.email_check_stats["errors"] += 1
                    state.email_check_stats["last_result"] = {
                        "name": name, "id": cid, "result": "error",
                        "message": f"Sheet write failed: {sheet_error[:50]}"
                    }

            # Remove from accepted and add to rejected so it doesn't requalify
            with state.lock:
                state.accepted = [c for c in state.accepted if c["id"] != cid]
                state.rejected.add(cid)
                state.rejected.add(name.lower())
                state.email_checked.add(cid)
                state.stats["total_accepted"] = len(state.accepted)

        else:
            state.log_msg(f"[EMAIL] FAIL {name} - no business email button")
            with state.lock:
                state.accepted = [c for c in state.accepted if c["id"] != cid]
                state.rejected.add(cid)
                state.rejected.add(name.lower())
                state.email_checked.add(cid)
                state.stats["total_accepted"] = len(state.accepted)
                state.email_check_stats["failed"] += 1
                state.email_check_stats["last_result"] = {
                    "name": name, "id": cid, "result": "failed",
                    "message": "No business email button"
                }

        with state.lock:
            state.email_check_stats["completed"] += 1

        save_state()
        await asyncio.sleep(2)


# ----------------------------------------------
# RECOVERY — rejected channels are just re-queued into validation_queue
# and processed by validation_worker exactly like any hop-sourced channel.
# See /api/recover-rejected/start below.
# ----------------------------------------------


@app.on_event("startup")
async def on_startup():
    load_state()
    load_queue()
    # Refresh from Google Sheets
    try:
        result = await refresh_sheets_safe()
        print(f"  [Startup] Google Sheets loaded: {result.get('ids', 0)} IDs, "
              f"{result.get('names', 0)} names, {result.get('urls', 0)} URLs")
        state.log_msg(f"Google Sheets loaded — {result.get('ids', 0)} known channels")
    except Exception as e:
        print(f"  [Startup] Google Sheets load error: {e}")
        state.log_msg(f"WARNING: Could not load Google Sheets: {e}")
    # Start background workers
    asyncio.create_task(periodic_sheet_refresh())
    asyncio.create_task(validation_worker())
    asyncio.create_task(queue_saver())
    asyncio.create_task(email_check_worker())

# ----------------------------------------------
# MODELS
# ----------------------------------------------

class ChannelAction(BaseModel):
    channel_ids: List[str]
    action: str

class SelectAllRequest(BaseModel):
    select: bool

class ForceAddRequest(BaseModel):
    url: str              # YouTube channel URL, handle (@name), or UC... ID
    note: Optional[str] = ""

# ----------------------------------------------
# ENDPOINTS
# ----------------------------------------------

@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    content   = await file.read()
    blocklist = parse_excel_blocklist(content)
    state.blocklist = blocklist
    state.log_msg(f"Blocklist loaded: {len(blocklist)} channels")
    return {"blocklist_size": len(blocklist), "message": "Excel loaded"}

@app.post("/api/refresh-sheet")
async def refresh_sheet():
    """Manually trigger a refresh of the Google Sheet data."""
    try:
        result = await refresh_sheets_safe()
        state.log_msg(f"Manual sheet refresh: {result.get('ids', 0)} IDs, "
                     f"{result.get('names', 0)} names, {result.get('urls', 0)} URLs")
        return {"status": "ok", **result}
    except Exception as e:
        state.log_msg(f"Manual sheet refresh error: {e}")
        return {"status": "error", "error": str(e)}

@app.get("/api/sheet-status")
async def sheet_status():
    """Get the status of the Google Sheets dedup."""
    return sheet_dedup.status()

@app.post("/api/channels/discover")
async def discover_channels(data: dict):
    """Receive channels from hop_scraper.py — queue for validation."""
    channels = data.get("channels", [])
    queued   = 0
    skipped  = 0

    with state.lock:
        for ch in channels:
            cid      = ch.get("id", "")
            video_id = ch.get("video_id", "")
            url      = ch.get("url", "")
            name_l   = (ch.get("name") or "").lower()

            if not cid and not video_id:
                skipped += 1; continue
            if cid and cid in state.all_seen:
                skipped += 1; continue
            if cid in state.blocklist or name_l in state.blocklist or url in state.blocklist:
                skipped += 1; continue
            if sheet_dedup.is_known(channel_id=cid, name=name_l, url=url):
                skipped += 1; continue

            state.validation_queue.append(ch)
            if cid: state.all_seen.add(cid)
            queued += 1

        state.stats["queue_size"] = len(state.validation_queue)

    return {
        "queued": queued, "skipped": skipped,
        "queue_size": len(state.validation_queue),
        "total_pending": len(state.channels_found), "saved": queued,
    }

@app.post("/api/channels/force-add")
async def force_add_channel(req: ForceAddRequest):
    """
    Bypass all filters and add a channel directly to channels_found.
    Accepts: full YouTube URL, @handle, or UC... channel ID.
    Still fetches real metadata so the card shows correct info in the UI.
    """
    raw = req.url.strip()

    # Extract ID or handle from URL
    cid = extract_channel_id_from_url(raw) or raw

    async with httpx.AsyncClient() as client:
        # Resolve handle if needed
        if cid and not cid.startswith("UC"):
            resolved = await resolve_handle(client, cid)
            if not resolved:
                return {"error": f"Could not resolve channel: {raw}"}
            cid = resolved

        meta = await get_full_meta(client, cid)
        if not meta:
            return {"error": f"Could not fetch metadata for: {raw}"}

    channel_data = {
        "id":          meta["channel_id"],
        "name":        meta["channel_name"],
        "url":         meta["channel_url"],
        "subscribers": meta["subscriber_count"],
        "description": meta["description"][:250] + ("..." if len(meta["description"]) > 250 else ""),
        "uploadDate":  meta["last_upload_date"],
        "thumbnail":   meta.get("thumbnail"),
        "source":      "force_added",
        "niche_score": niche_score(f"{meta['channel_name'].lower()} {meta['description'].lower()}"),
        "note":        req.note or "",
        "timestamp":   datetime.now().isoformat(),
        "selected":    False,
    }

    with state.lock:
        existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
        if channel_data["id"] in existing:
            return {"message": "Channel already in list", "name": channel_data["name"]}
        state.channels_found.append(channel_data)
        state.all_seen.add(channel_data["id"])
        state.stats["validated_passed"] += 1

    state.log_msg(f"FORCE-ADD  {channel_data['name']} ({channel_data['subscribers']:,} subs)")
    save_state()
    return {"message": "Added", "name": channel_data["name"], "id": channel_data["id"]}

@app.post("/api/channels/force-save")
async def force_save_channel(req: ForceAddRequest):
    """
    Bypass ALL filters except dedup/blocklist and add directly to accepted.
    Accepts: full YouTube URL, @handle, or UC... channel ID.
    """
    raw = req.url.strip()

    # Extract ID or handle from URL
    cid = extract_channel_id_from_url(raw) or raw
    state.log_msg(f"FORCE-SAVE input='{raw}' -> extracted='{str(cid)[:60]}'")

    async with httpx.AsyncClient() as client:
        # Resolve handle if needed
        if cid and not cid.startswith("UC"):
            resolved = await resolve_handle(client, cid)
            if not resolved:
                return {"error": f"Could not resolve channel: {raw}"}
            cid = resolved

        meta = await get_full_meta(client, cid)
        if not meta:
            return {"error": f"Could not fetch metadata for: {raw}"}

    # Only check: blocklist + sheets + already saved/rejected (dedup)
    name_l = (meta.get("channel_name") or "").strip().lower()
    if meta["channel_id"] in state.blocklist or name_l in state.blocklist:
        return {"error": f"Channel is in blocklist — skipped", "name": meta["channel_name"]}
    if sheet_dedup.is_known(channel_id=meta["channel_id"], name=name_l, url=meta.get("channel_url", "")):
        return {"error": f"Channel already exists in Google Sheets — skipped", "name": meta["channel_name"]}

    with state.lock:
        existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
        if meta["channel_id"] in existing:
            return {"message": "Channel already in list", "name": meta["channel_name"]}
        if meta["channel_id"] in state.rejected or name_l in state.rejected:
            return {"message": "Channel was previously rejected — saving anyway", "name": meta["channel_name"]}

        channel_data = {
            "id":          meta["channel_id"],
            "name":        meta["channel_name"],
            "url":         meta["channel_url"],
            "subscribers": meta["subscriber_count"],
            "description": meta["description"][:250] + ("..." if len(meta["description"]) > 250 else ""),
            "uploadDate":  meta["last_upload_date"],
            "thumbnail":   meta.get("thumbnail"),
            "source":      "force_saved",
            "niche_score": niche_score(f"{meta['channel_name'].lower()} {meta['description'].lower()}"),
            "note":        req.note or "",
            "timestamp":   datetime.now().isoformat(),
            "selected":    False,
        }

        state.accepted.append(channel_data)
        state.all_seen.add(meta["channel_id"])
        state.stats["total_accepted"] += 1

    state.log_msg(f"FORCE-SAVE {channel_data['name']} ({channel_data['subscribers']:,} subs) — saved directly")
    save_state()
    return {"message": "Saved!", "name": channel_data["name"], "id": channel_data["id"]}

@app.get("/api/channels")
async def get_channels(
    limit:    int  = Query(50, ge=1),
    offset:   int  = Query(0,  ge=0),
    show_all: bool = Query(False)
):
    with state.lock:
        # Borderline channels appear in the same grid, flagged so the UI can badge them
        borderline_flagged = [{**c, "borderline": True} for c in state.borderline]
        pending = state.channels_found + borderline_flagged
        channels  = pending if not show_all else pending + state.accepted
        total     = len(channels)
        paginated = channels[offset:offset + limit]
    return {"channels": paginated, "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}

@app.get("/api/channels/borderline")
async def get_borderline(
    limit:  int = Query(50, ge=1, le=200),
    offset: int = Query(0,  ge=0)
):
    """
    Channels that passed all hard filters but scored below niche threshold.
    Review these manually — some will be your ICP that the filter missed.
    Use /api/channels/action with action=accept to approve any of them.
    """
    with state.lock:
        total     = len(state.borderline)
        paginated = state.borderline[offset:offset + limit]
    return {"channels": paginated, "total": total, "offset": offset, "limit": limit}

@app.post("/api/channels/action")
async def channel_action(action: ChannelAction):
    """Accept or reject channels. Works for both channels_found and borderline.
    Accepted channels are automatically queued for email checking."""
    newly_accepted = []
    with state.lock:
        for cid in action.channel_ids:
            # Check both found and borderline lists
            all_pending = state.channels_found + state.borderline
            for ch in all_pending:
                if ch["id"] == cid:
                    if action.action == "accept":
                        ch["selected"] = True
                        state.accepted.append(ch)
                        state.stats["total_accepted"] += 1
                        state.channels_found = [c for c in state.channels_found if c["id"] != cid]
                        state.borderline     = [c for c in state.borderline     if c["id"] != cid]
                        newly_accepted.append(ch)
                    elif action.action == "reject":
                        state.rejected.add(cid)
                        state.channels_found = [c for c in state.channels_found if c["id"] != cid]
                        state.borderline     = [c for c in state.borderline     if c["id"] != cid]
                        state.stats["total_rejected"] += 1
                    break
    save_state()

    # Queue newly accepted channels for email checking
    for ch in newly_accepted:
        if ch["id"] not in state.email_checked:
            with state.lock:
                state.email_check_queue.append(ch)
                state.email_check_stats["total"] += 1
            if state.email_check_stats["status"] == "idle":
                state.email_check_stats["status"] = "queued"

    if newly_accepted:
        state.log_msg(f"[EMAIL] Queued {len(newly_accepted)} channel(s) for email verification")

    return {"accepted": len(state.accepted), "rejected": len(state.rejected), "pending": len(state.channels_found)}

@app.post("/api/channels/select-all")
async def select_all(req: SelectAllRequest):
    with state.lock:
        for ch in state.channels_found:
            ch["selected"] = req.select
    return {"selected_count": sum(1 for c in state.channels_found if c["selected"])}

@app.get("/api/queue")
async def get_waiting_queue(limit: int = Query(300, ge=1), offset: int = Query(0, ge=0)):
    """Channels still waiting to be validated (e.g. while all API keys are exhausted)."""
    with state.lock:
        total = len(state.validation_queue)
        page = state.validation_queue[offset:offset + limit]
    items = [{"id": i.get("id", ""), "name": i.get("name", ""), "video_id": i.get("video_id", ""),
              "url": i.get("url", ""), "source": i.get("source", "")} for i in page]
    return {"total": total, "items": items, "keys_exhausted": keys.available_keys() == 0}

@app.get("/api/stats")
async def get_stats():
    return {**state.stats, "api_keys": keys.status()}

@app.get("/api/reject-stats")
async def get_reject_stats():
    return {"rejections": sorted(state.reject_log.items(), key=lambda x: -x[1])}

@app.get("/api/logs")
async def get_logs(limit: int = Query(50, ge=1, le=200)):
    return {"logs": state.log[-limit:]}

@app.get("/api/accepted")
async def get_accepted():
    return {"accepted": state.accepted, "count": len(state.accepted)}

@app.get("/api/blocklist")
async def get_blocklist():
    return {"blocklist": list(state.blocklist)}

# ----------------------------------------------
# EMAIL CHECK ENDPOINTS
# ----------------------------------------------

@app.get("/api/email-check/status")
async def email_check_status():
    """Get the status of the email checking process."""
    with state.lock:
        return {
            **state.email_check_stats,
            "queue_remaining": len(state.email_check_queue),
            "email_checked_count": len(state.email_checked),
        }

@app.post("/api/email-check/start")
async def start_email_check():
    """Start email checking for all accepted channels that haven't been checked yet."""
    with state.lock:
        to_check = [
            ch for ch in state.accepted
            if ch["id"] not in state.email_checked
        ]
        if not to_check:
            return {"message": "All accepted channels have already been email-checked", "queued": 0}

        for ch in to_check:
            state.email_check_queue.append(ch)

        state.email_check_stats["total"] += len(to_check)
        if state.email_check_stats["status"] == "idle":
            state.email_check_stats["status"] = "queued"

    state.log_msg(f"[EMAIL] Manual trigger: {len(to_check)} accepted channel(s) queued for email verification")
    return {"queued": len(to_check), "message": f"{len(to_check)} channel(s) queued for email checking"}

class EmailQueueRequest(BaseModel):
    channel_ids: List[str]

@app.post("/api/email-check/queue")
async def queue_selected_for_email_check(req: EmailQueueRequest):
    """Send specific accepted channels (picked in the Accepted tab) back to the
    email checker, e.g. ones that never got processed because the internet dropped
    or the backend restarted. Forces a re-check even if they were marked checked."""
    wanted = set(req.channel_ids)
    with state.lock:
        already_queued = {c["id"] for c in state.email_check_queue}
        to_queue = [ch for ch in state.accepted
                    if ch["id"] in wanted and ch["id"] not in already_queued]
        for ch in to_queue:
            state.email_checked.discard(ch["id"])
            state.email_check_queue.append(ch)
        skipped = len([i for i in wanted if i in already_queued])
        if to_queue:
            state.email_check_stats["total"] += len(to_queue)
            if state.email_check_stats["status"] == "idle":
                state.email_check_stats["status"] = "queued"
    save_state()
    state.log_msg(f"[EMAIL] {len(to_queue)} selected accepted channel(s) sent back to email check"
                  + (f" ({skipped} already queued)" if skipped else ""))
    return {"queued": len(to_queue), "already_queued": skipped}

@app.post("/api/email-check/process-accepted")
async def process_all_accepted():
    """⚠️ Process ALL currently accepted channels (including the initial batch).
    This will check each for a business email button. Those with email get saved
    to Google Sheet '5. Needs Email'. Those without get rejected."""
    with state.lock:
        accepted_count = len(state.accepted)
        already_checked = sum(1 for ch in state.accepted if ch["id"] in state.email_checked)
        to_check = [
            ch for ch in state.accepted
            if ch["id"] not in state.email_checked
        ]
        if not to_check:
            return {
                "message": "All channels have already been processed",
                "queued": 0,
                "accepted_total": accepted_count,
                "already_checked": already_checked
            }

        for ch in to_check:
            state.email_check_queue.append(ch)

        state.email_check_stats["total"] += len(to_check)

    state.log_msg(f"[EMAIL] Processing all accepted: {len(to_check)} channels queued for email check")
    return {
        "message": f"{len(to_check)} channel(s) queued for email checking",
        "queued": len(to_check),
        "accepted_total": accepted_count,
        "already_checked": already_checked
    }

# ----------------------------------------------
# RECOVERY ENDPOINTS — re-check wrongly rejected channels
# ----------------------------------------------

@app.post("/api/recover-rejected/start")
async def start_recover_rejected():
    """Re-queue every rejected UC channel into validation_queue — the exact same
    queue hop_scraper.py feeds into — so validation_worker processes them one
    by one, identically to any other channel.

    Note: rejected/all_seen bookkeeping is intentionally left untouched here.
    validation_worker() lifts the block for exactly one channel at a time, right
    when it's about to process it — never the whole batch up front. That way if
    the app restarts mid-recovery, every not-yet-processed channel is still
    sitting safely in state.rejected and this endpoint can just be called again
    to pick up where it left off, instead of that data being wiped the moment
    "Recover Rejected" is clicked.

    Channels already run through a recovery pass (pass, borderline, or still
    failed) are tracked in state.recovery_checked and skipped on subsequent
    calls — a rejected channel that failed recovery gets added back to
    state.rejected so it stays visible elsewhere, but re-checking it here
    would just waste quota re-deriving the same "still failing" result. Call
    /api/recover-rejected/reset to clear that and allow a full re-run."""
    if state.recover_stats["status"] == "running":
        return {"status": "error", "message": "Recovery already running"}

    with state.lock:
        existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
        queued_ids = {i["id"] for i in state.validation_queue if i.get("id")}
        rejected_ids = [
            x for x in state.rejected
            if isinstance(x, str) and x.startswith("UC")
            and x not in existing and x not in queued_ids
            and x not in state.recovery_checked
        ]

        for cid in rejected_ids:
            state.validation_queue.append({"id": cid, "name": "", "source": "recovered"})

        state.stats["queue_size"] = len(state.validation_queue)
        state.recover_stats["status"] = "running" if rejected_ids else "idle"
        state.recover_stats["total"] = len(rejected_ids)
        state.recover_stats["processed"] = 0
        state.recover_stats["recovered"] = 0
        state.recover_stats["still_failed"] = 0
        state.recover_stats["current"] = None

    state.log_msg(f"[RECOVER] {len(rejected_ids)} rejected channels re-queued for validation")
    return {"status": "ok", "message": "Recovery started", "queued": len(rejected_ids)}


@app.post("/api/recover-rejected/start-from-history")
async def start_recover_from_history():
    """Re-queue every channel hop_scraper.py has EVER discovered (from its own
    independent seen_urls/seen_channels log in hop_scraper_state.json) back
    through the same validation_queue -> validation_worker pipeline, as if it
    were a fresh discover() call.

    Why this exists: an earlier version of /api/recover-rejected/start stripped
    rejected/all_seen bookkeeping for ~1400 channels up front (not one at a
    time), and a crash mid-run wiped that in-memory bookkeeping before it was
    ever saved to disk — permanently losing track of which channels were
    pending recovery. hop_scraper.py keeps its own separate record of every
    channel it has ever seen, independent of state.json, so replaying that list
    through validation_worker re-derives correct pass/fail/borderline status
    for everything — including the lost channels. Anything already known (in
    all_seen, channels_found, accepted, borderline, or the Google Sheets) gets
    skipped automatically by the existing dedup checks in validation_worker, so
    this is safe to run even though it heavily overlaps with what's already
    been processed.
    """
    if state.recover_stats["status"] == "running":
        return {"status": "error", "message": "Recovery already running"}

    hop_state_path = os.path.join(os.path.dirname(__file__), "hop_scraper_state.json")
    try:
        with open(hop_state_path, encoding="utf-8") as f:
            hop_data = json.load(f)
    except Exception as e:
        return {"status": "error", "message": f"Could not read hop_scraper_state.json: {e}"}

    raw_entries = list(dict.fromkeys(hop_data.get("seen_urls", []) + hop_data.get("seen_channels", [])))

    def normalize(entry):
        if entry.startswith("http"):
            if "/channel/UC" in entry:
                cid = entry.split("/channel/")[-1].split("?")[0].split("/")[0]
                return {"id": cid, "name": "", "source": "recovered"}
            if "/@" in entry:
                handle = entry.split("/@")[-1].split("?")[0].split("/")[0]
                return {"id": f"@{handle}", "name": "", "source": "recovered"}
            return None
        # bare 11-char YouTube video ID
        return {"video_id": entry, "name": "", "source": "recovered"}

    with state.lock:
        queued_video_ids = {i.get("video_id") for i in state.validation_queue if i.get("video_id")}
        queued_ids       = {i.get("id") for i in state.validation_queue if i.get("id")}

        items = []
        for entry in raw_entries:
            item = normalize(entry)
            if not item:
                continue
            if item.get("video_id") and item["video_id"] in queued_video_ids:
                continue
            if item.get("id") and item["id"] in queued_ids:
                continue
            items.append(item)

        for item in items:
            state.validation_queue.append(item)

        state.stats["queue_size"] = len(state.validation_queue)
        state.recover_stats["status"] = "running" if items else "idle"
        state.recover_stats["total"] = len(items)
        state.recover_stats["processed"] = 0
        state.recover_stats["recovered"] = 0
        state.recover_stats["still_failed"] = 0
        state.recover_stats["current"] = None

    state.log_msg(f"[RECOVER] {len(items)} channels from hop history re-queued for validation "
                  f"({len(raw_entries)} total ever seen by hop_scraper)")
    return {"status": "ok", "message": "Recovery from history started",
            "queued": len(items), "total_seen": len(raw_entries)}


@app.get("/api/recover-rejected/status")
async def recover_rejected_status():
    """Get recovery progress for the frontend."""
    with state.lock:
        rejected_ids = {x for x in state.rejected if isinstance(x, str) and x.startswith("UC")}
        existing = {c["id"] for c in state.channels_found + state.accepted + state.borderline}
        queued_ids = {i["id"] for i in state.validation_queue if i.get("id")}
        remaining = [
            x for x in rejected_ids
            if x not in existing and x not in queued_ids and x not in state.recovery_checked
        ]
        return {
            **state.recover_stats,
            "checked_ever": len(state.recovery_checked),
            "remaining_to_check": len(remaining),
        }


@app.post("/api/recover-rejected/reset")
async def reset_recover_rejected():
    """Clear recovery_checked so every rejected channel becomes eligible for a
    recovery pass again. Use this when you deliberately want to re-run recovery
    from scratch — normal recovery runs never touch this on their own."""
    if state.recover_stats["status"] == "running":
        return {"status": "error", "message": "Recovery is currently running — wait for it to finish first"}

    with state.lock:
        count = len(state.recovery_checked)
        state.recovery_checked = set()
        state.recover_stats["status"] = "idle"
        state.recover_stats["total"] = 0
        state.recover_stats["processed"] = 0
        state.recover_stats["recovered"] = 0
        state.recover_stats["still_failed"] = 0
        state.recover_stats["current"] = None
    save_state()
    state.log_msg(f"[RECOVER] Reset — {count} previously-checked channels are eligible again")
    return {"status": "ok", "message": f"Reset {count} previously-checked channels"}


# ----------------------------------------------
# MANUAL HOP SCRAPER — launches hop_scraper.py as its own process (own
# console, own Chrome window on chrome_profile_hop), the same way the AI hop
# and email scrapers are launched below. Pause/resume suspend the whole
# process tree via psutil since the script has no pause logic of its own;
# the queue-driven /api/stats "status" field doubles as this button's
# running/paused/idle indicator (see validation_worker's idle branch).
# ----------------------------------------------

HOP_SCRAPER_SCRIPT = os.path.join(os.path.dirname(__file__), "hop_scraper.py")
_hop_scraper_proc = None
_hop_scraper_paused = False

def _hop_scraper_idle_status():
    if _hop_scraper_proc is not None and _hop_scraper_proc.poll() is None:
        return "paused" if _hop_scraper_paused else "running"
    return "idle"

def _suspend_tree(pid, suspend):
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for p in [proc] + proc.children(recursive=True):
        try:
            p.suspend() if suspend else p.resume()
        except psutil.NoSuchProcess:
            pass

@app.post("/api/start")
async def start_scraper():
    global _hop_scraper_proc, _hop_scraper_paused
    if _hop_scraper_proc is not None and _hop_scraper_proc.poll() is None:
        return {"status": "already_running"}
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    _hop_scraper_proc = subprocess.Popen(
        [sys.executable, HOP_SCRAPER_SCRIPT],
        cwd=os.path.dirname(__file__),
        creationflags=creationflags,
    )
    _hop_scraper_paused = False
    state.stats["status"] = "running"
    state.log_msg("[SCRAPER] Started hop_scraper.py (own window)")
    return {"status": "ok"}

@app.post("/api/stop")
async def stop_scraper():
    global _hop_scraper_proc, _hop_scraper_paused
    if _hop_scraper_proc is None or _hop_scraper_proc.poll() is not None:
        state.stats["status"] = "idle"
        return {"status": "not_running"}
    if _hop_scraper_paused:
        _suspend_tree(_hop_scraper_proc.pid, suspend=False)
        _hop_scraper_paused = False
    _hop_scraper_proc.terminate()
    state.stats["status"] = "stopped"
    state.log_msg("[SCRAPER] Stopped hop_scraper.py")
    return {"status": "ok"}

@app.post("/api/pause")
async def pause_scraper():
    global _hop_scraper_paused
    if _hop_scraper_proc is None or _hop_scraper_proc.poll() is not None:
        return {"status": "not_running"}
    _suspend_tree(_hop_scraper_proc.pid, suspend=True)
    _hop_scraper_paused = True
    state.stats["status"] = "paused"
    state.log_msg("[SCRAPER] Paused hop_scraper.py")
    return {"status": "ok"}

@app.post("/api/resume")
async def resume_scraper():
    global _hop_scraper_paused
    if _hop_scraper_proc is None or _hop_scraper_proc.poll() is not None:
        return {"status": "not_running"}
    _suspend_tree(_hop_scraper_proc.pid, suspend=False)
    _hop_scraper_paused = False
    state.stats["status"] = "running"
    state.log_msg("[SCRAPER] Resumed hop_scraper.py")
    return {"status": "ok"}

# ----------------------------------------------
# BUSINESS EMAIL SCRAPER — launches business_email_scraper.py as its own
# process (own console, own event loop, own tkinter overlay) instead of
# folding it into this server's async workers. It opens a real, visible
# Chrome window per Gmail profile in chrome_yt_profiles and drives the
# exact same sheet-read/sheet-write logic as the standalone script.
# ----------------------------------------------

BUSINESS_EMAIL_SCRAPER_SCRIPT = os.path.join(os.path.dirname(__file__), "business_email_scraper.py")
_business_scraper_proc = None

@app.post("/api/business-email-scraper/start")
async def start_business_email_scraper():
    global _business_scraper_proc
    if _business_scraper_proc is not None and _business_scraper_proc.poll() is None:
        return {"status": "already_running"}
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    _business_scraper_proc = subprocess.Popen(
        [sys.executable, BUSINESS_EMAIL_SCRAPER_SCRIPT],
        cwd=os.path.dirname(__file__),
        creationflags=creationflags,
    )
    state.log_msg("[BIZ EMAIL] Started business_email_scraper.py (own window)")
    return {"status": "ok"}

@app.get("/api/business-email-scraper/status")
async def business_email_scraper_status():
    running = _business_scraper_proc is not None and _business_scraper_proc.poll() is None
    return {"running": running}

# ----------------------------------------------
# EMAIL SCRAPER WITH AI — launches auto_email_scraper.py as its own process.
# Same accounts/profiles/sheet as the manual business email scraper, but the
# clicking (View email address -> captcha tick -> Submit -> grab email) and
# the rate-limit account switching are fully automatic. Progress is written
# to auto_email_status.json.
# ----------------------------------------------

AUTO_EMAIL_SCRIPT      = os.path.join(os.path.dirname(__file__), "auto_email_scraper.py")
AUTO_EMAIL_STATUS_FILE = os.path.join(os.path.dirname(__file__), "auto_email_status.json")
_auto_email_proc = None

@app.post("/api/email-ai/start")
async def start_email_ai():
    global _auto_email_proc
    if _auto_email_proc is not None and _auto_email_proc.poll() is None:
        return {"status": "already_running"}
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    _auto_email_proc = subprocess.Popen(
        [sys.executable, AUTO_EMAIL_SCRIPT],
        cwd=os.path.dirname(__file__),
        creationflags=creationflags,
    )
    state.log_msg("[EMAIL AI] Started auto_email_scraper.py (own window)")
    return {"status": "ok"}

@app.post("/api/email-ai/stop")
async def stop_email_ai():
    global _auto_email_proc
    if _auto_email_proc is None or _auto_email_proc.poll() is not None:
        return {"status": "not_running"}
    # every found email is written to the sheet immediately, so a hard
    # terminate loses at most the channel currently on screen
    _auto_email_proc.terminate()
    state.log_msg("[EMAIL AI] Stopped auto_email_scraper.py")
    return {"status": "ok"}

@app.get("/api/email-ai/status")
async def email_ai_status():
    running = _auto_email_proc is not None and _auto_email_proc.poll() is None
    progress = {}
    try:
        with open(AUTO_EMAIL_STATUS_FILE, encoding="utf-8") as f:
            progress = json.load(f)
    except Exception:
        pass
    return {"running": running, **progress}

# ----------------------------------------------
# HOP WITH AI — launches auto_hop.py as its own process (own console, own
# Chrome window on the same chrome_profile_hop profile). Fully automatic
# replacement for manual hopping: picks videos itself using the niche filter
# lists + local face detection, feeds /api/channels/discover exactly like
# the manual hop_scraper does. Progress is written to auto_hop_status.json.
# ----------------------------------------------

AUTO_HOP_SCRIPT      = os.path.join(os.path.dirname(__file__), "hop_assist.py")
AUTO_HOP_STATUS_FILE = os.path.join(os.path.dirname(__file__), "hop_assist_status.json")
HOP_ASSIST_CMD_DIR   = os.path.join(os.path.dirname(__file__), "hop_assist_cmds")
_auto_hop_proc = None

@app.post("/api/hop-ai/start")
async def start_hop_ai(tabs: int = Query(1, ge=1, le=10), autopick: bool = Query(True)):
    global _auto_hop_proc
    if _auto_hop_proc is not None and _auto_hop_proc.poll() is None:
        return {"status": "already_running"}
    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    _auto_hop_proc = subprocess.Popen(
        [sys.executable, AUTO_HOP_SCRIPT],
        cwd=os.path.dirname(__file__),
        creationflags=creationflags,
        env={**os.environ, "HOP_TABS": str(tabs), "HOP_AUTOPICK": "1" if autopick else "0"},
    )
    state.log_msg(f"[HOP AI] Started hop_assist.py with {tabs} tab(s), auto-pick={autopick} (own window)")
    return {"status": "ok"}

@app.post("/api/hop-ai/stop")
async def stop_hop_ai():
    global _auto_hop_proc
    if _auto_hop_proc is None or _auto_hop_proc.poll() is not None:
        return {"status": "not_running"}
    # auto_hop saves state after every hop, so a hard terminate loses at
    # most the current page's progress.
    _auto_hop_proc.terminate()
    state.log_msg("[HOP AI] Stopped auto_hop.py")
    return {"status": "ok"}

def _hop_assist_command(cmd: dict):
    os.makedirs(HOP_ASSIST_CMD_DIR, exist_ok=True)
    name = f"{time.time_ns()}_{cmd.get('type')}.json"
    tmp = os.path.join(HOP_ASSIST_CMD_DIR, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cmd, f)
    os.replace(tmp, os.path.join(HOP_ASSIST_CMD_DIR, name))

class HopPickRequest(BaseModel):
    tab: int
    prompt_id: str
    video_id: str

class HopSkipRequest(BaseModel):
    tab: int

@app.post("/api/hop-ai/pick")
async def hop_ai_pick(req: HopPickRequest):
    _hop_assist_command({"type": "pick", "tab": req.tab,
                         "prompt_id": req.prompt_id, "video_id": req.video_id})
    return {"status": "ok"}

@app.post("/api/hop-ai/skip")
async def hop_ai_skip(req: HopSkipRequest):
    _hop_assist_command({"type": "skip", "tab": req.tab})
    return {"status": "ok"}

@app.post("/api/hop-ai/next-batch")
async def hop_ai_next_batch():
    _hop_assist_command({"type": "next_batch"})
    return {"status": "ok"}

@app.get("/api/hop-ai/status")
async def hop_ai_status():
    running = _auto_hop_proc is not None and _auto_hop_proc.poll() is None
    progress = {}
    try:
        with open(AUTO_HOP_STATUS_FILE, encoding="utf-8") as f:
            progress = json.load(f)
    except Exception:
        pass
    return {"running": running, **progress}

# ----------------------------------------------
# KEYWORD DONE — signal for hop_scraper
# ----------------------------------------------

@app.post("/api/keyword/done")
async def keyword_done():
    """Called from the frontend — tells the scraper to move to the next keyword."""
    with state.next_keyword_lock:
        state.next_keyword = True
    state.log_msg("Keyword marked done — scraper will advance")
    return {"done": True}

@app.get("/api/keyword/check-done")
async def check_keyword_done():
    """Polled by hop_scraper.py — returns True once, then clears the flag."""
    with state.next_keyword_lock:
        if state.next_keyword:
            state.next_keyword = False
            return {"done": True}
        return {"done": False}

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "keys_available": keys.available_keys(),
        "queue": len(state.validation_queue),
        "pending": len(state.channels_found),
        "borderline": len(state.borderline),
        "accepted": len(state.accepted),
    }

@app.get("/api/export/csv")
async def export_csv():
    if not state.accepted:
        return {"error": "No accepted channels to export"}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Channel Name", "Channel ID", "Channel URL", "Subscribers", "Description", "Source", "Upload Date", "Niche Score"])
    for ch in state.accepted:
        writer.writerow([ch["name"], ch["id"], ch["url"], ch["subscribers"],
                         ch["description"], ch.get("source",""), ch["uploadDate"], ch.get("niche_score","")])
    output.seek(0)
    return StreamingResponse(io.BytesIO(output.getvalue().encode()), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=accepted_channels.csv"})

@app.get("/api/export/excel")
async def export_excel():
    if not state.accepted:
        return {"error": "No accepted channels to export"}
    df = pd.DataFrame(state.accepted).rename(columns={
        "name": "Channel Name", "id": "Channel ID", "url": "Channel URL",
        "subscribers": "Subscribers", "description": "Description",
        "source": "Source", "uploadDate": "Upload Date", "niche_score": "Niche Score",
    })
    df.insert(1, "Email", "")
    output = io.BytesIO()
    df.to_excel(output, index=False, engine="openpyxl")
    output.seek(0)
    return StreamingResponse(output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=accepted_channels.xlsx"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)