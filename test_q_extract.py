import asyncio
from bytexl_agent import ByteXLAgent

async def main():
    agent = ByteXLAgent()
    await agent.connect()
    
    q_info = await agent.bytexl_page.evaluate("""() => {
        const radioGroup = document.querySelector('[role="radiogroup"], .MuiRadioGroup-root');
        if (!radioGroup) return { error: 'no radiogroup' };
        
        // Traverse previous siblings or parent's previous elements
        let container = radioGroup.closest('.MuiBox-root, .MuiPaper-root') || radioGroup.parentElement;
        
        // Find question paragraph / markdown view
        // Look at all elements before radioGroup in the main content area
        const main = radioGroup.closest('main') || radioGroup.parentElement.parentElement;
        const allText = Array.from(main.querySelectorAll('p, div.md-view, h1, h2, h3, h4, span'))
            .filter(el => !radioGroup.contains(el))
            .map(el => el.innerText.trim())
            .filter(t => t.length > 10 && !t.includes('Difficulty:') && !t.includes('Score:'));

        return {
            foundQuestions: allText,
            options: Array.from(radioGroup.querySelectorAll('label')).map(l => l.innerText.trim())
        };
    }""")
    print("Extracted question info:", q_info)
    await agent.close()

if __name__ == "__main__":
    asyncio.run(main())

