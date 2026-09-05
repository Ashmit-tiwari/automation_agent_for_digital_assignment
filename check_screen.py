import asyncio
from bytexl_agent import ByteXLAgent

async def main():
    agent = ByteXLAgent()
    await agent.connect()
    shot = await agent.take_screenshot("current_state")
    print(f"Current screenshot: {shot}")
    
    info = await agent.bytexl_page.evaluate("""() => {
        const pEls = Array.from(document.querySelectorAll('p')).map(p => p.innerText.trim()).filter(Boolean);
        const activeNav = Array.from(document.querySelectorAll('.css-100k9bu, .css-1uoioob, p')).filter(p => !isNaN(parseInt(p.innerText)));
        const radioLabels = Array.from(document.querySelectorAll('[role="radiogroup"] label')).map(l => l.innerText.trim());
        return {
            pElements: pEls.slice(0, 15),
            options: radioLabels
        };
    }""")
    print("Page info:", info)
    await agent.close()

if __name__ == "__main__":
    asyncio.run(main())

