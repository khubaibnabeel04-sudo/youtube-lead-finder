"""
auto_hop.py — "Hop with AI"
============================
Fully automatic version of hop_scraper.py. Does the same harvesting
(search results + watch-page sidebar -> backend validation queue), but a
picker chooses which video to hop to next instead of a human:

  1. Candidates come ONLY from organic renderers (ytd-video-renderer /
     yt-lockup-view-model). Ads live in different DOM elements and we
     navigate by URL instead of clicking, so landing on an ad is impossible.
  2. Text scoring reuses the exact NICHE_SIGNALS / NEGATIVE_HARD lists from
     main.py — off-niche videos are rejected before they're considered.
     If sentence-transformers is installed, titles are also scored
     semantically against high-RPM vs off-niche exemplar titles (optional,
     local, free — the picker works fine without it).
  3. Candidates are enriched via the YouTube Data API (videos.list, 1 quota
     unit per 50 videos, cached): full description, tags, category, exact
     views/duration. Descriptions with monetization markers (affiliate
     links, sponsor codes, courses) mean a monetizing creator = high RPM.
     News/Gaming/Music/Sports categories are dropped outright.
  4. "Real person in thumbnail" = local YuNet face detection (OpenCV DNN,
     small ONNX model, offline; falls back to Haar cascades if the model is
     missing) on maxres thumbnails, plus a skin/texture check that tells a
     photographed human from a cartoon or game render. Cached per video ID.
  5. The picker LEARNS: every hop records which feature buckets (face,
     duration, views, category, ...) produced how many new channels, and —
     once the backend has validated them — how many actually PASSED. Future
     scoring gets a bonus/penalty from those running stats
     (auto_hop_learn.json).
  6. Dead ends backtrack: runner-up candidates from previous pages are
     remembered, so when a sidebar has nothing high-RPM the picker jumps to
     an earlier page's second-best pick instead of giving up on the keyword.
  7. Each picked video actually PLAYS (muted) for ~20-40s while scraping
     happens, so YouTube's recommender learns this profile wants high-RPM
     creator content — the sidebar quality compounds run after run.

Per keyword: search -> harvest -> hop up to MAX_HOPS_PER_KEYWORD times.
Early-stops only when even backtracking finds nothing high-RPM.

Shares hop_scraper_state.json (keyword index + seen urls) with the manual
scraper, so you can switch between manual and AI hopping freely.
Writes live progress to auto_hop_status.json for the app UI.
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
import requests
from patchright.async_api import async_playwright

from hop_scraper import (
    load_state, save_state, load_keywords, get_blocklist,
    send_channels, dedup, scrape_search_results, scrape_sidebar,
)
# Reuse the exact same filter lists/logic + API keys the backend uses.
# main.py has no import-time side effects (workers only start under uvicorn).
from main import niche_score, negative_hit, API_KEYS

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

MAX_HOPS_PER_KEYWORD = 25    # sidebar hops before advancing to next keyword
THUMB_CHECK_TOP_N    = 8     # thumbnail-analyze only the best text-scored candidates
MIN_HOP_SIGNAL       = 1     # candidate must hit at least this many niche signals
PICK_TOP_K           = 3     # hop to a random one of the K best (varies the path
                             # between runs so it doesn't loop the same videos)

SCROLL_TIME_BUDGET = 10.0    # max seconds spent scrolling + loading per page

DWELL_RANGE = (22.0, 40.0)   # seconds each picked video plays (muted) — teaches
                             # YouTube's recommender what this profile "likes".
                             # Runs concurrently with scraping, so the real
                             # extra wall-clock cost per hop is small.

MAX_BACKTRACKS_PER_KEYWORD = 6   # dead-end recoveries before truly early-stopping
TRAIL_KEEP    = 8            # how many previous pages' runner-ups to remember
ALTS_PER_PAGE = 4            # runner-ups remembered per page

SENT_RESOLVE_AFTER = 24 * 3600   # sent channel not validated-passed within 24h -> counts as failed
SENT_MAX_TRACKED   = 4000        # cap on outcome-tracking entries

STATUS_FILE       = "auto_hop_status.json"
FACE_CACHE_FILE   = "auto_hop_face_cache.json"
VISITED_FILE      = "auto_hop_visited.json"   # video ids hopped to, across all runs
ENRICH_CACHE_FILE = "auto_hop_enrich_cache.json"
LEARN_FILE        = "auto_hop_learn.json"

BACKEND_URL = "http://localhost:8000/api"

# NICHE_SIGNALS uses full words ("investing", "finance"); video titles often
# only contain shorter forms ("How I'd Invest $1000"). These stems exist ONLY
# to decide which video to hop to — the backend still validates every channel
# with the strict lists, so a loose match here can't pollute the final list.
HOP_EXTRA_STEMS = [
    "invest", "money", "stock", "wealth", "financ", "budget", "retire",
    "million", "billion", "net worth", "debt", "credit", "tax", "dividend",
    "income", "cash flow", "salary", "profit", "revenue", "$",
    "business", "entrepreneur", "startup", "market", "brand", "agenc",
    "ecommerce", "e-commerce", "saas", "software", "automat", "ai ",
    "real estate", "property", "rental", "mortgage", "airbnb",
    "crypto", "bitcoin", "ethereum", "trading", "portfolio", "401k", "roth",
    "side hustle", "passive", "freelanc", "client", "sales", "sell",
    "productiv", "habit", "rich", "frugal", "compound",
]

# Creators and news outlets write structurally different titles. Creator
# formulas ("How I Made $10k in 30 Days") get a bonus; headline-style titles
# ("...could tip economy into recession, says Apollo Global's...") get
# penalized, and known news outlets are rejected outright.
CREATOR_TITLE_RE = [re.compile(p) for p in (
    r"\bhow (i|we|to)\b", r"\bwhy (i|you)\b",
    r"\bi (tried|made|built|spent|quit|tested|turned|bought|sold|started|copied|paid|asked|found|lost)\b",
    r"\bi'?(m|ve|ll|d)\b", r"\bmy\b", r"\bwatch this\b",
    r"\byou (need|should|can|must)\b", r"\bthe truth about\b", r"\bnobody (tells|talks about)\b",
    r"\b\d+ (ways|things|mistakes|tips|habits|rules|lessons|steps|reasons|ideas|side hustles|niches|tools)\b",
    r"\bin \d+ (days|weeks|months|minutes|hours)\b", r"\$\d", r"\bper (month|week|day|year)\b",
    r"\bstep[- ]by[- ]step\b", r"\bfor beginners\b", r"\bbeginner'?s guide\b",
    r"\bfull (guide|course|tutorial)\b", r"\btutorial\b", r"\bchallenge\b",
    r"\bpassive income\b", r"\bside hustle\b", r"\bwithout (a|any|money|experience)\b",
    r"\bexposed\b", r"\bhonest\b", r"\bmistakes\b", r"\bmake money\b",
)]
NEWS_TITLE_RE = [re.compile(p) for p in (
    r"\bsays\b", r"\bwarns?\b", r"\bbreaking\b", r"\bnews\b", r"\breportedly\b",
    r"\bfull interview\b", r"\bpress conference\b", r"\btestif(y|ies)\b",
    r"\bfed chair\b", r"\bfederal reserve\b", r"\bwhite house\b", r"\bcongress\b",
    r"\bsenate\b", r"\belection\b", r"\btrump\b", r"\bbiden\b", r"\btariffs?\b",
    r"\bceo of\b", r"'[^']{12,}'", r"‘[^’]{12,}’",   # quote-style headlines
    r"\|\s*(cnbc|bloomberg|cnn|fox|forbes|wsj|reuters|yahoo finance)",
)]
NEWS_CHANNEL_RE = re.compile(
    r"\b(cnbc|bloomberg|cnn|msnbc|bbc|reuters|forbes|wsj|nbc news|abc news|"
    r"cbs news|sky news|fox news|fox business|yahoo finance|the economist|"
    r"financial times|business insider|news)\b|television",
    re.IGNORECASE,
)

def title_style_score(title):
    """Positive = written like a creator; negative = written like a headline."""
    t = title.lower()
    creator = sum(1 for p in CREATOR_TITLE_RE if p.search(t))
    news    = sum(1 for p in NEWS_TITLE_RE if p.search(t))
    return 1.5 * min(creator, 4) - 2.5 * news, news

def safe_print(msg):
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass

# ──────────────────────────────────────────────
# FACE DETECTION (free, offline)
# YuNet (OpenCV DNN, small ONNX model) with Haar cascades as fallback.
# ──────────────────────────────────────────────

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
YUNET_PATH = os.path.join(_BASE_DIR, "models", "face_detection_yunet_2023mar.onnx")
YUNET_URL  = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
              "face_detection_yunet/face_detection_yunet_2023mar.onnx")

_FRONTAL = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
_PROFILE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

_yunet = None   # None = not tried yet, False = unavailable, else the detector

def get_yunet():
    global _yunet
    if _yunet is None:
        try:
            if not os.path.exists(YUNET_PATH):
                os.makedirs(os.path.dirname(YUNET_PATH), exist_ok=True)
                r = requests.get(YUNET_URL, timeout=30)
                r.raise_for_status()
                with open(YUNET_PATH, "wb") as f:
                    f.write(r.content)
                safe_print("[Faces] downloaded YuNet model (~230 KB, one-time)")
            _yunet = cv2.FaceDetectorYN.create(YUNET_PATH, "", (320, 320), 0.6, 0.3, 5000)
            safe_print("[Faces] YuNet face detector active")
        except Exception as e:
            safe_print(f"[Faces] YuNet unavailable ({e}) — falling back to Haar cascades")
            _yunet = False
    return _yunet or None

def detect_faces(img):
    """Returns [(x, y, w, h), ...]. YuNet if available, Haar otherwise.
    Faces smaller than ~6% of the image width are ignored (b-roll crowds)."""
    h, w = img.shape[:2]
    min_w = w * 0.06
    det = get_yunet()
    if det is not None:
        scale = 640.0 / max(w, h) if max(w, h) > 640 else 1.0
        small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale != 1.0 else img
        det.setInputSize((small.shape[1], small.shape[0]))
        try:
            _, faces = det.detect(small)
        except Exception:
            faces = None
        out = []
        if faces is not None:
            for f in faces:
                x, y, fw, fh = (int(v / scale) for v in f[:4])
                if fw >= min_w:
                    out.append((x, y, fw, fh))
        return out
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    ms = (max(int(min_w), 24), max(int(min_w), 24))
    faces = _FRONTAL.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=ms)
    if len(faces) == 0:
        faces = _PROFILE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=ms)
    return [tuple(int(v) for v in f) for f in faces]

def looks_real_photo(img, box):
    """Photographed human vs cartoon/game render: real faces show skin-tone
    pixels (YCrCb range) and photographic texture; renders are flat-shaded."""
    x, y, w, h = box
    roi = img[max(y, 0):y + h, max(x, 0):x + w]
    if roi.size == 0:
        return False
    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    skin  = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))
    skin_ratio = float(skin.mean()) / 255.0
    texture    = float(roi.reshape(-1, 3).std(axis=0).mean())
    return skin_ratio > 0.15 and texture > 18.0

def _has_bold_text(gray):
    """Detects big bold thumbnail text without OCR: strong edges dilated into
    word-shaped blobs. Creator thumbnails have huge high-contrast words;
    news stills usually don't. Expects a ~480px-wide grayscale image."""
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    _, bw = cv2.threshold(grad, 60, 255, cv2.THRESH_BINARY)
    bw = cv2.dilate(bw, np.ones((3, 9), np.uint8), iterations=2)
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w >= 70 and 18 <= h <= 130 and w / h >= 1.6:
            return True
    return False

def fetch_thumbnail(video_id):
    """maxresdefault (1280px, no letterbox) first — small faces that are
    invisible at 480px are findable there — then hqdefault, letterbox cropped."""
    for variant in ("maxresdefault", "hqdefault"):
        try:
            r = requests.get(f"https://i.ytimg.com/vi/{video_id}/{variant}.jpg", timeout=8)
            if r.status_code != 200 or len(r.content) < 2000:
                continue
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if variant == "hqdefault" and img.shape[0] >= 320:
                img = img[45:315, :]   # crop hqdefault's black letterbox bars
            if img.shape[0] >= 180:
                return img
        except Exception:
            continue
    return None

FACE_CACHE_VER = 2   # v1 entries were Haar-based; recompute them with YuNet

face_cache = {}

def load_face_cache():
    global face_cache
    if os.path.exists(FACE_CACHE_FILE):
        try:
            with open(FACE_CACHE_FILE, "r", encoding="utf-8") as f:
                face_cache = json.load(f)
        except Exception:
            face_cache = {}

def save_face_cache():
    try:
        with open(FACE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(face_cache, f)
    except Exception:
        pass

def analyze_thumbnail(video_id):
    """Reads the creator-thumbnail signature off the video's thumbnail, all
    free/offline: a real photographed person, big bold text, punchy saturated
    colors. Cached per video ID."""
    cached = face_cache.get(video_id)
    if isinstance(cached, dict) and cached.get("v") == FACE_CACHE_VER:
        return cached
    out = {"v": FACE_CACHE_VER, "face": False, "big_face": False,
           "real_face": False, "text": False, "punchy": False}
    img = fetch_thumbnail(video_id)
    if img is not None:
        faces = detect_faces(img)
        if faces:
            out["face"] = True
            biggest = max(faces, key=lambda f: f[2])
            # talking-head thumbnails have BIG faces; news b-roll has small ones
            out["big_face"]  = biggest[2] >= img.shape[1] * 0.16
            out["real_face"] = looks_real_photo(img, biggest)
        # text/punchy heuristics are tuned for ~480px-wide images
        small = img if img.shape[1] <= 640 else \
            cv2.resize(img, (480, int(img.shape[0] * 480 / img.shape[1])))
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        out["text"] = _has_bold_text(gray)
        sat = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1]
        out["punchy"] = float(sat.mean()) >= 60.0
    face_cache[video_id] = out
    return out

def thumb_score(a):
    s = 0.0
    if a.get("face"):      s += 2.0
    if a.get("real_face"): s += 2.0   # photographed human, not a render
    if a.get("big_face"):  s += 1.5
    if a.get("text"):      s += 1.5
    if a.get("punchy"):    s += 1.0
    return s

# ──────────────────────────────────────────────
# DATA API ENRICHMENT (videos.list — 1 quota unit per 50 videos, cached)
# ──────────────────────────────────────────────

YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

_key_i     = 0
_dead_keys = set()   # keys that hit quota this run

def yt_videos_items(video_ids):
    """Batched videos.list. Returns (items, api_ok). Rotates through the same
    API_KEYS main.py uses; a quota-dead key stays dead for this run."""
    global _key_i
    items, api_ok = [], True
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        got = None
        for _ in range(len(API_KEYS)):
            if len(_dead_keys) >= len(API_KEYS):
                return items, False
            key = API_KEYS[_key_i % len(API_KEYS)]
            if key in _dead_keys:
                _key_i += 1
                continue
            try:
                r = requests.get(YT_VIDEOS_URL, params={
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(batch), "maxResults": 50, "key": key,
                }, timeout=15)
            except Exception:
                api_ok = False
                break
            if r.status_code == 200:
                got = r.json().get("items", [])
                break
            if r.status_code in (403, 429):
                _dead_keys.add(key)
                _key_i += 1
                continue
            api_ok = False
            break
        if got is None:
            api_ok = False
        else:
            items.extend(got)
    return items, api_ok

def iso_duration_s(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m or not any(m.groups()):
        return None
    h, mi, se = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + se

# Description markers of a MONETIZING creator (affiliate/sponsor/course/
# newsletter links) — nearly the definition of a high-RPM commercial niche.
MONETIZATION_RE = [re.compile(p, re.IGNORECASE) for p in (
    r"affiliate", r"sponsor", r"use (my )?code", r"discount code", r"promo code",
    r"my (free )?(course|newsletter|community|coaching|program|ebook|guide|masterclass|workshop)",
    r"join (my|the) (community|newsletter|discord|skool)",
    r"skool\.com", r"patreon\.com", r"gumroad\.com", r"kajabi", r"teachable",
    r"stan\.store", r"beacons\.ai", r"free training", r"book a call",
    r"1[- ]on[- ]1", r"mentorship", r"consulting",
)]

# YouTube category ids. Education/Sci-Tech/HowTo lean high-RPM;
# News/Gaming/Music/Sports are dropped outright (low RPM / not creators).
CATEGORY_BONUS = {
    "27":  2.0,   # Education
    "28":  1.5,   # Science & Technology
    "26":  1.0,   # Howto & Style
    "22":  0.5,   # People & Blogs
    "24": -1.5,   # Entertainment
    "23": -1.5,   # Comedy
    "1":  -2.0,   # Film & Animation
}
CATEGORY_DROP = {"25", "20", "10", "17"}   # News, Gaming, Music, Sports

enrich_cache = {}

def load_enrich_cache():
    global enrich_cache
    if os.path.exists(ENRICH_CACHE_FILE):
        try:
            with open(ENRICH_CACHE_FILE, "r", encoding="utf-8") as f:
                enrich_cache = json.load(f)
        except Exception:
            enrich_cache = {}

def save_enrich_cache():
    try:
        # keep the newest ~30k entries so the file can't grow forever
        if len(enrich_cache) > 30000:
            for k in list(enrich_cache.keys())[:len(enrich_cache) - 30000]:
                del enrich_cache[k]
        with open(ENRICH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(enrich_cache, f)
    except Exception:
        pass

def enrich_videos(video_ids):
    """Fill enrich_cache for any not-yet-known ids (one batched API call)."""
    missing = [v for v in video_ids if v not in enrich_cache]
    if not missing:
        return
    items, api_ok = yt_videos_items(missing)
    for it in items:
        sn    = it.get("snippet", {}) or {}
        stats = it.get("statistics", {}) or {}
        desc  = sn.get("description", "") or ""
        tags  = sn.get("tags", []) or []
        hay   = (desc + " " + " ".join(tags)).lower()
        e = {
            "cat":    sn.get("categoryId"),
            "nscore": min(niche_score(hay) + sum(1 for s in HOP_EXTRA_STEMS if s in hay), 12),
            "mono":   sum(1 for rx in MONETIZATION_RE if rx.search(desc)),
            "neg":    bool(negative_hit(hay)),
            "ch":     sn.get("channelId", ""),
        }
        dur = iso_duration_s(it.get("contentDetails", {}).get("duration"))
        if dur is not None:
            e["dur"] = dur
        try:
            e["views"] = int(stats["viewCount"])
        except (KeyError, ValueError, TypeError):
            pass
        enrich_cache[it["id"]] = e
    if api_ok:
        # ids the API answered for but didn't return = deleted/private videos;
        # cache them empty so we never ask again. (Not cached when the API
        # itself failed — those deserve a retry once quota is back.)
        for v in missing:
            enrich_cache.setdefault(v, {})

def enrich_score(e):
    if not e:
        return 0.0
    s = CATEGORY_BONUS.get(e.get("cat"), 0.0)
    s += 0.5 * min(e.get("nscore", 0), 6)   # niche density of description+tags
    s += 0.8 * min(e.get("mono", 0), 3)     # monetizing-creator markers
    if e.get("neg"):
        s -= 3.0
    return s

# ──────────────────────────────────────────────
# SEMANTIC TITLE SCORING (optional — needs `pip install sentence-transformers`)
# ──────────────────────────────────────────────

HIGH_RPM_EXEMPLARS = [
    "How I'd Invest $1,000 If I Was Starting Over",
    "The 7 Income Streams That Made Me a Millionaire",
    "How to Start a Business With No Money",
    "I Tried Dropshipping for 30 Days (Honest Results)",
    "Roth IRA Explained: The Best Retirement Account for Beginners",
    "How This 28-Year-Old Makes $40K/Month With Airbnb",
    "5 Side Hustles You Can Start Today With Your Laptop",
    "Why 99% of Small Businesses Fail (And How to Avoid It)",
    "How I Built a $10K/Month SaaS as a Solo Founder",
    "The Real Estate Investing Strategy Nobody Talks About",
    "Watch This Before You Buy Your First Rental Property",
    "How to Actually Get Rich in Your 20s (Realistic Guide)",
    "My Honest Advice for Anyone Starting a Marketing Agency",
    "ChatGPT Just Changed How I Run My Business Forever",
    "How I Make Passive Income With AI Tools (Full Guide)",
    "Beginner's Guide to Dividend Investing (Step by Step)",
    "How Much Money My Business Made This Year (Full Breakdown)",
    "The Credit Card Strategy Banks Don't Want You to Know",
    "How to Negotiate a $20K Raise (Word for Word Script)",
    "I Quit My 9-5 to Freelance: Here's What Happened",
    "Email Marketing Tutorial: How I Get 50% Open Rates",
    "Amazon FBA for Beginners: Complete Guide",
]
OFF_NICHE_EXEMPLARS = [
    "We Spent 24 Hours in a Haunted House!",
    "Minecraft But Every Block Is Random",
    "NBA Top 10 Plays of the Night",
    "Full Highlights: Lakers vs Warriors",
    "Trying Viral TikTok Food Hacks",
    "My Morning Routine as a College Student",
    "GRWM: First Date Edition",
    "Fortnite Chapter 5 Live Event (Full Reaction)",
    "Official Trailer (2025 Movie)",
    "Best Goals of the Premier League Season",
    "ASMR 100 Triggers in 10 Minutes",
    "Extreme Hide and Seek in a Mall",
    "Learn Colors With Cartoon Animals for Kids",
    "Relaxing Piano Music for Sleep",
    "Fed Chair Powell's Full Press Conference",
    "Breaking: Markets Tumble as Tariffs Announced",
]

_embedder = None   # None = not tried, False = unavailable
_ex_pos = _ex_neg = None

def get_embedder():
    global _embedder, _ex_pos, _ex_neg
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
            _ex_pos = _embedder.encode(HIGH_RPM_EXEMPLARS, normalize_embeddings=True)
            _ex_neg = _embedder.encode(OFF_NICHE_EXEMPLARS, normalize_embeddings=True)
            safe_print("[Embeddings] sentence-transformers active — semantic title scoring on")
        except Exception:
            _embedder = False
            safe_print("[Embeddings] not installed — stem matching only "
                       "(`pip install sentence-transformers` to enable)")
    return _embedder or None

def embed_diffs(titles):
    """Per title: (similarity to high-RPM exemplars) - (similarity to off-niche
    ones), roughly [-1, 1]. Returns None when embeddings are unavailable."""
    m = get_embedder()
    if not m or not titles:
        return None
    try:
        vecs = m.encode(titles, normalize_embeddings=True)
        pos = (vecs @ _ex_pos.T).max(axis=1)
        neg = (vecs @ _ex_neg.T).max(axis=1)
        return pos - neg
    except Exception:
        return None

# ──────────────────────────────────────────────
# LEARNING STORE — which hop features actually produce channels that PASS
# ──────────────────────────────────────────────

learn = {
    "global":  {"hops": 0, "yield": 0, "resolved": 0, "passed": 0},
    "buckets": {},   # bucket -> {hops, yield, resolved, passed}
    "sent":    {},   # channel key -> {keys, buckets, ts} awaiting an outcome
}

def load_learn():
    global learn
    if os.path.exists(LEARN_FILE):
        try:
            with open(LEARN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in ("global", "buckets", "sent"):
                if k in data:
                    learn[k] = data[k]
        except Exception:
            pass

def save_learn():
    try:
        with open(LEARN_FILE, "w", encoding="utf-8") as f:
            json.dump(learn, f)
    except Exception:
        pass

def channel_key(c):
    """Stable key for matching a sent channel to backend outcomes:
    @handle from its URL when available, else lowercased name."""
    m = re.search(r"/@([\w.\-]+)", (c.get("url") or "").lower())
    if m:
        return "@" + m.group(1)
    n = (c.get("name") or "").strip().lower()
    return n or None

def outcome_keys(ch):
    """All keys a validated backend channel can be matched under."""
    ks = set()
    m = re.search(r"/@([\w.\-]+)", (ch.get("url") or "").lower())
    if m:
        ks.add("@" + m.group(1))
    n = (ch.get("name") or "").strip().lower()
    if n:
        ks.add(n)
    return ks

def _bucket_stats(b):
    return learn["buckets"].setdefault(
        b, {"hops": 0, "yield": 0, "resolved": 0, "passed": 0})

def feature_buckets(p):
    """Coarse feature buckets of a candidate, used as learning keys."""
    a = p.get("a") or {}
    e = p.get("e") or {}
    b = ["face:" + ("Y" if a.get("face") else "N")]
    if a.get("face"):
        b.append("real:" + ("Y" if a.get("real_face") else "N"))
    d = p.get("dur")
    b.append("dur:" + ("sweet" if d and 480 <= d <= 1500 else
                       "short" if d and d < 480 else "long" if d else "unk"))
    v = p.get("views")
    b.append("views:" + ("mid" if v and 1e4 <= v <= 1e6 else
                         "big" if v and v > 1e6 else
                         "small" if v is not None else "unk"))
    if e.get("cat"):
        b.append("cat:" + str(e["cat"]))
    sig = p.get("signal", 0)
    b.append("sig:" + ("hi" if sig >= 4 else "mid" if sig >= 2 else "lo"))
    b.append("mono:" + ("Y" if e.get("mono") else "N"))
    return b

def learn_update_yield(buckets, n_new_channels):
    g = learn["global"]
    g["hops"]  += 1
    g["yield"] += n_new_channels
    for b in buckets:
        d = _bucket_stats(b)
        d["hops"]  += 1
        d["yield"] += n_new_channels

def learn_record_sent(keys, buckets):
    now = time.time()
    for k in keys:
        learn["sent"][k] = {"keys": [k], "buckets": buckets, "ts": now}
    # keep the tracker bounded (dict preserves insertion order -> oldest first)
    excess = len(learn["sent"]) - SENT_MAX_TRACKED
    if excess > 0:
        for k in list(learn["sent"].keys())[:excess]:
            del learn["sent"][k]

def _resolve_sent(rec, passed):
    g = learn["global"]
    g["resolved"] += 1
    g["passed"]   += 1 if passed else 0
    for b in rec.get("buckets", []):
        d = _bucket_stats(b)
        d["resolved"] += 1
        d["passed"]   += 1 if passed else 0

def sync_outcomes():
    """Match previously-sent channels against the backend's validated lists
    (passed + borderline + accepted). Sent >24h ago and never passed = failed.
    This is what turns raw yield-tracking into acceptance-weighted learning."""
    if not learn["sent"]:
        return
    passed = set()
    try:
        offset = 0
        while True:
            r = requests.get(f"{BACKEND_URL}/channels",
                             params={"show_all": "true", "limit": 200, "offset": offset},
                             timeout=10)
            data = r.json()
            for ch in data.get("channels", []):
                passed |= outcome_keys(ch)
            if not data.get("has_more"):
                break
            offset += 200
    except Exception as e:
        safe_print(f"[Learn] outcome sync skipped ({e})")
        return
    now = time.time()
    n_pass = n_fail = 0
    for pk, rec in list(learn["sent"].items()):
        hit = any(k in passed for k in rec.get("keys", []))
        if hit:
            _resolve_sent(rec, True)
            del learn["sent"][pk]
            n_pass += 1
        elif now - rec.get("ts", now) > SENT_RESOLVE_AFTER:
            _resolve_sent(rec, False)
            del learn["sent"][pk]
            n_fail += 1
    if n_pass or n_fail:
        g = learn["global"]
        safe_print(f"[Learn] outcomes synced: +{n_pass} passed, +{n_fail} failed "
                   f"(lifetime: {g['passed']}/{g['resolved']} passed)")

def learn_bonus(buckets):
    """Score adjustment from history: buckets that produced more channels per
    hop — and more channels that PASSED validation — score higher."""
    g = learn["global"]
    if g["hops"] < 10:
        return 0.0
    g_yield = g["yield"] / g["hops"]
    g_pass  = (g["passed"] / g["resolved"]) if g["resolved"] >= 20 else None
    bonus = 0.0
    for b in buckets:
        d = learn["buckets"].get(b)
        if not d or d["hops"] < 5:
            continue
        by = d["yield"] / d["hops"]
        bonus += max(-0.8, min(0.8, 0.6 * (by - g_yield)))
        if g_pass is not None and d.get("resolved", 0) >= 8:
            bp = d["passed"] / d["resolved"]
            bonus += max(-1.0, min(1.0, 3.0 * (bp - g_pass)))
    return max(-2.5, min(2.5, bonus))

# ──────────────────────────────────────────────
# VISITED VIDEOS (persistent across runs/keywords)
# ──────────────────────────────────────────────

visited_global = set()

def load_visited():
    global visited_global
    if os.path.exists(VISITED_FILE):
        try:
            with open(VISITED_FILE, "r", encoding="utf-8") as f:
                visited_global = set(json.load(f))
        except Exception:
            visited_global = set()

def save_visited():
    try:
        with open(VISITED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(visited_global)[-20000:], f)
    except Exception:
        pass

# ──────────────────────────────────────────────
# STATUS FILE (read by /api/hop-ai/status)
# ──────────────────────────────────────────────

_status = {
    "phase": "starting", "keyword": "", "keyword_idx": 0, "total_keywords": 0,
    "hop": 0, "max_hops": MAX_HOPS_PER_KEYWORD, "channels_sent": 0,
    "last_pick": None, "message": "",
}

def write_status(**kw):
    _status.update(kw)
    _status["updated_at"] = datetime.now().isoformat()
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_status, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass

# ──────────────────────────────────────────────
# CANDIDATE EXTRACTION
# ──────────────────────────────────────────────

SEARCH_CANDIDATES_JS = """
() => {
    const out = [];
    document.querySelectorAll('ytd-video-renderer').forEach(v => {
        // Ads/promoted results live in their own renderers — exclude anything
        // inside them, plus anything carrying a Sponsored/Ad badge.
        if (v.closest('ytd-ad-slot-renderer, ytd-promoted-sparkles-web-renderer, ytd-search-pyv-renderer, ytd-in-feed-ad-layout-renderer')) return;
        if (v.querySelector('.badge-style-type-ad, ytd-badge-supported-renderer [aria-label="Sponsored"]')) return;
        const t = v.querySelector('a#video-title');
        if (!t) return;
        const href = t.getAttribute('href') || '';
        if (!href.includes('/watch') || !href.includes('v=')) return;
        const videoId = href.split('v=')[1].split('&')[0];
        const title = (t.textContent || '').trim();
        const chEl = v.querySelector('ytd-channel-name a');
        const channel = chEl ? chEl.textContent.trim() : '';
        const meta = v.querySelector('#metadata-line');
        let badgeText = '';
        const badge = v.querySelector('ytd-thumbnail-overlay-time-status-renderer, yt-thumbnail-badge-view-model, .badge-shape-wiz__text');
        if (badge) badgeText = (badge.textContent || '').trim();
        out.push({ videoId, title, channel, metaText: meta ? meta.innerText : '', badgeText });
    });
    return out;
}
"""

SIDEBAR_CANDIDATES_JS = """
() => {
    const out = [];
    document.querySelectorAll('yt-lockup-view-model').forEach(item => {
        let videoId = null;
        for (const a of item.querySelectorAll('a')) {
            const href = a.getAttribute('href') || '';
            if (href.includes('/watch') && href.includes('v=')) {
                videoId = href.split('v=')[1].split('&')[0];
                break;
            }
        }
        if (!videoId) return;
        let title = '';
        const h3 = item.querySelector('h3');
        if (h3) title = h3.innerText.trim();
        if (!title) {
            const at = item.querySelector('a[title]');
            if (at) title = (at.getAttribute('title') || '').trim();
        }
        let channel = '', metaText = '';
        const meta = item.querySelector('yt-content-metadata-view-model');
        if (meta) {
            metaText = meta.innerText.trim();
            const lines = metaText.split('\\n').map(s => s.trim()).filter(Boolean);
            if (lines.length) channel = lines[0];
        }
        let badgeText = '';
        const badge = item.querySelector('yt-thumbnail-badge-view-model, .badge-shape-wiz__text, ytd-thumbnail-overlay-time-status-renderer');
        if (badge) badgeText = (badge.textContent || '').trim();
        out.push({ videoId, title, channel, metaText, badgeText });
    });
    return out;
}
"""

async def extract_candidates(page, page_type):
    try:
        js = SEARCH_CANDIDATES_JS if page_type == "search" else SIDEBAR_CANDIDATES_JS
        return await page.evaluate(js) or []
    except Exception as e:
        safe_print(f"  [Pick] candidate extraction error: {e}")
        return []

# ──────────────────────────────────────────────
# SCORING / PICKING
# ──────────────────────────────────────────────

def parse_views(meta_text):
    m = re.search(r"([\d.,]+)\s*([KMB]?)\s*views", meta_text or "", re.IGNORECASE)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2).upper(), 1)
        return int(n * mult)
    except Exception:
        return None

def parse_duration(text):
    """'12:34' or '1:02:34' -> seconds, else None."""
    m = re.search(r"\b(\d+):(\d{2})(?::(\d{2}))?\b", text or "")
    if not m:
        return None
    if m.group(3):
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2))

def mostly_english(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 5:
        return False
    ascii_letters = sum(1 for c in letters if ord(c) < 128)
    return ascii_letters / len(letters) > 0.8

def hop_signal_score(haystack):
    """Strict niche signals + loose stems, for hop-picking only."""
    score = niche_score(haystack)
    score += sum(1 for s in HOP_EXTRA_STEMS if s in haystack)
    return score

def views_duration_bonus(views, dur):
    s = 0.0
    if views is not None:
        if 10_000 <= views <= 1_000_000:
            s += 1.5
        elif views > 2_000_000:          # mega-viral = huge channels / news
            s -= 1.5
    if dur is not None:
        if 480 <= dur <= 1500:           # 8-25 min: classic monetized creator video
            s += 1.5
        elif dur < 120 or dur > 3600:    # news clips/shorts, lives/podcasts
            s -= 2.0
    return s

def make_info(p):
    a = p.get("a") or {}
    return {"title": p["title"][:90], "channel": p["channel"][:50],
            "face": bool(a.get("face")), "real_face": bool(a.get("real_face")),
            "niche": p["signal"], "score": round(p["s"], 1),
            "buckets": feature_buckets(p)}

def rank_candidates(candidates, visited_videos, seen_channel_names):
    """All filters + scoring. Returns candidates sorted best-first; the top
    THUMB_CHECK_TOP_N carry a thumbnail analysis in p["a"].

    Hard rejects: already-visited video (channels stay allowed — a different
    video from the same creator opens new recommendations), live streams,
    news outlets / headline-style titles, negative-niche hit, non-English,
    zero high-RPM signals (unless the semantic score clearly disagrees),
    dropped categories (News/Gaming/Music/Sports, from the Data API)."""
    pre = []
    for c in candidates:
        vid     = c.get("videoId") or ""
        title   = (c.get("title") or "").strip()
        channel = (c.get("channel") or "").strip()
        meta    = c.get("metaText") or ""
        badge   = c.get("badgeText") or ""
        if not vid or vid in visited_videos:
            continue
        if len(title) < 8:
            continue
        if "watching" in meta.lower() or "live" in badge.lower():   # live stream
            continue
        if not mostly_english(title):
            continue
        if channel and NEWS_CHANNEL_RE.search(channel):             # news outlet
            continue
        hay = f"{title} {channel}".lower()
        if negative_hit(hay):
            continue
        style, news_hits = title_style_score(title)
        if news_hits >= 2:                                          # headline-style title
            continue
        pre.append({"c": c, "vid": vid, "title": title, "channel": channel,
                    "meta": meta, "badge": badge,
                    "signal": hop_signal_score(hay), "style": style})
    if not pre:
        return []

    # Semantic title score (optional, local). Lets a clearly on-niche title
    # with zero stem hits through, and vice versa.
    diffs = embed_diffs([p["title"] for p in pre])
    for i, p in enumerate(pre):
        p["embed"] = float(diffs[i]) if diffs is not None else None
    pre = [p for p in pre if p["signal"] >= MIN_HOP_SIGNAL
           or (p["embed"] is not None and p["embed"] >= 0.15)]
    if not pre:
        return []

    # Data API enrichment (batched + cached; graceful no-op without quota)
    enrich_videos([p["vid"] for p in pre])
    kept = []
    for p in pre:
        e = enrich_cache.get(p["vid"]) or {}
        if e.get("cat") in CATEGORY_DROP:
            continue
        p["e"] = e
        kept.append(p)
    pre = kept
    if not pre:
        return []

    for p in pre:
        e = p["e"]
        s = 2.0 * min(p["signal"], 5) + p["style"]
        if p["embed"] is not None:
            s += 4.0 * max(-0.5, min(0.75, p["embed"]))
        p["views"] = e["views"] if "views" in e else parse_views(p["meta"])
        p["dur"]   = e["dur"]   if "dur"   in e else parse_duration(p["badge"])
        s += views_duration_bonus(p["views"], p["dur"])
        s += enrich_score(e)
        if p["channel"] and p["channel"].lower() not in seen_channel_names:
            s += 1.5
        p["s"] = s
    pre.sort(key=lambda p: -p["s"])

    # Thumbnails + learned history for the finalists only (downloads cost time)
    finalists = pre[:THUMB_CHECK_TOP_N]
    for p in finalists:
        p["a"] = analyze_thumbnail(p["vid"])
        p["s"] += thumb_score(p["a"])
        p["s"] += learn_bonus(feature_buckets(p))
    finalists.sort(key=lambda p: -p["s"])
    return finalists + pre[THUMB_CHECK_TOP_N:]

def pick_from_ranked(ranked):
    """Returns (chosen, info, alternates). Real photographed people first: if
    any finalist has a real face, only those can win; else any face; else all.
    The winner is a weighted-random one of the top PICK_TOP_K so different
    runs take different paths. Alternates feed the backtracking trail."""
    if not ranked:
        return None, "no high-RPM candidates", []
    finalists = [p for p in ranked if "a" in p]
    real  = [p for p in finalists if p["a"].get("real_face")]
    faced = [p for p in finalists if p["a"].get("face")]
    pool  = real or faced or finalists or ranked
    top = pool[:PICK_TOP_K]
    weights = [0.55, 0.30, 0.15][:len(top)]
    chosen = random.choices(top, weights=weights, k=1)[0]
    alternates = [p for p in ranked if p is not chosen][:ALTS_PER_PAGE]
    return chosen, make_info(chosen), alternates

# ──────────────────────────────────────────────
# HARVEST (same as manual hop_scraper loop) + DWELL
# ──────────────────────────────────────────────

# Reports whether YouTube is mid-load (continuation spinner visible) and how
# many organic video items are rendered, so we wait only as long as needed.
LOAD_STATE_JS = """() => {
  const sp = document.querySelector('ytd-continuation-item-renderer tp-yt-paper-spinner[active]');
  return {
    loading: !!(sp && sp.offsetParent !== null),
    items: document.querySelectorAll('ytd-video-renderer, yt-lockup-view-model').length,
  };
}"""

# Muted playback still counts as watch history — this is what teaches the
# recommender that chrome_profile_hop "likes" high-RPM creator content.
ENSURE_PLAYING_JS = """() => {
  const v = document.querySelector('video');
  if (!v) return false;
  v.muted = true;
  if (v.paused) { try { v.play(); } catch (e) {} }
  return !v.paused;
}"""

async def scroll_slowly(page):
    """Scroll down and wait only while YouTube is actually loading.

    Instead of fixed sleeps, after each scroll step we poll the page: if the
    continuation spinner is visible or new video items are still appearing we
    keep waiting; the moment the page settles we scroll again. Hard-capped at
    SCROLL_TIME_BUDGET seconds total so a page never eats more than ~10s."""
    deadline = time.monotonic() + SCROLL_TIME_BUDGET
    last_items = -1
    while time.monotonic() < deadline:
        try:
            await page.mouse.wheel(0, 1200)
        except Exception:
            return
        await asyncio.sleep(0.4)
        # wait for this batch to finish loading, then move on immediately
        stable = 0
        while time.monotonic() < deadline:
            try:
                st = await page.evaluate(LOAD_STATE_JS)
            except Exception:
                return
            if st["loading"] or st["items"] != last_items:
                last_items = st["items"]
                stable = 0
                await asyncio.sleep(0.35)
                continue
            stable += 1
            if stable >= 2:   # settled twice in a row -> loaded
                break
            await asyncio.sleep(0.25)

async def harvest(page, page_type, blocklist, seen):
    """Scrape channels off the current page and send new ones to the backend.
    Returns (count, channel keys) — the keys feed outcome tracking."""
    if page_type == "search":
        channels = await scrape_search_results(page)
    else:
        channels = await scrape_sidebar(page)
    filtered = [
        c for c in channels
        if c.get("id") not in blocklist
        and c.get("name", "").lower() not in blocklist
    ]
    new_channels = dedup(filtered, seen)
    keys = []
    if new_channels:
        result = send_channels(new_channels)
        if result:
            safe_print(f"  [Queued] {result.get('queued', 0)} for validation "
                       f"| {result.get('total_pending', 0)} pending in app")
        keys = [k for k in (channel_key(c) for c in new_channels) if k]
    return len(new_channels), keys

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

async def main():
    keywords  = load_keywords()
    state     = load_state()
    blocklist = get_blocklist()
    load_face_cache()
    load_visited()
    load_enrich_cache()
    load_learn()
    sync_outcomes()   # fold backend validation results into the learned stats
    save_learn()

    idx  = state.get("current_keyword_idx", 0)
    seen = set(state.get("seen_urls", []))
    seen_channel_names = set()
    channels_sent = 0

    g = learn["global"]
    safe_print("=" * 60)
    safe_print(f"[Hop with AI] {len(keywords)} keywords | resuming at #{idx + 1}")
    safe_print(f"[Hop with AI] blocklist: {len(blocklist)} | seen: {len(seen)} "
               f"| thumb cache: {len(face_cache)} | visited videos: {len(visited_global)}")
    safe_print(f"[Hop with AI] learned from {g['hops']} hops "
               f"({g['passed']}/{g['resolved']} sent channels passed validation)")
    safe_print(f"[Hop with AI] up to {MAX_HOPS_PER_KEYWORD} hops per keyword, "
               f"{MAX_BACKTRACKS_PER_KEYWORD} backtracks before early stop")
    safe_print("=" * 60)

    write_status(phase="starting", total_keywords=len(keywords), keyword_idx=idx,
                 channels_sent=0, message="launching browser")

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir="chrome_profile_hop",
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    # keep rendering + JS timers alive when the window is
                    # minimized or covered by other windows
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
                ],
                viewport={"width": 1400, "height": 900},
            )
        except Exception as e:
            msg = ("Could not open Chrome profile - is the manual hop scraper "
                   f"already running? ({e})")
            safe_print(f"[Hop with AI] {msg}")
            write_status(phase="error", message=msg)
            return

        page = await context.new_page()

        try:
            while idx < len(keywords):
                kw = keywords[idx]
                safe_print(f"\n[Keyword {idx + 1}/{len(keywords)}] {kw}")
                write_status(phase="searching", keyword=kw, keyword_idx=idx,
                             hop=0, channels_sent=channels_sent, last_pick=None,
                             message="loading search results")

                await page.goto(
                    f"https://www.youtube.com/results?search_query={kw.replace(' ', '+')}",
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(3)
                # Scroll down so lazy-loaded results render (max ~10s)
                await scroll_slowly(page)

                n, keys_sent = await harvest(page, "search", blocklist, seen)
                channels_sent += n

                candidates = await extract_candidates(page, "search")
                ranked = rank_candidates(candidates, visited_global, seen_channel_names)
                chosen, info, alternates = pick_from_ranked(ranked)

                trail = []        # runner-ups of pages we already hopped through
                backtracks = 0
                hop = 0
                stop_reason = None

                while hop < MAX_HOPS_PER_KEYWORD:
                    if chosen is None:
                        # Dead end — try a runner-up from an earlier page
                        # instead of abandoning the keyword.
                        alt = None
                        while trail and alt is None:
                            page_alts = trail[-1]
                            while page_alts:
                                cand = page_alts.pop(0)
                                if cand["vid"] not in visited_global:
                                    alt = cand
                                    break
                            if alt is None:
                                trail.pop()
                        if alt is None or backtracks >= MAX_BACKTRACKS_PER_KEYWORD:
                            stop_reason = info if isinstance(info, str) else "no candidates"
                            break
                        backtracks += 1
                        if "a" not in alt:
                            alt["a"] = analyze_thumbnail(alt["vid"])
                            alt["s"] += thumb_score(alt["a"])
                        chosen, info = alt, make_info(alt)
                        safe_print(f"  [Backtrack {backtracks}/{MAX_BACKTRACKS_PER_KEYWORD}] "
                                   f"runner-up: {info['title'][:60]}")

                    hop += 1
                    visited_global.add(chosen["vid"])
                    if chosen["channel"]:
                        seen_channel_names.add(chosen["channel"].lower())

                    safe_print(f"  [Hop {hop}/{MAX_HOPS_PER_KEYWORD}] "
                               f"{info['title']} | {info['channel']} "
                               f"| face={'REAL' if info['real_face'] else 'Y' if info['face'] else 'N'} "
                               f"| signal={info['niche']}")
                    write_status(phase="hopping", hop=hop, channels_sent=channels_sent,
                                 last_pick=info, message="")

                    hop_buckets = info["buckets"]
                    t0 = time.monotonic()
                    await page.goto(
                        f"https://www.youtube.com/watch?v={chosen['vid']}",
                        wait_until="domcontentloaded",
                    )
                    await asyncio.sleep(random.uniform(2.5, 4.0))
                    try:
                        await page.evaluate(ENSURE_PLAYING_JS)
                    except Exception:
                        pass
                    # Scroll down so the sidebar loads more recommendations (max ~10s)
                    await scroll_slowly(page)

                    n, keys_sent = await harvest(page, "video", blocklist, seen)
                    channels_sent += n
                    learn_update_yield(hop_buckets, n)
                    learn_record_sent(keys_sent, hop_buckets)

                    state["seen_urls"] = list(seen)
                    save_state(state)

                    candidates = await extract_candidates(page, "sidebar")
                    ranked = rank_candidates(candidates, visited_global, seen_channel_names)
                    next_chosen, next_info, next_alternates = pick_from_ranked(ranked)

                    # Dwell: let the video keep playing so the recommender
                    # learns this profile's taste. Most of the time was already
                    # spent scraping, so only the remainder is actually waited.
                    elapsed = time.monotonic() - t0
                    dwell   = random.uniform(*DWELL_RANGE)
                    if elapsed < dwell:
                        await asyncio.sleep(dwell - elapsed)

                    trail.append(alternates[:ALTS_PER_PAGE])
                    trail = trail[-TRAIL_KEEP:]
                    chosen, info, alternates = next_chosen, next_info, next_alternates

                if hop < MAX_HOPS_PER_KEYWORD and stop_reason:
                    safe_print(f"  [Early stop] after {hop} hops "
                               f"({backtracks} backtracks) - {stop_reason}")

                # Advance keyword
                idx += 1
                state["current_keyword_idx"] = idx
                state["seen_urls"] = list(seen)
                save_state(state)
                save_face_cache()
                save_visited()
                save_enrich_cache()
                save_learn()
                blocklist = get_blocklist()   # refresh once per keyword
                write_status(phase="keyword_done", keyword_idx=idx,
                             channels_sent=channels_sent,
                             message=f"finished '{kw}' after {hop} hops")

            write_status(phase="finished", channels_sent=channels_sent,
                         message="all keywords processed")
            safe_print("\n[Hop with AI] All keywords processed!")

        except KeyboardInterrupt:
            safe_print("\n[Hop with AI] Stopping - saving state...")
        finally:
            state["current_keyword_idx"] = idx
            state["seen_urls"] = list(seen)
            save_state(state)
            save_face_cache()
            save_visited()
            save_enrich_cache()
            save_learn()
            write_status(phase="stopped", channels_sent=channels_sent,
                         message=f"stopped at keyword #{idx + 1}")
            try:
                await context.close()
            except Exception:
                pass
            safe_print(f"[Hop with AI] State saved. Next keyword: #{idx + 1}")

if __name__ == "__main__":
    asyncio.run(main())
