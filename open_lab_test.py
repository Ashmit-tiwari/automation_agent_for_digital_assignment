import asyncio
from playwright.async_api import async_playwright

async def inspect():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.contexts[0].pages[0]

    btn = await page.query_selector('button.MuiButton-containedPrimary')
    if btn:
        text = await btn.inner_text()
        print("Clicking button:", text)
        await btn.click()
        await asyncio.sleep(5)

    print("Open pages count:", len(browser.contexts[0].pages))
    for i, pg in enumerate(browser.contexts[0].pages):
        print(f"Page {i}: {pg.url} ({await pg.title()})")

    # If new page opened, screenshot it
    target_page = browser.contexts[0].pages[-1]
    await target_page.screenshot(path="e:/Agent/screenshots/lab_in_editor.png")
    print("Screenshot saved to e:/Agent/screenshots/lab_in_editor.png")
    await p.stop()

if __name__ == "__main__":
    asyncio.run(inspect())

