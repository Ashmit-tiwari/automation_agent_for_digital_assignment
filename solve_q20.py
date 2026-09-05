import asyncio
import re
from bytexl_agent import ByteXLAgent

async def solve_q20():
    agent = ByteXLAgent()
    await agent.connect()
    
    # Click Option D (index 3)
    await agent.bytexl_page.bring_to_front()
    clicked = await agent.bytexl_page.evaluate("""() => {
        const labels = Array.from(document.querySelectorAll('[role="radiogroup"] label, .MuiRadioGroup-root label'));
        // D is index 3
        if (labels[3]) {
            labels[3].click();
            const input = labels[3].querySelector('input[type="radio"]');
            if (input && input.click) input.click();
            return true;
        }
        return false;
    }""")
    print("Clicked Option D:", clicked)
    await asyncio.sleep(1)
    
    # Save screenshot
    shot = await agent.take_screenshot("mcq_q20_D")
    print("Screenshot saved:", shot)
    
    # Inspect buttons (Next, Previous, Submit, etc.)
    buttons = await agent.bytexl_page.evaluate("""() => {
        return Array.from(document.querySelectorAll('button')).map(b => ({
            text: b.innerText.trim(),
            disabled: b.disabled,
            classes: b.className
        }));
    }""")
    print("Available buttons:", buttons)
    
    await agent.close()

if __name__ == "__main__":
    asyncio.run(solve_q20())

