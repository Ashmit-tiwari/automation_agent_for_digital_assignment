import asyncio
from playwright.async_api import async_playwright

async def inspect():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    bytexl_page = None
    gemini_page = None
    for page in browser.contexts[0].pages:
        if "bytexl.ai" in page.url:
            bytexl_page = page
        elif "gemini.google.com" in page.url:
            gemini_page = page

    print("ByteXL page:", bytexl_page.url if bytexl_page else "None")
    print("Gemini page:", gemini_page.url if gemini_page else "None")

    if bytexl_page:
        eval_script = """() => {
            const buttons = Array.from(document.querySelectorAll('button')).map(b => ({
                text: b.innerText,
                className: b.className,
                disabled: b.disabled
            }));
            
            // Find question title
            const bodyLines = document.body.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
            
            // Look for radio or option containers
            const elements = Array.from(document.querySelectorAll('*')).filter(el => {
                return el.children.length === 0 && (el.innerText || el.textContent);
            }).map(el => ({
                tag: el.tagName,
                text: (el.innerText || el.textContent).trim(),
                className: el.className
            })).filter(e => e.text.length > 0 && e.text.length < 200);

            return {
                bodySnippet: bodyLines.slice(0, 20),
                buttons: buttons,
                leafElements: elements.slice(0, 30)
            };
        }"""
        data = await bytexl_page.evaluate(eval_script)
        import json
        print("ByteXL DOM data:")
        print(json.dumps(data, indent=2))

    await p.stop()

if __name__ == "__main__":
    asyncio.run(inspect())

