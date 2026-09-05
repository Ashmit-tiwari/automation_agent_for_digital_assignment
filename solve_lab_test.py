import asyncio
import re
from playwright.async_api import async_playwright

async def solve_lab():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")

    # Find the active test page with the editor
    lab_page = None
    gemini_page = None
    for pg in browser.contexts[0].pages:
        if "/test/" in pg.url:
            lab_page = pg
        elif "gemini.google.com" in pg.url:
            gemini_page = pg

    if not lab_page:
        print("[!] No active lab test page found.")
        await p.stop()
        return

    print(f"[*] Found Lab Page: {lab_page.url}")
    await lab_page.bring_to_front()

    # Step 1: Extract Problem Statement and Language
    info = await lab_page.evaluate("""() => {
        const bodyText = document.body.innerText;
        // Language
        let lang = "PostgreSQL";
        const langEl = document.querySelector('.MuiSelect-select, .language-selector, [aria-haspopup="listbox"]');
        if (langEl) lang = langEl.innerText.trim();

        // Problem statement
        const leftPanel = document.querySelector('.MuiBox-root:has(.MuiTable-root), .MuiGrid-item, .problem-statement') || document.body;
        
        return {
            language: lang,
            text: document.body.innerText.substring(0, 2000)
        };
    }""")

    language = info.get("language", "PostgreSQL")
    problem_text = info.get("text", "")
    print(f"[*] Detected Language: {language}")
    print(f"[*] Problem snippet: {problem_text[:200]}...")

    # Step 2: Query Gemini for solution
    prompt = (
        f"Solve the following SQL / coding challenge in {language}.\n"
        f"Return ONLY the exact query or code inside triple backticks with no commentary, explanations, or notes.\n\n"
        f"Problem:\n{problem_text}"
    )

    print("[*] Asking Gemini for solution...")
    await gemini_page.bring_to_front()
    await asyncio.sleep(0.5)

    input_elem = await gemini_page.wait_for_selector('rich-textarea [contenteditable="true"], div[role="textbox"]')
    await input_elem.click()
    await gemini_page.evaluate("""(text) => {
        const el = document.querySelector('rich-textarea [contenteditable="true"]') || document.querySelector('div[role="textbox"]');
        el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
    }""", prompt)
    await asyncio.sleep(1)

    btn = await gemini_page.query_selector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button')
    if btn:
        await btn.click()
    else:
        await gemini_page.keyboard.press("Enter")

    # Wait for completion
    answer = ""
    start_t = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_t < 60:
        await asyncio.sleep(1.5)
        stop_btn = await gemini_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
        if stop_btn and await stop_btn.is_visible():
            continue
        msgs = await gemini_page.query_selector_all('message-content, .model-response-text')
        if msgs:
            latest = await msgs[-1].inner_text()
            if latest and ("SELECT" in latest or "```" in latest):
                answer = latest.strip()
                break

    print(f"[*] Gemini replied:\n{answer}\n")

    # Extract clean code
    match = re.search(r'```(?:[a-zA-Z0-9_\+#\-]*\n)?([\s\S]*?)```', answer)
    clean_code = match.group(1).strip() if match else answer.strip()
    print(f"[*] Code to type:\n{clean_code}\n")

    # Step 3: Focus editor and TYPE the code out character by character (NOT copy-pasting)
    await lab_page.bring_to_front()
    await asyncio.sleep(1)

    # Click inside the Monaco editor
    editor = await lab_page.query_selector('.monaco-editor .view-lines, .monaco-editor, textarea')
    if editor:
        await editor.click()
        # Select all and delete any starter text
        await lab_page.keyboard.press("Control+A")
        await lab_page.keyboard.press("Backspace")
        await asyncio.sleep(0.5)

        print(f"[*] Simulating real keyboard typing for {len(clean_code)} characters...")
        # Type character by character with realistic keystroke delay
        await lab_page.keyboard.type(clean_code, delay=20)
        print("[OK] Finished typing code into editor!")

    await asyncio.sleep(1)

    # Step 4: Click Submit or Test & Results
    print("[*] Submitting solution...")
    submit_btn = await lab_page.query_selector('button:has-text("Submit"), button.MuiButton-containedPrimary')
    if submit_btn:
        await submit_btn.click()
        print("[OK] Clicked Submit button!")

    # Wait for test cases
    await asyncio.sleep(6)

    # Save screenshot of results
    shot = await lab_page.screenshot(path="e:/Agent/screenshots/lab_result_typed.png")
    print("Screenshot saved to e:/Agent/screenshots/lab_result_typed.png")

    await p.stop()

if __name__ == "__main__":
    asyncio.run(solve_lab())

