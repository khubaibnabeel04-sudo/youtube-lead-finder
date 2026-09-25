"""
hop_assist.py — "Hop with AI" (assisted mode)
=============================================
Same harvesting as auto_hop.py, but YOU choose where to hop:

  1. A tab opens the search results for its keyword and scrolls all the way
     down (waiting for YouTube's loading spinner every time) until nothing more
     loads, harvesting every channel into the backend validation queue.
  2. The best 10-15 candidate videos (same scoring/filters as auto_hop.py:
     niche signals, negative lists, creator-style titles, Data API enrichment,
     face detection on thumbnails) are published to the app as a picker popup.
  3. You click one. That video opens, its recommendations are scrolled + harvested,
     a fresh list is offered, and so on. No hop limit.

Several tabs can run in parallel (HOP_TABS env var, default 1), each on its own
keyword. Videos offered to any tab in the current batch are never offered to
another tab. "Next Batch" (button in the app) moves every tab on to the next
keywords whenever you want.

IPC with the backend (main.py):
  hop_assist_status.json   this script -> app   (tabs, suggestions, counters)
  hop_assist_cmds/*.json   app -> this script   (pick / skip / next_batch)

Shares hop_scraper_state.json (keyword index + seen urls), the visited-video
list and the learn/enrich/face caches with auto_hop.py, so nothing already
saved changes shape.
"""

import asyncio
import glob
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# The title-embedding model is cached locally; skip HuggingFace's slow online check.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from patchright.async_api import async_playwright

import auto_hop
from auto_hop import (
    safe_print, load_face_cache, save_face_cache, load_visited, save_visited,
    load_enrich_cache, save_enrich_cache, load_learn, save_learn, sync_outcomes,
    learn_update_yield, learn_record_sent, feature_buckets, rank_candidates,
    extract_candidates, harvest, ENSURE_PLAYING_JS,
)
from hop_scraper import load_state, save_state, load_keywords, get_blocklist
from hop_overlay import OVERLAY_JS, HIDE_OVERLAY_JS, attach_bridge
import hop_groq

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

NUM_TABS               = max(1, int(os.environ.get("HOP_TABS", "1")))
SUGGESTIONS_PER_PROMPT = 12      # videos offered in each popup (10-15)
THUMB_TOP_N            = 14      # thumbnails analysed per page (must cover the popup size)

# Scrolling. Every scroll step is followed by a check of the page; we keep going
# until we are at the bottom AND nothing new has loaded AND no spinner is showing.
FAST_ITEMS          = 55           # scroll until this many videos have loaded, THEN show the popup ...
FAST_SECONDS        = 40.0         # ... or until this long has passed, whichever comes first
MIN_SUGGESTIONS     = 8            # if fewer relevant videos than this, keep scrolling before showing
SCROLL_STEP_PX      = (650, 950)   # pixels per step (random in range)
SCROLL_STEP_PAUSE   = (0.35, 0.6)  # seconds after each step before checking
SCROLL_LOAD_WAIT    = 20.0         # max seconds to wait for one spinner to finish
SCROLL_QUIET_END    = 6            # quiet checks at bottom when no continuation exists
SCROLL_QUIET_STUCK  = 8            # quiet checks at bottom when a continuation exists but never loads
SCROLL_MAX_SECONDS  = 180.0        # safety cap per page (YouTube feeds can be endless)
SCROLL_MAX_ITEMS    = 400          # safety cap on video items per page

AUTOPICK = os.environ.get("HOP_AUTOPICK", "1") == "1" and bool(hop_groq.get_key())

_groq_semaphore = None
def _groq_sem():
    global _groq_semaphore
    if _groq_semaphore is None:
        _groq_semaphore = asyncio.Semaphore(5)      # a few calls at once is plenty for 10 tabs
    return _groq_semaphore

LOW_QUALITY_JS = """() => {
  const p = document.getElementById('movie_player');
  if (p && p.setPlaybackQualityRange) { p.setPlaybackQualityRange('tiny'); p.setPlaybackQuality('tiny'); }
}"""

STATUS_FILE = "hop_assist_status.json"
CMD_DIR     = "hop_assist_cmds"

PROFILE_DIR = os.environ.get("HOP_PROFILE", "chrome_profile_hop")
HEADLESS    = os.environ.get("HOP_HEADLESS", "0") == "1"

# ──────────────────────────────────────────────
# STATUS / COMMANDS
# ──────────────────────────────────────────────

status = {
    "phase": "starting", "batch": 0, "batch_size": NUM_TABS,
    "keyword_idx": 0, "total_keywords": 0, "channels_sent": 0,
    "tabs": [], "message": "",
}

def write_status(**kw):
    status.update(kw)
    status["updated_at"] = datetime.now().isoformat()
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass

def set_tab(i, **kw):
    for t in status["tabs"]:
        if t["tab"] == i:
            t.update(kw)
            break
    write_status()

def drain_commands():
    out = []
    os.makedirs(CMD_DIR, exist_ok=True)
    for path in sorted(glob.glob(os.path.join(CMD_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            pass
        try:
            os.remove(path)
        except Exception:
            pass
    return out

# ──────────────────────────────────────────────
# SCROLLING
# ──────────────────────────────────────────────

# kind = "search" (results page) or "watch" (recommendation sidebar). Scoped to
# the right container so the comments section's spinner is never mistaken for
# the recommendations' one.
SCROLL_STATE_JS = """(kind) => {
  const root = kind === 'search'
      ? (document.querySelector('ytd-search') || document)
      : (document.querySelector('#secondary') || document);
  const itemSel = kind === 'search'
      ? 'ytd-video-renderer'
      : 'yt-lockup-view-model, ytd-compact-video-renderer';
  const items = root.querySelectorAll(itemSel);
  // Only the recommendation/result list's own continuation counts — the watch page
  // also holds hidden zero-height ones for comments, replies and the transcript panel.
  const conts = Array.from(root.querySelectorAll('ytd-continuation-item-renderer')).filter(c =>
      !c.closest('ytd-engagement-panel-section-list-renderer, ytd-comments, ytd-comment-replies-renderer')
      && c.getBoundingClientRect().height > 0);
  const loading = conts.some(c => {
      const sp = c.querySelector('tp-yt-paper-spinner[active], tp-yt-paper-spinner.active');
      return !!(sp && sp.offsetParent !== null);
  });
  // The "end" of the list: its continuation spinner if there is one, else the last video.
  const endEl = conts.length ? conts[conts.length - 1] : (items.length ? items[items.length - 1] : null);
  let endTop = null, endAbs = 0;
  if (endEl) {
      const r = endEl.getBoundingClientRect();
      endTop = r.top;
      endAbs = Math.round(r.top + window.scrollY);
  }
  return {
    items:   items.length,
    more:    conts.length > 0,
    loading: loading,
    endTop:  endTop,
    endAbs:  endAbs,
    vh:      window.innerHeight,
  };
}"""

async def _scroll_state(page, kind):
    return await page.evaluate(SCROLL_STATE_JS, kind)

# Extra ways to make YouTube load more, tried whenever the list looks finished:
# scroll any inner scroll container around the list to its bottom, and click a
# visible "Show more" button that belongs to the list itself.
NUDGE_JS = r"""(kind) => {
  const listRoot = kind === 'search'
      ? (document.querySelector('ytd-search') || document)
      : (document.querySelector('ytd-watch-next-secondary-results-renderer')
         || document.querySelector('#secondary') || document);
  const itemSel = kind === 'search' ? 'ytd-video-renderer' : 'yt-lockup-view-model, ytd-compact-video-renderer';
  const items = listRoot.querySelectorAll(itemSel);
  const last = items.length ? items[items.length - 1] : null;
  let moved = 0, clicked = 0;
  let e = last ? last.parentElement : null;
  while (e && e !== document.body && e !== document.documentElement) {
    const cs = getComputedStyle(e);
    if (/(auto|scroll)/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 20) {
      e.scrollTop = e.scrollHeight; moved++;
    }
    e = e.parentElement;
  }
  listRoot.querySelectorAll('button').forEach(b => {
    if (b.closest('ytd-engagement-panel-section-list-renderer, ytd-comments')) return;
    if (b.offsetParent === null) return;
    const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')).trim().toLowerCase();
    if (/^(show|load|see) more( videos| results)?$/.test(t)) { b.click(); clicked++; }
  });
  return {moved, clicked};
}"""

SCROLL_LOG = "hop_assist_scroll.log"

def log_scroll(tab_i, kind, info):
    try:
        with open(SCROLL_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} tab{tab_i} {kind} "
                    f"items={info.get('items')} {info.get('seconds')}s reason={info.get('reason')} "
                    f"more_flag={info.get('more')} loading_flag={info.get('loading')}\n")
    except Exception:
        pass

async def scroll_to_end(page, kind, on_progress=None, stop_items=None, max_seconds=None,
                        reset_top=True):
    """Scroll until the list has nothing more to load. Returns a summary dict.

    The target is the END OF THE LIST (its loading spinner, or the last video),
    not the bottom of the page — on watch pages the comments column is far taller
    than the recommendations and would otherwise drag us away from the spinner.
    Loop: scroll toward the end in moderate steps, park with the end in view,
    and wait for YouTube's spinner. New videos or a moved end reset the 'quiet'
    counter. When parked, nothing grew and no spinner is showing we count quiet
    rounds; each one wiggles the scroll (up a little, back to the end) to
    re-trigger YouTube's infinite-scroll observer before declaring the end."""
    t0 = time.monotonic()
    deadline = t0 + (max_seconds or SCROLL_MAX_SECONDS)
    try:
        st = await _scroll_state(page, kind)
    except Exception as e:
        return {"items": 0, "seconds": 0.0, "reason": f"error: {e}"}
    best_items, best_end = st["items"], st["endAbs"]
    quiet, empty, stuck_waits, reason = 0, 0, 0, "time cap"
    prev_end_top = None
    while time.monotonic() < deadline:
        if st["items"] >= SCROLL_MAX_ITEMS:
            reason = "item cap"
            break
        if stop_items and st["items"] >= stop_items:
            reason = "enough"
            break
        try:
            top, vh = st["endTop"], st["vh"]
            if top is None:
                dy = random.randint(*SCROLL_STEP_PX)          # nothing rendered yet
            elif top > vh - 150:
                dy = min(top - (vh - 150), random.randint(*SCROLL_STEP_PX))
            elif top < 0:
                dy = top - (vh - 250)                         # overshot: bring the end back
            else:
                dy = 0                                        # parked at the end
            parked = abs(dy) <= 40          # close enough to the end (the page may not scroll any further)
            if dy and not parked:
                await page.evaluate("s => window.scrollBy(0, s)", dy)
            await asyncio.sleep(random.uniform(*SCROLL_STEP_PAUSE))
            st = await _scroll_state(page, kind)

            still_loading = False
            if st["loading"]:
                lw = time.monotonic()
                while st["loading"] and time.monotonic() - lw < SCROLL_LOAD_WAIT \
                        and time.monotonic() < deadline:
                    await asyncio.sleep(0.4)
                    st = await _scroll_state(page, kind)
                still_loading = st["loading"]
                await asyncio.sleep(0.5)          # let the new batch render
                st = await _scroll_state(page, kind)

            if st["items"] == 0 and not st["loading"]:
                empty += 1
                if empty >= 16:
                    reason = "nothing loaded"
                    break
                continue
            empty = 0
            grew = st["items"] > best_items
            best_items = max(best_items, st["items"])
            best_end   = max(best_end, st["endAbs"])
            if on_progress:
                on_progress(st["items"])
            if grew:
                quiet = stuck_waits = 0
                continue
            if still_loading:
                # spinner spun for a full wait and nothing arrived - only give up if it
                # happens twice in a row (a slow connection must not end the scroll early)
                stuck_waits += 1
                if stuck_waits >= 2:
                    reason = "spinner stuck - no more results"
                    break
            if not parked:
                if st["endTop"] is not None and prev_end_top is not None \
                        and abs(st["endTop"] - prev_end_top) < 1:
                    parked = True                 # scrolling did nothing: we are at the bottom
                else:
                    prev_end_top = st["endTop"]
                    continue                      # still travelling toward the end

            quiet += 1
            if quiet >= (SCROLL_QUIET_STUCK if st["more"] else SCROLL_QUIET_END):
                reason = "no more loading" if not st["more"] else "spinner never loaded more"
                break
            # Looks finished - try hard to shake out more before believing it.
            try:
                await page.evaluate(NUDGE_JS, kind)
            except Exception:
                pass
            if quiet % 2 == 0:
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                await asyncio.sleep(1.2)
            else:
                await page.evaluate("window.scrollBy(0, -500)")
                await asyncio.sleep(0.5)
            st = await _scroll_state(page, kind)
            if st["endTop"] is not None:
                await page.evaluate("s => window.scrollBy(0, s)", st["endTop"] - (st["vh"] - 150) + 20)
            await asyncio.sleep(1.5 if st["more"] else 1.0)
            st = await _scroll_state(page, kind)
        except Exception as e:
            reason = f"error: {e}"
            break
    if reset_top:
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
    return {"items": best_items, "seconds": round(time.monotonic() - t0, 1), "reason": reason,
            "more": bool(st.get("more")), "loading": bool(st.get("loading"))}

# ──────────────────────────────────────────────
# SHARED BATCH STATE
# ──────────────────────────────────────────────

class Shared:
    def __init__(self, state, seen):
        self.state = state
        self.seen = seen
        self.blocklist = set()
        self.seen_channel_names = set()
        self.claimed = set()              # video ids offered to any tab this batch
        self.channels_sent = 0
        self.warm = None                  # task: AI models loading in the background
        self.keywords = []
        self.next_kw = 0                  # next keyword index to hand out
        self.active = {}                  # tab index -> keyword index it is working on

    def take_keyword(self):
        """Hand out the next unused keyword as (index, text), or None when they run out."""
        if self.next_kw >= len(self.keywords):
            return None
        i = self.next_kw
        self.next_kw += 1
        return i, self.keywords[i]

    def resume_idx(self):
        """Where to resume after a restart: the lowest keyword still being worked on."""
        return min(self.active.values()) if self.active else self.next_kw

def persist(S, idx=None):
    S.state["current_keyword_idx"] = S.resume_idx() if idx is None else idx
    S.state["seen_urls"] = list(S.seen)
    save_state(S.state)
    save_face_cache()
    save_visited()
    save_enrich_cache()
    save_learn()

def build_suggestion(p):
    a = p.get("a") or {}
    e = p.get("e") or {}
    vid = p["vid"]
    return {
        "video_id":  vid,
        "title":     p["title"][:120],
        "channel":   p["channel"][:60],
        "url":       f"https://www.youtube.com/watch?v={vid}",
        "thumb":     f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "views":     p.get("views"),
        "dur":       p.get("dur"),
        "face":      bool(a.get("face")),
        "real_face": bool(a.get("real_face")),
        "text":      bool(a.get("text")),
        "mono":      int(e.get("mono", 0) or 0),
        "niche":     p["signal"],
        "score":     round(p["s"], 1),
    }

# ──────────────────────────────────────────────
# ONE TAB
# ──────────────────────────────────────────────

class Tab:
    def __init__(self, i, page):
        self.i = i
        self.page = page
        self.q = asyncio.Queue()
        self.options = {}        # video_id -> ranked candidate dict (for buckets)
        self.hop_buckets = None  # feature buckets of the video we are on
        self.page_yield = 0      # new channels harvested from the current page
        self.page_keys = []
        self.bg = None           # background scroll/harvest task for the current page
        self.bg_done = False
        self.trail = []          # titles hopped through under the current keyword

# Thumbnails are downloaded in parallel ahead of ranking (network was the slow part);
# analyze_thumbnail() then finds them here instead of fetching one by one.
_prefetched = {}
_orig_fetch_thumbnail = auto_hop.fetch_thumbnail

def _fetch_thumbnail_prefetched(video_id):
    img = _prefetched.pop(video_id, None)
    return img if img is not None else _orig_fetch_thumbnail(video_id)

auto_hop.fetch_thumbnail = _fetch_thumbnail_prefetched

def _prefetch_thumbnails(video_ids):
    todo = [v for v in video_ids
            if not (isinstance(auto_hop.face_cache.get(v), dict)
                    and auto_hop.face_cache[v].get("v") == auto_hop.FACE_CACHE_VER)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        for vid, img in zip(todo, ex.map(_orig_fetch_thumbnail, todo)):
            if img is not None:
                _prefetched[vid] = img

_model_lock = threading.Lock()
_orig_detect_faces = auto_hop.detect_faces
_orig_embed_diffs  = auto_hop.embed_diffs
auto_hop.detect_faces = lambda img: _locked(_orig_detect_faces, img)
auto_hop.embed_diffs  = lambda titles: _locked(_orig_embed_diffs, titles)

def _locked(fn, arg):
    with _model_lock:      # YuNet / the embedder are shared objects, not thread-safe
        return fn(arg)

def _rank_fast(candidates, excluded, seen_channel_names):
    """Two-pass ranking: a cheap pass without thumbnails picks the finalists, their
    thumbnails are fetched in parallel, then the normal pass scores them."""
    auto_hop.THUMB_CHECK_TOP_N = 0
    try:
        pre = rank_candidates(candidates, excluded, seen_channel_names)
    finally:
        auto_hop.THUMB_CHECK_TOP_N = THUMB_TOP_N
    _prefetch_thumbnails([p["vid"] for p in pre[:THUMB_TOP_N]])
    return rank_candidates(candidates, excluded, seen_channel_names)

async def harvest_page(S, tab, page_type):
    """Send the channels currently on the page to the backend (already-seen ones are skipped)."""
    n, keys = await harvest(tab.page, page_type, S.blocklist, S.seen)
    S.channels_sent += n
    tab.page_yield += n
    tab.page_keys += keys
    write_status(channels_sent=S.channels_sent)

def finish_page(S, tab):
    """Called when leaving a page: feeds the learning store with what it yielded."""
    if tab.hop_buckets:
        learn_update_yield(tab.hop_buckets, tab.page_yield)
        learn_record_sent(tab.page_keys, tab.hop_buckets)
    tab.page_yield, tab.page_keys = 0, []
    persist(S)

async def background_finish(S, tab, page_type):
    """Runs while the popup is on screen. No scrolling here (the page is already
    scrolled and must stay still) - it only sends the loaded channels to the backend."""
    try:
        await harvest_page(S, tab, page_type)
        tab.bg_done = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        safe_print(f"  [Tab {tab.i}] harvest error: {e}")
        tab.bg_done = True

async def stop_background(S, tab, page_type):
    """The user picked: stop the background scroll and make sure nothing loaded so far is lost."""
    bg, tab.bg = tab.bg, None
    if bg is None:
        return
    if not tab.bg_done:
        bg.cancel()
        await asyncio.gather(bg, return_exceptions=True)
        await harvest_page(S, tab, page_type)
    else:
        await asyncio.gather(bg, return_exceptions=True)

async def scan_page(S, tab, page_type):
    """FAST PATH: short scroll -> rank -> popup list. Harvesting and the rest of the
    scrolling continue in the background (tab.bg) while the user is choosing."""
    i, page = tab.i, tab.page
    kind = "search" if page_type == "search" else "watch"
    tab.page_yield, tab.page_keys, tab.bg_done, tab.bg = 0, [], False, None
    tab.options = {}      # only THIS page's claims may be handed back on a retry

    set_tab(i, phase="scrolling", message="scrolling to load videos", items=0)
    # search page: stop at FAST_ITEMS videos; recommendation sidebar: scroll all the way to its end
    watch = kind == "watch"
    target, top, info = (None if watch else FAST_ITEMS), [], {"reason": ""}
    t_scroll = t_rank = 0.0
    reloaded = False
    for _ in range(3):
        t0 = time.monotonic()
        info = await scroll_to_end(page, kind, stop_items=target,
                                   max_seconds=None if watch else FAST_SECONDS,
                                   on_progress=lambda n: set_tab(i, items=n))
        log_scroll(i, kind, info)
        if info["reason"] == "nothing loaded" and not reloaded:
            reloaded = True                   # blank / half-loaded page: reload it once and retry
            set_tab(i, message="page did not load - reloading")
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            await asyncio.sleep(3)
            info = await scroll_to_end(page, kind, stop_items=target,
                                       max_seconds=None if watch else FAST_SECONDS,
                                       on_progress=lambda n: set_tab(i, items=n))
            log_scroll(i, kind, info)
        t_scroll += time.monotonic() - t0
        t0 = time.monotonic()
        set_tab(i, phase="ranking", message="picking the most relevant videos")
        candidates = await extract_candidates(page, page_type)
        await S.warm
        S.claimed.difference_update(tab.options)      # retry: give back last round's claims
        excluded = auto_hop.visited_global | S.claimed
        ranked = await asyncio.to_thread(
            _rank_fast, candidates, excluded, S.seen_channel_names)
        # claim atomically (no await in here) so two tabs never offer the same video
        top = []
        for p in ranked:
            if p["vid"] in S.claimed or _non_english_title(p["title"]):
                continue
            top.append(p)
            S.claimed.add(p["vid"])
            if len(top) >= SUGGESTIONS_PER_PROMPT:
                break
        tab.options = {p["vid"]: p for p in top}
        t_rank += time.monotonic() - t0
        if watch or len(top) >= MIN_SUGGESTIONS or info["reason"] not in ("enough", "time cap"):
            break
        target += FAST_ITEMS          # too few relevant videos yet — load more first
    safe_print(f"  [Tab {i}] popup ready after {info['items']} items ({info['reason']}), "
               f"{len(top)} suggestions | scroll {t_scroll:.1f}s, rank {t_rank:.1f}s")

    tab.bg = asyncio.create_task(background_finish(S, tab, page_type))
    return [build_suggestion(p) for p in top]

_WATCH_ID_RE = re.compile(r"[?&]v=([\w-]{11})")

def _watch_id(url):
    m = _WATCH_ID_RE.search(url or "")
    return m.group(1) if m and "/watch" in url else None

async def _watch_manual_navigation(tab):
    """While the popup is open (or hidden), notice the user opening a different
    video in Chrome themselves (clicking one in the sidebar / results) and report it."""
    base = _watch_id(tab.page.url)
    while True:
        await asyncio.sleep(0.7)
        try:
            vid = _watch_id(tab.page.url)
        except Exception:
            return
        if vid and vid != base:
            tab.q.put_nowait({"type": "manual", "video_id": vid})
            return

async def auto_choose(tab, suggestions):
    """Ask the Groq model for the best next video; fall back to the top-ranked one
    immediately if the API is slow / rate-limited / unusable. Never stalls a tab."""
    cur = next((t for t in status["tabs"] if t["tab"] == tab.i), {})
    watching = None
    if cur.get("current"):
        watching = f'{cur["current"].get("title", "")} | {cur["current"].get("channel", "")}'
    async with _groq_sem():
        idx = await asyncio.to_thread(
            hop_groq.pick_index, suggestions, cur.get("keyword", ""), watching,
            list(tab.trail[-8:]))
    if idx is None:
        # AI unavailable: fall back to the best-ranked title that is clearly English
        return 0, "top-ranked (AI unavailable)"
    return idx, "AI"

async def wait_for_choice(S, tab, suggestions, hop):
    """Publish the popup list and block until the user picks / skips.
    With auto-pick on, the AI answers instead and no popup is shown - unless the
    list is empty, which always waits for the user."""
    prompt_id = f"b{status['batch']}-t{tab.i}-h{hop}-{int(time.time())}"
    while not tab.q.empty():           # drop clicks left over from an earlier prompt
        tab.q.get_nowait()
    if AUTOPICK and suggestions:
        set_tab(tab.i, phase="auto_picking", prompt_id=None, suggestions=[],
                message="AI is choosing the next video")
        idx, src = await auto_choose(tab, suggestions)
        if idx >= 0:
            chosen = suggestions[idx]
            safe_print(f"  [Tab {tab.i}] {src} picked #{idx + 1}: {chosen['title'][:70]} | {chosen['channel']}")
            return tab.options[chosen["video_id"]]
        # every candidate has a non-English title: treat the page as empty and wait for the user
        safe_print(f"  [Tab {tab.i}] AI found no English-titled video - waiting for you")
        suggestions = []
    if suggestions:
        set_tab(tab.i, phase="choosing", prompt_id=prompt_id, suggestions=suggestions,
                message=f"{len(suggestions)} videos to choose from")
    else:
        set_tab(tab.i, phase="dead_end", prompt_id=prompt_id, suggestions=[],
                message="nothing relevant left here — skip this keyword or start the next batch")
    try:
        cur = next((t for t in status["tabs"] if t["tab"] == tab.i), {})
        await tab.page.evaluate(OVERLAY_JS, {
            "items": suggestions, "promptId": prompt_id, "tab": tab.i,
            "keyword": cur.get("keyword", ""), "hop": hop})
    except Exception as e:
        safe_print(f"  [Tab {tab.i}] could not show in-page picker ({e}) - use the app popup")
    watcher = asyncio.create_task(_watch_manual_navigation(tab))
    try:
        while True:
            cmd = await tab.q.get()
            if cmd.get("type") == "skip" and cmd.get("prompt_id") in (None, prompt_id):
                return None
            if cmd.get("type") == "pick" and cmd.get("prompt_id") == prompt_id \
                    and cmd.get("video_id") in tab.options:
                return tab.options[cmd["video_id"]]
            if cmd.get("type") == "manual":
                return {"manual": True, "vid": cmd["video_id"]}
    finally:
        watcher.cancel()
        try:
            await tab.page.evaluate(HIDE_OVERLAY_JS)
        except Exception:
            pass

RETRY_WAIT_S = 10

def _non_english_title(title):
    """True if the title contains any letter outside plain English/ASCII (non-Latin
    scripts and accented Latin). Romanized foreign words are left to the AI's prompt."""
    return any(c.isalpha() and ord(c) > 127 for c in title)

async def goto_retry(tab, url):
    """Open a URL, and if it fails (timeout, internet down, ...) wait 10 seconds and
    try again - forever, until it works - so a network hiccup never kills a tab."""
    attempt = 0
    while True:
        try:
            await tab.page.goto(url, wait_until="domcontentloaded")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if tab.page.is_closed():
                raise
            attempt += 1
            reason = (str(e).strip().splitlines() or [type(e).__name__])[0][:90]
            safe_print(f"  [Tab {tab.i}] page load failed ({reason}) - retrying in {RETRY_WAIT_S}s "
                       f"(attempt {attempt})")
            set_tab(tab.i, phase="retrying",
                    message=f"connection problem - retrying in {RETRY_WAIT_S}s (attempt {attempt})")
            await asyncio.sleep(RETRY_WAIT_S)

async def run_tab(S, tab, kw, kw_idx):
    """Run this tab's keywords one after another. When you press "Skip this keyword" the
    tab takes the next unused keyword right away, without waiting for the other tabs."""
    while True:
        result = await _run_keyword(S, tab, kw, kw_idx)
        if result != "skipped":
            return
        nxt = S.take_keyword()
        if nxt is None:
            S.active.pop(tab.i, None)
            set_tab(tab.i, phase="done", keyword="", suggestions=[], prompt_id=None,
                    message="no keywords left")
            return
        kw_idx, kw = nxt
        safe_print(f"  [Tab {tab.i}] skipped - next keyword #{kw_idx + 1}: {kw}")
        persist(S)

async def _run_keyword(S, tab, kw, kw_idx):
    i, page = tab.i, tab.page
    hop = 0
    tab.hop_buckets = None
    tab.trail = []
    S.active[i] = kw_idx
    set_tab(i, keyword=kw, kw_idx=kw_idx, phase="searching", hop=0, current=None, suggestions=[],
            prompt_id=None, items=0, message="loading search results")
    try:
        await goto_retry(tab, f"https://www.youtube.com/results?search_query={kw.replace(' ', '+')}")
        await asyncio.sleep(1.5)
        page_type = "search"
        while True:
            while True:
                try:
                    suggestions = await scan_page(S, tab, page_type)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if page.is_closed():
                        raise
                    if tab.bg is not None:
                        tab.bg.cancel()
                    reason = (str(e).strip().splitlines() or [type(e).__name__])[0][:90]
                    safe_print(f"  [Tab {i}] scanning failed ({reason}) - retrying in {RETRY_WAIT_S}s")
                    set_tab(i, phase="retrying",
                            message=f"problem loading this page - retrying in {RETRY_WAIT_S}s")
                    await asyncio.sleep(RETRY_WAIT_S)
            choice = await wait_for_choice(S, tab, suggestions, hop)
            set_tab(i, phase="loading_video", suggestions=[], prompt_id=None,
                    message="saving channels from this page")
            await stop_background(S, tab, page_type)
            finish_page(S, tab)
            if choice is None:
                return "skipped"
            hop += 1
            vid = choice["vid"]
            auto_hop.visited_global.add(vid)
            if choice.get("manual"):
                # The user clicked a video in Chrome themselves - the page is already there.
                tab.hop_buckets = None
                set_tab(i, phase="loading_video", hop=hop, suggestions=[], prompt_id=None,
                        current={"video_id": vid, "title": "(picked by you in Chrome)", "channel": ""},
                        message="video opened by you - continuing")
                safe_print(f"  [Tab {i}] hop {hop}: picked manually in Chrome ({vid})")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
            else:
                if choice["channel"]:
                    S.seen_channel_names.add(choice["channel"].lower())
                info = auto_hop.make_info(choice)
                tab.hop_buckets = info["buckets"]
                tab.trail.append(info["title"][:80])
                set_tab(i, phase="loading_video", hop=hop, suggestions=[], prompt_id=None,
                        current={"video_id": vid, "title": info["title"], "channel": info["channel"]},
                        message="opening video")
                safe_print(f"  [Tab {i}] hop {hop}: {info['title']} | {info['channel']}")
                await goto_retry(tab, f"https://www.youtube.com/watch?v={vid}")
            await asyncio.sleep(random.uniform(1.0, 1.8))
            try:
                await page.evaluate(ENSURE_PLAYING_JS)
                if NUM_TABS > 1:        # many videos playing at once: keep them light
                    await page.evaluate(LOW_QUALITY_JS)
            except Exception:
                pass
            page_type = "sidebar"
    except asyncio.CancelledError:
        if tab.bg is not None:
            tab.bg.cancel()
        raise
    except Exception as e:
        if tab.bg is not None:
            tab.bg.cancel()
        safe_print(f"  [Tab {i}] error: {e}")
        set_tab(i, phase="error", suggestions=[], prompt_id=None, message=str(e)[:200])
        return "error"

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

async def main():
    keywords  = load_keywords()
    state     = load_state()
    load_face_cache()
    load_visited()
    load_enrich_cache()
    load_learn()
    sync_outcomes()
    save_learn()
    auto_hop.THUMB_CHECK_TOP_N = THUMB_TOP_N

    for stale in glob.glob(os.path.join(CMD_DIR, "*.json")):
        try:
            os.remove(stale)
        except Exception:
            pass

    idx  = state.get("current_keyword_idx", 0)
    seen = set(state.get("seen_urls", []))
    S = Shared(state, seen)
    S.keywords = keywords

    def _warm():
        auto_hop.get_embedder()
        auto_hop.get_yunet()
    S.warm = asyncio.create_task(asyncio.to_thread(_warm))   # loads while Chrome starts

    safe_print("=" * 60)
    safe_print(f"[Hop with AI] assisted mode | {len(keywords)} keywords | "
               f"resuming at #{idx + 1} | {NUM_TABS} tab(s)")
    safe_print(f"[Hop with AI] seen: {len(seen)} | visited videos: {len(auto_hop.visited_global)}")
    safe_print("=" * 60)

    status["tabs"] = [{"tab": i, "keyword": "", "phase": "idle", "hop": 0, "current": None,
                       "suggestions": [], "prompt_id": None, "items": 0, "message": ""}
                      for i in range(NUM_TABS)]
    write_status(phase="starting", total_keywords=len(keywords), keyword_idx=idx,
                 message="launching browser")

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=HEADLESS,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
                ],
                viewport={"width": 1400, "height": 900},
            )
        except Exception as e:
            msg = ("Could not open Chrome profile - is the manual hop scraper or another "
                   f"hop window already running? ({e})")
            safe_print(f"[Hop with AI] {msg}")
            write_status(phase="error", message=msg)
            return

        tabs = [Tab(i, None) for i in range(NUM_TABS)]
        try:
            while True:
                if idx >= len(keywords):
                    write_status(phase="finished", message="all keywords processed")
                    safe_print("\n[Hop with AI] All keywords processed!")
                    break

                S.next_kw = idx
                S.active.clear()
                assigned = []
                for _ in range(NUM_TABS):
                    item = S.take_keyword()
                    if item is None:
                        break
                    assigned.append(item)
                batch_kws = [kw for _, kw in assigned]

                S.claimed.clear()
                S.blocklist = get_blocklist()
                write_status(phase="running", batch=status["batch"] + 1, keyword_idx=idx,
                             message="")
                safe_print(f"\n[Batch {status['batch']}] keywords "
                           f"#{idx + 1}-{idx + len(batch_kws)}: {batch_kws}")

                tasks = {}
                for t, (kw_idx, kw) in zip(tabs, assigned):
                    if t.page is None or t.page.is_closed():
                        t.page = await context.new_page()
                        await attach_bridge(t)
                    t.q = asyncio.Queue()
                    tasks[t.i] = asyncio.create_task(run_tab(S, t, kw, kw_idx))
                for t in tabs[len(batch_kws):]:
                    set_tab(t.i, keyword="", phase="idle", suggestions=[], prompt_id=None,
                            current=None, message="no keyword left for this tab")

                next_batch = False
                while not next_batch:
                    for cmd in drain_commands():
                        kind = cmd.get("type")
                        if kind == "next_batch":
                            next_batch = True
                        elif kind in ("pick", "skip"):
                            ti = cmd.get("tab")
                            if ti in tasks and not tasks[ti].done():
                                tabs[ti].q.put_nowait(cmd)
                    await asyncio.sleep(0.4)

                for task in tasks.values():
                    task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                idx = S.next_kw          # continue after the highest keyword handed out
                S.active.clear()
                persist(S, idx)
                write_status(keyword_idx=idx)
        except KeyboardInterrupt:
            safe_print("\n[Hop with AI] Stopping - saving state...")
        finally:
            idx = S.resume_idx()
            persist(S, idx)
            if status["phase"] != "finished":
                write_status(phase="stopped", message=f"stopped at keyword #{idx + 1}")
            try:
                await context.close()
            except Exception:
                pass
            safe_print(f"[Hop with AI] State saved. Next keyword: #{idx + 1}")

if __name__ == "__main__":
    asyncio.run(main())
