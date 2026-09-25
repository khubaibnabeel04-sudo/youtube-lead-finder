"""
hop_groq.py — lets a Groq-hosted LLM pick the next video from the popup list.

It does ONE job: given the numbered candidate list, return the index of the best
next video for this lead-discovery crawl. If the API is slow, rate-limited or
answers nonsense, the caller falls back to the top-ranked candidate, so a tab
never stalls waiting on it.

Key: GROQ_API_KEY in the project .env (falls back to backend/keys/groq_api_key.txt).
"""

import json
import os
import re
import time

import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
TIMEOUT_S  = 8

_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys", "groq_api_key.txt")

def get_key():
    k = os.environ.get("GROQ_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

SYSTEM_PROMPT = """You choose which YouTube video to open next in a lead-discovery crawl. The crawl hops from video to video to discover many DIFFERENT YouTube creator channels in high-paying (high-RPM) niches, which will later be contacted for a service. You are given the search keyword, the video being watched now, the recent trail of videos already hopped through, and a numbered list of candidate videos. Pick exactly ONE.

HARD RULE - ENGLISH ONLY: never pick a video whose title contains ANY non-English word. This includes other languages written in Latin letters (Hindi/Urdu/Hinglish such as "kaise", "paise", "kamaye", "hai"; Indonesian/Malay such as "cara", "uang"; Spanish, Portuguese, French, German, Turkish, etc.) and any non-Latin script. Names of people, brands and places are fine; a whole non-English word or phrase is not. Skip those candidates completely, however good they look. If EVERY candidate breaks this rule, answer {"pick": 0}.

Otherwise judge in this priority order:
1. TOPIC NOVELTY (most important): pick the candidate whose topic is different from the current video and from the recent trail - even only a little bit different (a new sub-topic, new angle, adjacent niche or new tool/strategy). Hopping into the same sub-topic again keeps YouTube's sidebar recycling the same channels, so a fresh topic is what surfaces new channels. Avoid candidates that repeat a topic already in the trail; among all candidates choose the one that adds the most new territory.
2. HIGH-RPM NICHE: the new topic must still be high-paying: personal finance, investing, business / entrepreneurship, marketing, SaaS and AI tools for business, real estate, side hustles, freelancing, e-commerce. Drifting away from the search keyword is completely fine as long as it stays in such a niche. Never pick news, politics, gaming, music, sports, entertainment, kids or off-niche content.
3. REAL PERSON: a real photographed person in the thumbnail (real_person=true) is a strong plus; solo-creator / talking-head videos beat b-roll, cartoons, slides or logos.
4. CHANNEL SIZE: channel size does NOT matter for the choice and small channels get no special treatment. When two candidates are about equally new in topic, prefer the BIGGER one (higher view count / more established channel). A big channel with a slightly new topic beats a tiny channel with a repeated topic.
5. MONETIZING (monetizing=true) is a small plus. Prefer creator-style titles (how I..., tutorial, guide, step by step, results) over headline or news-style titles, and normal-length videos over very short clips or multi-hour streams.

Use "niche_signal" and "rank_score" only as weak tie-breakers. Do not explain. Reply with ONLY a JSON object: {"pick": <number of the chosen video>} (or {"pick": 0} if every candidate has a non-English title)."""

def _fmt_views(n):
    if n is None:
        return "unknown"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}K"
    return str(n)

def _fmt_dur(s):
    if s is None:
        return "unknown"
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def build_user_message(suggestions, keyword, watching, history=None):
    lines = [f'Search keyword: "{keyword}"',
             f"Currently watching: {watching or '(the search results page)'}"]
    if history:
        lines.append("Recent trail (oldest first): " + " || ".join(history))
    lines += ["", "Candidates:"]
    for n, v in enumerate(suggestions, 1):
        lines.append(
            f'{n}. "{v["title"]}" | channel: {v["channel"]} | views: {_fmt_views(v.get("views"))} '
            f'| duration: {_fmt_dur(v.get("dur"))} | real_person: {str(bool(v.get("real_face"))).lower()} '
            f'| monetizing: {str(bool(v.get("mono"))).lower()} '
            f'| niche_signal: {v.get("niche")} | rank_score: {v.get("score")}')
    return "\n".join(lines)

# Each Groq model has its own free-tier token allowance (per minute and per day), so when
# one is rate-limited we move on to the next instead of giving up. A model that answered 429
# is skipped until its "try again in ..." time has passed.
MODELS = [m.strip() for m in os.environ.get(
    "GROQ_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")
    if m.strip()]
TOTAL_BUDGET_S = 12
_cooldown = {}          # model -> monotonic time until which it is skipped
_last_error = ""

def _retry_after_s(message):
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", message or "")
    if m and any(m.groups()):
        h, mi, se = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + se + 1
    return 60.0

def last_error():
    return _last_error

def pick_index(suggestions, keyword, watching, history=None):
    """Blocking call (run it in a thread). Returns a 0-based index into `suggestions`,
    -1 if the model says no candidate is acceptable (all non-English), or None if no
    model could answer (caller falls back to the top-ranked candidate)."""
    global _last_error
    key = get_key()
    if not key or not suggestions:
        return None
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(suggestions, keyword, watching, history)},
    ]
    t_start = time.monotonic()
    for model in MODELS:
        if time.monotonic() - t_start > TOTAL_BUDGET_S:
            break
        if _cooldown.get(model, 0) > time.monotonic():
            continue
        payload = {
            "model": model, "messages": messages, "temperature": 0.2,
            "max_completion_tokens": 400, "response_format": {"type": "json_object"},
        }
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"
        try:
            r = requests.post(GROQ_URL, json=payload, timeout=TIMEOUT_S,
                              headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 429:
                try:
                    msg = r.json()["error"]["message"]
                except Exception:
                    msg = ""
                _cooldown[model] = time.monotonic() + _retry_after_s(msg)
                _last_error = f"{model}: rate limited"
                continue
            if r.status_code != 200:
                _cooldown[model] = time.monotonic() + 30
                _last_error = f"{model}: HTTP {r.status_code}"
                continue
            text = r.json()["choices"][0]["message"]["content"]
            n = int(json.loads(text)["pick"])
            if n == 0:
                return -1                   # every candidate is non-English
            if 1 <= n <= len(suggestions):
                return n - 1
            _last_error = f"{model}: bad pick {n}"
        except Exception as e:
            _last_error = f"{model}: {type(e).__name__}"
    return None
