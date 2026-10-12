import asyncio
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from flask import Flask, render_template, request, jsonify, send_file
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
    # If user or friend is logged into ByteXL in Chrome/Edge/Brave, that browser is running!
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
    pref = preferred or (controller.preferred_browser if 'controller' in globals() else "auto")
    exe = find_installed_browser(pref)
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
        os.startfile(exe, arguments=flag_str)
        return True
    except Exception:
        pass
    try:
        DETACHED_FLAGS = 0x00000008 | 0x00000200
        subprocess.Popen([exe] + flags, creationflags=DETACHED_FLAGS, close_fds=True)
        return True
    except Exception:
        subprocess.Popen(f'start "" "{exe}" {flag_str}', shell=True)
        return True

# Clean up obsolete Desktop shortcut if it exists
try:
    _dt = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    _obs_lnk = os.path.join(_dt, "Brave (ByteXL Agent Ready).lnk")
    if os.path.exists(_obs_lnk):
        os.remove(_obs_lnk)
except Exception:
    pass

app = Flask(__name__, template_folder="templates")

class Controller:
    def __init__(self):
        self.state = "Agent Ready"
        self.logs = []
        self.pending_report = None
        self.stop_requested = False
        self.running_thread = None
        self.latest_screenshot = None
        self.current_module_name = ""
        self.target_module = ""
        self.cdp_port = 9222
        self.port_error = False
        self.last_detected_browser = None
        self.preferred_browser = "auto"

    def log(self, text, level="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = {"text": f"[{timestamp}] {text}", "level": level}
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        print(entry["text"])

controller = Controller()

class AutonomousByteXLAgent:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.playwright = None
        self.browser = None
        self.context = None
        self.bytexl_page = None
        self.gemini_page = None

    def ensure_browser_running(self):
        pref = getattr(self.ctrl, "preferred_browser", "auto")
        target_port = self.ctrl.cdp_port or 9222
        ok, info = check_cdp_port(target_port, timeout=1.0)
        
        # If target port is active, check if it matches requested preferred browser
        if ok and info:
            b_name = info.get("browser", "Chromium")
            if pref == "auto" or pref.lower() in b_name.lower():
                self.ctrl.last_detected_browser = b_name
                self.ctrl.port_error = False
                if info.get("has_bytexl"):
                    self.ctrl.log(f"🎯 Found active ByteXL session in {b_name} on port {target_port}!", "success")
                else:
                    self.ctrl.log(f"Connected to {b_name} on port {target_port}.", "info")
                return True
            else:
                self.ctrl.log(f"Port {target_port} currently has {b_name}, looking for {pref.upper()}...", "info")

        # Scan other ports (9222 - 9235) prioritizing preferred browser
        active_port, active_browser = find_active_browser_port(9222, 9235, preferred=pref)
        if active_port:
            if pref == "auto" or pref.lower() in active_browser.lower():
                self.ctrl.log(f"Detected {active_browser} on port {active_port}! Switching to port {active_port}...", "success")
                self.ctrl.cdp_port = active_port
                self.ctrl.last_detected_browser = active_browser
                self.ctrl.port_error = False
                return True

        # If target_port is occupied by a different browser, pick a free port for requested browser
        if ok and info and pref != "auto" and pref.lower() not in info.get("browser", "").lower():
            target_port = find_free_port(9223)
            self.ctrl.cdp_port = target_port
            self.ctrl.log(f"Port 9222 is in use by another browser. Using port {target_port} for {pref.capitalize()}...", "info")

        # Directly use installed browser (Brave, Chrome, or Edge)
        exe = find_installed_browser(pref)
        proc_name = os.path.basename(exe) if exe else "chrome.exe"
        b_label = proc_name.replace(".exe", "").capitalize()

        # If browser process is already running without remote debugging port, gracefully restart it
        # Chromium rejects --remote-debugging-port on a running instance unless restarted with session restore
        if is_browser_process_running(proc_name):
            self.ctrl.log(f"🔄 Detected {b_label} already open without remote debugging. Seamlessly enabling debugging (restoring all tabs and ByteXL session)...", "info")
            restart_browser_with_debugging(exe, target_port)
        else:
            self.ctrl.log(f"🚀 Starting {b_label} with remote debugging on port {target_port} (all logins preserved)...", "info")
            launch_browser_on_port(target_port, pref, use_default_profile=True)

        self.ctrl.log(f"Waiting for {b_label} on port {target_port}...", "info")
        for i in range(12):
            time.sleep(1)
            ok, info = check_cdp_port(target_port, timeout=1.0)
            if ok and info:
                b_name = info.get("browser", "Chromium")
                self.ctrl.last_detected_browser = b_name
                self.ctrl.port_error = False
                self.ctrl.log(f"Browser ({b_name}) ready on port {target_port}!", "success")
                return True

        self.ctrl.log(f"Browser did not respond on port {target_port} within 12 seconds.", "warn")
        return False

    async def connect(self):
        ready = self.ensure_browser_running()
        if not ready:
            self.ctrl.port_error = True
            self.ctrl.state = f"Port {self.ctrl.cdp_port} Failed"
            return False

        self.playwright = await async_playwright().start()

        target_port = self.ctrl.cdp_port or 9222
        cdp_url = f"http://127.0.0.1:{target_port}"

        for _ in range(4):
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url, timeout=5000)
                self.ctrl.port_error = False
                break
            except Exception:
                await asyncio.sleep(1.0)

        # Fallback: scan if browser opened on another port (prioritizing preferred browser & ByteXL)
        pref = getattr(self.ctrl, "preferred_browser", "auto")
        if not self.browser:
            active_p, active_b = find_active_browser_port(9222, 9235, preferred=pref)
            if active_p and active_p != target_port:
                self.ctrl.log(f"Found active browser ({active_b}) on port {active_p}. Auto-connecting...", "info")
                try:
                    self.browser = await self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{active_p}", timeout=5000)
                    self.ctrl.cdp_port = active_p
                    self.ctrl.last_detected_browser = active_b
                    self.ctrl.port_error = False
                except Exception:
                    pass

        if not self.browser:
            self.ctrl.port_error = True
            self.ctrl.state = f"Port {self.ctrl.cdp_port} Failed"
            self.ctrl.log(f"Failed to connect to browser on port {self.ctrl.cdp_port}.", "warn")
            self.ctrl.log(f"Opened port configuration popup in UI to proceed with other ports.", "info")
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
                self.ctrl.log(f"Connected to open ByteXL tab: {p.url[:65]}...", "success")
            elif ("gemini" in url_lower or "gemini" in title_lower) and not self.gemini_page:
                self.gemini_page = p
                self.ctrl.log(f"Connected to open Gemini tab: {p.url[:65]}...", "success")

        # If ByteXL is not open, reuse an error/blank tab or open a new one
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
                self.ctrl.log("Navigating tab to ByteXL (https://app.bytexl.ai/courses)...", "info")
                await self.bytexl_page.goto("https://app.bytexl.ai/courses")
            else:
                self.ctrl.log("Opening ByteXL (https://app.bytexl.ai/courses)...", "info")
                self.bytexl_page = await self.context.new_page()
                await self.bytexl_page.goto("https://app.bytexl.ai/courses")
            await asyncio.sleep(2)

        # If Gemini API key is configured, no browser tab is needed!
        if brain.has_key():
            self.ctrl.log(f"⚡ Brain AI Active: Connected to Google Gemini API ({brain.active_model}) - 0 extra tabs needed!", "success")
        else:
            # Fallback: Open Gemini web tab if no API key configured
            if not self.gemini_page:
                self.ctrl.log("Opening Google Gemini (https://gemini.google.com/app)...", "info")
                self.ctrl.log("💡 Tip: Enter your Google Free API Key in Settings to enable 10x faster solving & vision!", "info")
                self.gemini_page = await self.context.new_page()
                await self.gemini_page.goto("https://gemini.google.com/app")
                await asyncio.sleep(2)

        try:
            await self.bytexl_page.evaluate(PAGE_VISIBILITY_SHIM)
            if self.gemini_page:
                await self.gemini_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        self.ctrl.log(f"ByteXL: {self.bytexl_page.url}", "success")
        if self.gemini_page:
            self.ctrl.log(f"Gemini: {self.gemini_page.url}", "success")
        return True

    async def take_screenshot(self, label: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{label}_{timestamp}.png"
        filepath = os.path.join(SCREENSHOTS_DIR, filename)

        active = self.bytexl_page
        for p in self.context.pages:
            if "/test/" in p.url:
                active = p
                break

        if active:
            try:
                await active.bring_to_front()
                await active.screenshot(path=filepath, timeout=5000)
                self.ctrl.latest_screenshot = filepath
            except Exception:
                pass

        # Rolling cleanup to preserve disk space (keep only latest 30)
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
            self.ctrl.log("🌐 Opening Google Gemini tab (https://gemini.google.com/app)...", "info")
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
                self.ctrl.log(f"⚠️ Could not open Gemini tab: {e}", "warn")
        return None

    async def ask_gemini(self, prompt: str, screenshot_bytes: Optional[bytes] = None) -> str:
        is_coding = any(k in prompt.lower() for k in ["coding", "sql", "select", "challenge", "incomplete code", "def ", "function", "monaco", "query", "code/query"])

        # 1. Direct Brain API (fastest, 1-2s)
        if brain.has_key():
            self.ctrl.log(f"⚡ Brain AI Query ({len(prompt)} chars, model={brain.active_model})...", "info")
            try:
                if screenshot_bytes:
                    ans = brain.ask_vision(prompt, screenshot_bytes)
                else:
                    ans = brain.ask_text(prompt)
                if ans and len(ans.strip()) > 0:
                    self.ctrl.log(f"⚡ Brain replied: {ans[:100]}...", "success")
                    return ans
            except Exception as e:
                self.ctrl.log(f"⚠️ Gemini API: {e}. Switching to Gemini browser tab to cut/copy/paste...", "warn")

        # 2. Browser Tab Cut-Copy-Paste: Switch to Gemini tab
        gem_page = await self.ensure_gemini_page()
        if not gem_page:
            self.ctrl.log("❌ No Gemini tab available.", "warn")
            if is_coding:
                return "SELECT 1;"
            return "A"

        self.ctrl.log(f"📋 Switching to Gemini tab to prompt ({len(prompt)} chars)...", "info")
        try:
            await gem_page.bring_to_front()
            await asyncio.sleep(0.5)

            existing = await gem_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
            baseline_count = len(existing)

            # Locate input field
            input_elem = await gem_page.wait_for_selector('rich-textarea [contenteditable="true"], div[role="textbox"], textarea', timeout=12000)
            await input_elem.click()

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

            # Wait for response
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

            self.ctrl.log(f"📋 Copied response from Gemini ({len(answer)} chars): {answer[:100]}...", "success")
            return answer
        except Exception as e:
            self.ctrl.log(f"⚠️ Gemini tab interaction error: {e}", "warn")
            if is_coding:
                return "SELECT 1;"
            return "A"

    async def solve_mcq(self, test_page):
        self.ctrl.log("MCQ Quiz opened. Inspecting questions one by one...", "info")
        try:
            await test_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        q_num = 0
        while q_num < 35 and not self.ctrl.stop_requested:
            q_num += 1
            try:
                await test_page.bring_to_front()
            except Exception:
                pass
            await asyncio.sleep(1)

            mcq_data = await test_page.evaluate("""() => {
                const radioGroup = document.querySelector('[role="radiogroup"], .MuiRadioGroup-root');
                if (!radioGroup) return null;

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
                self.ctrl.log("No active MCQ question found or quiz completed.", "info")
                break

            q_text = mcq_data["question"]
            options = mcq_data["options"]
            is_answered = mcq_data.get("isAlreadyAnswered")

            if is_answered:
                self.ctrl.log(f"MCQ Q{q_num}: Already answered/completed. Verifying next question...", "info")
            else:
                self.ctrl.log(f"MCQ Q{q_num} (Unanswered): {q_text[:70]}...", "info")

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
                    exp = sol.get("explanation", "")
                    self.ctrl.log(f"⚡ Brain Solution for Q{q_num}: Option {selected_letter} - {exp[:80]}", "success")
                else:
                    prompt = f"Question:\n{q_text}\n\nOptions:\n" + "\n".join(formatted_opts) + "\n\nReply with only the correct option letter (example: B) and a short reason."
                    reply = await self.ask_gemini(prompt)
                    match = re.search(r'\b([A-D])\b', reply)
                    selected_letter = match.group(1) if match else "A"
                    opt_idx = ord(selected_letter) - ord('A')
                    self.ctrl.log(f"Selecting Option {selected_letter} for Q{q_num}", "info")

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
                self.ctrl.log(f"Answer submitted for Q{q_num}. Advanced to next question.", "info")
                await asyncio.sleep(1.5)
                continue

            elif action == "SUBMIT_CLICKED":
                self.ctrl.log("Final question reached. Submitting assessment...", "info")
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
                    self.ctrl.log(f"Answer submitted for Q{q_num}. Advanced to next question.", "info")
                    await asyncio.sleep(1.5)
                    continue
                elif retry_action == "SUBMIT_CLICKED":
                    self.ctrl.log("Assessment completed. Submitted successfully!", "success")
                    await asyncio.sleep(2)
                    break
                else:
                    self.ctrl.log(f"Assessment concluded at question {q_num}.", "info")
                    break

        self.ctrl.log("MCQ Quiz finished successfully.", "success")
        await asyncio.sleep(2)

        # Close the test tab if opened separately
        TEST_CLOSE_KEYS = ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/']
        for p in list(self.context.pages):
            try:
                u = p.url.lower()
                if any(k in u for k in TEST_CLOSE_KEYS) and p != self.bytexl_page:
                    await p.close()
            except Exception:
                pass

        if self.bytexl_page and not self.bytexl_page.is_closed():
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass
            await asyncio.sleep(1.5)

            # Advance if on landing page
            await self.bytexl_page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button, a')).find(b => (b.innerText || '').toLowerCase().includes('next unit'));
                if (btn) btn.click();
            }""")
            await asyncio.sleep(1.5)
        self.ctrl.log("MCQ Quiz finished and tab closed.", "success")
        return q_num > 0

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

    async def solve_coding(self, test_page) -> bool:
        self.ctrl.log("🔬 Evaluating and Solving Coding / SQL Lab Challenge...", "info")
        await test_page.bring_to_front()

        total_questions = await test_page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('h6, p, button, div')).filter(el => {
                const t = el.innerText ? el.innerText.trim() : '';
                return /^[0-9]+$/.test(t) && parseInt(t) >= 1 && parseInt(t) <= 50 && el.children.length === 0 && (el.className.includes('Typography') || (el.parentElement && el.parentElement.className.includes('MuiBox')));
            });
            return els.length > 0 ? els.length : 1;
        }""")
        self.ctrl.log(f"Lab contains {total_questions} challenges to evaluate & solve.", "info")

        all_lab_passed = True

        for q_idx in range(1, total_questions + 1):
            if self.ctrl.stop_requested:
                return False

            self.ctrl.log(f"📋 Lab Challenge {q_idx}/{total_questions}", "info")

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

            # Check if editor has solution delimiter markers
            has_markers = bool(
                re.search(r'(--|//|#|/\*)\s*start\s*(?:your\s*)?solution', initial_starter_code, re.IGNORECASE) and
                re.search(r'(--|//|#|/\*)\s*end\s*(?:your\s*)?solution', initial_starter_code, re.IGNORECASE)
            )

            current_prompt = ""
            if has_markers:
                current_prompt = (
                    f"Solve the following {language} coding / SQL challenge.\n\n"
                    f"Problem Statement:\n{problem_text}\n\n"
                    f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                    f"INSTRUCTIONS:\n"
                    f"- Write ONLY the code/query that goes strictly between the start and end solution markers.\n"
                    f"- Do NOT repeat the marker lines or Driver Code in your response.\n"
                    f"- Return ONLY the executable query or code inside triple backticks with no commentary."
                )
            else:
                current_prompt = (
                    f"Solve the following coding / SQL challenge in {language}.\n"
                    f"Return ONLY the executable query or code inside triple backticks with no introductory or concluding text.\n\n"
                    f"Problem Statement:\n{problem_text}"
                )
                if initial_starter_code.strip():
                    current_prompt += f"\n\nIncomplete / Starter Code in Editor:\n```{language}\n{initial_starter_code}\n```\n"

            question_passed = False

            for attempt in range(1, 5):
                if attempt > 1:
                    self.ctrl.log(f"Attempt {attempt}/4: AI Self-Correction loop with error feedback...", "warn")
                else:
                    self.ctrl.log(f"Attempt {attempt}/4: Querying Gemini for optimal solution...", "info")

                try:
                    shot_bytes = await test_page.screenshot(type="jpeg", quality=85)
                except Exception:
                    shot_bytes = None

                answer = await self.ask_gemini(current_prompt, screenshot_bytes=shot_bytes)

                clean_code = answer
                if "```" in answer:
                    match = re.search(r'```(?:[a-zA-Z0-9_\+#\-]*\n)?([\s\S]*?)```', answer)
                    if match:
                        clean_code = match.group(1).strip()

                lines = clean_code.split("\n")
                if lines and lines[0].strip().lower() in ['sql', 'postgresql', 'postgres', 'python', 'python3', 'java', 'cpp']:
                    clean_code = "\n".join(lines[1:]).strip()

                # Clean out any echoes of start/end solution or driver code
                clean_code = re.sub(r'(--|//|#|/\*)\s*start(?:\s*your)?\s*solution.*?\n', '', clean_code, flags=re.IGNORECASE)
                clean_code = re.sub(r'(--|//|#|/\*)\s*end(?:\s*your)?\s*solution.*', '', clean_code, flags=re.IGNORECASE)
                clean_code = re.sub(r'(--|//|#|/\*)\s*driver\s*code[\s\S]*$', '', clean_code, flags=re.IGNORECASE).strip()

                # ANTI-DUMMY CODE GUARD: Never inject dummy letters ("A", "AA", etc.) into code editor
                if not clean_code or clean_code.strip() in ("A", "AA", "B", "C", "D") or len(clean_code.strip()) <= 3:
                    self.ctrl.log("⚠️ Extracted code was invalid/single-letter. Requesting clean executable code...", "warn")
                    clean_code = await self.request_clean_code_from_gemini(problem_text, language, initial_starter_code)

                # Final guard: if still invalid, skip to avoid destroying the editor
                if not clean_code or clean_code.strip() in ("A", "AA", "B", "C", "D") or len(clean_code.strip()) <= 3:
                    self.ctrl.log("⚠️ Could not obtain valid code from Gemini this attempt. Retrying...", "warn")
                    continue

                try:
                    await test_page.bring_to_front()
                except Exception:
                    pass
                await asyncio.sleep(0.3)

                # Cut-Copy-Paste injection: directly set Monaco editor value instantly with full precision
                self.ctrl.log(f"📋 Injecting verified solution into editor ({len(clean_code)} chars)...", "info")
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

                self.ctrl.log("✅ Code injected into editor. Submitting for evaluation...", "info")

                await asyncio.sleep(1)
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
                    self.ctrl.log(f"🎉 Evaluation PASSED: Challenge {q_idx} passed all test cases ({eval_res['passed_count']}/{eval_res['total_count']})!", "success")
                    question_passed = True
                    break
                else:
                    fail_info = eval_res.get('details', 'Test cases failed') if eval_res else 'Execution failed'
                    self.ctrl.log(f"❌ Evaluation FAILED (Attempt {attempt}/4): {fail_info[:120]}. Self-correcting...", "warn")
                    # Construct self-correcting prompt with precise error feedback
                    if has_markers:
                        current_prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem Statement:\n{problem_text}\n\n"
                            f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                            f"Previous Code Typed:\n```{language}\n{clean_code}\n```\n\n"
                            f"Test Failure Details:\n{fail_info}\n\n"
                            f"Fix the error. Return ONLY the corrected code to place between the start and end solution markers inside triple backticks with no commentary."
                        )
                    else:
                        current_prompt = (
                            f"The previous solution in {language} failed test cases.\n\n"
                            f"Problem:\n{problem_text}\n\n"
                            f"Previous Code:\n```{language}\n{clean_code}\n```\n\n"
                            f"Test Failure Details:\n{fail_info}\n\n"
                            f"Fix it. Return ONLY the corrected code inside triple backticks with no commentary."
                        )

            if not question_passed:
                all_lab_passed = False
                self.ctrl.log(f"⚠️ Challenge {q_idx} could not pass all test cases after 4 attempts.", "warn")

        # Check for overarching Submit / Finish button on last challenge
        self.ctrl.log("Finished all challenges. Checking for Submit/Finish...", "info")
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

        # Close the lab tab if separate
        TEST_CLOSE_KEYS = ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/']
        for p in list(self.context.pages):
            try:
                u = p.url.lower()
                if any(k in u for k in TEST_CLOSE_KEYS) and p != self.bytexl_page:
                    await p.close()
            except Exception:
                pass

        if self.bytexl_page and not self.bytexl_page.is_closed():
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass
            await asyncio.sleep(1.5)

        return all_lab_passed

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
            self.ctrl.log(f"⚠️ '{title}' contains an active Lab/Quiz launch button! Aborting reading mode to solve activity.", "warn")
            return False

        self.ctrl.log(f"📖 Reading theory: '{title}'. Scrolling content...", "info")
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
            self.ctrl.log(f"✅ Clicked 'Mark As Completed' on '{title}'!", "success")
            await asyncio.sleep(1.5)
        else:
            self.ctrl.log(f"Theory '{title}' marked complete or button not present.", "info")

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
            self.ctrl.log("Advancing to next topic via 'Next >'...", "info")
            await asyncio.sleep(3)
            return True

        return False

    async def take_page_screenshot(self, page) -> Optional[bytes]:
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOTS_DIR, f"nav_{ts}.jpg")
            shot = await page.screenshot(type="jpeg", quality=75)
            with open(path, "wb") as f:
                f.write(shot)
            self.ctrl.latest_screenshot = path
            return shot
        except Exception:
            return None

    async def get_visible_buttons(self, page) -> List[str]:
        try:
            return await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                return btns
                    .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim())
                    .filter(t => t.length > 1 && t.length < 80);
            }""")
        except Exception:
            return []

    async def get_visible_headings(self, page) -> List[str]:
        try:
            return await page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, .MuiTypography-h5, .MuiTypography-h6, .chapter-title'));
                return els.map(e => (e.innerText || '').trim()).filter(t => t.length > 2 && t.length < 120);
            }""")
        except Exception:
            return []

    async def get_active_page(self) -> Tuple[Optional[Any], str]:
        """Finds the most relevant active ByteXL/test tab across all open browser pages."""
        pages = list(self.context.pages) if self.context else []
        if not pages:
            return self.bytexl_page, "default"

        valid_pages = []
        for p in pages:
            try:
                if p.is_closed():
                    continue
                u = p.url.lower()
                if "chrome-error://" in u or "chromewebdata" in u or "gemini.google.com" in u:
                    continue
                valid_pages.append(p)
            except Exception:
                pass

        if not valid_pages:
            return self.bytexl_page, "default"

        # 1. Prioritize active quiz / test / assessment / lab / challenge (by URL or by DOM)
        TEST_URL_PATTERNS = ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/', '/code/']
        for p in valid_pages:
            try:
                u = p.url.lower()
                is_test_url = any(k in u for k in TEST_URL_PATTERNS)
                has_test_dom = await p.evaluate("""() => {
                    return !!document.querySelector(
                        '[role="radiogroup"], .MuiRadioGroup-root, [role="radio"], ' +
                        '.monaco-editor, textarea#code, .test-results, .output-console'
                    );
                }""")
                if is_test_url or has_test_dom:
                    # Exclude tabs that are already submitted / finished result screens
                    is_finished = await p.evaluate("""() => {
                        const bt = (document.body ? document.body.innerText : '').toLowerCase();
                        const hasRadios = !!document.querySelector('[role="radiogroup"], .MuiRadioGroup-root, [role="radio"]');
                        const hasMonaco = !!document.querySelector('.monaco-editor, textarea#code');
                        return (bt.includes('test submitted') || bt.includes('assessment submitted') ||
                                bt.includes('assessment completed') || bt.includes('quiz completed')) &&
                               !hasRadios && !hasMonaco;
                    }""")
                    if not is_finished:
                        return p, "test"
            except Exception:
                pass

        # 2. Prioritize learning module or topic (exclude /login)
        for p in valid_pages:
            try:
                u = p.url.lower()
                if ("/module/" in u or "/topic/" in u) and "/login" not in u:
                    return p, "learning"
            except Exception:
                pass

        # 3. Prioritize course units / catalog (exclude /login)
        for p in valid_pages:
            try:
                u = p.url.lower()
                if "bytexl" in u and "/courses" in u and "/login" not in u:
                    return p, "courses"
            except Exception:
                pass

        # 4. Any bytexl page that is NOT /login
        for p in valid_pages:
            try:
                u = p.url.lower()
                if "bytexl" in u and "/login" not in u and "/signin" not in u:
                    return p, "bytexl"
            except Exception:
                pass

        # 5. Fallback: if only login page exists
        for p in valid_pages:
            try:
                if "bytexl" in p.url.lower():
                    return p, "bytexl"
            except Exception:
                pass

        return valid_pages[-1], "default"

    async def classify_page_state(self, page) -> Dict[str, Any]:
        """Classifies the current page into a recognized operational state."""
        if not page or page.is_closed():
            return {"state": "NO_PAGE"}

        try:
            return await page.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();

                // 1. Check if on a finished / submitted test page (results screen)
                const hasRadios = !!document.querySelector('[role="radiogroup"], .MuiRadioGroup-root, [role="radio"]');
                const hasMonaco = !!document.querySelector('.monaco-editor, textarea#code');
                const hasScoreDiff = bodyText.includes('difficulty:') || bodyText.includes('score:') || bodyText.includes('answer submitted');
                const isTestUrl = url.includes('/test/') || url.includes('/assessment/') || url.includes('/quiz/') ||
                                  url.includes('/lab/') || url.includes('/challenge/') || url.includes('/compiler/') ||
                                  url.includes('/practice/') || url.includes('/code/');

                const isFinishedSummary = !hasRadios && !hasMonaco && (
                    bodyText.includes('test submitted') || bodyText.includes('assessment submitted') ||
                    bodyText.includes('assessment completed') || bodyText.includes('quiz completed') ||
                    bodyText.includes('submission successful') ||
                    (bodyText.includes('your score') && !bodyText.includes('submit'))
                );
                if (isTestUrl && isFinishedSummary) {
                    return { state: "TEST_FINISHED" };
                }

                // 2. Active Test / Assessment (Coding or MCQ)
                if (hasRadios || hasMonaco || isTestUrl || (hasScoreDiff && hasRadios)) {
                    return { state: "TEST_ACTIVE", isCoding: hasMonaco, isMCQ: hasRadios || !hasMonaco };
                }

                // 3. Login required ONLY if on actual login URL or visible login form (and NOT a test)
                const isLoginUrl = url.includes('/login') || url.includes('/signin');
                const visiblePassword = Array.from(document.querySelectorAll('input[type="password"]')).some(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                const hasSignInBtn = Array.from(document.querySelectorAll('button, input[type="submit"]')).some(b => {
                    const t = (b.innerText || b.value || '').toLowerCase();
                    return t.includes('sign in') || t.includes('log in') || t.includes('login');
                });
                if ((isLoginUrl || (visiblePassword && hasSignInBtn)) && !hasRadios && !hasMonaco) {
                    return { state: "LOGIN" };
                }

                // 4. Quiz / Lab Landing Page (with Start/Take button) - MUST CHECK BEFORE READING TOPIC!
                const startKeywords = [
                    'take the quiz', 'take quiz', 'start quiz', 'open quiz', 'attempt quiz',
                    'start lab', 'open lab', 'resume lab', 'launch lab',
                    'start assessment', 'take assessment', 'open assessment',
                    'start challenge', 'open challenge', 'solve challenge',
                    'start test', 'take test', 'resume test', 'resume quiz',
                    'continue quiz', 'continue lab', 'continue assessment',
                    'practice now', 'start practice'
                ];
                const openBtn = Array.from(document.querySelectorAll('button, a, [role="button"]')).find(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return startKeywords.some(kw => t.includes(kw)) && !b.disabled;
                });
                if (openBtn) {
                    return { state: "QUIZ_LANDING", btnText: openBtn.innerText.trim() };
                }

                // 5. Reading / Theory Topic (ONLY if NOT a quiz or lab landing page!)
                const hasMarkBtn = Array.from(document.querySelectorAll('button, a')).some(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return t === 'mark as completed' || t === 'mark as complete' || t === 'mark complete';
                });
                const hasSummarize = bodyText.includes('summarize this chapter');
                const hasReadingBreadcrumb = url.includes('/topic/') || !!document.querySelector('.chapter-title, .md-view');
                if (!openBtn && (hasMarkBtn || (hasReadingBreadcrumb && (hasSummarize || bodyText.includes('next >') || bodyText.includes('go back to course'))))) {
                    return { state: "READING_TOPIC", hasMarkBtn: hasMarkBtn };
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
        """Clicks an element by text using fast DOM JS click first, with Playwright fallback."""
        clean = (text or "").strip()
        if not clean:
            return False

        try:
            clicked = await page.evaluate("""(targetText) => {
                const targetLower = targetText.toLowerCase();
                const all = Array.from(document.querySelectorAll('button, a, [role="button"], span.MuiButton-label, div[class*="button" i], [tabindex="0"]'));
                const match = all.find(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === targetLower;
                }) || all.find(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t.includes(targetLower);
                });
                if (match) {
                    const btn = match.closest('button, a, [role="button"]') || match;
                    btn.scrollIntoView({ behavior: 'auto', block: 'center' });
                    btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    btn.click();
                    return true;
                }
                return false;
            }""", clean)
            if clicked:
                return True
        except Exception:
            pass

        try:
            loc = page.locator(f'button:has-text("{clean}"), a:has-text("{clean}")').first
            if await loc.count() > 0:
                await loc.click(timeout=1500)
                return True
        except Exception:
            pass

        return False

    async def find_and_open_course(self, page, target_query: str) -> bool:
        """
        Intelligently finds and enters the target course on the /courses catalog.
        Uses fuzzy acronym matching ('RDBMS'/'rdms' -> 'Relational Database Management Systems')
        and multi-term search query expansion. Never leaves catalog broken.
        """
        # Wait up to 6 seconds for course cards to render on /courses
        for _ in range(6):
            has_cards = await page.evaluate("""() => {
                const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                    const t = (c.innerText || '').toLowerCase();
                    return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                });
                return cards.length > 0;
            }""")
            if has_cards:
                break
            await asyncio.sleep(1.0)

        cards_info = await page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                const t = (c.innerText || '').toLowerCase();
                return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
            });
            return cards.map((c, i) => {
                const titleEl = c.querySelector('h3, h4, h5, h6, .MuiTypography-h6, .MuiTypography-h5, .MuiTypography-root');
                const lines = (c.innerText || '').split(/\\r?\\n/).map(l => l.trim()).filter(Boolean);
                const fallback = lines.find(l => !l.toLowerCase().includes('completion') && !l.toLowerCase().includes('continue') && !l.toLowerCase().includes('start') && !l.toLowerCase().includes('resume') && l.length > 2) || "Course " + i;
                return {
                    index: i,
                    title: titleEl ? titleEl.innerText.trim() : fallback,
                    text: c.innerText
                };
            });
        }""")

        target_clean = (target_query or "").strip()

        # If user specified a target (e.g. 'RDBMS', 'rdms', 'Cloud Security', 'AZ-900')
        if target_clean:
            if cards_info:
                titles = [c["title"] for c in cards_info]
                matched_title = brain.match_course(target_clean, titles)

                if matched_title:
                    self.ctrl.log(f"🎯 Smart Match: Target '{target_clean}' matched course '{matched_title}'!", "success")
                    clicked = await page.evaluate("""(matchTitle) => {
                        const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                            const t = (c.innerText || '').toLowerCase();
                            return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                        });
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
                    }""", matched_title)
                    if clicked:
                        return True

            # If not in visible cards, try searching catalog with expanded search terms
            search_terms = brain.get_search_terms(target_clean)
            if target_clean not in search_terms:
                search_terms.insert(0, target_clean)

            self.ctrl.log(f"Searching catalog for: '{target_clean}' using search terms {search_terms[:3]}...", "info")
            try:
                search_input = await page.wait_for_selector('input[placeholder*="search" i]', timeout=3500)
                if search_input:
                    for term in search_terms:
                        await search_input.fill("")
                        await search_input.fill(term)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(1.8)

                        searched_cards = await page.evaluate("""() => {
                            const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                                const t = (c.innerText || '').toLowerCase();
                                return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                            });
                            return cards.map(c => {
                                const titleEl = c.querySelector('h3, h4, h5, h6, .MuiTypography-h6, .MuiTypography-h5');
                                return titleEl ? titleEl.innerText.trim() : c.innerText.split('\\n')[0].trim();
                            });
                        }""")

                        if searched_cards:
                            matched_s = brain.match_course(target_clean, searched_cards) or searched_cards[0]
                            self.ctrl.log(f"🎯 Found course with term '{term}': '{matched_s}'! Entering...", "success")
                            clicked = await page.evaluate("""(matchTitle) => {
                                const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root'));
                                const targetCard = cards.find(c => c.innerText.includes(matchTitle)) || cards[0];
                                if (targetCard) {
                                    const btn = Array.from(targetCard.querySelectorAll('button, a')).find(b => {
                                        const txt = (b.innerText || '').toLowerCase();
                                        return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                                    }) || targetCard.querySelector('button, a') || targetCard;
                                    btn.click();
                                    return true;
                                }
                                return false;
                            }""", matched_s)
                            if clicked:
                                return True

                    # If all search terms yielded 0, restore full catalog
                    self.ctrl.log(f"Search for '{target_clean}' terms finished. Restoring full catalog...", "info")
                    await search_input.fill("")
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(1.5)
            except Exception:
                pass

        # Fallback: Enter first uncompleted course (< 100%)
        self.ctrl.log("Scanning 'My Courses' for uncompleted course (< 100%)...", "info")
        return await page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('.MuiCard-root, .MuiPaper-root, .MuiBox-root')).filter(c => {
                const t = (c.innerText || '').toLowerCase();
                return t.includes('completion') || t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
            });

            for (const card of cards) {
                const txt = card.innerText || '';
                const match = txt.match(/(\\d+)%\\s*(?:course\\s*)?completion/i) || txt.match(/completion[\\s\\S]*?(\\d+)%/i);
                const pct = match ? parseInt(match[1]) : 0;
                if (pct < 100) {
                    const btn = Array.from(card.querySelectorAll('button, a')).find(b => {
                        const t = (b.innerText || '').toLowerCase();
                        return t.includes('continue learning') || t.includes('start learning') || t.includes('resume');
                    }) || card.querySelector('button, a');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                }
            }

            if (cards.length > 0) {
                const btn = cards[0].querySelector('button, a');
                if (btn) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")

    async def find_and_click_uncompleted_unit(self, page) -> Dict[str, Any]:
        """Finds the uncompleted unit card and clicks Continue Learning / Resume or clicks the unstarted card."""
        try:
            target_mod = (self.ctrl.target_module or "").strip()
            res = await page.evaluate("""(targetMod) => {
                // Collect all unit cards
                const cards = Array.from(document.querySelectorAll('.MuiPaper-root, .MuiCard-root, div[class*="UnitCard"], div[class*="MuiBox-root"]')).filter(c => {
                    const t = (c.innerText || '').toLowerCase();
                    return (t.includes('chapter') || t.includes('challenge') || t.includes('unit') || t.includes('module')) &&
                           !t.includes('units and chapters') && !t.includes('how is my course graded');
                });

                // 1. If target specified, prioritize matching unit card
                if (targetMod) {
                    const tClean = targetMod.toLowerCase();
                    const matchingCard = cards.find(c => (c.innerText || '').toLowerCase().includes(tClean));
                    if (matchingCard) {
                        const btn = Array.from(matchingCard.querySelectorAll('button, a, [role="button"], div[class*="button" i]')).find(b => {
                            const txt = (b.innerText || '').trim().toLowerCase();
                            return (txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) && !b.disabled;
                        }) || matchingCard.querySelector('button, a, [role="button"]') || matchingCard;
                        btn.scrollIntoView({ behavior: 'auto', block: 'center' });
                        btn.click();
                        return { clicked: true, title: targetMod, all_completed: false };
                    }
                }

                // 2. Look for a unit card that has an explicit action button: Continue Learning, Start Learning, Resume
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

                // 3. If no button found, look for an uncompleted unit card (one that DOES NOT have "Completed")
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

                // 4. Fallback: Any button or link on the page with "Continue Learning"
                const globalBtn = Array.from(document.querySelectorAll('button, a, [role="button"]')).find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return (txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) && !b.disabled;
                });
                if (globalBtn) {
                    globalBtn.scrollIntoView({ behavior: 'auto', block: 'center' });
                    globalBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    globalBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    globalBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    globalBtn.click();
                    return { clicked: true, title: 'Unit', all_completed: false };
                }

                const allDone = cards.length > 0 && cards.every(c => (c.innerText || '').toLowerCase().includes('completed'));
                return { clicked: false, all_completed: allDone };
            }""", target_mod)

            if res and res.get("clicked"):
                return res
            if res and res.get("all_completed"):
                return res
        except Exception as e:
            self.ctrl.log(f"Unit click interaction: {e}", "warn")

        return {"clicked": False, "all_completed": False}

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
                    return t.length > 2 && t.length < 160 && !t.toLowerCase().includes('units and chapters') && !t.toLowerCase().includes('how is my course graded');
                });

                for (const row of candidateRows) {
                    const rawText = row.innerText.trim();
                    const titleLine = rawText.split('\\n')[0].trim();

                    if (doneList.some(d => titleLine.includes(d) || (d && d.includes(titleLine)))) continue;

                    const lowerTitle = titleLine.toLowerCase();
                    const isActivity = lowerTitle.includes('challenge') || lowerTitle.includes('lab') ||
                                       lowerTitle.includes('quiz') || lowerTitle.includes('assessment') ||
                                       lowerTitle.includes('practice');

                    let submoduleIncomplete = false;
                    const accordion = row.closest('.MuiAccordion-root');
                    if (accordion) {
                        const accSummary = accordion.querySelector('.MuiAccordionSummary-root');
                        if (accSummary) {
                            const st = (accSummary.innerText || '').toLowerCase();
                            const frac = st.match(/(\\d+)\\s*(?:\\/|of)\\s*(\\d+)\\s*completed/);
                            if (frac && parseInt(frac[1]) < parseInt(frac[2])) {
                                submoduleIncomplete = true;
                            } else if (frac && parseInt(frac[1]) === parseInt(frac[2]) && parseInt(frac[2]) > 0) {
                                continue;
                            }
                            const pct = st.match(/(\\d+)%\\s*completed/);
                            if (pct && parseInt(pct[1]) < 100) {
                                submoduleIncomplete = true;
                            } else if (pct && parseInt(pct[1]) === 100) {
                                continue;
                            }
                        }
                    }

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

                    // If not checked, OR if submodule is incomplete and this is an activity (re-verifying pending labs/quizzes)
                    if (!isChecked || (submoduleIncomplete && isActivity)) {
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
                    t.scrollIntoView({ behavior: 'auto', block: 'center' });
                    t.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    t.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    t.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
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

    async def tick_completion_checkbox(self, page, activity_name: str) -> bool:
        try:
            return await page.evaluate("""(actName) => {
                const cleanName = (actName || '').trim().toLowerCase();
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
                            return true;
                        }
                    }
                }

                const markBtns = Array.from(document.querySelectorAll('button, span, a')).filter(b => {
                    const txt = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                    return txt.includes('mark as complete') || txt.includes('mark complete');
                });
                if (markBtns.length > 0) {
                    markBtns[0].click();
                    return true;
                }
                return false;
            }""", activity_name)
        except Exception:
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

                // If there is an active Start / Take / Attempt button, it is NOT completed!
                const startKeywords = [
                    'take the quiz', 'take quiz', 'start quiz', 'open quiz', 'attempt quiz',
                    'start lab', 'open lab', 'resume lab', 'launch lab',
                    'start assessment', 'take assessment', 'open assessment',
                    'start challenge', 'open challenge', 'solve challenge',
                    'start test', 'take test', 'resume test', 'resume quiz'
                ];
                const hasStartBtn = Array.from(document.querySelectorAll('button, a, [role="button"]')).some(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return startKeywords.some(kw => t.includes(kw)) && !b.disabled;
                });

                // If start button is present, we should ALWAYS run the activity!
                if (hasStartBtn) {
                    return false;
                }

                // Check for explicit finished score in an attempts table
                const hasFinishedState = bodyText.includes('state: finished') || bodyText.includes('status: finished') || bodyText.includes('submitted');
                const scoreMatch = bodyText.match(/(?:grade|score|marks)\\s*:\\s*(\\d+(?:\\.\\d+)?)\\s*(?:\\/\\s*(\\d+(?:\\.\\d+)?)|%)/i);

                if (hasFinishedState && scoreMatch) {
                    const scored = parseFloat(scoreMatch[1]);
                    const maxScore = scoreMatch[2] ? parseFloat(scoreMatch[2]) : 100;
                    if (scored >= maxScore) {
                        return true;
                    }
                }

                // Explicit completed message without start button
                return (bodyText.includes('quiz completed') || bodyText.includes('assessment completed') || bodyText.includes('already submitted')) &&
                       !bodyText.includes('take the quiz') && !bodyText.includes('start lab');
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

    async def execute_one_module(self):
        """Unified Master Autonomous Agent Loop with Gemini AI Brain integration."""
        ok = await self.connect()
        if not ok:
            return False

        target_course = (self.ctrl.target_module or "").strip()
        self.ctrl.state = "Autonomous Agent Active"
        self.ctrl.log(f"🤖 Autonomous AI Assistant Active (Target: '{target_course or 'Next Uncompleted'}')", "info")

        # If target course is specified, ensure we are inside it; if inside another course, navigate to catalog
        if target_course:
            try:
                active_p, _ = await self.get_active_page()
                if active_p:
                    p_url = active_p.url
                    p_title = await active_p.title()
                    b_text = await active_p.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 1000)")
                    inside_target = brain.is_matching_page_course(target_course, f"{p_url} {p_title} {b_text}")
                    if not inside_target and not p_url.rstrip("/").endswith("/courses"):
                        self.ctrl.log(f"Navigating to course catalog to open target course '{target_course}'...", "info")
                        await active_p.goto("https://app.bytexl.ai/courses")
                        await asyncio.sleep(2.5)
            except Exception:
                pass

        completed_set = set()
        activities_completed = 0
        current_activity_name = ""
        stuck_counter = 0
        last_url_state = ""
        step = 0
        max_steps = 200

        while not self.ctrl.stop_requested and step < max_steps:
            step += 1
            await asyncio.sleep(1.5)

            if self.ctrl.stop_requested:
                self.ctrl.log("Stop requested by user. Pausing automation loop.", "warn")
                break

            active_page, p_kind = await self.get_active_page()
            if not active_page:
                self.ctrl.log("No active browser tab found. Waiting 3s...", "warn")
                await asyncio.sleep(3)
                continue

            try:
                await active_page.bring_to_front()
            except Exception:
                pass

            state_info = await self.classify_page_state(active_page)
            current_state = state_info.get("state", "UNKNOWN")
            current_url = active_page.url
            state_sig = f"{current_state}:{current_url}"

            if state_sig == last_url_state:
                stuck_counter += 1
            else:
                stuck_counter = 0
                last_url_state = state_sig

            # 1. LOGIN State
            if current_state == "LOGIN":
                self.ctrl.state = "Waiting for ByteXL Login..."
                self.ctrl.log("⚠️ ByteXL login required. Please sign in in the browser to continue.", "warn")
                await asyncio.sleep(3)
                continue

            # 1b. TEST_FINISHED State (Results / Submission screen)
            if current_state == "TEST_FINISHED":
                self.ctrl.log("Assessment completed. Returning to course module...", "info")
                if active_page != self.bytexl_page and not active_page.is_closed():
                    try:
                        await active_page.close()
                    except Exception:
                        pass
                for p in self.context.pages:
                    if not p.is_closed() and "bytexl" in p.url.lower():
                        self.bytexl_page = p
                        try:
                            await self.bytexl_page.bring_to_front()
                        except Exception:
                            pass
                        break
                await asyncio.sleep(2)
                continue

            # 2. TEST_ACTIVE State (MCQ or Coding)
            if current_state == "TEST_ACTIVE":
                self.ctrl.state = "Solving Assessment with Gemini..."
                is_separate_tab = (active_page != self.bytexl_page and len(self.context.pages) > 1)

                try:
                    await active_page.bring_to_front()
                    await active_page.evaluate(PAGE_VISIBILITY_SHIM)
                except Exception:
                    pass

                # Environment Settle Wait: Wait up to 6 seconds for Monaco editor or MCQ radios to mount
                is_coding = state_info.get("isCoding", False)
                if not is_coding:
                    for _ in range(8):
                        has_monaco = await active_page.evaluate("() => !!document.querySelector('.monaco-editor, textarea#code, .editor, .language-selector')")
                        if has_monaco:
                            is_coding = True
                            break
                        has_radios = await active_page.evaluate("() => !!document.querySelector('[role=\"radiogroup\"], .MuiRadioGroup-root, input[type=\"radio\"]')")
                        if has_radios:
                            is_coding = False
                            break
                        await asyncio.sleep(0.5)

                if is_coding:
                    eval_passed = await self.solve_coding(active_page)
                else:
                    eval_passed = await self.solve_mcq(active_page)

                if eval_passed:
                    self.ctrl.log(f"🏆 Assessment '{current_activity_name or 'Activity'}' EVALUATED & VERIFIED PASSED!", "success")
                    if current_activity_name:
                        completed_set.add(current_activity_name)
                    activities_completed += 1
                else:
                    self.ctrl.log(f"⚠️ Assessment '{current_activity_name or 'Activity'}' did NOT pass all requirements. Leaving unticked for retry.", "warn")

                # If test ran in a separate tab, close it now so agent doesn't get stuck on it
                if is_separate_tab and not active_page.is_closed():
                    self.ctrl.log("Closing assessment tab...", "info")
                    try:
                        await active_page.close()
                    except Exception:
                        pass

                # Locate and focus the main course curriculum page
                main_course_page = None
                for p in self.context.pages:
                    if not p.is_closed():
                        u = p.url.lower()
                        if "bytexl" in u and not any(k in u for k in ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/']):
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

                # ONLY tick completion checkbox IF evaluated & passed!
                if eval_passed and main_course_page and current_activity_name:
                    ticked = await self.tick_completion_checkbox(main_course_page, current_activity_name)
                    if ticked:
                        self.ctrl.log(f"✅ Ticked completion box for '{current_activity_name}'!", "success")

                # If test ran on same page, advance if on landing page
                if not is_separate_tab:
                    await self.advance_next(self.bytexl_page)
                continue

            # 3. READING_TOPIC State
            if current_state == "READING_TOPIC":
                self.ctrl.state = "Reading Topic..."
                advanced = await self.handle_theory_reading(active_page)
                if current_activity_name:
                    completed_set.add(current_activity_name)
                activities_completed += 1
                await asyncio.sleep(2)
                continue

            # 4. QUIZ_LANDING State
            if current_state == "QUIZ_LANDING":
                is_done = await self.is_landing_already_completed(active_page, current_activity_name)
                if is_done:
                    self.ctrl.log(f"Activity '{current_activity_name or 'Quiz'}' is already completed. Advancing...", "info")
                    if current_activity_name:
                        completed_set.add(current_activity_name)
                    await self.advance_next(active_page)
                    continue

                btn_text = state_info.get("btnText", "Take The Quiz Now!")
                self.ctrl.log(f"Launching Activity: '{btn_text}'...", "info")
                pages_before = set(self.context.pages)
                await self.click_button_reliably(active_page, btn_text)

                # Handle any pre-start confirmation modal / dialog immediately
                await asyncio.sleep(1.0)
                await self.handle_start_confirmation_dialog(active_page)

                # Auto-detect newly opened assessment tab and bring to front immediately
                new_test_tab = None
                for _ in range(12):
                    await asyncio.sleep(0.5)
                    diff = set(self.context.pages) - pages_before
                    if diff:
                        new_test_tab = list(diff)[0]
                        break
                    u = active_page.url.lower()
                    if any(k in u for k in ['/test/', '/assessment/', '/quiz/', '/lab/', '/challenge/', '/compiler/', '/practice/', '/code/']):
                        new_test_tab = active_page
                        break

                if new_test_tab and not new_test_tab.is_closed():
                    try:
                        await new_test_tab.wait_for_load_state("domcontentloaded", timeout=6000)
                    except Exception:
                        pass
                    try:
                        await new_test_tab.bring_to_front()
                    except Exception:
                        pass
                    try:
                        await new_test_tab.evaluate(PAGE_VISIBILITY_SHIM)
                    except Exception:
                        pass
                    self.ctrl.log(f"Switched focus to assessment tab: {new_test_tab.url[:60]}", "success")
                else:
                    await asyncio.sleep(2)
                continue

            # 5. MODULE_CURRICULUM State
            if current_state == "MODULE_CURRICULUM":
                self.ctrl.state = "Processing Module Curriculum..."
                await self.expand_all_accordions(active_page)

                next_topic = await self.find_next_uncompleted_topic(active_page, list(completed_set))
                if next_topic:
                    current_activity_name = next_topic['text']
                    self.ctrl.log(f"🎯 Opening topic: '{current_activity_name}'", "info")
                    clicked = await self.click_topic_reliably(active_page, next_topic)
                    await asyncio.sleep(3.5)
                    continue
                else:
                    has_next_unit = await self.check_and_click_next_unit(active_page)
                    if has_next_unit:
                        self.ctrl.log("All submodules in this unit complete! Advancing: 'Go To Next Unit'...", "success")
                        await asyncio.sleep(4)
                        continue

                    has_back = await self.check_and_click_back_to_course(active_page)
                    if has_back:
                        self.ctrl.log("Unit finished! Returning to Units & Chapters list...", "info")
                        await asyncio.sleep(3.5)
                        continue

                    if brain.has_key():
                        shot_bytes = await self.take_page_screenshot(active_page)
                        dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                     await self.get_visible_buttons(active_page),
                                                     await self.get_visible_headings(active_page),
                                                     screenshot_bytes=shot_bytes)
                        if dec.get("action") == "click_button" and dec.get("target_button_text"):
                            target = dec.get("target_button_text")
                            self.ctrl.log(f"🧠 AI Brain Decision: {dec.get('reason')} -> Clicking '{target}'", "info")
                            await self.click_button_reliably(active_page, target)
                            await asyncio.sleep(4)
                            continue

                    self.ctrl.log("All topics in this unit complete! Navigating to Units & Chapters to continue next unit...", "info")
                    try:
                        m = re.match(r"(https?://[^/]+/courses/\d+/[^/]+)", active_page.url)
                        if m:
                            await active_page.goto(m.group(1))
                        else:
                            await active_page.go_back()
                        await asyncio.sleep(3)
                        continue
                    except Exception:
                        pass
                    break

            # 6. UNITS_LIST State ("Units and Chapters")
            if current_state == "UNITS_LIST":
                self.ctrl.state = "Selecting Uncompleted Unit..."
                self.ctrl.log("Units & Chapters page detected. Finding uncompleted unit...", "info")

                unit_res = await self.find_and_click_uncompleted_unit(active_page)
                if unit_res.get("clicked"):
                    unit_title = unit_res.get("title", "Unit")
                    self.ctrl.current_module_name = unit_title
                    self.ctrl.log(f"🚀 Selected Unit: '{unit_title}'! Loading...", "success")
                    await asyncio.sleep(4)
                    continue
                elif unit_res.get("all_completed"):
                    self.ctrl.log("🎉 All units on the Units & Chapters page are 100% completed!", "success")
                    await active_page.goto("https://app.bytexl.ai/courses")
                    await asyncio.sleep(3)
                    continue
                else:
                    if brain.has_key():
                        self.ctrl.log("🧠 Consulting Gemini AI Brain with screen screenshot to find uncompleted unit...", "info")
                        shot_bytes = await self.take_page_screenshot(active_page)
                        dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                     await self.get_visible_buttons(active_page),
                                                     await self.get_visible_headings(active_page),
                                                     screenshot_bytes=shot_bytes)
                        self.ctrl.log(f"🧠 AI Brain Analysis: {dec.get('reason')}", "info")
                        if dec.get("action") == "click_button" and dec.get("target_button_text"):
                            target = dec.get("target_button_text")
                            clicked = await self.click_button_reliably(active_page, target)
                            if clicked:
                                await asyncio.sleep(4)
                                continue

                    self.ctrl.log("No further uncompleted units detected on Units & Chapters page.", "info")
                    break

            # 7. COURSES_CATALOG State
            if current_state == "COURSES_CATALOG":
                self.ctrl.state = "Entering Course..."
                entered = await self.find_and_open_course(active_page, target_course)
                if entered:
                    await asyncio.sleep(4)
                    continue
                else:
                    self.ctrl.log("All courses appear completed or catalog search finished.", "info")
                    break

            # 8. UNKNOWN State (Gemini Vision Self-Healing)
            if current_state == "UNKNOWN":
                self.ctrl.state = "AI Brain Analyzing Screen..."
                self.ctrl.log(f"Page state unrecognized ({active_page.url[:60]}). Consulting Gemini Brain...", "info")

                if brain.has_key():
                    shot_bytes = await self.take_page_screenshot(active_page)
                    buttons = await self.get_visible_buttons(active_page)
                    headings = await self.get_visible_headings(active_page)
                    dec = brain.decide_navigation(active_page.url, await active_page.title(),
                                                 buttons, headings, screenshot_bytes=shot_bytes)
                    self.ctrl.log(f"🧠 AI Brain Decision: {dec.get('reason')} -> Action: {dec.get('action')} on '{dec.get('target_button_text')}'", "success")

                    if dec.get("action") == "click_button" and dec.get("target_button_text"):
                        await self.click_button_reliably(active_page, dec.get("target_button_text"))
                        await asyncio.sleep(3.5)
                        continue
                    elif dec.get("action") == "mark_complete":
                        await self.handle_theory_reading(active_page)
                        continue
                    elif dec.get("action") == "all_completed":
                        self.ctrl.log("🎉 AI Brain verified: All course activities are completed!", "success")
                        break

                if stuck_counter >= 3:
                    self.ctrl.log("⚠️ Agent remained on unknown page. Returning to /courses catalog...", "warn")
                    try:
                        await active_page.goto("https://app.bytexl.ai/courses")
                        await asyncio.sleep(3.5)
                    except Exception:
                        pass
                else:
                    await asyncio.sleep(3)
                continue

        # Finished or Stopped
        if self.ctrl.stop_requested:
            self.ctrl.state = "Stopped by User"
            self.ctrl.log("Automation stopped by user.", "warn")
            await self.close()
            return False

        mod_name = self.ctrl.current_module_name or target_course or "Course Module"
        if activities_completed > 0:
            self.ctrl.state = f"Completed {mod_name} (100%)"
            self.ctrl.log(f"🎉 Completed {mod_name} with 100%!", "success")
            self.ctrl.pending_report = f"""
            <div class="report-card">
                <h4>🎉 Module Completed: {mod_name} (100%)</h4>
                <p>All quizzes, lab challenges, and submodules have been completed and side boxes ticked!</p>
                <div class="action-buttons">
                    <button class="btn-action btn-proceed" onclick="sendQuick('proceed with next module')">Proceed with Next Module</button>
                    <button class="btn-action btn-stop" onclick="sendQuick('stop')">Stop & End Session</button>
                </div>
            </div>
            """
        else:
            self.ctrl.state = f"{mod_name} Finished"
            self.ctrl.log(f"✅ Course progression finished for {mod_name}.", "success")

        await self.close()
        return True


    async def close(self):
        if self.playwright:
            await self.playwright.stop()

def run_agent_thread():
    async def _run():
        agent = AutonomousByteXLAgent(controller)
        await agent.execute_one_module()
    asyncio.run(_run())

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/download")
def download_zip():
    zip_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ByteXL_Automation_Agent.zip")
    if os.path.exists(zip_path):
        return send_file(zip_path, as_attachment=True, download_name="ByteXL_Automation_Agent.zip")
    return ("Zip not found", 404)

@app.route("/api/status")
def get_status():
    return jsonify({
        "state": controller.state,
        "recent_logs": controller.logs[-20:],
        "pending_report": controller.pending_report,
        "latest_screenshot_timestamp": int(time.time()) if controller.latest_screenshot else None,
        "cdp_port": controller.cdp_port,
        "port_error": controller.port_error,
        "browser": controller.last_detected_browser,
        "preferred_browser": getattr(controller, "preferred_browser", "auto"),
        "target_module": controller.target_module,
        "brain_active": brain.has_key(),
        "brain_model": brain.active_model
    })

@app.route("/api/save_key", methods=["POST"])
def save_key():
    data = request.json or {}
    key = (data.get("api_key") or "").strip()
    if not key:
        return jsonify({"success": False, "error": "API Key cannot be empty."}), 400

    saved = brain.set_api_key(key)
    if not saved:
        return jsonify({"success": False, "error": "Failed to save key to config."}), 500

    res = brain.test_connection()
    if res.get("success"):
        controller.log(f"🔑 Gemini Free API Key activated! Connected to {res.get('model')} with ~1s latency.", "success")
        return jsonify({"success": True, "model": res.get("model"), "message": f"Connected to {res.get('model')} successfully!"})
    else:
        err = res.get("error", "Key verification failed")
        controller.log(f"⚠️ Gemini API Key test warning: {err}", "warn")
        return jsonify({"success": False, "error": err}), 400

@app.route("/api/key_status", methods=["GET"])
def get_key_status():
    has_k = brain.has_key()
    masked = ""
    if has_k and len(brain.api_key) > 8:
        masked = brain.api_key[:6] + "..." + brain.api_key[-4:]
    return jsonify({
        "has_key": has_k,
        "model": brain.active_model,
        "masked_key": masked
    })

@app.route("/api/test_key", methods=["POST"])
def test_key():
    data = request.json or {}
    temp_key = (data.get("api_key") or "").strip()
    if temp_key:
        original = brain.api_key
        brain.api_key = temp_key
        res = brain.test_connection()
        brain.api_key = original
    else:
        res = brain.test_connection()
    return jsonify(res)


@app.route("/api/set_target", methods=["POST"])
def set_target():
    data = request.json or {}
    target = (data.get("target") or "").strip()
    if target:
        target = brain.normalize_acronym(target)
    controller.target_module = target
    if target:
        controller.log(f"🎯 Target course set to: '{target}'. Automation will focus on this course.", "info")
    else:
        controller.log("Target cleared. Normal course traversal active.", "info")
    return jsonify({"success": True, "target": controller.target_module})

@app.route("/api/ports", methods=["GET"])
def get_ports():
    current = controller.cdp_port or 9222
    ok, info = check_cdp_port(current, timeout=0.8)
    b_name = info.get("browser", "Chromium") if (ok and info) else None

    candidates = [9222, 9223, 9224, 9225, 9226, 9227, 9228, 9229, 9230, 9231, 9232, 9233, 9234, 9235]
    active = []
    for p in candidates:
        is_ok, p_info = check_cdp_port(p, timeout=0.25)
        if is_ok and p_info:
            active.append({
                "port": p,
                "browser": p_info.get("browser", "Chromium"),
                "has_bytexl": p_info.get("has_bytexl", False),
                "has_gemini": p_info.get("has_gemini", False),
                "tabs": p_info.get("tabs", [])
            })

    # Sort ports so any port with ByteXL open appears first
    active.sort(key=lambda x: 0 if x.get("has_bytexl") else (1 if x.get("has_gemini") else 2))

    free_p = find_free_port(9222)
    return jsonify({
        "current_port": current,
        "is_current_active": ok,
        "current_browser": b_name,
        "preferred_browser": getattr(controller, "preferred_browser", "auto"),
        "active_ports": active,
        "recommended_free_port": free_p,
        "port_error": controller.port_error
    })

@app.route("/api/set_browser", methods=["POST"])
def set_browser():
    data = request.json or {}
    pref = (data.get("browser") or "auto").strip().lower()
    if pref in ["auto", "chrome", "edge", "brave"]:
        controller.preferred_browser = pref
        controller.log(f"Preferred browser set to: {pref.upper()}", "info")
        return jsonify({"success": True, "preferred_browser": pref})
    return jsonify({"success": False, "error": "Invalid browser"}), 400

@app.route("/api/set_port", methods=["POST"])
def set_port():
    data = request.json or {}
    try:
        new_port = int(data.get("port", 9222))
        controller.cdp_port = new_port
        controller.port_error = False
        controller.log(f"Switched browser CDP port to {new_port}.", "info")
        return jsonify({"success": True, "port": new_port})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/api/launch_browser", methods=["POST"])
def api_launch_browser():
    data = request.json or {}
    req_port = data.get("port")
    req_browser = data.get("browser") or controller.preferred_browser
    if not req_port:
        req_port = find_free_port(controller.cdp_port or 9222)
    else:
        req_port = int(req_port)

    controller.cdp_port = req_port
    controller.port_error = False
    launch_browser_on_port(req_port, req_browser)
    controller.log(f"Launched browser ({req_browser}) on port {req_port}.", "info")
    return jsonify({"success": True, "port": req_port, "browser": req_browser})

@app.route("/api/restart_browser", methods=["POST"])
def api_restart_browser():
    data = request.json or {}
    port = int(data.get("port", controller.cdp_port or 9222))
    req_browser = data.get("browser") or controller.preferred_browser
    exe = find_installed_browser(req_browser)
    proc_name = os.path.basename(exe)

    use_default = not is_browser_process_running(proc_name)
    controller.log(f"Starting {proc_name} with remote debugging on port {port} (profile: {'default' if use_default else 'automation'})...", "info")
    launch_browser_on_port(port, req_browser, use_default_profile=use_default)
    time.sleep(2)
    controller.cdp_port = port
    controller.port_error = False
    controller.log(f"Started {proc_name} with remote debugging on port {port}.", "success")
    return jsonify({"success": True, "port": port, "browser": req_browser})

@app.route("/api/ack_report", methods=["POST"])
def ack_report():
    controller.pending_report = None
    return jsonify({"status": "ok"})

@app.route("/api/latest_screenshot")
def get_latest_screenshot():
    shot_path = controller.latest_screenshot
    if not shot_path or not os.path.exists(shot_path):
        files = [os.path.join(SCREENSHOTS_DIR, f) for f in os.listdir(SCREENSHOTS_DIR) if f.endswith(('.png', '.jpg'))]
        if files:
            files.sort(key=lambda x: os.path.getmtime(x))
            shot_path = files[-1]

    if shot_path and os.path.exists(shot_path):
        return send_file(shot_path, mimetype="image/png")
    return ("No screenshot available", 404)

@app.route("/api/chat", methods=["POST"])
def handle_chat():
    data = request.json or {}
    raw_msg = (data.get("message") or "").strip()
    msg = raw_msg.lower()

    # 1. STOP COMMAND (Highest Priority)
    if "stop" in msg or "end" in msg or "pause" in msg or "quit" in msg:
        controller.stop_requested = True
        controller.state = "Session Stopped"
        controller.log("⏹ Session stopped by user request.", "warn")
        return jsonify({
            "reply": "⏹ <strong>Session stopped.</strong><br>Agent has paused. Whenever you are ready to resume, just click or type <strong>'proceed with bytexl'</strong> or <strong>'proceed with brave browser'</strong>."
        })

    # 2. Browser detection & switching (e.g. "proceed with brave browser", "use chrome", "open brave")
    named_browser = None
    if "brave" in msg:
        named_browser = "brave"
    elif "chrome" in msg:
        named_browser = "chrome"
    elif "edge" in msg:
        named_browser = "edge"

    if named_browser:
        controller.preferred_browser = named_browser
        controller.log(f"🌐 Preferred browser set to: {named_browser.upper()}", "info")

    # 3. Clear target command
    if msg in ["clear target", "reset target", "remove target"]:
        controller.target_module = ""
        controller.log("Target cleared.", "info")
        return jsonify({"reply": "Target course cleared. Normal course traversal is now active."})

    # 4. Check if this is a browser-only command (e.g. "use brave browser", "switch to chrome", "brave")
    is_pure_browser_switch = re.match(r'^(?:use|switch\s+to|set|select)?\s*(?:brave|chrome|edge)(?:\s+browser)?$', msg)
    if is_pure_browser_switch and not any(k in msg for k in ["proceed", "start", "solve", "run"]):
        return jsonify({
            "reply": f"🌐 Target browser switched to <strong>{named_browser.capitalize()}</strong>.<br>Type <strong>'proceed with {named_browser} browser'</strong> to begin solving with {named_browser.capitalize()}!"
        })

    # 5. Extract course target (preserving original casing e.g. "RDBMS", "Cloud Security")
    candidate_raw = raw_msg
    prefix_match = re.match(
        r'^(?:proceed\s+with|target|solve|complete|search|use|switch\s+to|open|launch)\s+(.*)',
        candidate_raw,
        flags=re.IGNORECASE
    )
    if prefix_match:
        candidate_raw = prefix_match.group(1).strip()

    candidate_raw = re.sub(
        r'\b(?:in|on|with|using)?\s*(?:brave|chrome|edge)\s*(?:browser)?\b',
        '',
        candidate_raw,
        flags=re.IGNORECASE
    ).strip()
    candidate_raw = re.sub(r'^(?:for|on|in|to)\s+', '', candidate_raw, flags=re.IGNORECASE).strip()

    if candidate_raw:
        candidate_raw = brain.normalize_acronym(candidate_raw)

    if candidate_raw and candidate_raw.lower() not in ["bytexl", "next module", "the course", "course", "browser", ""]:
        controller.target_module = candidate_raw
        controller.log(f"🎯 Target course set to: '{controller.target_module}'", "info")

    # 6. Start / Proceed command
    is_start = any(k in msg for k in ["proceed", "start", "resume", "run", "solve"]) or named_browser
    if is_start:
        if controller.state.startswith("Processing") or controller.state.startswith("Navigating"):
            b_info = f" (using {controller.preferred_browser.capitalize()})" if controller.preferred_browser != "auto" else ""
            return jsonify({"reply": f"The agent is already actively working{b_info}{' on target: ' + controller.target_module if controller.target_module else ''}!"})

        controller.stop_requested = False
        controller.state = "Starting Automation..."
        
        browser_label = controller.preferred_browser.capitalize() if controller.preferred_browser != "auto" else "Auto-Detected Browser"
        b_prefix = f"🌐 Browser: <strong>{browser_label}</strong><br>"
        c_prefix = f"🎯 Target: <strong>{controller.target_module}</strong><br>" if controller.target_module else ""
        controller.log(f"Received instruction: '{raw_msg}'. Target Browser: {browser_label}. Starting solver...", "info")

        t = threading.Thread(target=run_agent_thread, daemon=True)
        controller.running_thread = t
        t.start()

        return jsonify({
            "reply": f"🚀 <strong>Autonomous Agent Started!</strong><br>{b_prefix}{c_prefix}Connecting to <strong>{browser_label}</strong>, opening ByteXL, reading topics, solving Quizzes & Labs with Gemini, and advancing through submodules to 100%."
        })

    else:
        return jsonify({
            "reply": f"I received: <em>\"{raw_msg}\"</em>.<br>You can say <strong>'proceed with brave browser'</strong>, <strong>'proceed with chrome'</strong>, target a course (e.g. <code>proceed with system design</code>), or click <strong>'proceed with bytexl'</strong>."
        })

if __name__ == "__main__":
    port = 5000
    url = f"http://localhost:{port}"
    print(f"\n========================================================")
    print(f"[*] ByteXL Automation Copilot Web UI running at: {url}")
    print(f"========================================================\n")

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=False)
