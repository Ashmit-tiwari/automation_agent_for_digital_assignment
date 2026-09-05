import asyncio
import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
from playwright.async_api import async_playwright

async def read_gemini():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    gemini_page = [pg for pg in browser.contexts[0].pages if "gemini.google.com" in pg.url][0]
    msgs = await gemini_page.query_selector_all("message-content, .model-response-text")
    if msgs:
        latest = await msgs[-1].inner_text()
        print("LATEST GEMINI RESPONSE:\n", latest)
    await p.stop()

if __name__ == "__main__":
    asyncio.run(read_gemini())

