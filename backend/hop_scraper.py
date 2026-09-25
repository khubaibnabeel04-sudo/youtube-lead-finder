import asyncio
import json
import os
import re
from datetime import datetime
from patchright.async_api import async_playwright
import requests

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

BACKEND_URL    = "http://localhost:8000/api"
STATE_FILE     = "hop_scraper_state.json"
KEYWORDS_FILE  = "keywords.txt"
SCAN_INTERVAL  = 2.5

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"current_keyword_idx": 0, "seen_urls": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_keywords():
    if not os.path.exists(KEYWORDS_FILE):
        return []
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

# ──────────────────────────────────────────────
# BACKEND
# ──────────────────────────────────────────────

def send_channels(channels):
    try:
        resp = requests.post(
            f"{BACKEND_URL}/channels/discover",
            json={"channels": channels},
            timeout=10
        )
        return resp.json()
    except Exception as e:
        print(f"  [Backend Error] {e}")
        return None

def get_blocklist():
    try:
        resp = requests.get(f"{BACKEND_URL}/blocklist", timeout=5)
        return set(resp.json().get("blocklist", []))
    except:
        return set()

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def extract_channel_id(url):
    if not url:
        return None
    for p in [r"youtube\.com/channel/([\w-]+)", r"youtube\.com/@([\w-]+)", r"youtube\.com/c/([\w-]+)"]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def dedup(channels, seen):
    new = []
    for c in channels:
        # Dedup key: channel URL if we have one, else video_id
        key = c.get("url") or c.get("video_id") or c.get("name", "").lower()
        if key and key not in seen:
            seen.add(key)
            new.append(c)
    return new

# ──────────────────────────────────────────────
# SCRAPING — SEARCH RESULTS
# ──────────────────────────────────────────────

async def scrape_search_results(page):
    channels = []
    try:
        videos = await page.query_selector_all("ytd-video-renderer")
        for v in videos:
            try:
                ch_el = await v.query_selector("ytd-channel-name")
                if not ch_el:
                    continue
                name = (await ch_el.inner_text() or "").strip()
                if not name:
                    continue
                ch_link = await v.query_selector("ytd-channel-name a")
                if not ch_link:
                    continue
                href = await ch_link.get_attribute("href")
                if not href or ("/@" not in href and "/channel/" not in href):
                    continue
                url = f"https://www.youtube.com{href}" if href.startswith("/") else href
                if any(c["name"] == name for c in channels):
                    continue
                channels.append({
                    "id":       extract_channel_id(url) or name.lower().replace(" ", "_"),
                    "name":     name,
                    "url":      url,
                    "source":   "search_video",
                    "timestamp": datetime.now().isoformat()
                })
            except:
                continue

        channel_boxes = await page.query_selector_all("ytd-channel-renderer")
        for box in channel_boxes:
            try:
                link = await box.query_selector("a#main-link, a.channel-link")
                if not link:
                    continue
                href = await link.get_attribute("href")
                if not href or ("/@" not in href and "/channel/" not in href):
                    continue
                name_el = await box.query_selector("ytd-channel-name")
                name    = (await name_el.inner_text()).strip() if name_el else (await link.inner_text()).strip()
                if not name:
                    continue
                url = f"https://www.youtube.com{href}" if href.startswith("/") else href
                channels.append({
                    "id":       extract_channel_id(url) or name.lower().replace(" ", "_"),
                    "name":     name,
                    "url":      url,
                    "source":   "search_channel",
                    "timestamp": datetime.now().isoformat()
                })
            except:
                continue

    except Exception as e:
        print(f"  [Scrape Error] Search: {e}")

    print(f"  [Search] Found {len(channels)} channels")
    for c in channels[:3]:
        print(f"    -> {c['name']}")
    return channels


# ──────────────────────────────────────────────
# SCRAPING — SIDEBAR (new DOM + video ID fallback)
# ──────────────────────────────────────────────

async def scrape_sidebar(page):
    """
    Extract channels from sidebar using yt-lockup-view-model.

    Two cases:
      A) Playlist/channel items  -> have /@handle or /channel/ link directly
      B) Regular video items     -> only have /watch?v= links

    For case B: extract channel name from metaText first line,
    video ID from watch href, and let the backend resolve
    video -> channel via videos.list API (1 unit per 50 videos).
    """
    channels = []
    try:
        await asyncio.sleep(2)

        items_data = await page.evaluate("""
            () => {
                const items = document.querySelectorAll('yt-lockup-view-model');
                return Array.from(items).map(item => {
                    const links = Array.from(item.querySelectorAll('a'));

                    // Case A: look for direct channel link
                    let channelUrl = null;
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        if (href.includes('/@') || href.includes('/channel/')) {
                            channelUrl = a.href;
                            break;
                        }
                    }

                    // Case B: grab video ID from watch link (no regex — string ops only)
                    let videoId = null;
                    if (!channelUrl) {
                        for (const a of links) {
                            const href = a.getAttribute('href') || '';
                            if (href.includes('/watch') && href.includes('v=')) {
                                const vIdx = href.indexOf('v=');
                                if (vIdx >= 0) {
                                    videoId = href.slice(vIdx + 2).split('&')[0];
                                    break;
                                }
                            }
                        }
                    }

                    // Channel name: first non-empty line of metadata text
                    // Pattern: "warikoo\\n2.5M views\\n•\\n1 year ago"
                    const meta = item.querySelector('yt-content-metadata-view-model');
                    let name = null;
                    if (meta) {
                        const lines = meta.innerText.trim().split('\\n')
                            .map(s => s.trim()).filter(Boolean);
                        if (lines.length > 0) name = lines[0];
                    }

                    return { name, channelUrl, videoId };
                });
            }
        """)

        print(f"  [Sidebar] Found {len(items_data)} lockup items")

        seen_local = set()
        for d in items_data:
            name       = (d.get("name") or "").strip()
            channel_url = d.get("channelUrl")
            video_id    = d.get("videoId")

            if not name or len(name) < 2:
                continue

            # Case A: have a direct channel URL
            if channel_url:
                key = channel_url
                if key in seen_local:
                    continue
                seen_local.add(key)
                channels.append({
                    "id":       extract_channel_id(channel_url) or name.lower().replace(" ", "_"),
                    "name":     name,
                    "url":      channel_url,
                    "source":   "recommendation",
                    "timestamp": datetime.now().isoformat()
                })

            # Case B: only have a video ID — backend will resolve it
            elif video_id:
                key = video_id
                if key in seen_local:
                    continue
                seen_local.add(key)
                channels.append({
                    "id":       "",           # unknown until backend resolves
                    "name":     name,
                    "url":      "",
                    "video_id": video_id,     # backend uses this to find channel
                    "source":   "recommendation",
                    "timestamp": datetime.now().isoformat()
                })

        direct = sum(1 for c in channels if c.get("url"))
        via_video = sum(1 for c in channels if c.get("video_id"))
        print(f"  [Sidebar] Extracted {len(channels)} channels "
              f"({direct} direct, {via_video} via video ID)")
        for c in channels[:5]:
            print(f"    -> {c['name']}")

    except Exception as e:
        print(f"  [Scrape Error] Sidebar: {e}")

    return channels


# ──────────────────────────────────────────────
# SCRAPING — CHANNEL PAGE
# ──────────────────────────────────────────────

async def scrape_channel_page(page):
    channels = []
    try:
        url = page.url
        if "/@" not in url and "/channel/" not in url:
            return channels
        name = None
        for sel in [
            "yt-dynamic-text-view-model .yt-core-attributed-string",
            "#channel-name .yt-core-attributed-string",
            "#channel-name #text",
            "#channel-title",
            "ytd-channel-name #text",
        ]:
            el = await page.query_selector(sel)
            if el:
                name = (await el.inner_text() or "").strip()
                if name:
                    break
        if not name:
            return channels
        channels.append({
            "id":       extract_channel_id(url) or name.lower().replace(" ", "_"),
            "name":     name,
            "url":      url.split("?")[0],
            "source":   "channel_page",
            "timestamp": datetime.now().isoformat()
        })
        print(f"  [Channel] Captured: {name}")
    except Exception as e:
        print(f"  [Scrape Error] Channel: {e}")
    return channels


# ──────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────

async def main():
    keywords  = load_keywords()
    state     = load_state()
    blocklist = get_blocklist()

    print(f"\n{'='*60}")
    print(f"[Hop Scraper] {len(keywords)} keywords loaded")
    print(f"[Hop Scraper] Resuming from keyword #{state['current_keyword_idx'] + 1}")
    print(f"[Hop Scraper] Blocklist: {len(blocklist)} channels")
    print(f"{'='*60}")
    print(f"\nControls:")
    print(f"  Browse YouTube freely — channels saved automatically")
    print(f"  Click 'Done' in the app UI to advance to the next keyword")
    print(f"  Press Ctrl+C to stop and save state")
    print(f"{'='*60}\n")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="chrome_profile_hop",
            headless=False,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1400, "height": 900},
        )

        page = await context.new_page()

        idx = state["current_keyword_idx"]
        if idx < len(keywords):
            kw = keywords[idx]
            print(f"[Keyword {idx+1}/{len(keywords)}] Searching: {kw}")
            await page.goto(
                f"https://www.youtube.com/results?search_query={kw.replace(' ', '+')}",
                wait_until="domcontentloaded"
            )
            await asyncio.sleep(3)

        seen      = set(state.get("seen_urls", []))
        last_url  = ""
        page_type = "search"

        try:
            while True:
                # Check for "done" signal from the backend (non-blocking)
                is_done = False
                try:
                    resp = requests.get(f"{BACKEND_URL}/keyword/check-done", timeout=3)
                    is_done = resp.json().get("done", False)
                except:
                    pass

                if is_done:
                    idx += 1
                    if idx < len(keywords):
                        kw = keywords[idx]
                        print(f"\n{'='*60}")
                        print(f"[Keyword {idx+1}/{len(keywords)}] Searching: {kw}")
                        print(f"{'='*60}")
                        await page.goto(
                            f"https://www.youtube.com/results?search_query={kw.replace(' ', '+')}",
                            wait_until="domcontentloaded"
                        )
                        await asyncio.sleep(3)
                        state["current_keyword_idx"] = idx
                        save_state(state)
                    else:
                        print("\n[Done] All keywords processed!")
                        break

                current_url  = page.url
                current_type = "other"
                if "youtube.com/results" in current_url:
                    current_type = "search"
                elif "youtube.com/watch" in current_url:
                    current_type = "video"
                elif "youtube.com/channel/" in current_url or "youtube.com/@" in current_url:
                    current_type = "channel"

                if current_url != last_url:
                    print(f"\n[Page] {current_type.upper()}: {current_url[:80]}...")
                    last_url  = current_url
                    page_type = current_type
                    await asyncio.sleep(2)

                channels = []
                if page_type == "search":
                    channels = await scrape_search_results(page)
                elif page_type == "video":
                    channels = await scrape_sidebar(page)
                elif page_type == "channel":
                    channels = await scrape_channel_page(page)

                if channels:
                    filtered = [
                        c for c in channels
                        if c.get("id") not in blocklist
                        and c.get("name", "").lower() not in blocklist
                    ]
                    new_channels = dedup(filtered, seen)

                    if new_channels:
                        print(f"[Found] {len(new_channels)} new channels!")
                        for c in new_channels[:5]:
                            print(f"  -> {c['name']} ({c['source']})")
                        result = send_channels(new_channels)
                        if result:
                            queued  = result.get("queued", result.get("saved", 0))
                            pending = result.get("total_pending", 0)
                            print(f"[Queued] {queued} for validation | {pending} pending in app")

                state["seen_urls"] = list(seen)
                save_state(state)

                await asyncio.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n[Stop] Saving state...")
        finally:
            state["current_keyword_idx"] = idx
            state["seen_urls"]           = list(seen)
            save_state(state)
            print(f"[State] Saved. Next keyword: #{idx + 1}")
            await context.close()
            print("[Done] Goodbye!")

if __name__ == "__main__":
    asyncio.run(main())