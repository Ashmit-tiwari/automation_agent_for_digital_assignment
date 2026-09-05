import asyncio
import json
from playwright.async_api import async_playwright

async def inspect():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.contexts[0].pages[0]

    items = await page.evaluate("""() => {
        const allItems = Array.from(document.querySelectorAll('li, .MuiListItem-root, a, div.MuiBox-root')).filter(el => {
            const txt = (el.innerText || '').trim();
            return (txt.startsWith('Quiz') || txt.startsWith('Lab')) && txt.length < 80 && el.children.length > 0;
        });

        return allItems.map(el => {
            const txt = el.innerText.trim();
            const svgs = Array.from(el.querySelectorAll('svg')).map(s => {
                const cn = s.className && s.className.baseVal ? s.className.baseVal : (typeof s.className === 'string' ? s.className : '');
                return {
                    dataTestId: s.getAttribute('data-testid'),
                    className: cn
                };
            });
            const isChecked = svgs.some(s => (s.dataTestId && s.dataTestId.toLowerCase().includes('check')) || s.className.includes('Checked') || s.className.includes('Primary'));
            const isUnchecked = svgs.some(s => (s.dataTestId && s.dataTestId.includes('CheckBoxOutlineBlank')));

            return {
                text: txt,
                tag: el.tagName,
                svgs: svgs,
                isChecked: isChecked,
                isUnchecked: isUnchecked
            };
        });
    }""")

    print(json.dumps(items[:10], indent=2))
    await p.stop()

if __name__ == "__main__":
    asyncio.run(inspect())

