import asyncio
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from playwright.async_api import async_playwright

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
BROWSER_CANDIDATES = [
    # Brave
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    # Google Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    # Microsoft Edge
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]

def find_installed_browser():
    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return "brave"

def check_cdp_port(port, timeout=0.8):
    import urllib.request
    import json
    try:
        url = f"http://localhost:{port}/json/version"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return True, data.get("Browser", "Chromium")
    except Exception:
        return False, None

def find_active_browser_port(start_port=9222, end_port=9230):
    for p in range(start_port, end_port + 1):
        ok, browser = check_cdp_port(p, timeout=0.3)
        if ok:
            return p, browser
    return None, None

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

def launch_browser_on_port(port):
    exe = find_installed_browser()
    prof = os.path.expandvars(r"%USERPROFILE%\.bytexl_profile")
    flags = [
        f'--user-data-dir={prof}',
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "https://www.bytexl.app",
        "https://gemini.google.com",
    ]
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
        target_port = self.ctrl.cdp_port or 9222
        ok, b_name = check_cdp_port(target_port, timeout=1.0)
        if ok:
            self.ctrl.last_detected_browser = b_name
            self.ctrl.port_error = False
            return True

        # Check if browser is running on another common port (e.g. 9222 - 9230)
        active_port, active_browser = find_active_browser_port(9222, 9230)
        if active_port:
            self.ctrl.log(f"Auto-detected active browser ({active_browser}) on port {active_port}! Switching to port {active_port}...", "info")
            self.ctrl.cdp_port = active_port
            self.ctrl.last_detected_browser = active_browser
            self.ctrl.port_error = False
            return True

        # If not running on port, check if browser is already open without remote debugging
        exe = find_installed_browser()
        proc_name = os.path.basename(exe)
        b_label = proc_name.replace(".exe", "").capitalize()

        self.ctrl.log(f"Launching {b_label} with remote debugging on port {target_port}...", "info")
        launch_browser_on_port(target_port)

        self.ctrl.log(f"Waiting for browser on port {target_port}...", "info")
        for i in range(12):
            time.sleep(1)
            ok, b_name = check_cdp_port(target_port, timeout=1.0)
            if ok:
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

        # Fallback: scan if browser opened on another port
        if not self.browser:
            active_p, active_b = find_active_browser_port(9222, 9230)
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

        # Locate existing ByteXL and Gemini pages
        for p in pages:
            url_lower = p.url.lower()
            if "bytexl.ai" in url_lower and not self.bytexl_page:
                self.bytexl_page = p
            elif "gemini.google.com" in url_lower and not self.gemini_page:
                self.gemini_page = p

        # If ByteXL is not open, open it
        if not self.bytexl_page:
            self.ctrl.log("Opening ByteXL (https://app.bytexl.ai/courses)...", "info")
            self.bytexl_page = await self.context.new_page()
            await self.bytexl_page.goto("https://app.bytexl.ai/courses")
            await asyncio.sleep(2)

        # Ensure Gemini is open
        if not self.gemini_page:
            self.ctrl.log("Opening Google Gemini (https://gemini.google.com/app)...", "info")
            self.gemini_page = await self.context.new_page()
            await self.gemini_page.goto("https://gemini.google.com/app")
            await asyncio.sleep(2)

        try:
            await self.bytexl_page.evaluate(PAGE_VISIBILITY_SHIM)
            await self.gemini_page.evaluate(PAGE_VISIBILITY_SHIM)
        except Exception:
            pass

        self.ctrl.log(f"ByteXL: {self.bytexl_page.url}", "success")
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

    async def ask_gemini(self, prompt: str) -> str:
        self.ctrl.log(f"Consulting Gemini ({len(prompt)} chars)...", "info")
        await self.gemini_page.bring_to_front()
        await asyncio.sleep(0.5)

        existing = await self.gemini_page.query_selector_all('message-content, .model-response-text, [data-message-id]')
        baseline_count = len(existing)

        input_elem = await self.gemini_page.wait_for_selector('rich-textarea [contenteditable="true"], div[role="textbox"], textarea', timeout=10000)
        await input_elem.click()

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

        btn = await self.gemini_page.query_selector('button[aria-label*="Send message"], button[aria-label*="Send prompt"], button[aria-label*="Send"], .send-button')
        if btn:
            await btn.click()
        else:
            await self.gemini_page.keyboard.press("Enter")

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
        self.ctrl.log(f"Gemini replied: {answer[:120]}...", "success")
        return answer

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

            if mcq_data.get("isNextDisabled") or mcq_data.get("hasSubmit") or q_num >= 30:
                self.ctrl.log("Final question of quiz reached. Submitting assessment...", "info")
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
            if not clicked_next:
                break
            await asyncio.sleep(1.5)

        self.ctrl.log("Cutting quiz tab and moving to next topic...", "info")
        for p in list(self.context.pages):
            if "/test/" in p.url:
                try:
                    await p.close()
                except Exception:
                    pass

        await self.bytexl_page.bring_to_front()
        await asyncio.sleep(2)

        # Advance if on landing page
        await self.bytexl_page.evaluate("""() => {
            const btn = Array.from(document.querySelectorAll('button, a')).find(b => (b.innerText || '').toLowerCase().includes('next unit'));
            if (btn) btn.click();
        }""")
        await asyncio.sleep(2)
        self.ctrl.log("MCQ Quiz finished and tab closed.", "success")

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

    async def solve_coding(self, test_page):
        self.ctrl.log("Starting Coding / SQL Lab challenge with typed keystrokes...", "info")
        await test_page.bring_to_front()

        total_questions = await test_page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('h6, p, button, div')).filter(el => {
                const t = el.innerText ? el.innerText.trim() : '';
                return /^[0-9]+$/.test(t) && parseInt(t) >= 1 && parseInt(t) <= 50 && el.children.length === 0 && (el.className.includes('Typography') || (el.parentElement && el.parentElement.className.includes('MuiBox')));
            });
            return els.length || 10;
        }""")
        self.ctrl.log(f"Lab contains {total_questions} challenges.", "info")

        for q_idx in range(1, total_questions + 1):
            if self.ctrl.stop_requested:
                break

            self.ctrl.log(f"Lab Challenge {q_idx}/{total_questions}", "info")

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

            # Prepare prompt with incomplete code included as requested
            if has_markers:
                prompt = (
                    f"Solve the following {language} coding / SQL challenge.\n\n"
                    f"Problem Statement:\n{problem_text}\n\n"
                    f"Incomplete Code from Editor:\n```{language}\n{initial_starter_code}\n```\n\n"
                    f"INSTRUCTIONS:\n"
                    f"- Write ONLY the code/query that goes strictly between the start and end solution markers.\n"
                    f"- Do NOT repeat the marker lines ('-- start your solution', '-- end your solution') or Driver Code in your response.\n"
                    f"- Return ONLY the executable query or code inside triple backticks with no commentary."
                )
            else:
                prompt = (
                    f"Solve the following coding / SQL challenge in {language}.\n"
                    f"Return ONLY the executable query or code inside triple backticks with no introductory or concluding text.\n\n"
                    f"Problem Statement:\n{problem_text}"
                )
                if initial_starter_code.strip():
                    prompt += f"\n\nIncomplete / Starter Code in Editor:\n```{language}\n{initial_starter_code}\n```\n"

            for attempt in range(1, 4):
                if attempt > 1:
                    self.ctrl.log(f"Attempt {attempt}/3: Re-checking solution with Gemini...", "warn")
                else:
                    self.ctrl.log(f"Attempt {attempt}/3: Querying Gemini for solution...", "info")

                answer = await self.ask_gemini(prompt)

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

                self.ctrl.log(f"Erasing previously typed code and preparing clean editor...", "info")
                try:
                    await test_page.bring_to_front()
                except Exception:
                    pass
                await self.prepare_editor_for_code(test_page)
                await asyncio.sleep(0.4)

                is_hidden = await test_page.evaluate("() => document.hidden || document.visibilityState === 'hidden'")
                if not is_hidden:
                    self.ctrl.log(f"Simulating typing ({len(clean_code)} chars)...", "info")
                    try:
                        await test_page.keyboard.type(clean_code, delay=15)
                    except Exception:
                        pass
                else:
                    self.ctrl.log("Minimized/Background mode active: Injecting solution directly into editor...", "info")

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
                    self.ctrl.log("Code synchronized into Monaco (background/minimized mode active)!", "success")
                else:
                    self.ctrl.log("Finished typing code!", "success")

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
                self.ctrl.log("Submitted code. Waiting for test results...", "info")

                await asyncio.sleep(6)
                await self.take_screenshot(f"lab_q{q_idx}_attempt{attempt}")

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
                    self.ctrl.log(f"Challenge {q_idx} PASSED!", "success")
                    break
                else:
                    self.ctrl.log(f"Attempt {attempt} failed. Re-prompting Gemini with error...", "warn")
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

        # Check for overarching Submit / Finish button on last challenge
        self.ctrl.log("Finished challenges. Checking for Submit/Finish...", "info")
        await test_page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, a'));
            const finish = buttons.find(b => {
                const txt = (b.innerText || '').toLowerCase();
                return (txt.includes('submit test') || txt.includes('finish') || txt.includes('end test') || txt.includes('submit assessment')) && !b.disabled;
            });
            if (finish) finish.click();
        }""")
        await asyncio.sleep(2)

        # Cut (close) the lab tabs
        self.ctrl.log("Cutting lab tab and moving to next topic...", "info")
        for p in list(self.context.pages):
            if "/test/" in p.url:
                try:
                    await p.close()
                except Exception:
                    pass

        await self.bytexl_page.bring_to_front()
        await asyncio.sleep(2)

        # Advance if on landing page
        await self.bytexl_page.evaluate("""() => {
            const btn = Array.from(document.querySelectorAll('button, a')).find(b => (b.innerText || '').toLowerCase().includes('next unit'));
            if (btn) btn.click();
        }""")
        await asyncio.sleep(2)
        self.ctrl.log("Coding Lab completed and tab closed.", "success")

    async def handle_theory_reading(self, page):
        """Scrolls smoothly through theory portion, clicks Mark As Completed, and clicks Next >."""
        title = await page.evaluate("() => { const h = document.querySelector('h1, h2, h3, h4, .chapter-title'); return h ? h.innerText.trim() : 'Theory Topic'; }")
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

    async def execute_one_module(self):
        """Navigates to the uncompleted module (or searches targeted course), finishes reading topics, quizzes & labs, ticks side checkboxes, and stops with a report."""
        ok = await self.connect()
        if not ok:
            return False

        self.ctrl.state = "Navigating Course..."

        target_course = (self.ctrl.target_module or "").strip()

        # If a specific target course/module was specified (e.g. "System Design" or "Cloud Security")
        if target_course:
            self.ctrl.log(f"🎯 Target course specified: '{target_course}'. Searching in 'My Courses'...", "info")
            curr_url = self.bytexl_page.url.lower()
            if not curr_url.endswith("/courses") or "/courses/" in curr_url:
                try:
                    await self.bytexl_page.goto("https://app.bytexl.ai/courses")
                    await asyncio.sleep(3)
                except Exception:
                    pass

            try:
                search_input = await self.bytexl_page.wait_for_selector('input[placeholder*="search" i]', timeout=6000)
                if search_input:
                    await search_input.fill("")
                    await search_input.fill(target_course)
                    await self.bytexl_page.keyboard.press("Enter")
                    await asyncio.sleep(2.5)
            except Exception as e:
                self.ctrl.log(f"Search input interaction: {e}", "warn")

            found_card = await self.bytexl_page.evaluate("""(target) => {
                const tLower = target.toLowerCase();
                const cards = Array.from(document.querySelectorAll('.MuiPaper-root, .MuiCard-root, .MuiBox-root')).filter(c => {
                    const txt = (c.innerText || '').toLowerCase();
                    return (txt.includes('completion') || txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) && txt.includes(tLower);
                });
                if (cards.length > 0) {
                    const card = cards[0];
                    const btn = Array.from(card.querySelectorAll('button, a')).find(b => {
                        const txt = (b.innerText || '').toLowerCase();
                        return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                    }) || card.querySelector('button, a') || card;
                    btn.click();
                    return true;
                }
                return false;
            }""", target_course)

            if found_card:
                self.ctrl.log(f"Opened target course: '{target_course}'!", "success")
                await asyncio.sleep(4)
            else:
                self.ctrl.log(f"Target '{target_course}' search finished. Continuing with active course...", "info")

        # Stage 1 (Fallback / default if on /courses): click course card with < 100% completion
        curr_url = self.bytexl_page.url.lower()
        if curr_url.rstrip("/").endswith("/courses"):
            self.ctrl.log("Scanning 'My Courses' for uncompleted courses (< 100%)...", "info")
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass

            target_idx = await self.bytexl_page.evaluate("""() => {
                const btnList = Array.from(document.querySelectorAll('button, a')).filter(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                });
                for (let idx = 0; idx < btnList.length; idx++) {
                    const btn = btnList[idx];
                    let p = btn;
                    for (let i = 0; i < 7; i++) {
                        if (!p.parentElement) break;
                        p = p.parentElement;
                        const txt = p.innerText || '';
                        if (txt.includes('Course completion') || txt.includes('completion')) {
                            const pctMatch = txt.match(/(\\d+)%\\s*(?:Course\\s*)?completion/i) || txt.match(/completion[\\s\\S]*?(\\d+)%/i);
                            const pct = pctMatch ? parseInt(pctMatch[1]) : 0;
                            if (pct < 100) {
                                return idx;
                            }
                        }
                    }
                }
                return 0;
            }""")

            self.ctrl.log(f"Entering Course (button index {target_idx})...", "info")
            await self.bytexl_page.evaluate("""(idx) => {
                const btnList = Array.from(document.querySelectorAll('button, a')).filter(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume');
                });
                if (btnList[idx]) btnList[idx].click();
            }""", target_idx)
            await asyncio.sleep(4)

        # Stage 2: If on course units list (Units and Chapters page: /courses/<id>/<slug>)
        curr_url = self.bytexl_page.url.lower()
        if "/courses/" in curr_url and "/module/" not in curr_url:
            self.ctrl.log("Units & Chapters page detected. Finding uncompleted unit...", "info")
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass

            unit_clicked = await self.bytexl_page.evaluate("""() => {
                // Look for blue 'Continue Learning' or 'Start Learning' or 'Resume' button
                const allButtons = Array.from(document.querySelectorAll('button, a'));
                const actionBtn = allButtons.find(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    return (txt.includes('continue learning') || txt.includes('start learning') || txt.includes('resume')) &&
                           !txt.includes('completed');
                });

                if (actionBtn) {
                    let container = actionBtn.closest('.MuiPaper-root, .MuiCard-root, .MuiBox-root') || actionBtn.parentElement;
                    let unitTitle = 'Unit';
                    if (container) {
                        const titleEl = container.querySelector('h1, h2, h3, h4, h5, h6');
                        if (titleEl) unitTitle = titleEl.innerText.trim();
                        else {
                            const lines = container.innerText.split('\\n').map(l => l.trim()).filter(Boolean);
                            if (lines.length > 0) unitTitle = lines[0];
                        }
                    }
                    actionBtn.click();
                    return unitTitle;
                }
                return null;
            }""")

            if unit_clicked:
                self.ctrl.current_module_name = unit_clicked
                self.ctrl.log(f"Clicked 'Continue Learning' on Unit: {unit_clicked}", "success")
            await asyncio.sleep(4)

        # Stage 3: Inside Module view (.../module/...)
        self.ctrl.state = "Processing Module Activities..."
        try:
            await self.bytexl_page.bring_to_front()
        except Exception:
            pass

        completed_set = set()
        current_activity_name = ""
        activities_completed = 0

        while not self.ctrl.stop_requested:
            # 1. Check if STOP was requested
            if self.ctrl.stop_requested:
                self.ctrl.log("Stop flag detected. Halting automation loop.", "warn")
                break

            # 2. Check if an assessment / test tab is open
            test_page = None
            for p in self.context.pages:
                if "/test/" in p.url:
                    test_page = p
                    break

            if test_page:
                has_editor = await test_page.evaluate("() => !!document.querySelector('.monaco-editor, textarea#code')")
                if has_editor:
                    await self.solve_coding(test_page)
                else:
                    await self.solve_mcq(test_page)

                if current_activity_name:
                    completed_set.add(current_activity_name)
                activities_completed += 1

                # Guarantee test tab is closed
                for p in list(self.context.pages):
                    if "/test/" in p.url:
                        try:
                            await p.close()
                        except Exception:
                            pass
                await asyncio.sleep(2)

                # Post-Activity: Tick the side completion box on the sidebar/accordion!
                try:
                    await self.bytexl_page.bring_to_front()
                except Exception:
                    pass
                await asyncio.sleep(1)

                ticked_res = await self.bytexl_page.evaluate("""(actName) => {
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
                                return { ticked: true, mode: "row_match", name: actName };
                            } else {
                                return { already: true, name: actName };
                            }
                        }
                    }

                    const markBtns = Array.from(document.querySelectorAll('button, span, a')).filter(b => {
                        const txt = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                        return txt.includes('mark as complete') || txt.includes('mark complete');
                    });
                    if (markBtns.length > 0) {
                        markBtns[0].click();
                        return { ticked: true, mode: "main_button" };
                    }

                    return { ticked: false };
                }""", current_activity_name)

                if ticked_res.get("ticked"):
                    self.ctrl.log(f"✅ Ticked side completion box for '{current_activity_name}'!", "success")

                await asyncio.sleep(1.5)

                # Open next submodule: Expand any collapsed accordion sections
                await self.bytexl_page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button.MuiAccordionSummary-root[aria-expanded="false"], .MuiAccordionSummary-root:not(.Mui-expanded)'));
                    buttons.forEach(b => b.click());
                }""")
                await asyncio.sleep(1)
                continue

            # 3. Check if on an Activity landing page with "Open Quiz...", "Open Lab...", "Take The Quiz Now!"
            try:
                await self.bytexl_page.bring_to_front()
            except Exception:
                pass

            action_info = await self.bytexl_page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                const openBtn = btns.find(b => {
                    const txt = (b.innerText || '').toLowerCase();
                    return (txt.includes('open quiz') || txt.includes('take the quiz') || txt.includes('open lab') ||
                            txt.includes('start lab') || txt.includes('start quiz') || txt.includes('take quiz')) && !b.disabled;
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

            if action_info.get("openText"):
                open_lower = action_info['openText'].lower()
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
                    const hasCompletedResult = bodyText.includes('highest score') ||
                                               bodyText.includes('quiz completed') ||
                                               bodyText.includes('you scored') ||
                                               bodyText.includes('already submitted');

                    return {
                        isChecked: isChecked,
                        hasCompletedResult: hasCompletedResult
                    };
                }""", current_activity_name)

                # 1. If checkbox is already ticked -> ignore it, it is already completed!
                if check_status.get("isChecked"):
                    self.ctrl.log(f"Side checkbox for '{current_activity_name or 'quiz'}' is already ticked (Completed). Ignoring and advancing to next topic...", "info")
                    if current_activity_name:
                        completed_set.add(current_activity_name)
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

                # 2. If quiz has completed results on page but checkbox not checked -> tick it and advance!
                if check_status.get("hasCompletedResult") and not check_status.get("isChecked"):
                    self.ctrl.log(f"Quiz '{current_activity_name or 'quiz'}' shows completed results. Ticking side checkbox...", "success")
                    await self.bytexl_page.evaluate("""(name) => {
                        const cleanName = (name || '').trim().toLowerCase();
                        const allItems = Array.from(document.querySelectorAll(
                            '.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, .MuiListItem-root, a, div[role="button"]'
                        ));
                        let row = allItems.find(el => {
                            const t = (el.innerText || '').trim().toLowerCase();
                            return cleanName && (t.includes(cleanName) || cleanName.includes(t.split('\\n')[0]));
                        }) || document.querySelector('.MuiListItem-root.Mui-selected, .Mui-selected');
                        if (row) {
                            const cb = row.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], [data-testid*="Check"]');
                            if (cb && cb.click) cb.click();
                        }
                    }""", current_activity_name)
                    if current_activity_name:
                        completed_set.add(current_activity_name)
                    await asyncio.sleep(2)
                    continue

                # 3. Assessment is not completed -> launch and solve it!
                self.ctrl.log(f"Launching Assessment: '{action_info['openText']}'...", "info")
                await self.bytexl_page.evaluate("""(txt) => {
                    const btns = Array.from(document.querySelectorAll('button, a'));
                    const target = btns.find(b => (b.innerText || '').trim().toLowerCase() === txt.toLowerCase()) ||
                                   document.querySelector('button.MuiButton-containedPrimary');
                    if (target) target.click();
                }""", action_info['openText'])
                await asyncio.sleep(5)
                continue

            # 4. Check if on a Theory / Reading Page (see user screenshot media_1788603167172.png & media_1788603197643.png)
            is_theory_page = await self.bytexl_page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
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
                if current_activity_name:
                    completed_set.add(current_activity_name)
                activities_completed += 1
                if advanced:
                    continue

            # 5. Check if all activities in unit done and "Go To Next Unit" present
            if action_info.get("hasNextUnit") and activities_completed > 0:
                self.ctrl.log("Submodules in unit finished! Advancing: 'Go To Next Unit'...", "success")
                await self.bytexl_page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button, a')).find(b => (b.innerText || '').toLowerCase().includes('next unit'));
                    if (btn) btn.click();
                }""")
                await asyncio.sleep(3)
                continue

            # 6. On Submodule Accordions view: Expand submodules & find next uncompleted topic (Reading or Quiz/Lab)
            uncompleted = await self.bytexl_page.evaluate("""(doneList) => {
                // Expand all collapsed submodule accordions first
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

                    // Skip if in doneList
                    if (doneList.some(d => titleLine.includes(d) || (d && d.includes(titleLine)))) continue;

                    // Skip if parent submodule accordion is marked as "Completed"
                    const accordion = row.closest('.MuiAccordion-root');
                    if (accordion) {
                        const accSummary = accordion.querySelector('.MuiAccordionSummary-root');
                        if (accSummary && (accSummary.innerText || '').toLowerCase().includes('completed')) {
                            continue;
                        }
                    }

                    // Check if checked
                    const cb = row.querySelector('input[type="checkbox"], .MuiCheckbox-root, [data-testid*="CheckBox"], span[aria-label*="complete"]');
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
            }""", list(completed_set))

            if uncompleted:
                current_activity_name = uncompleted['text']
                self.ctrl.log(f"Opening topic: '{current_activity_name}'", "info")
                clicked = await self.bytexl_page.evaluate("""(name) => {
                    const links = Array.from(document.querySelectorAll('.MuiAccordion-root a, .MuiAccordion-root .MuiListItem-root, a, .MuiListItem-root, [role="button"]'));
                    const target = links.find(l => (l.innerText || '').includes(name));
                    if (target) {
                        const t = target.querySelector('.MuiListItemText-root') || target;
                        t.click();
                        return true;
                    }
                    return false;
                }""", current_activity_name)

                if not clicked and uncompleted.get("href"):
                    await self.bytexl_page.goto(uncompleted["href"])
                await asyncio.sleep(4)
            else:
                self.ctrl.log("All topics, quizzes, and labs in this unit are completed!", "success")
                break

        # Finished 1 Module!
        mod_name = self.ctrl.current_module_name or target_course or "Module"
        self.ctrl.state = f"Completed {mod_name} (100%)"
        self.ctrl.log(f"🎉 Completed {mod_name} with 100%!", "success")

        # Set user report with action buttons
        self.ctrl.pending_report = f"""
        <div class="report-card">
            <h4>🎉 Module Completed: {mod_name} (100%)</h4>
            <p>All quizzes, lab challenges, and submodules have been completed and side boxes ticked!</p>
            <p style="margin-top: 8px; color: var(--text-heading); font-weight: 500;">
                What would you like to do next?
            </p>
            <div class="action-buttons">
                <button class="btn-action btn-proceed" onclick="sendQuick('proceed with next module')">Proceed with Next Module</button>
                <button class="btn-action btn-stop" onclick="sendQuick('stop')">Stop & End Session</button>
            </div>
        </div>
        """
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
        "recent_logs": controller.logs[-25:],
        "pending_report": controller.pending_report,
        "latest_screenshot_timestamp": int(time.time()) if controller.latest_screenshot else None,
        "cdp_port": controller.cdp_port,
        "port_error": controller.port_error,
        "browser": controller.last_detected_browser,
        "target_module": controller.target_module
    })

@app.route("/api/set_target", methods=["POST"])
def set_target():
    data = request.json or {}
    target = (data.get("target") or "").strip()
    controller.target_module = target
    if target:
        controller.log(f"🎯 Target course set to: '{target}'. Automation will focus on this course.", "info")
    else:
        controller.log("Target cleared. Normal course traversal active.", "info")
    return jsonify({"success": True, "target": controller.target_module})

@app.route("/api/ports", methods=["GET"])
def get_ports():
    current = controller.cdp_port or 9222
    ok, b_name = check_cdp_port(current, timeout=0.8)

    candidates = [9222, 9223, 9224, 9225, 9226, 9227, 9228, 9229, 9333, 9555]
    active = []
    for p in candidates:
        is_ok, browser = check_cdp_port(p, timeout=0.25)
        if is_ok:
            active.append({"port": p, "browser": browser})

    free_p = find_free_port(9222)
    return jsonify({
        "current_port": current,
        "is_current_active": ok,
        "current_browser": b_name,
        "active_ports": active,
        "recommended_free_port": free_p,
        "port_error": controller.port_error
    })

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
    if not req_port:
        req_port = find_free_port(controller.cdp_port or 9222)
    else:
        req_port = int(req_port)

    controller.cdp_port = req_port
    controller.port_error = False
    launch_browser_on_port(req_port)
    controller.log(f"Launched browser on port {req_port}.", "info")
    return jsonify({"success": True, "port": req_port})

@app.route("/api/restart_browser", methods=["POST"])
def api_restart_browser():
    data = request.json or {}
    port = int(data.get("port", controller.cdp_port or 9222))
    exe = find_installed_browser()
    proc_name = os.path.basename(exe)
    controller.log(f"Starting {proc_name} with remote debugging on port {port}...", "info")
    launch_browser_on_port(port)
    time.sleep(2)
    controller.cdp_port = port
    controller.port_error = False
    controller.log(f"Started {proc_name} with remote debugging on port {port}.", "success")
    return jsonify({"success": True, "port": port})

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
            "reply": "⏹ <strong>Session stopped.</strong><br>Agent has paused. Whenever you are ready to resume, just click or type <strong>'proceed with bytexl'</strong> or <strong>'proceed with next module'</strong>."
        })

    # 2. Clear target command
    if msg in ["clear target", "reset target", "remove target"]:
        controller.target_module = ""
        controller.log("Target cleared.", "info")
        return jsonify({"reply": "Target course cleared. Normal course traversal is now active."})

    # 3. Target command (e.g. "proceed with system design", "target cloud security")
    target_match = re.search(r'^(?:proceed\s+with|target|solve|complete|search)\s+(.+)$', msg, re.IGNORECASE)
    if target_match:
        extracted = target_match.group(1).strip()
        if extracted.lower() not in ["bytexl", "next module", "the course"]:
            controller.target_module = extracted
            controller.log(f"🎯 Target course set to: '{controller.target_module}'", "info")

    # 4. Start / Proceed command
    is_start = any(k in msg for k in ["proceed with bytexl", "proceed with next module", "start", "resume", "run"]) or target_match
    if is_start:
        if controller.state.startswith("Processing") or controller.state.startswith("Navigating"):
            return jsonify({"reply": f"The agent is already actively working{' on target: ' + controller.target_module if controller.target_module else ''}!"})

        controller.stop_requested = False
        controller.state = "Starting Automation..."
        prefix = f"🎯 Target: <strong>{controller.target_module}</strong><br>" if controller.target_module else ""
        controller.log(f"Received instruction: '{raw_msg}'. Starting autonomous solver...", "info")

        t = threading.Thread(target=run_agent_thread, daemon=True)
        controller.running_thread = t
        t.start()

        return jsonify({
            "reply": f"🚀 <strong>Autonomous Agent Started!</strong><br>{prefix}Opening module, scrolling theory topics, clicking Mark As Completed, solving Quizzes & Labs, and advancing through all submodules."
        })

    else:
        return jsonify({
            "reply": f"I received: <em>\"{raw_msg}\"</em>.<br>You can target a specific course (e.g. <code>proceed with system design</code>), or click <strong>'proceed with bytexl'</strong> / <strong>'stop'</strong>."
        })

if __name__ == "__main__":
    port = 5000
    url = f"http://localhost:{port}"
    print(f"\n========================================================")
    print(f"[*] ByteXL Automation Copilot Web UI running at: {url}")
    print(f"========================================================\n")

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=False)
