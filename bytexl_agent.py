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
        const u = window.location.href.toLowerCase();
        // 1. NEVER interfere with login, signin, or SSO authentication pages!
        if (u.includes('/login') || u.includes('/signin') || u.includes('accounts.google') || u.includes('login.microsoft') || u.includes('cloudflare')) {
            return;
        }

        if (window.__page_visibility_shim_active) return;
        window.__page_visibility_shim_active = true;

        // Mask navigator.webdriver so Cloudflare Turnstile and SSO auth treat this as genuine human browser
        try {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
        } catch(e) {}

        // 2. Permanent Visibility & Focus Property Overrides (prevents tab-switch warnings)
        const fakeProps = {
            hidden: { get: () => false, configurable: true },
            visibilityState: { get: () => 'visible', configurable: true },
            webkitHidden: { get: () => false, configurable: true },
            webkitVisibilityState: { get: () => 'visible', configurable: true },
            hasFocus: { value: () => true, writable: true, configurable: true }
        };

        for (const [prop, desc] of Object.entries(fakeProps)) {
            try { Object.defineProperty(document, prop, desc); } catch(e) {}
            try { Object.defineProperty(Document.prototype, prop, desc); } catch(e) {}
        }

        // 3. Drop anti-cheat visibilitychange events ONLY (Do NOT block blur/focusout which form inputs require!)
        const blockEvents = ['visibilitychange', 'webkitvisibilitychange'];
        try {
            const origWinAdd = window.addEventListener;
            window.addEventListener = function(type, listener, options) {
                if (blockEvents.includes(type)) return;
                return origWinAdd.apply(this, arguments);
            };
        } catch(e) {}
        try {
            const origDocAdd = document.addEventListener;
            document.addEventListener = function(type, listener, options) {
                if (blockEvents.includes(type)) return;
                return origDocAdd.apply(this, arguments);
            };
        } catch(e) {}

        const stopHandler = (e) => {
            try {
                e.stopImmediatePropagation();
                e.stopPropagation();
            } catch(err) {}
        };
        for (const ev of blockEvents) {
            try { window.addEventListener(ev, stopHandler, true); } catch(e) {}
            try { document.addEventListener(ev, stopHandler, true); } catch(e) {}
        }

        try { Object.defineProperty(document, 'onvisibilitychange', { get: () => null, set: () => {}, configurable: true }); } catch(e) {}

        // 5. Continuous requestAnimationFrame ticker (runs at 60fps even when minimized)
        if (!window.__raf_shim_active) {
            window.__raf_shim_active = true;
            let lastTime = performance.now();
            window.requestAnimationFrame = function(callback) {
                const currTime = performance.now();
                const timeToCall = Math.max(0, 16.6 - (currTime - lastTime));
                const id = window.setTimeout(() => {
                    try { callback(performance.now()); } catch(e) {}
                }, timeToCall);
                lastTime = currTime + timeToCall;
                return id;
            };
            window.cancelAnimationFrame = function(id) {
                clearTimeout(id);
            };
        }
    } catch(e) {}
})();
"""

def restart_browser_with_debugging(exe_path, port, urls=None):
    """
    Gracefully restarts a running Chromium browser with remote debugging on the user's default profile.
    Uses CloseMainWindow so that --restore-last-session brings back all open tabs, cookies, and active ByteXL logins.
    """
    if not exe_path:
        return False
    proc_name = os.path.basename(exe_path)
    short_name = proc_name.replace(".exe", "")

    # 1. Gracefully close main window so tabs and session are saved by Chromium
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Get-Process {short_name} -ErrorAction SilentlyContinue | ForEach-Object {{ $_.CloseMainWindow() }}"],
            timeout=5, capture_output=True
        )
        time.sleep(2)
    except Exception:
        pass

    # 2. Terminate any stubborn background worker process
    if is_browser_process_running(proc_name):
        try:
            subprocess.run(f'taskkill /F /IM {proc_name}', shell=True, capture_output=True, timeout=5)
            time.sleep(1.5)
        except Exception:
            pass

    # 3. Relaunch with remote debugging and session restoration
    flags = [
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--restore-last-session",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes,ThrottleJavascript",
        "--run-all-compositor-stages-before-draw",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
    ]
    if urls:
        flags.extend(urls)
    else:
        flags.extend(["https://app.bytexl.ai/courses", "https://gemini.google.com/app"])

    flag_str = " ".join(flags)
    try:
        os.startfile(exe_path, arguments=flag_str)
        return True
    except Exception:
        pass
    try:
        DETACHED_FLAGS = 0x00000008 | 0x00000200
        subprocess.Popen([exe_path] + flags, creationflags=DETACHED_FLAGS, close_fds=True)
        return True
    except Exception:
        subprocess.Popen(f'start "" "{exe_path}" {flag_str}', shell=True)
        return True

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
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes,ThrottleJavascript",
        "--run-all-compositor-stages-before-draw",
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
        self.target = brain.normalize_acronym((target or "").strip())
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
        proc_name = os.path.basename(exe) if exe else "chrome.exe"
        b_label = proc_name.replace(".exe", "").capitalize()

        # If browser process is already running without remote debugging port, gracefully restart it
        # Chromium rejects --remote-debugging-port on a running instance unless restarted with session restore
        if is_browser_process_running(proc_name):
            print(f"[*] 🔄 Detected {b_label} already running without remote debugging.")
            print(f"[*] Seamlessly enabling remote debugging on port {self.port} (restoring all tabs and ByteXL session)...")
            restart_browser_with_debugging(exe, self.port)
        else:
            print(f"[*] Launching {b_label} browser with remote debugging on port {self.port} (all logins preserved)...")
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

    async def ensure_gemini_page(self):
        """Finds an existing open Gemini tab or opens a new one."""
        if self.gemini_page and not self.gemini_page.is_closed():
            return self.gemini_page

        if self.context:
            for p in self.context.pages:
                try:
                    if not p.is_closed() and "gemini.google.com" in p.url.lower():
                        self.gemini_page = p
                        return self.gemini_page
                except Exception:
                    pass

        # Open new Gemini tab
        if self.context:
            print("[*] 🌐 Opening Google Gemini tab (https://gemini.google.com/app)...")
            try:
                self.gemini_page = await self.context.new_page()
                await self.gemini_page.goto("https://gemini.google.com/app", timeout=30000)
                await asyncio.sleep(2)
                try:
                    await self.gemini_page.evaluate(PAGE_VISIBILITY_SHIM)
                except Exception:
                    pass
                return self.gemini_page
            except Exception as e:
                print(f"[WARN] Could not open Gemini tab: {e}")
        return None

    async def ask_gemini(self, prompt: str, screenshot_bytes: Optional[bytes] = None) -> str:
        """Sends prompt to Gemini and retrieves the clean response."""
        is_coding = any(k in prompt.lower() for k in ["coding", "sql", "select", "challenge", "incomplete code", "def ", "function", "monaco", "query", "code/query"])

        if brain.has_key():
            print(f"\n[*] ⚡ Brain AI Query ({len(prompt)} chars, model={brain.active_model})...")
            try:
                if screenshot_bytes:
                    ans = brain.ask_vision(prompt, screenshot_bytes)
                else:
                    ans = brain.ask_text(prompt)
                if ans and len(ans.strip()) > 0:
                    print(f"[OK] ⚡ Brain replied in 1s: {ans[:80]}...")
                    return ans
            except Exception as e:
                print(f"[WARN] Gemini API error: {e}. Switching to Gemini browser tab to cut/copy/paste...")

        # Browser Tab Cut-Copy-Paste: Switch to Gemini tab
        gem_page = await self.ensure_gemini_page()
        if not gem_page:
            print("[WARN] No Gemini tab and no API key configured.")
            if is_coding:
                return "SELECT 1;"
            return "A"

        print(f"\n[*] 📋 Switching to Gemini tab to prompt ({len(prompt)} chars)...")
        try:
            await gem_page.bring_to_front()
            await asyncio.sleep(0.5)

            existing = await gem_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
            baseline_count = len(existing)

            input_elem = await gem_page.wait_for_selector('rich-textarea [contenteditable="true"], div[role="textbox"], textarea', timeout=12000)
            await input_elem.click()
            try:
                await gem_page.evaluate(PAGE_VISIBILITY_SHIM)
            except Exception:
                pass

            await gem_page.evaluate("""(text) => {
                const el = document.querySelector('rich-textarea [contenteditable="true"]') ||
                           document.querySelector('div[role="textbox"]') ||
                           document.querySelector('textarea');
                if (el) {
                    el.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", prompt)
            await asyncio.sleep(0.8)

            btn = await gem_page.query_selector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button')
            if btn:
                await btn.click()
            else:
                await gem_page.keyboard.press("Enter")
            await gem_page.evaluate("""() => {
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
                stop_btn = await gem_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
                if stop_btn and await stop_btn.is_visible():
                    break
                await asyncio.sleep(0.5)

            answer = ""
            while time.time() - start_wait < 75:
                await asyncio.sleep(1.5)
                stop_btn = await gem_page.query_selector('button[aria-label*="Stop"], button[aria-label*="Pause"]')
                if stop_btn and await stop_btn.is_visible():
                    continue
                responses = await gem_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
                if len(responses) > baseline_count or len(responses) > 0:
                    latest = responses[-1]
                    answer = (await latest.inner_text()).strip()
                    if answer:
                        break

            print(f"[OK] 📋 Copied response from Gemini ({len(answer)} chars): {answer[:80]}...")
            return answer
        except Exception as e:
            print(f"[WARN] Gemini tab interaction error: {e}")
            if is_coding:
                return "SELECT 1;"
            return "A"

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

                # Wait for ByteXL to register answer and dismiss toast
                await asyncio.sleep(1.2)

            # Evaluate navigation buttons dynamically AFTER option is selected
            nav_info = await test_page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));

                // 1. Check for enabled Next button
                const nextBtn = buttons.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return (txt === 'next' || txt === 'next >' || txt.includes('next')) && !b.disabled;
                });
                if (nextBtn) {
                    nextBtn.scrollIntoView({ behavior: 'auto', block: 'center' });
                    nextBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    nextBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    nextBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    nextBtn.click();
                    return { action: "NEXT_CLICKED" };
                }

                // 2. Check for Submit / Finish / End Test button (last question)
                const subBtn = buttons.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return (txt.includes('submit') || txt.includes('finish') || txt.includes('end test') || txt.includes('complete test')) && !b.disabled;
                });
                if (subBtn) {
                    subBtn.scrollIntoView({ behavior: 'auto', block: 'center' });
                    subBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    subBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    subBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    subBtn.click();
                    return { action: "SUBMIT_CLICKED" };
                }

                return { action: "NONE" };
            }""")

            action = nav_info.get("action") if nav_info else "NONE"

            if action == "NEXT_CLICKED":
                print(f"[OK] Answer submitted for Q{q_num}. Advanced to next question.")
                await asyncio.sleep(1.5)
                continue

            elif action == "SUBMIT_CLICKED":
                print("[*] Reached last question of MCQ quiz. Submitting assessment...")
                await asyncio.sleep(1.5)
                await test_page.evaluate("""() => {
                    const confirmBtns = Array.from(document.querySelectorAll('.MuiDialog-root button, .MuiModal-root button, [role="dialog"] button, button')).filter(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return (txt.includes('confirm') || txt.includes('yes') || txt.includes('end test') || txt.includes('submit')) && !b.disabled;
                    });
                    if (confirmBtns.length > 0) {
                        confirmBtns[confirmBtns.length - 1].click();
                    }
                }""")
                await asyncio.sleep(2.5)
                break

            else:
                # Retry after 1.5s in case save request is in flight
                await asyncio.sleep(1.5)
                retry_action = await test_page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    const nextBtn = buttons.find(b => {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        return (txt === 'next' || txt === 'next >' || txt.includes('next')) && !b.disabled;
                    });
                    if (nextBtn) {
                        nextBtn.click();
                        return "NEXT_CLICKED";
                    }
                    const subBtn = buttons.find(b => {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        return (txt.includes('submit') || txt.includes('finish') || txt.includes('end test')) && !b.disabled;
                    });
                    if (subBtn) {
                        subBtn.click();
                        return "SUBMIT_CLICKED";
                    }
                    return "NONE";
                }""")
                if retry_action == "NEXT_CLICKED":
                    print(f"[OK] Answer submitted for Q{q_num}. Advanced to next question.")
                    await asyncio.sleep(1.5)
                    continue
                elif retry_action == "SUBMIT_CLICKED":
                    print("[OK] Assessment completed. Submitted successfully!")
                    await asyncio.sleep(2)
                    break
                else:
                    print(f"[*] Assessment concluded at question {q_num}.")
                    break

        print("[OK] MCQ Assessment finished.")
        return q_num > 0

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
    async def handle_start_confirmation_dialog(self, page) -> bool:
        """Clicks any confirmation modal dialog that appears after clicking Start Lab / Take Quiz."""
        try:
            for _ in range(4):
                clicked = await page.evaluate("""() => {
                    const dialogs = Array.from(document.querySelectorAll('.MuiDialog-root, .MuiModal-root, [role="dialog"], .modal'));
                    for (const d of dialogs) {
                        const cb = d.querySelector('input[type="checkbox"]');
                        if (cb && !cb.checked) {
                            cb.click();
                        }
                        const btns = Array.from(d.querySelectorAll('button, a, [role="button"]')).filter(b => {
                            const t = (b.innerText || '').toLowerCase();
                            return (t.includes('start') || t.includes('confirm') || t.includes('proceed') ||
                                    t.includes('yes') || t.includes('begin') || t.includes('agree') ||
                                    t.includes('continue') || t.includes('launch')) && !b.disabled;
                        });
                        if (btns.length > 0) {
                            btns[btns.length - 1].click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if clicked:
                    return True
                await asyncio.sleep(0.5)
        except Exception:
            pass
        return False

    async def evaluate_lab_test_cases(self, test_page) -> Dict[str, Any]:
        """
        AI Evaluation Engine: Inspects ByteXL Test Cases results table,
        status badges, terminal console, and summary metrics.
        Returns:
            {
                'evaluated': bool,
                'all_passed': bool,
                'passed_count': int,
                'failed_count': int,
                'total_count': int,
                'details': str
            }
        """
        try:
            return await test_page.evaluate("""() => {
                const bodyText = (document.body ? document.body.innerText : '');
                const bodyLower = bodyText.toLowerCase();

                // 1. Check table rows for individual test cases
                const rows = Array.from(document.querySelectorAll('tr, .MuiTableRow-root, div[class*="test-case" i], div[class*="testcase" i]'));
                let passedCount = 0;
                let failedCount = 0;
                let failedDetails = [];

                for (const r of rows) {
                    const txt = (r.innerText || '').trim();
                    const lower = txt.toLowerCase();
                    if (lower.includes('test case') || lower.includes('testcase')) {
                        if (lower.includes('passed') && !lower.includes('failed') && !lower.includes('wrong')) {
                            passedCount++;
                        } else if (lower.includes('failed') || lower.includes('wrong answer') || lower.includes('error')) {
                            failedCount++;
                            failedDetails.push(txt.replace(/\\s+/g, ' '));
                        }
                    }
                }

                // 2. Check test result badges / chips (.MuiChip-root, .status)
                const chips = Array.from(document.querySelectorAll('.MuiChip-root, [class*="status" i], [class*="badge" i]'));
                for (const c of chips) {
                    const t = (c.innerText || '').toLowerCase();
                    if (t.includes('passed') && !t.includes('failed')) passedCount = Math.max(passedCount, 1);
                    if (t.includes('failed') || t.includes('wrong answer')) failedCount = Math.max(failedCount, 1);
                }

                // 3. Check output console / terminal logs
                const consoleEl = document.querySelector('.test-results, .output-console, .console, .terminal, pre, code');
                const consoleText = consoleEl ? consoleEl.innerText.trim() : '';

                // 4. Fraction detection: e.g. 'Passed 3/3' or '3 / 3 test cases passed'
                const fracMatch = bodyText.match(/(?:passed\\s*:?\\s*)?(\\d+)\\s*\\/\\s*(\\d+)(?:\\s*test\\s*cases?\\s*passed)?/i);
                if (fracMatch) {
                    const p = parseInt(fracMatch[1]);
                    const tot = parseInt(fracMatch[2]);
                    if (tot > 0 && p === tot) {
                        return {
                            evaluated: true,
                            all_passed: true,
                            passed_count: p,
                            failed_count: 0,
                            total_count: tot,
                            details: 'All test cases passed (' + p + '/' + tot + ')'
                        };
                    }
                }

                const textHasFailed = bodyLower.includes('failed') || bodyLower.includes('wrong answer') ||
                                      bodyLower.includes('compilation error') || bodyLower.includes('syntax error');
                const textHasPassed = bodyLower.includes('passed') || bodyLower.includes('all test cases passed') ||
                                      bodyLower.includes('submission accepted') || bodyLower.includes('correct answer');

                const total = passedCount + failedCount;
                const allPassed = (passedCount > 0 && failedCount === 0) || (textHasPassed && !textHasFailed && !failedCount);

                return {
                    evaluated: total > 0 || textHasPassed || textHasFailed,
                    all_passed: allPassed,
                    passed_count: passedCount,
                    failed_count: failedCount,
                    total_count: total > 0 ? total : (allPassed ? 1 : 0),
                    details: failedDetails.join(' | ') || consoleText.slice(0, 500) || (allPassed ? 'Passed' : 'Pending/Failed')
                };
            }""")
        except Exception as e:
            return {'evaluated': False, 'all_passed': False, 'passed_count': 0, 'failed_count': 1, 'total_count': 1, 'details': str(e)}

    async def request_clean_code_from_gemini(self, problem_text: str, language: str, starter_code: str = "") -> str:
        """Reliably generates executable solution code from Gemini, with zero option letters allowed."""
        prompt = (
            f"You are an expert programmer writing {language} code for this challenge:\n\n"
            f"Problem Statement:\n{problem_text}\n\n"
        )
        if starter_code:
            prompt += f"Starter Code:\n```{language}\n{starter_code}\n```\n\n"
        prompt += (
            f"REQUIREMENTS:\n"
            f"- Output ONLY valid, executable {language} code inside triple backticks.\n"
            f"- DO NOT output single letters like A, B, C, D.\n"
            f"- DO NOT output explanations, introductory text, or markdown outside backticks."
        )

        resp = await self.ask_gemini(prompt)
        clean = resp
        if "```" in resp:
            m = re.search(r'```(?:[a-zA-Z0-9_\+#\-]*\n)?([\s\S]*?)```', resp)
            if m:
                clean = m.group(1).strip()
        lines = clean.split("\n")
        if lines and lines[0].strip().lower() in ['sql', 'postgresql', 'postgres', 'python', 'python3', 'java', 'cpp']:
            clean = "\n".join(lines[1:]).strip()

        # Clean markers
        clean = re.sub(r'(--|//|#|/\*)\s*start(?:\s*your)?\s*solution.*?\n', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'(--|//|#|/\*)\s*end(?:\s*your)?\s*solution.*', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'(--|//|#|/\*)\s*driver\s*code[\s\S]*$', '', clean, flags=re.IGNORECASE).strip()
        return clean

    async def solve_coding_lab(self, test_page) -> bool:
        print("\n[*] 🔬 Evaluating and Solving Coding Lab Challenge...")
        await test_page.bring_to_front()

        # Check total questions in sidebar (1 to 10)
        total_questions = await test_page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('h6, p, button, div')).filter(el => {
                const t = el.innerText ? el.innerText.trim() : '';
                return /^[0-9]+$/.test(t) && parseInt(t) >= 1 && parseInt(t) <= 50 && el.children.length === 0 && (el.className.includes('Typography') || (el.parentElement && el.parentElement.className.includes('MuiBox')));
            });
            return els.length > 0 ? els.length : 1;
        }""")
        print(f"[*] Total Lab Questions: {total_questions}")

        all_lab_passed = True

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
            current_prompt = ""
            if has_markers:
                current_prompt = (
                    f"Solve the following {language} coding / SQL challenge.\n\n"
                    f"Problem Statement:\n{problem_text}\n\n"
                    f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                    f"INSTRUCTIONS:\n"
                    f"- Write ONLY the code/query that belongs strictly between the start and end solution markers.\n"
                    f"- Do NOT repeat the marker lines ('-- start your solution', '-- end your solution') or Driver Code in your response.\n"
                    f"- Return ONLY the executable query or code inside triple backticks with no commentary."
                )
            else:
                current_prompt = (
                    f"Solve the following SQL query / programming problem in {language}.\n"
                    f"Return ONLY the executable query or code inside triple backticks with no comments, introductory or concluding text.\n\n"
                    f"Problem Statement:\n{problem_text}"
                )
                if initial_starter_code.strip():
                    current_prompt += f"\n\nIncomplete / Starter Code in Editor:\n```{language}\n{initial_starter_code}\n```\n"

            question_passed = False

            for attempt in range(1, 5):
                if attempt > 1:
                    print(f"[*] --- Attempt {attempt}/4 (AI Self-Correction) ---")
                else:
                    print(f"[*] --- Attempt {attempt}/4 ---")

                try:
                    shot_bytes = await test_page.screenshot(type="jpeg", quality=85)
                except Exception:
                    shot_bytes = None
                answer = await self.ask_gemini(current_prompt, screenshot_bytes=shot_bytes)

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

                # ANTI-DUMMY CODE GUARD: Never type dummy single letters like "A" or "AA" in a coding lab!
                if not clean_code or clean_code.strip() in ("A", "AA", "B", "C", "D") or len(clean_code.strip()) <= 3:
                    print("[WARN] Extracted code was single letter/invalid. Requesting clean executable code...")
                    clean_code = await self.request_clean_code_from_gemini(problem_text, language, initial_starter_code)

                if not clean_code or clean_code.strip() in ("A", "AA", "B", "C", "D") or len(clean_code.strip()) <= 3:
                    print("[WARN] Could not obtain valid code from Gemini this attempt. Retrying...")
                    continue

                try:
                    await test_page.bring_to_front()
                except Exception:
                    pass
                await asyncio.sleep(0.3)

                # Cut-Copy-Paste injection: directly set Monaco editor value instantly with full precision
                print(f"[*] 📋 Pasting solution into editor ({len(clean_code)} chars)...")
                synced = await test_page.evaluate("""({ code, hasMarkers }) => {
                    if (window.monaco && window.monaco.editor && window.monaco.editor.getEditors().length > 0) {
                        const ed = window.monaco.editor.getEditors()[0];
                        const val = ed.getValue();

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
                    } else {
                        const ta = document.querySelector('textarea:not(.monaco-editor textarea)');
                        if (ta) {
                            ta.value = code;
                            ta.dispatchEvent(new Event('input', { bubbles: true }));
                            ta.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    }
                    return false;
                }""", { "code": clean_code, "hasMarkers": has_markers })

                print("[OK] ✅ Code pasted into editor successfully!")

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

                # Poll and evaluate test case execution
                await asyncio.sleep(5)
                eval_res = None
                for _ in range(5):
                    eval_res = await self.evaluate_lab_test_cases(test_page)
                    if eval_res.get('evaluated'):
                        break
                    await asyncio.sleep(1.0)

                await self.take_screenshot(f"lab_q{q_idx}_attempt{attempt}")

                if eval_res and eval_res.get("all_passed"):
                    print(f"[OK] 🎉 Question {q_idx} EVALUATION PASSED ({eval_res['passed_count']}/{eval_res['total_count']})!")
                    question_passed = True
                    break
                else:
                    fail_info = eval_res.get('details', 'Test cases failed') if eval_res else 'Execution failed'
                    print(f"[!] ❌ Attempt {attempt} failed: {fail_info[:100]}. Self-correcting...")
                    if has_markers:
                        current_prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem Statement:\n{problem_text}\n\n"
                            f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                            f"Previous Code Typed:\n```{language}\n{clean_code}\n```\n\n"
                            f"Error Details:\n{fail_info}\n\n"
                            f"Fix the error. Return ONLY the corrected code to place between the start and end solution markers inside triple backticks with no commentary."
                        )
                    else:
                        current_prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem:\n{problem_text}\n\n"
                            f"Previous Code:\n```{language}\n{clean_code}\n```\n\n"
                            f"Error Details:\n{fail_info}\n\n"
                            f"Fix it. Return ONLY the corrected code inside triple backticks with no commentary."
                        )

            if not question_passed:
                all_lab_passed = False
                print(f"[WARN] ⚠️ Question {q_idx} did not pass all test cases after 4 attempts.")

        # Check for overarching Submit / Finish button on last challenge
        print("[*] Finished all challenges. Checking for Submit/Finish...")
        await test_page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, a'));
            const finish = buttons.find(b => {
                const txt = (b.innerText || '').toLowerCase();
                return (txt.includes('submit test') || txt.includes('finish') || txt.includes('end test') || txt.includes('submit assessment')) && !b.disabled;
            });
            if (finish) finish.click();
        }""")
        await asyncio.sleep(2)

        # Handle any confirmation popup/dialog
        await test_page.evaluate("""() => {
            const confirmBtns = Array.from(document.querySelectorAll('.MuiDialog-root button, .MuiModal-root button, [role="dialog"] button, button')).filter(b => {
                const txt = (b.innerText || '').toLowerCase();
                return (txt.includes('confirm') || txt.includes('yes') || txt.includes('end test') || txt.includes('submit')) && !b.disabled;
            });
            if (confirmBtns.length > 0) confirmBtns[confirmBtns.length - 1].click();
        }""")
        await asyncio.sleep(2)

        return all_lab_passed

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
        """Scrolls smoothly through theory portion, clicks Mark As Completed, and clicks Next >."""
        title = await page.evaluate("() => { const h = document.querySelector('h1, h2, h3, h4, .chapter-title'); return h ? h.innerText.trim() : 'Theory Topic'; }")

        # Safeguard: Verify this page is NOT actually a Quiz or Lab landing page before scrolling/marking complete!
        has_lab_btn = await page.evaluate("""() => {
            const startKws = ['start lab', 'open lab', 'resume lab', 'start challenge', 'solve challenge', 'take the quiz', 'start quiz', 'open quiz', 'start assessment'];
            return Array.from(document.querySelectorAll('button, a, [role="button"]')).some(b => {
                const t = (b.innerText || '').toLowerCase();
                return startKws.some(kw => t.includes(kw)) && !b.disabled;
            });
        }""")
        if has_lab_btn:
            print(f"[WARN] ⚠️ '{title}' contains an active Lab/Quiz launch button! Aborting reading mode to solve activity.")
            return False

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
                await new Promise(r => setTimeout(r, 350));
                window.scrollTo(0, y);
                window.dispatchEvent(new Event('scroll'));
                document.dispatchEvent(new Event('scroll'));
                await new Promise(r => setTimeout(r, 250));
            }
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            window.scrollTo(0, document.body.scrollHeight);
            window.dispatchEvent(new Event('scroll'));
            document.dispatchEvent(new Event('scroll'));
        }""")
        await asyncio.sleep(2.5)
        await asyncio.sleep(2)

        # 2. Click "Mark As Completed" button at bottom right (see user screenshot)
        marked = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            const markBtn = btns.find(b => {
                const txt = (b.innerText || '').trim().toLowerCase();
                return txt === 'mark as completed' || txt === 'mark as complete' || txt === 'mark complete';
            });
            if (markBtn && !markBtn.disabled) {
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

        # 3. Advance using 'Next >' button or next topic in sidebar
        advanced = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('button, a'));
            const nextBtn = links.find(l => {
                const txt = (l.innerText || '').trim().toLowerCase();
                return txt === 'next >' || txt === 'next' || txt.startsWith('next >') || txt.includes('next >');
            });
            if (nextBtn && !nextBtn.disabled) {
                nextBtn.click();
                return true;
            }
            return false;
        }""")

        if advanced:
            print("[*] Advancing to next topic via 'Next >'...")
            await asyncio.sleep(3)
            return True

        return False

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

        # Login Gatekeeper: Check if ByteXL is at login / signin screen
        is_login = await self.bytexl_page.evaluate("""() => {
            const url = window.location.href.toLowerCase();
            const hasRadios = !!document.querySelector('[role="radiogroup"], .MuiRadioGroup-root, [role="radio"]');
            const hasMonaco = !!document.querySelector('.monaco-editor, textarea#code');
            if (hasRadios || hasMonaco || url.includes('/courses/') || url.includes('/module/') || url.includes('/topic/') || url.includes('/test/') || url.includes('/assessment/')) {
                return false;
            }
            if (url.includes('/login') || url.includes('/signin')) return true;
            const hasPass = Array.from(document.querySelectorAll('input[type="password"]')).some(el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            });
            const hasSignIn = Array.from(document.querySelectorAll('button, input[type="submit"]')).some(b => {
                const t = (b.innerText || b.value || '').toLowerCase();
                return t.includes('sign in') || t.includes('log in') || t.includes('login');
            });
            return hasPass && hasSignIn;
        }""")

        if is_login:
            print("[⚠️] You are on the ByteXL login page! Please log in to ByteXL in the browser.")
            print("[*] The agent is paused and will automatically resume as soon as you log in...")
            while True:
                await asyncio.sleep(2)
                found_logged_in = None
                for p in list(self.context.pages):
                    try:
                        u = p.url.lower()
                        if "bytexl" in u:
                            if "/login" not in u and "/signin" not in u:
                                has_pass = await p.evaluate("() => !!document.querySelector('input[type=\"password\"]')")
                                if not has_pass:
                                    found_logged_in = p
                                    break
                    except Exception:
                        pass

                if found_logged_in:
                    self.bytexl_page = found_logged_in
                    print(f"[OK] ByteXL login detected in tab: {found_logged_in.url[:60]}! Proceeding to courses...")
                    await asyncio.sleep(2)
                    break

                try:
                    still_login = await self.bytexl_page.evaluate("""() => {
                        const url = window.location.href.toLowerCase();
                        if (url.includes('/login') || url.includes('/signin')) return true;
                        return !!document.querySelector('input[type="password"]');
                    }""")
                    if not still_login:
                        print("[OK] ByteXL login detected! Proceeding to courses...")
                        await asyncio.sleep(2)
                        break
                except Exception:
                    await asyncio.sleep(2)
                    print("[OK] ByteXL login redirect detected! Proceeding to courses...")
                    break

        # Check Gemini sign-in notice
        try:
            gemini_login = await self.gemini_page.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                return url.includes('accounts.google.com');
            }""")
            if gemini_login:
                print("[⚠️] Notice: Please ensure you are signed into Gemini in the Gemini tab.")
        except Exception:
            pass

        # Instant Check: Is an assessment or quiz tab ALREADY open in the browser?
        already_open_test = None
        TEST_URL_KEYS = ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/', '/code/']
        for p in self.context.pages:
            u = p.url.lower()
            if any(k in u for k in TEST_URL_KEYS):
                already_open_test = p
                break
        if not already_open_test and self.bytexl_page:
            u = self.bytexl_page.url.lower()
            if any(k in u for k in TEST_URL_KEYS):
                already_open_test = self.bytexl_page

        if already_open_test:
            print(f"[🎯] Active Quiz/Assessment already open in browser ({already_open_test.url[:60]}...). Solving directly!")
        elif self.target:
            # If target course/module is specified, check if already inside it
            curr_url = self.bytexl_page.url
            curr_title = await self.bytexl_page.title()
            page_snippet = await self.bytexl_page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 1000)")
            is_course_url = "/courses/" in curr_url or "/module/" in curr_url
            already_inside = is_course_url and brain.is_matching_page_course(self.target, f"{curr_url} {curr_title} {page_snippet}")

            if already_inside:
                print(f"[🎯] Already inside target course: '{self.target}'! Continuing directly...")
            else:
                print(f"[🎯] Target course specified: '{self.target}'. Resolving course in 'My Courses'...")
                if not curr_url.rstrip("/").endswith("/courses"):
                    await self.bytexl_page.goto("https://app.bytexl.ai/courses")
                    await asyncio.sleep(3)

                # Wait for cards to render
                for _ in range(6):
                    has_cards = await self.bytexl_page.evaluate("""() => {
                        const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                            const t = (c.innerText || '').toLowerCase();
                            return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                        });
                        return cards.length > 0;
                    }""")
                    if has_cards:
                        break
                    await asyncio.sleep(1.0)

                # Extract visible course cards
                cards_info = await self.bytexl_page.evaluate("""() => {
                    const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                        const t = (c.innerText || '').toLowerCase();
                        return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                    });
                    return cards.map((c, i) => {
                        const titleEl = c.querySelector('h3, h4, h5, h6, .MuiTypography-h6, .MuiTypography-h5');
                        return titleEl ? titleEl.innerText.trim() : c.innerText.split('\\n')[0].trim();
                    });
                }""")

                matched = brain.match_course(self.target, cards_info)
                if matched:
                    print(f"[OK] 🎯 Smart Match: Target '{self.target}' resolved to '{matched}'! Opening...")
                    clicked = await self.bytexl_page.evaluate("""(matchTitle) => {
                        const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root'));
                        const targetCard = cards.find(c => c.innerText.includes(matchTitle));
                        if (targetCard) {
                            const btn = Array.from(targetCard.querySelectorAll('button, a')).find(b => {
                                const txt = (b.innerText || '').toLowerCase();
                                return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                            }) || targetCard.querySelector('button, a') || targetCard;
                            btn.click();
                            return true;
                        }
                        return false;
                    }""", matched)
                    if clicked:
                        await asyncio.sleep(4)
                else:
                    # Try search bar with expanded search terms
                    search_terms = brain.get_search_terms(self.target)
                    if self.target not in search_terms:
                        search_terms.insert(0, self.target)

                    print(f"[*] Searching catalog for '{self.target}' using terms {search_terms[:3]}...")
                    try:
                        search_input = await self.bytexl_page.wait_for_selector('input[placeholder*="search" i]', timeout=3500)
                        if search_input:
                            found = False
                            for term in search_terms:
                                await search_input.fill("")
                                await search_input.fill(term)
                                await self.bytexl_page.keyboard.press("Enter")
                                await asyncio.sleep(1.8)

                                searched_cards = await self.bytexl_page.evaluate("""() => {
                                    const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                                        const t = (c.innerText || '').toLowerCase();
                                        return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                                    });
                                    return cards.map(c => {
                                        const titleEl = c.querySelector('h3, h4, h5, h6, .MuiTypography-h6, .MuiTypography-h5');
                                        return titleEl ? titleEl.innerText.trim() : c.innerText.split('\\n')[0].trim();
                                    });
                                }""")

                                matched_s = brain.match_course(self.target, searched_cards)
                                if matched_s:
                                    print(f"[OK] 🎯 Found course with term '{term}': '{matched_s}'! Entering...")
                                    await self.bytexl_page.evaluate("""(matchTitle) => {
                                        const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root'));
                                        const targetCard = cards.find(c => c.innerText.includes(matchTitle)) || cards[0];
                                        if (targetCard) {
                                            const btn = Array.from(targetCard.querySelectorAll('button, a')).find(b => {
                                                const txt = (b.innerText || '').toLowerCase();
                                                return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                                            }) || targetCard.querySelector('button, a') || targetCard;
                                            btn.click();
                                        }
                                    }""", matched_s)
                                    await asyncio.sleep(4)
                                    found = True
                                    break

                            if not found:
                                print(f"[!] Target '{self.target}' search yielded 0 results. Restoring full catalog...")
                                await search_input.fill("")
                                await self.bytexl_page.keyboard.press("Enter")
                                await asyncio.sleep(1.5)
                    except Exception:
                        pass

        TEST_KEYS = ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/', '/code/']
        while True:
            # Check open pages for any active Test / Quiz / Lab
            test_page = None
            for p in self.context.pages:
                if p.is_closed():
                    continue
                u = p.url.lower()
                if any(k in u for k in TEST_KEYS):
                    is_done = False
                    try:
                        is_done = await p.evaluate("""() => {
                            const bt = (document.body ? document.body.innerText : '').toLowerCase();
                            const hasRadios = !!document.querySelector('[role="radiogroup"], .MuiRadioGroup-root, [role="radio"]');
                            const hasMonaco = !!document.querySelector('.monaco-editor, textarea#code');
                            return (bt.includes('test submitted') || bt.includes('assessment submitted') ||
                                    bt.includes('assessment completed') || bt.includes('quiz completed')) &&
                                   !hasRadios && !hasMonaco;
                        }""")
                    except Exception:
                        pass
                    if not is_done:
                        test_page = p
                        break

            if not test_page and self.bytexl_page and not self.bytexl_page.is_closed():
                u = self.bytexl_page.url.lower()
                if any(k in u for k in TEST_KEYS):
                    test_page = self.bytexl_page

            if test_page:
                try:
                    await test_page.bring_to_front()
                except Exception:
                    pass

                # Environment Settle Wait: Wait up to 6 seconds for Monaco editor or MCQ radios to mount
                has_editor = False
                for _ in range(8):
                    has_editor = await test_page.evaluate("() => !!document.querySelector('.monaco-editor, textarea#code, .language-selector')")
                    if has_editor:
                        break
                    has_radios = await test_page.evaluate("() => !!document.querySelector('[role=\"radiogroup\"], .MuiRadioGroup-root, input[type=\"radio\"]')")
                    if has_radios:
                        has_editor = False
                        break
                    await asyncio.sleep(0.5)

                if has_editor:
                    eval_passed = await self.solve_coding_lab(test_page)
                else:
                    eval_passed = await self.solve_mcq_test(test_page)

                if eval_passed:
                    print(f"[OK] 🏆 Assessment '{self.current_activity_name or 'Activity'}' EVALUATED & VERIFIED PASSED!")
                    if self.current_activity_name:
                        self.completed_activities.add(self.current_activity_name)
                else:
                    print(f"[WARN] ⚠️ Assessment '{self.current_activity_name or 'Activity'}' did NOT pass all requirements. Leaving unticked for retry.")

                is_sep = (test_page != self.bytexl_page and len(self.context.pages) > 1)
                if is_sep and not test_page.is_closed():
                    print("[*] Closing completed assessment tab...")
                    try:
                        await test_page.close()
                    except Exception:
                        pass

                # Locate and focus the main course curriculum page
                main_course_page = None
                for p in self.context.pages:
                    if not p.is_closed():
                        u = p.url.lower()
                        if "bytexl" in u and not any(k in u for k in TEST_KEYS):
                            main_course_page = p
                            break
                if not main_course_page and self.bytexl_page and not self.bytexl_page.is_closed():
                    main_course_page = self.bytexl_page
                elif not main_course_page and self.context.pages:
                    main_course_page = self.context.pages[0]

                if main_course_page:
                    self.bytexl_page = main_course_page
                    try:
                        await self.bytexl_page.bring_to_front()
                    except Exception:
                        pass

                await asyncio.sleep(2)

                # Post-Activity: Tick the side completion box ONLY IF EVALUATED & PASSED!
                if eval_passed and self.current_activity_name:
                    await self.tick_activity_checkbox(self.current_activity_name)

                # Open next submodule: Expand any collapsed accordion sections
                await self.bytexl_page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button.MuiAccordionSummary-root[aria-expanded="false"], .MuiAccordionSummary-root:not(.Mui-expanded)'));
                    buttons.forEach(b => b.click());
                }""")
                await asyncio.sleep(1)
                continue

            # Check if on an Activity landing page with "Open Quiz..." or "Open Lab..." or "Take The Quiz Now!"
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass
            unit_action = await self.bytexl_page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                const startKeywords = [
                    'take the quiz', 'take quiz', 'start quiz', 'open quiz', 'attempt quiz',
                    'start lab', 'open lab', 'resume lab', 'launch lab',
                    'start assessment', 'take assessment', 'open assessment',
                    'start challenge', 'open challenge', 'solve challenge',
                    'start test', 'take test', 'resume test', 'resume quiz',
                    'continue quiz', 'continue lab', 'continue assessment',
                    'practice now', 'start practice'
                ];
                const openBtn = btns.find(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return startKeywords.some(kw => txt.includes(kw)) && !b.disabled;
                });
                const nextUnitBtn = btns.find(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return txt.includes('go to next unit') || txt.includes('next unit');
                });
                return {
                    openText: openBtn ? openBtn.innerText.trim() : null,
                    hasNextUnit: !!nextUnitBtn
                };
            }""")

            if unit_action.get("openText"):
                open_lower = unit_action['openText'].lower()
                is_quiz_activity = "quiz" in open_lower or "take" in open_lower

                # Check if this quiz/activity is already ticked or completed in sidebar
                check_status = await self.bytexl_page.evaluate("""(name) => {
                    const cleanName = (name || '').trim().toLowerCase();
                    const allItems = Array.from(document.querySelectorAll(
                        '.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, .MuiListItem-root, a, div[role="button"]'
                    ));

                    let row = null;
                    if (cleanName) {
                        row = allItems.find(el => {
                            const t = (el.innerText || '').trim().toLowerCase();
                            return t.includes(cleanName) || cleanName.includes(t.split('\\n')[0]);
                        });
                    }
                    if (!row) {
                        row = document.querySelector('.MuiListItem-root.Mui-selected, .Mui-selected, [aria-selected="true"]');
                    }

                    let isChecked = false;
                    if (row) {
                        const cb = row.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], [data-testid*="Check"]');
                        isChecked = row.innerHTML.includes('Mui-checked') ||
                                    row.querySelector('[data-testid*="Check"]') !== null ||
                                    row.getAttribute('aria-checked') === 'true' ||
                                    row.classList.contains('completed') ||
                                    (cb && (cb.checked || cb.getAttribute('aria-label') === 'Mark as incomplete'));
                    }

                    const bodyText = document.body.innerText.toLowerCase();
                    // Never consider completed just because 'highest score' is in text!
                    const hasCompletedResult = (bodyText.includes('state: finished') || bodyText.includes('submitted')) &&
                                               /(?:grade|score|marks)\\s*:\\s*\\d+/i.test(bodyText);

                    return {
                        isChecked: isChecked,
                        hasCompletedResult: hasCompletedResult
                    };
                }""", self.current_activity_name)

                # 1. If page shows completed results AND no active start button -> advance
                if check_status.get("hasCompletedResult") and not unit_action.get("openText"):
                    print(f"[OK] Activity '{self.current_activity_name or 'quiz'}' shows completed results. Advancing...")
                    if self.current_activity_name:
                        self.completed_activities.add(self.current_activity_name)
                    # Advance via Next button
                    advanced = await self.bytexl_page.evaluate("""() => {
                        const links = Array.from(document.querySelectorAll('button, a'));
                        const nextBtn = links.find(l => {
                            const txt = (l.innerText || '').trim().toLowerCase();
                            return txt === 'next >' || txt === 'next' || txt.includes('next >') || txt.includes('go to next unit');
                        });
                        if (nextBtn && !nextBtn.disabled) {
                            nextBtn.click();
                            return true;
                        }
                        return false;
                    }""")
                    await asyncio.sleep(2.5)
                    continue

                # 2. Assessment has active launch button -> launch and switch to it!
                print(f"[*] Assessment detected. Launching '{unit_action['openText']}'...")
                pages_before = set(self.context.pages)
                await self.bytexl_page.evaluate("""(txt) => {
                    const btns = Array.from(document.querySelectorAll('button, a'));
                    const target = btns.find(b => (b.innerText || '').trim().toLowerCase() === txt.toLowerCase()) ||
                                   document.querySelector('button.MuiButton-containedPrimary');
                    if (target) target.click();
                }""", unit_action['openText'])

                # Handle any pre-start modal / dialog confirmation immediately
                await asyncio.sleep(1.0)
                await self.handle_start_confirmation_dialog(self.bytexl_page)

                new_test = None
                for _ in range(12):
                    await asyncio.sleep(0.5)
                    diff = set(self.context.pages) - pages_before
                    if diff:
                        new_test = list(diff)[0]
                        break
                    u = self.bytexl_page.url.lower()
                    if any(k in u for k in TEST_KEYS):
                        new_test = self.bytexl_page
                        break

                if new_test and not new_test.is_closed():
                    try:
                        await new_test.bring_to_front()
                    except Exception:
                        pass
                    print(f"[OK] Switched focus to assessment tab: {new_test.url[:60]}")
                else:
                    await asyncio.sleep(2)
                continue

            # Check if on a Theory / Reading Page (contains Mark As Completed / Summarize / /topic/)
            is_theory_page = await self.bytexl_page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                const startKws = [
                    'start lab', 'open lab', 'resume lab', 'launch lab',
                    'start challenge', 'open challenge', 'solve challenge',
                    'take the quiz', 'start quiz', 'open quiz', 'start assessment',
                    'continue quiz', 'continue lab', 'continue assessment'
                ];
                const hasStartBtn = btns.some(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return startKws.some(kw => t.includes(kw)) && !b.disabled;
                });
                if (hasStartBtn) return false;

                const hasMarkBtn = !!btns.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return txt === 'mark as completed' || txt === 'mark as complete';
                });
                const hasSummarize = !!btns.find(b => (b.innerText || '').toLowerCase().includes('summarize this chapter'));
                const isTopicUrl = window.location.href.includes('/topic/');
                return hasMarkBtn || hasSummarize || isTopicUrl;
            }""")

            if is_theory_page:
                advanced = await self.handle_theory_reading(self.bytexl_page)
                if self.current_activity_name:
                    self.completed_activities.add(self.current_activity_name)
                if advanced:
                    continue

            elif unit_action.get("hasNextUnit") and len(self.completed_activities) > 0:
                print("[*] Advancing: 'Go To Next Unit'...")
                await self.bytexl_page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button, a')).find(b => (b.innerText || '').toLowerCase().includes('next unit'));
                    if (btn) btn.click();
                }""")
                await asyncio.sleep(3)
                continue

            current_url = self.bytexl_page.url.lower()

            # Check if on Course units list (Units and Chapters page: /courses/<id>/<slug>)
            if "/courses/" in current_url and "/module/" not in current_url:
                # Check if this page already contains course module accordions (e.g. System Design)
                has_curriculum = await self.bytexl_page.evaluate("""() => {
                    const accs = Array.from(document.querySelectorAll('.MuiAccordion-root'));
                    return accs.some(a => {
                        const t = (a.innerText || '').toLowerCase();
                        return t.includes('reading material') || t.includes('challenge') || t.includes('chapter') || t.includes('quiz');
                    });
                }""")

                if has_curriculum:
                    print("[*] 🎯 Module curriculum detected directly on course page! Proceeding to topics...")
                else:
                    print("\n[*] On Units & Chapters page. Finding uncompleted unit...")
                    unit_res = await self.bytexl_page.evaluate("""() => {
                        const cards = Array.from(document.querySelectorAll('.MuiPaper-root, .MuiCard-root, div[class*="UnitCard"], div[class*="MuiBox-root"]')).filter(c => {
                            const t = (c.innerText || '').toLowerCase();
                            return (t.includes('chapter') || t.includes('challenge')) && !t.includes('units and chapters') && !t.includes('how is my course graded');
                        });

                        // 1. Look for a unit card with an explicit action button
                        for (const card of cards) {
                            const btn = Array.from(card.querySelectorAll('button, a, [role="button"], div[class*="button" i]')).find(b => {
                                const txt = (b.innerText || '').trim().toLowerCase();
                                return (txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) && !b.disabled;
                            });
                            if (btn) {
                                const lines = card.innerText.split(/\\r?\\n/).map(l => l.trim()).filter(Boolean);
                                const title = lines.find(l => !l.toLowerCase().includes('learning') && !l.toLowerCase().includes('completed') && !l.toLowerCase().includes('chapter') && !l.toLowerCase().includes('challenge')) || 'Unit';
                                btn.scrollIntoView({ behavior: 'auto', block: 'center' });
                                btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                                btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                btn.click();
                                return { clicked: true, title: title, all_completed: false };
                            }
                        }

                        // 2. Look for an uncompleted unit card (one without "Completed")
                        const uncompletedCard = cards.find(card => {
                            const txt = (card.innerText || '').toLowerCase();
                            return !txt.includes('completed');
                        });

                        if (uncompletedCard) {
                            const lines = uncompletedCard.innerText.split(/\\r?\\n/).map(l => l.trim()).filter(Boolean);
                            const title = lines.find(l => !l.toLowerCase().includes('completed') && !l.toLowerCase().includes('chapter') && !l.toLowerCase().includes('challenge')) || 'Unit';
                            const clickTarget = uncompletedCard.querySelector('button, a, [role="button"], h3, h4, h5, h6, .MuiTypography-root') || uncompletedCard;
                            clickTarget.scrollIntoView({ behavior: 'auto', block: 'center' });
                            clickTarget.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            clickTarget.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            clickTarget.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            clickTarget.click();
                            return { clicked: true, title: title, all_completed: false };
                        }

                        const allDone = cards.length > 0 && cards.every(c => (c.innerText || '').toLowerCase().includes('completed'));
                        return { clicked: false, all_completed: allDone };
                    }""")

                    if unit_res.get("clicked"):
                        print(f"[OK] Selected Unit: {unit_res.get('title')}")
                        await asyncio.sleep(4)
                        try:
                            await self.bytexl_page.wait_for_url(lambda u: "/module/" in u.lower(), timeout=8000)
                            print(f"[OK] Entered Module view: '{unit_res.get('title')}'!")
                        except Exception:
                            await asyncio.sleep(4)
                        continue
                    elif unit_res.get("all_completed"):
                        print("[OK] 🎉 All units on Units & Chapters page are 100% completed! Returning to My Courses...")
                        await self.bytexl_page.goto("https://app.bytexl.ai/courses")
                        await asyncio.sleep(3)
                        continue

            # Check if on Module view / curriculum page
            if "/courses/" in current_url or "/module/" in current_url:
                print("\n[*] On Module view. Expanding all submodules and scanning for uncompleted Topics, Quizzes and Labs...")
                # Expand all collapsed submodule accordions
                await self.bytexl_page.evaluate("""() => {
                    const summaries = Array.from(document.querySelectorAll('.MuiAccordionSummary-root[aria-expanded="false"], .MuiAccordionSummary-root:not(.Mui-expanded)'));
                    summaries.forEach(s => s.click());
                }""")
                await asyncio.sleep(1.5)

                # Scan across all submodules for uncompleted topic (Reading or Quiz/Lab)
                uncompleted_item = await self.bytexl_page.evaluate("""(doneList) => {
                    const candidateRows = Array.from(document.querySelectorAll(
                        '.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, .MuiAccordionDetails-root a, .MuiAccordionDetails-root .MuiListItem-root, .MuiAccordionDetails-root > div, a.MuiListItem-root, .MuiListItem-root'
                    )).filter(el => {
                        const t = (el.innerText || '').trim();
                        return t.length > 2 && t.length < 160 && !t.toLowerCase().includes('units and chapters') && !t.toLowerCase().includes('how is my course graded');
                    });

                    for (const row of candidateRows) {
                        const rawText = row.innerText.trim();
                        const titleLine = rawText.split('\\n')[0].trim();

                        // Skip if already in doneList
                        if (doneList.some(d => titleLine.includes(d) || (d && d.includes(titleLine)))) continue;

                        const accordion = row.closest('.MuiAccordion-root');
                        if (accordion) {
                            const accSummary = accordion.querySelector('.MuiAccordionSummary-root');
                            if (accSummary) {
                                const st = (accSummary.innerText || '').toLowerCase();
                                const frac = st.match(/(\\d+)\\s*(?:\\/|of)\\s*(\\d+)\\s*completed/);
                                if (frac && parseInt(frac[1]) < parseInt(frac[2])) {
                                    // Submodule has uncompleted items (e.g. 0/3 or 1/3) -> Do not skip!
                                } else if (frac && parseInt(frac[1]) === parseInt(frac[2]) && parseInt(frac[2]) > 0) {
                                    continue;
                                }
                                const pct = st.match(/(\\d+)%\\s*completed/);
                                if (pct && parseInt(pct[1]) < 100) {
                                    // Submodule is partially complete -> Do not skip!
                                } else if (pct && parseInt(pct[1]) === 100) {
                                    continue;
                                }
                            }
                        }

                        // Check if checked
                        const cb = row.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], span[aria-label*="complete"]');
                        const hasBlankIcon = row.querySelector('[data-testid="CheckBoxOutlineBlankIcon"]') !== null;
                        if (hasBlankIcon) {
                            return {
                                text: titleLine,
                                href: row.href || (row.querySelector('a') ? row.querySelector('a').href : null)
                            };
                        }

                        const isChecked = (
                            row.innerHTML.includes('Mui-checked') ||
                            row.querySelector('[data-testid="CheckBoxIcon"], [data-testid="CheckCircleIcon"]') !== null ||
                            row.getAttribute('aria-checked') === 'true' ||
                            row.classList.contains('completed') ||
                            (cb && (cb.checked || cb.getAttribute('aria-label') === 'Mark as incomplete'))
                        );

                        if (!isChecked) {
                            return {
                                text: titleLine,
                                href: row.href || (row.querySelector('a') ? row.querySelector('a').href : null)
                            };
                        }
                    }
                    return null;
                }""", list(self.completed_activities))

                if uncompleted_item:
                    self.current_activity_name = uncompleted_item['text']
                    print(f"[OK] Found Uncompleted Submodule Topic: {self.current_activity_name}")
                    clicked = await self.bytexl_page.evaluate("""(name) => {
                        const links = Array.from(document.querySelectorAll('.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, a, .MuiListItem-root, [role="button"]'));
                        const target = links.find(l => (l.innerText || '').includes(name));
                        if (target) {
                            const linkText = target.querySelector('.MuiListItemText-root') || target;
                            linkText.scrollIntoView({ behavior: 'auto', block: 'center' });
                            linkText.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            linkText.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            linkText.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            linkText.click();
                            return true;
                        }
                        return false;
                    }""", self.current_activity_name)
                    if not clicked and uncompleted_item.get('href'):
                        await self.bytexl_page.goto(uncompleted_item['href'])
                    await asyncio.sleep(4)
                    continue
                else:
                    # Check for "Go To Next Unit" before returning to dashboard
                    next_unit = await self.bytexl_page.evaluate("""() => {
                        const btn = Array.from(document.querySelectorAll('button, a')).find(b => {
                            const txt = (b.innerText || '').toLowerCase();
                            return txt.includes('go to next unit') || txt.includes('next unit');
                        });
                        if (btn) {
                            btn.click();
                            return true;
                        }
                        return false;
                    }""")
                    if next_unit:
                        print("[OK] All submodules in this unit completed! Advancing to Next Unit...")
                        await asyncio.sleep(4)
                        continue

                    print("[OK] All Topics, Quizzes and Labs in this module are completed! Returning to My Courses...")
                    await self.bytexl_page.goto("https://app.bytexl.ai/courses")
                    await asyncio.sleep(4)
                    if len(self.completed_activities) > 0:
                        print("[OK] All Topics, Quizzes and Labs in this module are completed! Returning to My Courses...")
                        await self.bytexl_page.goto("https://app.bytexl.ai/courses")
                        await asyncio.sleep(4)
                    else:
                        print("[*] No uncompleted items found on this screen. Checking course status...")
                        await asyncio.sleep(3)
                    continue

            # Check if on My Courses page (`/courses`)
            if current_url.rstrip("/").endswith("/courses"):
                print("\n[*] On My Courses dashboard. Checking completion percentages...")
                target_course = await self.bytexl_page.evaluate("""() => {
                    // Find course cards
                    const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                        return c.innerText && (c.innerText.includes('COMPLETION') || c.innerText.includes('Continue learning') || c.innerText.includes('Start learning'));
                    });

                    for (const card of cards) {
                        const txt = card.innerText;
                        const match = txt.match(/COMPLETION[\\s\\S]*?(\\d+)%/i) || txt.match(/(\\d+)%[\\s\\S]*?COMPLETION/i);
                        const pct = match ? parseInt(match[1]) : 0;

                        // Target courses with completion < 100%
                        if (pct < 100) {
                            const titleEl = card.querySelector('h3, h4, h5, h6, .MuiTypography-h6, .MuiTypography-h5');
                            const btn = card.querySelector('button, a');
                            return {
                                title: titleEl ? titleEl.innerText.trim() : "Unknown Course",
                                percentage: pct,
                                btnText: btn ? btn.innerText.trim() : null
                            };
                        }
                    }
                    return null;
                }""")

                if target_course:
                    print(f"[OK] Selecting Course: {target_course['title']} ({target_course['percentage']}% completed)")
                    await self.bytexl_page.evaluate("""(title) => {
                        const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root'));
                        for (const card of cards) {
                            if (card.innerText.includes(title)) {
                                const btn = card.querySelector('button, a');
                                if (btn) btn.click();
                                break;
                            }
                        }
                    }""", target_course['title'])
                    await asyncio.sleep(4)
                    continue
                else:
                    print("[OK] All courses are 100% completed! Congratulations!")
                    break
                    has_cards = await self.bytexl_page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).some(c => {
                            return c.innerText && (c.innerText.includes('COMPLETION') || c.innerText.includes('Continue learning') || c.innerText.includes('Start learning'));
                        });
                    }""")
                    if has_cards:
                        print("[OK] All courses are 100% completed! Congratulations!")
                        break
                    else:
                        print("[!] No course cards detected on /courses page yet. Retrying...")
                        await asyncio.sleep(3)
                        continue

            print("[*] Monitoring navigation state. Sleeping 3s...")
            await asyncio.sleep(3)

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
