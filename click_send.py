import asyncio
from playwright.async_api import async_playwright

async def send():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    gemini_page = [pg for pg in browser.contexts[0].pages if "gemini.google.com" in pg.url][0]
    await gemini_page.bring_to_front()

    # Find the send button
    btn = await gemini_page.query_selector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button, .send-button-container button')
    if not btn:
        # Query any button inside input area
        btns = await gemini_page.query_selector_all('button')
        for b in btns:
            label = await b.get_attribute("aria-label") or ""
            if "send" in label.lower():
                btn = b
                break

    if btn:
        print("Found send button, clicking...")
        await btn.click()
    else:
        print("Falling back to Enter key...")
        await gemini_page.keyboard.press("Enter")

    await asyncio.sleep(4)
    await gemini_page.screenshot(path="e:/Agent/screenshots/gemini_after_send.png")
    
    msgs = await gemini_page.query_selector_all("message-content, .model-response-text")
    if msgs:
        latest = await msgs[-1].inner_text()
        print("Latest response after click:\n", latest)

    await p.stop()

if __name__ == "__main__":
    asyncio.run(send())

