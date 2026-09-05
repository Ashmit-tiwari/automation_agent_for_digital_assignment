import asyncio
from playwright.async_api import async_playwright

async def fix():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    lab_page = [pg for pg in browser.contexts[0].pages if "/test/" in pg.url][0]
    await lab_page.bring_to_front()

    correct_sql = "SELECT course_name, department, credits\nFROM courses\nORDER BY department ASC, credits DESC;"

    editor = await lab_page.query_selector(".monaco-editor .view-lines, textarea")
    if editor:
        await editor.click()
        await lab_page.keyboard.press("Control+A")
        await lab_page.keyboard.press("Backspace")
        await asyncio.sleep(0.5)

        print("Typing clean SQL character-by-character...")
        await lab_page.keyboard.type(correct_sql, delay=20)

    await asyncio.sleep(1)
    btn = await lab_page.query_selector('button.MuiButton-containedPrimary')
    if btn:
        await btn.click()
        print("Clicked Submit button!")

    await asyncio.sleep(6)
    await lab_page.screenshot(path="e:/Agent/screenshots/lab_passed_result.png")
    print("Saved screenshot: lab_passed_result.png")
    await p.stop()

if __name__ == "__main__":
    asyncio.run(fix())

