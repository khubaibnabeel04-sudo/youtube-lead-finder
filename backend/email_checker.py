"""
Standalone async Playwright-based email checker for YouTube channels.
Navigates to channel About page and checks for business email button.
"""
import re
import asyncio
import random
from playwright.async_api import async_playwright

EMAIL_INDICATORS = [
    "view email address", "for business inquiries",
    "business inquiry", "businessemail", "channelcontactemail",
    "emailrevealrenderer", "reveal email", "contact email",
    "business email", "show email address",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class EmailChecker:
    """Async Playwright-based email checker for YouTube channels."""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.count = 0

    async def start(self):
        """Start the Playwright browser."""
        ua = random.choice(USER_AGENTS)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", "--no-sandbox",
                "--disable-setuid-sandbox", "--disable-gpu",
            ],
        )
        self.ctx = await self.browser.new_context(
            user_agent=ua, locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await self.ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            window.chrome={runtime:{}};
        """)
        self.page = await self.ctx.new_page()
        self.page.set_default_timeout(18_000)

    async def close(self):
        """Clean up browser resources."""
        for obj in (self.page, self.ctx, self.browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass

    async def check(self, channel_id):
        """Check if a channel has a business email button on its About page.

        Returns dict with:
            has_email (bool): whether a business email button was found
            instagram (str): Instagram handle if found
            twitter (str): Twitter/X handle if found
        """
        result = {"has_email": False, "instagram": "", "twitter": ""}

        # Recycle browser every 40 checks to avoid detection
        if self.count > 0 and self.count % 40 == 0:
            await self.close()
            await self.start()

        # Anti-detect pause every 20 checks
        if self.count > 0 and self.count % 20 == 0:
            pause = random.uniform(15, 35)
            await asyncio.sleep(pause)

        url = f"https://www.youtube.com/channel/{channel_id}/about"
        backoff = 5
        network_error = None

        for attempt in range(3):
            try:
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await self.page.goto(url, wait_until="domcontentloaded", timeout=18_000)
                await self.page.evaluate(
                    "window.scrollTo(0,Math.floor(Math.random()*200))"
                )
                await asyncio.sleep(random.uniform(0.5, 1.2))
                html = await self.page.content()
                low = html.lower()

                result["has_email"] = any(ind in low for ind in EMAIL_INDICATORS)

                for pat, key in [
                    (r"instagram\.com/([A-Za-z0-9_.]+)", "instagram"),
                    (r"(?:twitter|x)\.com/([A-Za-z0-9_]+)", "twitter"),
                ]:
                    m = re.search(pat, html)
                    if m:
                        result[key] = m.group(0)

                self.count += 1
                return result

            except Exception as e:
                s = str(e).lower()
                if any(k in s for k in [
                    "timeout", "connection", "net", "dns", "reset"
                ]):
                    network_error = str(e)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                break

        if network_error and not result["has_email"]:
            # Every attempt failed on the network: report an error (the worker re-queues
            # the channel) instead of pretending the channel has no email button.
            raise RuntimeError(f"network problem: {network_error[:80]}")
        return result
