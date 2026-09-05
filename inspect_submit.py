import asyncio
from bytexl_agent import ByteXLAgent

async def inspect_submit():
    agent = ByteXLAgent()
    await agent.connect()
    
    data = await agent.bytexl_page.evaluate("""() => {
        const buttons = Array.from(document.querySelectorAll('button, a, div[role="button"]')).map(el => ({
            tag: el.tagName,
            text: el.innerText.trim(),
            classes: el.className,
            disabled: el.disabled
        })).filter(b => b.text.length > 0 && b.text.length < 50);

        return {
            buttons: buttons
        };
    }""")
    print("Clickable buttons on byteXL:")
    for b in data.get("buttons", []):
        print("  -", b)

    await agent.close()

if __name__ == "__main__":
    asyncio.run(inspect_submit())

