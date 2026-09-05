import asyncio
from playwright.async_api import async_playwright

async def test_gemini():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    gemini_page = [pg for pg in browser.contexts[0].pages if "gemini.google.com" in pg.url][0]
    await gemini_page.bring_to_front()

    prompt = (
        "Question:\n"
        "Which best describes why 'maintainability' matters even if a system currently works well?\n\n"
        "Options:\n"
        "A) It doesn't matter once a system is deployed\n"
        "B) It is solely the concern of the QA team\n"
        "C) It only matters for open-source projects\n"
        "D) Systems evolve over time\n\n"
        "Reply with only the correct option letter (example: B) and a short reason."
    )

    box = await gemini_page.query_selector('rich-textarea [contenteditable="true"], div[role="textbox"]')
    await box.click()

    await gemini_page.evaluate("""(text) => {
        const el = document.querySelector('rich-textarea [contenteditable="true"]') || document.querySelector('div[role="textbox"]');
        el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
    }""", prompt)

    await asyncio.sleep(1)

    btn = await gemini_page.query_selector('button[aria-label*="Send"], button.send-button, .send-button-container button')
    if btn:
        await btn.click()
        print("Clicked send button!")
    else:
        await gemini_page.keyboard.press("Enter")

    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(1.5)
        stop_btn = await gemini_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
        if stop_btn and await stop_btn.is_visible():
            continue
        msgs = await gemini_page.query_selector_all('message-content, .model-response-text')
        if msgs:
            latest = await msgs[-1].inner_text()
            if "Systems evolve" in latest or "D)" in latest or "maintainability" in latest.lower():
                print("Gemini response:\n", latest)
                break

    await p.stop()

if __name__ == "__main__":
    asyncio.run(test_gemini())

