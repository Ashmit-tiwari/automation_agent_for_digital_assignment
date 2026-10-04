import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from playwright.async_api import async_playwright
from gemini_brain import brain

# Ensure utf-8 output encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Candidates for Chromium-based browsers across common Windows paths
BROWSER_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ],
}

def get_installed_browser_path(b_name):
    if not b_name:
        return None
    for p in BROWSER_PATHS.get(b_name.lower(), []):
        if os.path.exists(p):
            return p
    return None

def find_installed_browser(preferred=None):
    if preferred and preferred.lower() in BROWSER_PATHS:
        p = get_installed_browser_path(preferred)
        if p:
            return p

    # Auto-detect which browser is currently running on the user's PC
    if is_browser_process_running("chrome.exe"):
        p = get_installed_browser_path("chrome")
        if p: return p
    if is_browser_process_running("msedge.exe"):
        p = get_installed_browser_path("edge")
        if p: return p
    if is_browser_process_running("brave.exe"):
        p = get_installed_browser_path("brave")
        if p: return p

    # Fallback to any installed browser
    for b in ["chrome", "edge", "brave"]:
        p = get_installed_browser_path(b)
        if p: return p

    return "chrome"

def get_browser_name_for_port(port):
    """
    Identifies the exact browser process listening on the given CDP port (e.g. Brave, Chrome, Edge).
    Chromium-based Brave returns 'Chrome/...' in /json/version, so process inspection is essential.
    """
    try:
        import psutil
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    pname = p.name().lower()
                    if "brave" in pname:
                        return "Brave"
                    elif "msedge" in pname or "edge" in pname:
                        return "Microsoft Edge"
                    elif "chrome" in pname:
                        return "Google Chrome"
                except Exception:
                    pass
    except Exception:
        pass

    try:
        import subprocess, re
        out = subprocess.check_output(f'netstat -ano -p tcp | findstr /R /C:":{port} .*LISTENING"', shell=True, text=True)
        m = re.search(r'\s+(\d+)\s*$', out.strip())
        if m:
            pid = int(m.group(1))
            task_out = subprocess.check_output(f'tasklist /fi "pid eq {pid}" /fo csv /nh', shell=True, text=True).lower()
            if "brave" in task_out:
                return "Brave"
            elif "msedge" in task_out or "edge" in task_out:
                return "Microsoft Edge"
            elif "chrome" in task_out:
                return "Google Chrome"
    except Exception:
        pass

    return None

def check_cdp_port(port, timeout=0.8):
    import urllib.request
    import json
    try:
        url = f"http://localhost:{port}/json/version"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            browser_raw = data.get("Browser", "Chromium")

        # Accurate browser identification by process name
        detected_browser = get_browser_name_for_port(port)
        if detected_browser:
            friendly_browser = detected_browser
        else:
            b_lower = browser_raw.lower()
            if "brave" in b_lower:
                friendly_browser = "Brave"
            elif "edg" in b_lower:
                friendly_browser = "Microsoft Edge"
            elif "chrome" in b_lower:
                friendly_browser = "Google Chrome"
            else:
                friendly_browser = browser_raw

        has_bytexl = False
        has_gemini = False
        tabs_list = []
        try:
            url_list = f"http://localhost:{port}/json/list"
            req_list = urllib.request.Request(url_list, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req_list, timeout=timeout) as resp_list:
                pages = json.loads(resp_list.read().decode())
                for pg in pages:
                    u = (pg.get("url") or "").lower()
                    t = (pg.get("title") or "").lower()
                    if pg.get("type") == "page":
                        tabs_list.append(pg.get("title", ""))
                    if "chrome-error://" in u or "chromewebdata" in u:
                        continue
                    if "bytexl" in u or "bytexl" in t:
                        has_bytexl = True
                    if "gemini" in u or "gemini" in t:
                        has_gemini = True
        except Exception:
            pass

        info = {
            "browser": friendly_browser,
            "has_bytexl": has_bytexl,
            "has_gemini": has_gemini,
            "tabs": tabs_list
        }
        return True, info
    except Exception:
        return False, None

def find_active_browser_port(start_port=9222, end_port=9235, preferred=None):
    """
    Intelligently scans open browser debugging ports:
    1. Highest priority: Browser matching preferred name (e.g. Brave) with ByteXL open.
    2. Second priority: Browser matching preferred name.
    3. Third priority: Any browser with ByteXL tab open.
    4. Fourth priority: Any browser with Gemini open.
    5. Fallback: First available browser port.
    """
    ports_found = []
    for p in range(start_port, end_port + 1):
        ok, info = check_cdp_port(p, timeout=0.3)
        if ok and info:
            ports_found.append((p, info))

    if not ports_found:
        return None, None

    pref_clean = (preferred or "").lower().strip()
    if pref_clean and pref_clean != "auto":
        # 1. Match preferred browser with ByteXL
        for p, info in ports_found:
            b = (info.get("browser") or "").lower()
            if pref_clean in b and info.get("has_bytexl"):
                return p, info.get("browser", "Chromium")

        # 2. Match preferred browser
        for p, info in ports_found:
            b = (info.get("browser") or "").lower()
            if pref_clean in b:
                return p, info.get("browser", "Chromium")

    # Priority 3: Port with ByteXL tab
    for p, info in ports_found:
        if info.get("has_bytexl"):
            return p, info.get("browser", "Chromium")

    # Priority 4: Port with Gemini tab
    for p, info in ports_found:
        if info.get("has_gemini"):
            return p, info.get("browser", "Chromium")

    # Priority 5: First available port
    return ports_found[0][0], ports_found[0][1].get("browser", "Chromium")

def find_free_port(start_port=9222, max_scan=50):
    import socket
    for p in range(start_port, start_port + max_scan):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    return 9222

def is_browser_process_running(proc_name):
    import subprocess
    try:
        out = subprocess.check_output(f'tasklist /fi "imagename eq {proc_name}"', shell=True, text=True)
        return proc_name.lower() in out.lower()
    except Exception:
        return False

PAGE_VISIBILITY_SHIM = """
(() => {
    try {
        Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
        Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
        window.addEventListener('visibilitychange', (e) => e.stopImmediatePropagation(), true);
        window.addEventListener('blur', (e) => e.stopImmediatePropagation(), true);
        if (!window.__raf_shim_active) {
            window.__raf_shim_active = true;
            const originalRAF = window.requestAnimationFrame;
            let lastTime = 0;
            window.requestAnimationFrame = function(callback) {
                if (document.visibilityState === 'visible' && !document.hidden) {
                    try { return originalRAF(callback); } catch(e) {}
                }
                const currTime = new Date().getTime();
                const timeToCall = Math.max(0, 16 - (currTime - lastTime));
                const id = window.setTimeout(() => callback(currTime + timeToCall), timeToCall);
                lastTime = currTime + timeToCall;
                return id;
            };
        }
    } catch(e) {}
})();
"""

def launch_browser_on_port(port, preferred=None, use_default_profile=True):
    exe = find_installed_browser(preferred)
    flags = [
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--restore-last-session",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "https://app.bytexl.ai/courses",
        "https://gemini.google.com/app",
    ]
    if not use_default_profile:
        prof = os.path.expandvars(r"%USERPROFILE%\.bytexl_profile")
        flags.insert(0, f'--user-data-dir={prof}')

    flag_str = " ".join(flags)
    try:
        # Launch directly with persistent GUI flags
        os.startfile(exe, arguments=flag_str)
        return True
    except Exception:
        pass
    try:
        DETACHED_FLAGS = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe] + flags, creationflags=DETACHED_FLAGS, close_fds=True)
        return True
    except Exception:
        subprocess.Popen(f'start "" "{exe}" {flag_str}', shell=True)
        return True

class MasterByteXLAgent:
    def __init__(self, port=9222, target=None, preferred_browser="auto"):
        self.port = port
        self.target = (target or "").strip()
        self.preferred_browser = preferred_browser
        self.playwright = None
        self.browser = None
        self.context = None
        self.bytexl_page = None
        self.gemini_page = None
        self.question_count = 0
        self.current_activity_name = ""
        self.completed_activities = set()

    def ensure_browser_running(self):
        """Checks if browser is running with debugging port, or auto-detects/launches it."""
        pref = getattr(self, "preferred_browser", "auto")
        ok, info = check_cdp_port(self.port, timeout=1.0)
        if ok and info:
            b_name = info.get("browser", "Chromium")
            if pref == "auto" or pref.lower() in b_name.lower():
                print(f"[OK] Browser ({b_name}) is already running on debugging port {self.port}.")
                if info.get("has_bytexl"):
                    print(f"[🎯] Verified active ByteXL login detected in {b_name} on port {self.port}!")
                return True
            else:
                print(f"[*] Port {self.port} has {b_name}, looking for {pref.upper()}...")

        # Check if browser is running on another common port (prioritizing preferred browser)
        active_p, active_b = find_active_browser_port(9222, 9235, preferred=pref)
        if active_p:
            if pref == "auto" or pref.lower() in active_b.lower():
                print(f"[*] Detected active browser ({active_b}) on port {active_p}! Switching to port {active_p}...")
                self.port = active_p
                return True

        if ok and info and pref != "auto" and pref.lower() not in info.get("browser", "").lower():
            free_p = find_free_port(9223)
            self.port = free_p
            print(f"[*] Port 9222 in use by another browser. Using port {self.port} for {pref.capitalize()}...")

        exe = find_installed_browser(pref)
        proc_name = os.path.basename(exe)
        b_label = proc_name.replace(".exe", "").capitalize()

        # If browser process is running without debugging, gracefully restart it with debugging on default profile
        if is_browser_process_running(proc_name):
            print(f"[*] Detected {b_label} is running in standard mode without remote debugging.")
            print(f"[*] 🔄 Restarting {b_label} with debugging enabled (preserving all open tabs & logins)...")
            try:
                subprocess.run(f'taskkill /IM "{proc_name}" /F', shell=True, capture_output=True)
                time.sleep(2.0)
            except Exception:
                pass

        print(f"[*] Launching {b_label} browser with remote debugging on port {self.port} (using your existing profile)...")
        launch_browser_on_port(self.port, pref, use_default_profile=True)

        print(f"[*] Waiting for browser on port {self.port}...")
        for i in range(12):
            time.sleep(1)
            ok, info = check_cdp_port(self.port, timeout=1.0)
            if ok and info:
                b_name = info.get("browser", "Chromium")
                print(f"[OK] Browser ({b_name}) is ready and listening on port {self.port}!")
                return True

        print(f"[!] Browser did not respond on port {self.port} within 12 seconds.")
        return False

    async def connect(self):
        ready = self.ensure_browser_running()
        if not ready:
            return False

        self.playwright = await async_playwright().start()

        cdp_url = f"http://127.0.0.1:{self.port}"
        for attempt in range(4):
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url, timeout=5000)
                break
            except Exception:
                await asyncio.sleep(1.0)

        # Fallback: scan if browser opened on another port
        pref = getattr(self, "preferred_browser", "auto")
        if not self.browser:
            active_p, active_b = find_active_browser_port(9222, 9235, preferred=pref)
            if active_p and active_p != self.port:
                print(f"[*] Found active browser ({active_b}) on port {active_p}. Auto-connecting...")
                try:
                    self.browser = await self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{active_p}", timeout=5000)
                    self.port = active_p
                except Exception:
                    pass

        if not self.browser:
            print(f"[!] Could not connect to browser on port {self.port}.")
            print(f"[*] Tip: Run 'launch_debug_browser.bat {self.port}' or check port availability.")
            return False

        self.context = self.browser.contexts[0]
        try:
            await self.context.add_init_script(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        pages = self.context.pages
        for p in pages:
            try:
                await p.evaluate(PAGE_VISIBILITY_SHIM)
            except Exception:
                pass

        # Locate existing ByteXL and Gemini pages across ANY domain or title
        for p in pages:
            url_lower = p.url.lower()
            if "chrome-error://" in url_lower or "chromewebdata" in url_lower:
                continue
            try:
                title_lower = (await p.title()).lower()
            except Exception:
                title_lower = ""
            if ("bytexl" in url_lower or "bytexl" in title_lower) and not self.bytexl_page:
                self.bytexl_page = p
                print(f"[🎯] Connected to open ByteXL tab: {p.url[:65]}...")
            elif ("gemini" in url_lower or "gemini" in title_lower) and not self.gemini_page:
                self.gemini_page = p
                print(f"[🎯] Connected to open Gemini tab: {p.url[:65]}...")

        if not self.bytexl_page:
            reusable_tab = None
            for p in pages:
                u = p.url.lower()
                if "chrome-error://" in u or "chromewebdata" in u or u in ("about:blank", "chrome://newtab/"):
                    if p != self.gemini_page:
                        reusable_tab = p
                        break
            if reusable_tab:
                self.bytexl_page = reusable_tab
                print("[*] Navigating tab to ByteXL (https://app.bytexl.ai/courses)...")
                await self.bytexl_page.goto("https://app.bytexl.ai/courses")
            else:
                print("[*] Opening ByteXL tab...")
                self.bytexl_page = await self.context.new_page()
                await self.bytexl_page.goto("https://app.bytexl.ai/courses")
            await asyncio.sleep(2)

        if brain.has_key():
            print(f"[OK] ⚡ Brain AI Active: Using direct Gemini API ({brain.active_model}) - 0 extra tabs needed!")
        else:
            if not self.gemini_page:
                print("[*] Opening Gemini tab (tip: add API key in config.json to skip browser tab)...")
                self.gemini_page = await self.context.new_page()
                await self.gemini_page.goto("https://gemini.google.com/app")
                await asyncio.sleep(2)

        try:
            await self.bytexl_page.evaluate(PAGE_VISIBILITY_SHIM)
            if self.gemini_page:
                await self.gemini_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        print(f"[OK] ByteXL Tab: {self.bytexl_page.url}")
        if self.gemini_page:
            print(f"[OK] Gemini Tab: {self.gemini_page.url}")
        return True

    async def take_screenshot(self, label: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{label}_{timestamp}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)
        active_page = self.bytexl_page
        for p in self.context.pages:
            if "/test/" in p.url:
                active_page = p
                break
        if active_page:
            await active_page.bring_to_front()
            try:
                await active_page.screenshot(path=filepath, timeout=5000)
                print(f"[SCREENSHOT] Saved: {filepath}")
            except Exception:
                pass

        # Rolling cleanup: keep only latest 30 screenshots
        try:
            files = [os.path.join(SCREENSHOTS_DIR, f) for f in os.listdir(SCREENSHOTS_DIR) if f.endswith(('.png', '.jpg'))]
            if len(files) > 30:
                files.sort(key=lambda x: os.path.getmtime(x))
                for old in files[:-30]:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
        except Exception:
            pass

        return filepath

    async def ask_gemini(self, prompt: str, screenshot_bytes: Optional[bytes] = None) -> str:
        """Sends prompt to Gemini and retrieves the clean response."""
        if brain.has_key():
            print(f"\n[*] ⚡ Brain AI Query ({len(prompt)} chars, model={brain.active_model})...")
            try:
                if screenshot_bytes:
                    ans = brain.ask_vision(prompt, screenshot_bytes)
                else:
                    ans = brain.ask_text(prompt)
                print(f"[OK] ⚡ Brain replied in 1s: {ans[:80]}...")
                return ans
            except Exception as e:
                print(f"[WARN] Gemini API error: {e}. Falling back to browser tab...")

        if not self.gemini_page:
            print("[WARN] No Gemini tab and no API key configured.")
            return "A"

        print(f"\n[*] Querying Gemini Tab ({len(prompt)} chars)...")
        await self.gemini_page.bring_to_front()
        await asyncio.sleep(0.5)

        existing = await self.gemini_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
        baseline_count = len(existing)

        input_elem = await self.gemini_page.wait_for_selector('rich-textarea [contenteditable="true"], div[role="textbox"], textarea', timeout=10000)
        await input_elem.click()
        try:
            await self.gemini_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        await self.gemini_page.evaluate("""(text) => {
            const el = document.querySelector('rich-textarea [contenteditable="true"]') ||
                       document.querySelector('div[role="textbox"]') ||
                       document.querySelector('textarea');
            if (el) {
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, text);
            }
        }""", prompt)
        await asyncio.sleep(1)
        await asyncio.sleep(0.8)

        btn = await self.gemini_page.query_selector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button')
        if btn:
            await btn.click()
        else:
            await self.gemini_page.keyboard.press("Enter")
        await self.gemini_page.evaluate("""() => {
            const btn = document.querySelector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button');
            if (btn && !btn.disabled) {
                btn.click();
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return true;
            }
            const el = document.querySelector('rich-textarea [contenteditable="true"]') || document.querySelector('div[role="textbox"]');
            if (el) {
                el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                return true;
            }
            return false;
        }""")

        print("[*] Waiting for Gemini response...")
        start_wait = time.time()
        while time.time() - start_wait < 10:
            stop_btn = await self.gemini_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
            if stop_btn and await stop_btn.is_visible():
                break
            await asyncio.sleep(0.5)

        answer = ""
        while time.time() - start_wait < 75:
            await asyncio.sleep(1.5)
            stop_btn = await self.gemini_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
            if stop_btn and await stop_btn.is_visible():
                continue
            responses = await self.gemini_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
            if len(responses) > baseline_count or len(responses) > 0:
                latest = responses[-1]
                answer = (await latest.inner_text()).strip()
                if answer:
                    break

        print(f"[OK] Gemini response received:\n{answer[:150]}...\n")
        return answer

    # ==========================
    # 1. SOLVING MCQs (Quiz Mode)
    # ==========================
    async def solve_mcq_test(self, test_page):
        print("\n[*] Starting MCQ Assessment. Inspecting questions one by one...")
        try:
            await test_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        q_num = 0
        while q_num < 35:
            q_num += 1
            try:
                await test_page.bring_to_front()
            except Exception:
                pass
            await asyncio.sleep(1)

            mcq_data = await test_page.evaluate("""() => {
                const radioGroup = document.querySelector('[role="radiogroup"], .MuiRadioGroup-root');
                if (!radioGroup) return null;

                // Check if an option is already selected / checked for this question
                const checkedRadio = radioGroup.querySelector(
                    'input[type="radio"]:checked, .Mui-checked, [role="radio"][aria-checked="true"], [data-testid="RadioButtonCheckedIcon"]'
                );
                const isAlreadyAnswered = !!checkedRadio;

                const main = radioGroup.closest('main') || radioGroup.parentElement.parentElement;
                const candidateTexts = Array.from(main.querySelectorAll('p, div.md-view, h1, h2, h3, h4, span'))
                    .filter(el => !radioGroup.contains(el))
                    .map(el => el.innerText.trim())
                    .filter(t => t.length > 5 && !t.includes('Difficulty:') && !t.includes('Score:') && !/^\\d+m?$/.test(t));

                const uniqueTexts = Array.from(new Set(candidateTexts));
                const qText = uniqueTexts.length > 0 ? uniqueTexts[0] : "";
                const labels = Array.from(radioGroup.querySelectorAll('label, [role="radio"]'));
                const options = labels.map(l => l.innerText.trim());

                const buttons = Array.from(document.querySelectorAll('button'));
                const nextBtn = buttons.find(b => (b.innerText || '').toLowerCase().includes('next'));
                const isNextDisabled = nextBtn ? nextBtn.disabled : true;

                const submitBtn = buttons.find(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return (txt.includes('submit') || txt.includes('finish') || txt.includes('end test')) && !b.disabled;
                });

                return {
                    question: qText,
                    options: options,
                    isAlreadyAnswered: isAlreadyAnswered,
                    isNextDisabled: isNextDisabled,
                    hasSubmit: !!submitBtn
                };
            }""")

            if not mcq_data or not mcq_data.get("options"):
                print("[*] No active MCQ question found or quiz completed.")
                break

            q_text = mcq_data["question"]
            options = mcq_data["options"]
            is_answered = mcq_data.get("isAlreadyAnswered")

            if is_answered:
                print(f"[OK] MCQ Q{q_num}: Already answered/completed. Verifying next question...")
            else:
                print(f"\n[*] Question {q_num} (Unanswered): {q_text[:70]}...")
                letters = ['A', 'B', 'C', 'D', 'E', 'F']
                formatted_opts = []
                for i, opt in enumerate(options):
                    lbl = letters[i] if i < len(letters) else str(i+1)
                    cleaned = re.sub(r'^[A-Fa-f][\)\.\:\-]\s*', '', opt)
                    formatted_opts.append(f"{lbl}) {cleaned}")

                opt_idx = 0
                selected_letter = "A"

                if brain.has_key():
                    try:
                        shot_bytes = await test_page.screenshot(type="jpeg", quality=85)
                    except Exception:
                        shot_bytes = None
                    sol = brain.solve_mcq(q_text, options, screenshot_bytes=shot_bytes)
                    selected_letter = sol.get("selected_option", "A")
                    opt_idx = sol.get("option_index", 0)
                    print(f"[OK] ⚡ Brain Solution: Option {selected_letter} - {sol.get('explanation', '')[:80]}")
                else:
                    prompt = f"Question:\n{q_text}\n\nOptions:\n" + "\n".join(formatted_opts) + "\n\nReply with only the correct option letter (example: B) and a short reason."
                    reply = await self.ask_gemini(prompt)
                    match = re.search(r'\b([A-D])\b', reply)
                    selected_letter = match.group(1) if match else "A"
                    opt_idx = ord(selected_letter) - ord('A')
                    print(f"[OK] Selected: {selected_letter}")

                await test_page.evaluate("""(idx) => {
                    const labels = Array.from(document.querySelectorAll('[role="radiogroup"] label, .MuiRadioGroup-root label'));
                    if (labels[idx]) {
                        labels[idx].click();
                        const r = labels[idx].querySelector('input[type="radio"]');
                        if (r && r.click) r.click();
                        labels[idx].dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    }
                }""", opt_idx)
                await asyncio.sleep(1)

                await self.take_screenshot(f"mcq_q{q_num}_{selected_letter}")

            # Check if this is the last question (Next is disabled, submit exists, or q_num >= 30)
            if mcq_data.get("isNextDisabled") or mcq_data.get("hasSubmit") or q_num >= 30:
                print("[*] Reached last question of MCQ quiz. Submitting assessment...")
                # Check for Submit button
                await test_page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const sub = buttons.find(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return (txt.includes('submit') || txt.includes('finish') || txt.includes('end test')) && !b.disabled;
                    });
                    if (sub) {
                        sub.click();
                        sub.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    }
                }""")
                await asyncio.sleep(1.5)

                # Confirm submission modal if one appears (e.g. "Confirm", "Yes", "End Test")
                await test_page.evaluate("""() => {
                    const confirmBtns = Array.from(document.querySelectorAll('.MuiDialog-root button, .MuiModal-root button, [role="dialog"] button, button')).filter(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return (txt.includes('confirm') || txt.includes('yes') || txt.includes('end test') || txt.includes('submit')) && !b.disabled;
                    });
                    if (confirmBtns.length > 0) {
                        confirmBtns[confirmBtns.length - 1].click();
                    }
                }""")
                await asyncio.sleep(2)
                break

            # Click Next
            clicked_next = await test_page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                const next = buttons.find(b => (b.innerText || '').toLowerCase().includes('next') && !b.disabled);
                if (next) {
                    next.click();
                    next.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    return true;
                }
                return false;
            }""")
            await asyncio.sleep(1.5)
            if not clicked_next:
                break

        print("[OK] MCQ Assessment finished.")

    # ==========================
    # 2. SOLVING LABS (Coding & SQL with Character-by-Character Typing)
    async def prepare_editor_for_code(self, test_page):
        """
        Completely erases any previously typed code:
        - If markers (-- start your solution ... -- end your solution) exist, it resets
          everything strictly between the markers to blank, preserving comments and driver code.
        - If no markers exist, it wipes the entire editor clean.
        - Places the cursor at the exact insertion line and focuses the Monaco textarea.
        """
        await test_page.evaluate("""() => {
            if (window.monaco && window.monaco.editor && window.monaco.editor.getEditors().length > 0) {
                const ed = window.monaco.editor.getEditors()[0];
                const val = ed.getValue();

                const startRegex = /(--|\\/\\/|#|\\/\\*)\\s*start\\s*(?:your\\s*)?solution[\\s\\S]*?\\n/i;
                const endRegex = /(--|\\/\\/|#|\\/\\*)\\s*end\\s*(?:your\\s*)?solution/i;

                const startMatch = val.match(startRegex);
                const endMatch = val.match(endRegex);

                if (startMatch && endMatch && startMatch.index < endMatch.index) {
                    const startIdx = startMatch.index + startMatch[0].length;
                    const endIdx = endMatch.index;

                    const before = val.substring(0, startIdx);
                    const after = val.substring(endIdx);

                    // Reset value with clean blank line between markers
                    ed.setValue(before + '\\n' + after);

                    const model = ed.getModel();
                    const pos = model.getPositionAt(startIdx);
                    ed.setPosition(pos);
                    ed.focus();
                } else {
                    // No markers: wipe editor completely clean
                    ed.setValue('');
                    ed.setPosition({ lineNumber: 1, column: 1 });
                    ed.focus();
                }
                const ta = document.querySelector('.monaco-editor textarea');
                if (ta) ta.focus();
            } else {
                const ta = document.querySelector('textarea:not(.monaco-editor textarea), .monaco-editor textarea');
                if (ta) {
                    ta.value = '';
                    ta.focus();
                }
            }
        }""")

    # ==========================
    async def solve_coding_lab(self, test_page):
        print("\n[*] Starting Coding Lab with Character-by-Character Typing...")
        await test_page.bring_to_front()

        # Check total questions in sidebar (1 to 10)
        total_questions = await test_page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('h6, p, button, div')).filter(el => {
                const t = el.innerText ? el.innerText.trim() : '';
                return /^[0-9]+$/.test(t) && parseInt(t) >= 1 && parseInt(t) <= 50 && el.children.length === 0 && (el.className.includes('Typography') || (el.parentElement && el.parentElement.className.includes('MuiBox')));
            });
            return els.length || 10;
        }""")
        print(f"[*] Total Lab Questions: {total_questions}")

        for q_idx in range(1, total_questions + 1):
            print(f"\n" + "="*50)
            print(f"[*] Solving Lab Question {q_idx}/{total_questions}")
            print("="*50)

            # Switch to the specific question if needed
            await test_page.evaluate("""(num) => {
                const items = Array.from(document.querySelectorAll('h6, p, button, div')).filter(el => {
                    const t = el.innerText ? el.innerText.trim() : '';
                    return t === String(num) && el.children.length === 0 && (el.className.includes('Typography') || (el.parentElement && el.parentElement.className.includes('MuiBox')));
                });
                if (items.length > 0) items[0].click();
            }""", q_idx)
            await asyncio.sleep(2)

            # Record initial incomplete/starter code before attempt 1
            initial_starter_code = await test_page.evaluate("""() => {
                if (window.monaco && window.monaco.editor && window.monaco.editor.getEditors().length > 0) {
                    return window.monaco.editor.getEditors()[0].getValue();
                }
                const ta = document.querySelector('textarea:not(.monaco-editor textarea)');
                return ta ? ta.value : '';
            }""")

            # Extract Problem Statement and Language
            info = await test_page.evaluate("""() => {
                let lang = "PostgreSQL";
                const langEl = document.querySelector('.MuiSelect-select, .language-selector, [aria-haspopup="listbox"]');
                if (langEl) lang = langEl.innerText.trim();
                return {
                    language: lang,
                    text: document.body.innerText.substring(0, 2500)
                };
            }""")

            language = info.get("language", "PostgreSQL")
            problem_text = info.get("text", "")
            print(f"[*] Language: {language}")

            # Check if editor has solution delimiter markers
            has_markers = bool(
                re.search(r'(--|//|#|/\*)\s*start\s*(?:your\s*)?solution', initial_starter_code, re.IGNORECASE) and
                re.search(r'(--|//|#|/\*)\s*end\s*(?:your\s*)?solution', initial_starter_code, re.IGNORECASE)
            )

            # Prepare prompt with incomplete code included as requested
            if has_markers:
                prompt = (
                    f"Solve the following {language} coding / SQL challenge.\n\n"
                    f"Problem Statement:\n{problem_text}\n\n"
                    f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                    f"INSTRUCTIONS:\n"
                    f"- Write ONLY the code/query that belongs strictly between the start and end solution markers.\n"
                    f"- Do NOT repeat the marker lines ('-- start your solution', '-- end your solution') or Driver Code in your response.\n"
                    f"- Return ONLY the executable query or code inside triple backticks with no commentary."
                )
            else:
                prompt = (
                    f"Solve the following SQL query / programming problem in {language}.\n"
                    f"Return ONLY the executable query or code inside triple backticks with no comments, introductory or concluding text.\n\n"
                    f"Problem Statement:\n{problem_text}"
                )
                if initial_starter_code.strip():
                    prompt += f"\n\nIncomplete / Starter Code in Editor:\n```{language}\n{initial_starter_code}\n```\n"

            for attempt in range(1, 4):
                print(f"[*] --- Attempt {attempt}/3 ---")
                try:
                    shot_bytes = await test_page.screenshot(type="jpeg", quality=85)
                except Exception:
                    shot_bytes = None
                answer = await self.ask_gemini(prompt, screenshot_bytes=shot_bytes)

                # Strip markdown syntax and language identifier
                clean_code = answer
                if "```" in answer:
                    match = re.search(r'```(?:[a-zA-Z0-9_\+#\-]*\n)?([\s\S]*?)```', answer)
                    if match:
                        clean_code = match.group(1).strip()
                # Remove accidental first line language tag like 'SQL' or 'PostgreSQL'
                lines = clean_code.split("\n")
                if lines and lines[0].strip().lower() in ['sql', 'postgresql', 'postgres', 'python', 'python3', 'java', 'cpp']:
                    clean_code = "\n".join(lines[1:]).strip()

                # Clean out any echoes of start/end solution or driver code
                clean_code = re.sub(r'(--|//|#|/\*)\s*start(?:\s*your)?\s*solution.*?\n', '', clean_code, flags=re.IGNORECASE)
                clean_code = re.sub(r'(--|//|#|/\*)\s*end(?:\s*your)?\s*solution.*', '', clean_code, flags=re.IGNORECASE)
                clean_code = re.sub(r'(--|//|#|/\*)\s*driver\s*code[\s\S]*$', '', clean_code, flags=re.IGNORECASE).strip()

                print(f"[*] Erasing previously typed code and preparing clean editor...")
                try:
                    await test_page.bring_to_front()
                except Exception:
                    pass
                await self.prepare_editor_for_code(test_page)
                await asyncio.sleep(0.4)

                is_hidden = await test_page.evaluate("() => document.hidden || document.visibilityState === 'hidden'")
                if not is_hidden:
                    print(f"[*] Typing code ({len(clean_code)} chars) into editor...")
                    try:
                        await test_page.keyboard.type(clean_code, delay=15)
                    except Exception:
                        pass
                else:
                    print("[*] Minimized/Background mode active: Injecting solution directly into editor...")

                # Minimized-window verification & marker-preserving synchronization
                synced = await test_page.evaluate("""({ code, hasMarkers }) => {
                    if (window.monaco && window.monaco.editor && window.monaco.editor.getEditors().length > 0) {
                        const ed = window.monaco.editor.getEditors()[0];
                        const val = ed.getValue();
                        const snippet = code.substring(0, Math.min(25, code.length)).trim();

                        if (!snippet || !val.includes(snippet)) {
                            if (hasMarkers) {
                                const startRegex = /(--|\\/\\/|#|\\/\\*)\\s*start\\s*(?:your\\s*)?solution[\\s\\S]*?\\n/i;
                                const endRegex = /(--|\\/\\/|#|\\/\\*)\\s*end\\s*(?:your\\s*)?solution/i;
                                const startMatch = val.match(startRegex);
                                const endMatch = val.match(endRegex);

                                if (startMatch && endMatch && startMatch.index < endMatch.index) {
                                    const startIdx = startMatch.index + startMatch[0].length;
                                    const endIdx = endMatch.index;
                                    const before = val.substring(0, startIdx);
                                    const after = val.substring(endIdx);
                                    ed.setValue(before + '\\n' + code + '\\n' + after);
                                    return true;
                                }
                            }
                            ed.setValue(code);
                            return true;
                        }
                    } else {
                        const ta = document.querySelector('textarea:not(.monaco-editor textarea)');
                        if (ta && (!ta.value || !ta.value.includes(code.substring(0, 20)))) {
                            ta.value = code;
                            return true;
                        }
                    }
                    return false;
                }""", { "code": clean_code, "hasMarkers": has_markers })

                if synced:
                    print("[OK] Synchronized code into Monaco (background/minimized mode ensured)!")
                else:
                    print("[OK] Finished typing code!")

                await asyncio.sleep(1)

                # Click Submit
                print("[*] Clicking Submit...")
                await test_page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button')).find(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return (txt.includes('submit') || txt.includes('run code')) && !b.disabled;
                    });
                    if (btn) {
                        btn.click();
                        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    }
                }""")

                await asyncio.sleep(6)
                await self.take_screenshot(f"lab_q{q_idx}_attempt{attempt}")

                # Check test case status
                test_status = await test_page.evaluate("""() => {
                    const text = document.body.innerText;
                    const passed = text.includes("Passed") && !text.includes("Failed") && !text.includes("Wrong Answer") && !text.includes("Error");
                    const errorBox = document.querySelector('.test-results, .output-console, .console, .terminal');
                    return {
                        passed: passed,
                        details: errorBox ? errorBox.innerText.substring(0, 1000) : text.substring(0, 1000)
                    };
                }""")

                if test_status.get("passed"):
                    print(f"[OK] Question {q_idx} PASSED!")
                    break
                else:
                    print(f"[!] Attempt {attempt} failed. Retrying...")
                    if has_markers:
                        prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem Statement:\n{problem_text}\n\n"
                            f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                            f"Previous Code Typed:\n```{language}\n{clean_code}\n```\n\n"
                            f"Error Details:\n{test_status.get('details', '')}\n\n"
                            f"Fix the error. Return ONLY the corrected code to place between the start and end solution markers inside triple backticks with no commentary."
                        )
                    else:
                        prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem:\n{problem_text}\n\n"
                            f"Previous Code:\n```{language}\n{clean_code}\n```\n\n"
                            f"Error Details:\n{test_status.get('details', '')}\n\n"
                            f"Fix it. Return ONLY the corrected code inside triple backticks with no commentary."
                        )

        print("[OK] Coding Lab finished.")

    async def tick_activity_checkbox(self, activity_name):
        """Ticks the completion checkbox on the sidebar / submodule card for the given activity."""
        print(f"[*] Ticking side checkbox for: '{activity_name}'...")
        try:
            await self.bytexl_page.bring_to_front()
        except Exception:
            pass
        await asyncio.sleep(1)

        result = await self.bytexl_page.evaluate("""(actName) => {
            const cleanName = (actName || '').trim().toLowerCase();

            // 1. Try to find the specific row in accordions or lists
            const allItems = Array.from(document.querySelectorAll(
                '.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, .MuiAccordionDetails-root > div, ' +
                'a.MuiListItem-root, .MuiListItem-root, [role="button"], tr, div'
            ));

            let matched = allItems.filter(el => {
                const txt = (el.innerText || '').trim().toLowerCase();
                return (txt.includes(cleanName) || (cleanName && cleanName.includes(txt))) && txt.length < 160;
            });

            for (const item of matched) {
                const row = item.closest('.MuiListItem-root, tr, [role="button"]') || item.parentElement || item;
                const cb = row.querySelector(
                    'input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], ' +
                    'span[aria-label*="complete"], button[aria-label*="complete"], [aria-label*="Mark as"]'
                );

                if (cb) {
                    const isChecked = cb.checked ||
                                      row.innerHTML.includes('Mui-checked') ||
                                      row.querySelector('[data-testid="CheckBoxIcon"]') !== null ||
                                      cb.getAttribute('aria-label') === 'Mark as incomplete';
                    if (!isChecked) {
                        if (cb.click) cb.click();
                        else cb.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                        return { ticked: true, mode: "row_match", name: actName };
                    } else {
                        return { already: true, name: actName };
                    }
                }
            }

            // 2. Try currently selected item (.Mui-selected)
            const selected = document.querySelector('.Mui-selected');
            if (selected) {
                const cb = selected.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], span[aria-label*="complete"]');
                if (cb && !selected.innerHTML.includes('Mui-checked')) {
                    if (cb.click) cb.click();
                    else cb.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    return { ticked: true, mode: "selected" };
                }
            }

            // 3. Look for any "Mark as Complete" button on the main page view
            const markBtns = Array.from(document.querySelectorAll('button, span, a')).filter(b => {
                const txt = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                return txt.includes('mark as complete') || txt.includes('mark complete');
            });
            if (markBtns.length > 0) {
                markBtns[0].click();
                return { ticked: true, mode: "main_button" };
            }

            return { ticked: false };
        }""", activity_name)

        if result.get("ticked"):
            print(f"[SUCCESS] ✅ Ticked side completion checkbox for '{activity_name}'!")
        elif result.get("already"):
            print(f"[OK] Side checkbox for '{activity_name}' is already ticked/complete.")
        else:
            print(f"[!] Checkbox for '{activity_name}' not found or already verified. Proceeding...")

        if activity_name:
            self.completed_activities.add(activity_name)
        await asyncio.sleep(1.5)

    async def handle_theory_reading(self, page):
        """Scrolls smoothly through theory portion, clicks Mark As Completed, and clicks Next > or Take Quiz."""
        title = await page.evaluate("() => { const h = document.querySelector('h1, h2, h3, h4, .chapter-title'); return h ? h.innerText.trim() : 'Theory Topic'; }")
        print(f"[*] 📖 Reading theory: '{title}'. Scrolling content...")
        try:
            await page.bring_to_front()
        except Exception:
            pass

        # 1. Smooth scroll down in chunks to simulate realistic reading & load diagrams
        await page.evaluate("""async () => {
            const total = document.body.scrollHeight;
            const step = Math.max(300, Math.floor(total / 5));
            for (let y = 0; y <= total; y += step) {
                window.scrollTo({ top: y, behavior: 'smooth' });
                await new Promise(r => setTimeout(r, 300));
                window.scrollTo(0, y);
                window.dispatchEvent(new Event('scroll'));
                document.dispatchEvent(new Event('scroll'));
                await new Promise(r => setTimeout(r, 200));
            }
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            window.scrollTo(0, document.body.scrollHeight);
            window.dispatchEvent(new Event('scroll'));
            document.dispatchEvent(new Event('scroll'));
        }""")
        await asyncio.sleep(2)

        # 2. Click "Mark As Completed" button at bottom right
        marked = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            const markBtn = btns.find(b => {
                const txt = (b.innerText || '').trim().toLowerCase();
                return txt === 'mark as completed' || txt === 'mark as complete' || txt === 'mark complete';
            });
            if (markBtn && !markBtn.disabled) {
                markBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                markBtn.click();
                markBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return true;
            }
            return false;
        }""")
        if marked:
            print(f"[OK] ✅ Clicked 'Mark As Completed' on '{title}'!")
            await asyncio.sleep(1.5)
        else:
            print(f"[*] Theory '{title}' marked complete or button not present.")

        # 3. Check if "Take The Quiz Now!" card is at the bottom of the reading
        take_quiz_btn = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            const quizBtn = btns.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return (t.includes('take the quiz') || t.includes('take quiz') || t.includes('start quiz')) && !b.disabled;
            });
            if (quizBtn) {
                quizBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                quizBtn.click();
                return true;
            }
            return false;
        }""")
        if take_quiz_btn:
            print("[OK] 🎯 Theory completed! Launching chapter quiz via 'Take The Quiz Now!'...")
            await asyncio.sleep(3.5)
            return True

        # 4. Advance using 'Next >' button
        advanced = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('button, a'));
            const nextBtn = links.find(l => {
                const txt = (l.innerText || '').trim().toLowerCase();
                return (txt === 'next >' || txt === 'next' || txt.startsWith('next >') || txt.includes('next >')) && !l.disabled;
            });
            if (nextBtn) {
                nextBtn.click();
                return true;
            }
            return false;
        }""")

        if advanced:
            print("[*] Advancing to next topic via 'Next >'...")
            await asyncio.sleep(3)
            return True

        # 5. Fallback: Click next uncompleted topic in left sidebar
        sidebar_next = await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('.MuiListItem-root, [role="button"], a')).filter(el => {
                const t = (el.innerText || '').trim();
                return t.length > 2 && t.length < 120 && !t.includes('Go Back To Course');
            });
            const selectedIdx = items.findIndex(el => el.classList.contains('Mui-selected') || el.getAttribute('aria-selected') === 'true');
            if (selectedIdx !== -1 && selectedIdx + 1 < items.length) {
                items[selectedIdx + 1].click();
                return true;
            }
            return false;
        }""")
        if sidebar_next:
            print("[*] Advancing to next topic via sidebar navigation...")
            await asyncio.sleep(3)
            return True

        return False

    async def take_page_screenshot(self, page) -> Optional[bytes]:
        try:
            return await page.screenshot(type="jpeg", quality=80)
        except Exception:
            return None

    async def get_visible_buttons(self, page) -> List[str]:
        try:
            return await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a')).map(b => (b.innerText || '').trim());
                return Array.from(new Set(btns)).filter(t => t.length > 0 && t.length < 50).slice(0, 30);
            }""")
        except Exception:
            return []

    async def get_visible_headings(self, page) -> List[str]:
        try:
            return await page.evaluate("""() => {
                const hs = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, .chapter-title')).map(h => (h.innerText || '').trim());
                return Array.from(new Set(hs)).filter(t => t.length > 1 && t.length < 100).slice(0, 15);
            }""")
        except Exception:
            return []

    async def get_active_page(self):
        """Scans all open tabs and returns the most relevant active page."""
        pages = list(self.context.pages)
        if not pages:
            return None, "none"

        for p in pages:
            try:
                u = p.url.lower()
                if "/test/" in u or "/assessment/" in u:
                    return p, "test"
            except Exception:
                pass

        for p in pages:
            try:
                u = p.url.lower()
                if "/module/" in u or "/topic/" in u:
                    return p, "module"
            except Exception:
                pass

        for p in pages:
            try:
                u = p.url.lower()
                if "bytexl" in u and "/courses" in u:
                    return p, "courses"
            except Exception:
                pass

        if self.bytexl_page and not self.bytexl_page.is_closed():
            return self.bytexl_page, "default"
        return pages[-1], "default"

    async def classify_page_state(self, page) -> Dict[str, Any]:
        """Classifies the current page into a recognized operational state."""
        if not page or page.is_closed():
            return {"state": "NO_PAGE"}

        try:
            return await page.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();

                // 1. Login required
                if (url.includes('/login') || url.includes('/signin') || !!document.querySelector('input[type="password"]')) {
                    return { state: "LOGIN" };
                }

                // 2. Active Test / Assessment
                const hasRadios = !!document.querySelector('[role="radiogroup"], .MuiRadioGroup-root, [role="radio"]');
                const hasMonaco = !!document.querySelector('.monaco-editor, textarea#code');
                if (url.includes('/test/') || url.includes('/assessment/') || hasRadios || hasMonaco) {
                    return { state: "TEST_ACTIVE", isCoding: hasMonaco, isMCQ: hasRadios };
                }

                // 3. Reading / Theory Topic
                const hasMarkBtn = Array.from(document.querySelectorAll('button, a')).some(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return t === 'mark as completed' || t === 'mark as complete' || t === 'mark complete';
                });
                const hasSummarize = bodyText.includes('summarize this chapter');
                const hasReadingBreadcrumb = url.includes('/topic/') || !!document.querySelector('.chapter-title, .md-view');
                if (hasMarkBtn || (hasReadingBreadcrumb && (hasSummarize || bodyText.includes('next >') || bodyText.includes('go back to course')))) {
                    return { state: "READING_TOPIC", hasMarkBtn: hasMarkBtn };
                }

                // 4. Quiz / Lab Landing Page (with Start/Take button)
                const openBtn = Array.from(document.querySelectorAll('button, a')).find(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return (t.includes('take the quiz') || t.includes('take quiz') || t.includes('start quiz') || 
                            t.includes('open quiz') || t.includes('start lab') || t.includes('open lab')) && !b.disabled;
                });
                if (openBtn) {
                    return { state: "QUIZ_LANDING", btnText: openBtn.innerText.trim() };
                }

                // 5. Units & Chapters List
                const isUnitsPage = bodyText.includes('units and chapters') || (url.includes('/courses/') && !url.includes('/module/') && !url.includes('/topic/'));
                const hasUnitCards = Array.from(document.querySelectorAll('button, a')).some(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return t.includes('continue learning') || t.includes('start learning') || t.includes('completed');
                });
                if (isUnitsPage && hasUnitCards) {
                    return { state: "UNITS_LIST" };
                }

                // 6. Module Curriculum View (Accordions / Topic items)
                const hasAccordions = document.querySelectorAll('.MuiAccordion-root').length > 0;
                const hasTopicRows = Array.from(document.querySelectorAll('a, .MuiListItem-root')).some(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return t.includes('reading material') || t.includes('challenge') || url.includes('/module/');
                });
                if (hasAccordions || hasTopicRows || url.includes('/module/')) {
                    return { state: "MODULE_CURRICULUM" };
                }

                // 7. Course Catalog
                if (url.endsWith('/courses') || url.endsWith('/courses/') || bodyText.includes('my courses')) {
                    return { state: "COURSES_CATALOG" };
                }

                return { state: "UNKNOWN" };
            }""")
        except Exception as e:
            return {"state": "UNKNOWN", "error": str(e)}

    async def click_button_reliably(self, page, text: str) -> bool:
        """Clicks an element by text using native Playwright events and JS fallback."""
        clean = (text or "").strip()
        if not clean:
            return False

        try:
            loc = page.locator(f'button:has-text("{clean}"), a:has-text("{clean}")').first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=3000)
                return True
        except Exception:
            pass

        try:
            return await page.evaluate("""(targetText) => {
                const targetLower = targetText.toLowerCase();
                const all = Array.from(document.querySelectorAll('button, a, [role="button"], span.MuiButton-label'));
                const match = all.find(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === targetLower || t.includes(targetLower);
                });
                if (match) {
                    const btn = match.closest('button, a, [role="button"]') || match;
                    btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    btn.click();
                    return true;
                }
                return false;
            }""", clean)
        except Exception:
            return False

    async def find_and_click_uncompleted_unit(self, page) -> Dict[str, Any]:
        """Finds the uncompleted unit card and clicks Continue Learning / Resume."""
        try:
            res = await page.evaluate("""() => {
                const allButtons = Array.from(document.querySelectorAll('button, a'));

                // 1. Find exact blue button: Continue Learning, Start Learning, Resume
                let targetBtn = allButtons.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return (txt === 'continue learning' || txt === 'start learning' || txt === 'resume' || txt === 'resume my journey') && !b.disabled;
                });

                // 2. Fallback: primary button not marked completed
                if (!targetBtn) {
                    targetBtn = Array.from(document.querySelectorAll('button.MuiButton-containedPrimary')).find(b => {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        return !txt.includes('completed');
                    });
                }

                // 3. Fallback: button containing continue learning / resume
                if (!targetBtn) {
                    targetBtn = allButtons.find(b => {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        return (txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) && !txt.includes('completed');
                    });
                }

                if (targetBtn) {
                    let card = targetBtn.closest('.MuiPaper-root, .MuiCard-root, .MuiBox-root, [class*="MuiButton-outlined"]') || targetBtn.parentElement;
                    let unitTitle = 'Unit';
                    if (card) {
                        const lines = card.innerText.split(/\\r?\\n/).map(l => l.trim()).filter(Boolean);
                        const tLine = lines.find(l => !l.toLowerCase().includes('continue learning') && !l.toLowerCase().includes('start learning') && !l.toLowerCase().includes('completed') && !l.toLowerCase().includes('challenge') && !l.toLowerCase().includes('chapter'));
                        if (tLine) unitTitle = tLine;
                    }
                    targetBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    targetBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    targetBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    targetBtn.click();
                    return { clicked: true, title: unitTitle };
                }
                return { clicked: false };
            }""")

            if res.get("clicked"):
                return res

            loc = page.locator('button:has-text("Continue Learning"), a:has-text("Continue Learning")').first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=3000)
                return {"clicked": True, "title": "Unit"}
        except Exception as e:
            print(f"[!] Unit click interaction: {e}")

        return {"clicked": False}

    async def expand_all_accordions(self, page):
        """Expands all collapsed submodule accordions."""
        try:
            await page.evaluate("""() => {
                const summaries = Array.from(document.querySelectorAll('.MuiAccordionSummary-root[aria-expanded="false"], .MuiAccordionSummary-root:not(.Mui-expanded)'));
                summaries.forEach(s => s.click());
            }""")
        except Exception:
            pass

    async def find_next_uncompleted_topic(self, page, done_list: List[str]) -> Optional[Dict[str, Any]]:
        """Scans module view for the next uncompleted topic row."""
        try:
            return await page.evaluate("""(doneList) => {
                const summaries = Array.from(document.querySelectorAll('.MuiAccordionSummary-root[aria-expanded="false"], .MuiAccordionSummary-root:not(.Mui-expanded)'));
                summaries.forEach(s => s.click());

                const candidateRows = Array.from(document.querySelectorAll(
                    '.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, .MuiAccordionDetails-root a, .MuiAccordionDetails-root .MuiListItem-root, .MuiAccordionDetails-root > div, a.MuiListItem-root, .MuiListItem-root'
                )).filter(el => {
                    const t = (el.innerText || '').trim();
                    return t.length > 2 && t.length < 160 && !t.toLowerCase().includes('reading materials') && !t.toLowerCase().includes('challenge');
                });

                for (const row of candidateRows) {
                    const rawText = row.innerText.trim();
                    const titleLine = rawText.split('\\n')[0].trim();

                    if (doneList.some(d => titleLine.includes(d) || (d && d.includes(titleLine)))) continue;

                    const accordion = row.closest('.MuiAccordion-root');
                    if (accordion) {
                        const accSummary = accordion.querySelector('.MuiAccordionSummary-root');
                        if (accSummary && (accSummary.innerText || '').toLowerCase().includes('completed')) {
                            continue;
                        }
                    }

                    const cb = row.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], [data-testid*="Check"], span[aria-label*="complete"]');
                    const isChecked = row.innerHTML.includes('Mui-checked') ||
                                      row.querySelector('[data-testid="CheckBoxIcon"]') !== null ||
                                      row.querySelector('[data-testid*="Check"]') !== null ||
                                      row.getAttribute('aria-checked') === 'true' ||
                                      row.classList.contains('completed') ||
                                      row.innerText.includes('Completed') ||
                                      (cb && (cb.checked || cb.getAttribute('aria-label') === 'Mark as incomplete'));

                    if (!isChecked) {
                        return {
                            text: titleLine,
                            href: row.href || (row.querySelector('a') ? row.querySelector('a').href : null)
                        };
                    }
                }
                return null;
            }""", done_list)
        except Exception:
            return None

    async def click_topic_reliably(self, page, topic_info: Dict[str, Any]) -> bool:
        name = topic_info.get("text", "")
        href = topic_info.get("href")

        try:
            clicked = await page.evaluate("""(targetName) => {
                const clean = targetName.toLowerCase();
                const links = Array.from(document.querySelectorAll('.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, a, .MuiListItem-root, [role="button"]'));
                const target = links.find(l => (l.innerText || '').toLowerCase().includes(clean));
                if (target) {
                    const t = target.querySelector('.MuiListItemText-root') || target;
                    t.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    t.click();
                    return true;
                }
                return false;
            }""", name)
            if clicked:
                return True
        except Exception:
            pass

        if href:
            try:
                await page.goto(href)
                return True
            except Exception:
                pass

        return False

    async def check_and_click_next_unit(self, page) -> bool:
        try:
            return await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                const btn = btns.find(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return (t.includes('go to next unit') || t.includes('next unit')) && !b.disabled;
                });
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
        except Exception:
            return False

    async def check_and_click_back_to_course(self, page) -> bool:
        try:
            return await page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('button, a'));
                const back = links.find(l => {
                    const t = (l.innerText || '').toLowerCase();
                    return t.includes('go back to course') || t.includes('back to course') || t.includes('units and chapters');
                });
                if (back) {
                    back.click();
                    return true;
                }
                return false;
            }""")
        except Exception:
            return False

    async def is_landing_already_completed(self, page, activity_name: str) -> bool:
        try:
            return await page.evaluate("""(name) => {
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                return bodyText.includes('highest score') ||
                       bodyText.includes('quiz completed') ||
                       bodyText.includes('you scored') ||
                       bodyText.includes('already submitted');
            }""", activity_name)
        except Exception:
            return False

    async def advance_next(self, page) -> bool:
        try:
            return await page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('button, a'));
                const nextBtn = links.find(l => {
                    const txt = (l.innerText || '').trim().toLowerCase();
                    return (txt === 'next >' || txt === 'next' || txt.includes('next >') || txt.includes('go to next unit')) && !l.disabled;
                });
                if (nextBtn) {
                    nextBtn.click();
                    return true;
                }
                return false;
            }""")
        except Exception:
            return False

    async def handle_courses_catalog(self, page, target_course: str) -> bool:
        """Navigates target course or first uncompleted course in catalog."""
        t_course = (target_course or "").strip()
        if t_course:
            print(f"[*] Searching for course '{t_course}' in catalog...")
            try:
                search_input = await page.wait_for_selector('input[placeholder*="search" i]', timeout=4000)
                if search_input:
                    await search_input.fill("")
                    await search_input.fill(t_course)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(2)
            except Exception:
                pass

            found = await page.evaluate("""(t) => {
                const tLower = t.toLowerCase();
                const cards = Array.from(document.querySelectorAll('.MuiPaper-root, .MuiCard-root, .MuiBox-root')).filter(c => {
                    const txt = (c.innerText || '').toLowerCase();
                    return (txt.includes('completion') || txt.includes('continue learning') || txt.includes('resume')) && txt.includes(tLower);
                });
                if (cards.length > 0) {
                    const btn = Array.from(cards[0].querySelectorAll('button, a')).find(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                    }) || cards[0].querySelector('button, a') || cards[0];
                    btn.click();
                    return true;
                }
                return false;
            }""", t_course)
            if found:
                print(f"[OK] 🎯 Opened course: '{t_course}'!")
                return True

        print("[*] Scanning 'My Courses' for uncompleted course (< 100%)...")
        return await page.evaluate("""() => {
            const btnList = Array.from(document.querySelectorAll('button, a')).filter(b => {
                const txt = (b.innerText || '').toLowerCase();
                return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
            });
            for (let btn of btnList) {
                let p = btn;
                let isCompleted = false;
                for (let i = 0; i < 7; i++) {
                    if (!p.parentElement) break;
                    p = p.parentElement;
                    const txt = p.innerText || '';
                    if (txt.includes('100%') && txt.includes('completion')) {
                        isCompleted = true;
                        break;
                    }
                }
                if (!isCompleted) {
                    btn.click();
                    return true;
                }
            }
            if (btnList.length > 0) {
                btnList[0].click();
                return true;
            }
            return false;
        }""")

    # ==========================
    # 3. FULL COURSE & MODULE AUTO-NAVIGATOR
    # ==========================
    async def run_master_loop(self):
        ok = await self.connect()
        if not ok:
            return

        print("\n" + "="*60)
        print("[🚀] BYTE-XL AUTONOMOUS AGENT ACTIVE")
        print("="*60)

        target_course = (self.target or "").strip()
        print(f"[*] Autonomous AI Assistant Active (Target: '{target_course or 'Next Uncompleted'}')")

        completed_set = set()
        activities_completed = 0
        current_activity_name = ""
        stuck_counter = 0
        last_url_state = ""
        step = 0
        max_steps = 200

        while step < max_steps:
            step += 1
            await asyncio.sleep(1.5)

            # 1. Multi-tab detection: find active page across all tabs
            active_page, p_kind = await self.get_active_page()
            if not active_page:
                print("[⚠️] No active browser tab found. Waiting 3s...")
                await asyncio.sleep(3)
                continue

            try:
                await active_page.bring_to_front()
            except Exception:
                pass

            # 2. Classify Page State
            state_info = await self.classify_page_state(active_page)
            current_state = state_info.get("state", "UNKNOWN")
            current_url = active_page.url
            state_sig = f"{current_state}:{current_url}"

            if state_sig == last_url_state:
                stuck_counter += 1
            else:
                stuck_counter = 0
                last_url_state = state_sig

            # Handle LOGIN State
            if current_state == "LOGIN":
                print("[⚠️] ByteXL login required! Please sign in in the browser to continue...")
                await asyncio.sleep(3)
                continue

            # Handle TEST_ACTIVE State (MCQ Quiz or Coding Lab)
            if current_state == "TEST_ACTIVE":
                print("[*] 🧠 Active Assessment detected. Solving with Gemini...")
                if state_info.get("isCoding"):
                    await self.solve_coding_lab(active_page)
                else:
                    await self.solve_mcq_test(active_page)

                if current_activity_name:
                    completed_set.add(current_activity_name)
                activities_completed += 1

                # If test_page was a secondary popup tab, close it
                if active_page != self.bytexl_page and len(self.context.pages) > 1:
                    try:
                        await active_page.close()
                    except Exception:
                        pass
                await asyncio.sleep(2)

                # Return focus to main course page and tick side completion checkbox
                course_page, _ = await self.get_active_page()
                if course_page and current_activity_name:
                    ticked = await self.tick_activity_checkbox(current_activity_name)
                    if ticked:
                        print(f"[OK] ✅ Ticked completion box for '{current_activity_name}'!")
                continue

            # Handle READING_TOPIC State
            if current_state == "READING_TOPIC":
                print("[*] Reading topic detected. Processing reading content...")
                advanced = await self.handle_theory_reading(active_page)
                if current_activity_name:
                    completed_set.add(current_activity_name)
                activities_completed += 1
                await asyncio.sleep(2)
                continue

            # Handle QUIZ_LANDING State
            if current_state == "QUIZ_LANDING":
                is_done = await self.is_landing_already_completed(active_page, current_activity_name)
                if is_done:
                    print(f"[OK] Activity '{current_activity_name or 'Quiz'}' is already completed. Advancing...")
                    if current_activity_name:
                        completed_set.add(current_activity_name)
                    await self.advance_next(active_page)
                    continue

                btn_text = state_info.get("btnText", "Take The Quiz Now!")
                print(f"[*] Launching Activity: '{btn_text}'...")
                await self.click_button_reliably(active_page, btn_text)
                await asyncio.sleep(4)
                continue

            # Handle MODULE_CURRICULUM State
            if current_state == "MODULE_CURRICULUM":
                print("[*] Processing module curriculum...")
                await self.expand_all_accordions(active_page)

                next_topic = await self.find_next_uncompleted_topic(active_page, list(completed_set))
                if next_topic:
                    current_activity_name = next_topic['text']
                    print(f"[🎯] Opening topic: '{current_activity_name}'")
                    clicked = await self.click_topic_reliably(active_page, next_topic)
                    await asyncio.sleep(3.5)
                    continue
                else:
                    # Check for Next Unit
                    has_next_unit = await self.check_and_click_next_unit(active_page)
                    if has_next_unit:
                        print("[OK] All submodules in this unit complete! Advancing: 'Go To Next Unit'...")
                        await asyncio.sleep(4)
                        continue

                    # Check for Back to Course
                    has_back = await self.check_and_click_back_to_course(active_page)
                    if has_back:
                        print("[*] Unit finished! Returning to Units & Chapters list...")
                        await asyncio.sleep(3.5)
                        continue

                    # Consult Gemini Brain
                    if brain.has_key():
                        shot_bytes = await self.take_page_screenshot(active_page)
                        dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                     await self.get_visible_buttons(active_page),
                                                     await self.get_visible_headings(active_page),
                                                     screenshot_bytes=shot_bytes)
                        if dec.get("action") == "click_button" and dec.get("target_button_text"):
                            target = dec.get("target_button_text")
                            print(f"[🧠] AI Brain Decision: {dec.get('reason')} -> Clicking '{target}'")
                            await self.click_button_reliably(active_page, target)
                            await asyncio.sleep(4)
                            continue

                    print("[OK] 🎉 All topics, quizzes, and labs in this unit are completed!")
                    break

            # Handle UNITS_LIST State ("Units and Chapters")
            if current_state == "UNITS_LIST":
                print("[*] Units & Chapters page detected. Finding uncompleted unit...")

                unit_res = await self.find_and_click_uncompleted_unit(active_page)
                if unit_res.get("clicked"):
                    unit_title = unit_res.get("title", "Unit")
                    print(f"[OK] 🚀 Clicked 'Continue Learning' on Unit: '{unit_title}'! Loading...")
                    await asyncio.sleep(4)
                    continue
                else:
                    # Standard selector didn't find button; consult Gemini Brain with screenshot
                    if brain.has_key():
                        print("[🧠] Consulting Gemini AI Brain with screen screenshot to find uncompleted unit...")
                        shot_bytes = await self.take_page_screenshot(active_page)
                        dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                     await self.get_visible_buttons(active_page),
                                                     await self.get_visible_headings(active_page),
                                                     screenshot_bytes=shot_bytes)
                        print(f"[🧠] AI Brain Analysis: {dec.get('reason')}")
                        if dec.get("action") == "click_button" and dec.get("target_button_text"):
                            target = dec.get("target_button_text")
                            clicked = await self.click_button_reliably(active_page, target)
                            if clicked:
                                await asyncio.sleep(4)
                                continue

                    print("[OK] 🎉 All units on the Units & Chapters page are completed!")
                    break

            # Handle COURSES_CATALOG State
            if current_state == "COURSES_CATALOG":
                print("[*] Entering Course from Catalog...")
                entered = await self.handle_courses_catalog(active_page, target_course)
                if entered:
                    await asyncio.sleep(4)
                    continue
                else:
                    print("[*] All courses appear completed or catalog search finished.")
                    break

            # Handle UNKNOWN State (Gemini Vision Self-Healing)
            if current_state == "UNKNOWN":
                print(f"[*] Page state unrecognized ({active_page.url[:60]}). Consulting Gemini Brain...")

                if brain.has_key():
                    shot_bytes = await self.take_page_screenshot(active_page)
                    buttons = await self.get_visible_buttons(active_page)
                    headings = await self.get_visible_headings(active_page)
                    dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                 buttons, headings, screenshot_bytes=shot_bytes)
                    print(f"[🧠] AI Brain Decision: {dec.get('reason')} -> Action: {dec.get('action')} on '{dec.get('target_button_text')}'")

                    if dec.get("action") == "click_button" and dec.get("target_button_text"):
                        await self.click_button_reliably(active_page, dec.get("target_button_text"))
                        await asyncio.sleep(3.5)
                        continue
                    elif dec.get("action") == "mark_complete":
                        await self.handle_theory_reading(active_page)
                        continue
                    elif dec.get("action") == "all_completed":
                        print("[OK] 🎉 AI Brain verified: All course activities are completed!")
                        break

                if stuck_counter >= 3:
                    print("[⚠️] Agent remained on unknown page. Returning to /courses catalog...")
                    try:
                        await active_page.goto("https://app.bytexl.ai/courses")
                        await asyncio.sleep(3.5)
                    except Exception:
                        pass
                else:
                    await asyncio.sleep(3)
                continue

        print("\n" + "="*60)
        print(f"[OK] Master loop finished. Activities completed: {activities_completed}")
        print("="*60)
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous ByteXL Solver Agent")
    parser.add_argument("--port", "-p", type=int, default=9222, help="Browser remote debugging port (default: 9222)")
    parser.add_argument("--target", "-t", type=str, default="", help="Specific course/module to target (e.g. 'Cloud Security')")
    args, unknown = parser.parse_known_args()

    port_arg = args.port
    if "CDP_PORT" in os.environ and port_arg == 9222:
        try:
            port_arg = int(os.environ["CDP_PORT"])
        except ValueError:
            pass

    target_arg = args.target or (unknown[0] if unknown else "")
    agent = MasterByteXLAgent(port=port_arg, target=target_arg)
    asyncio.run(agent.run_master_loop())
