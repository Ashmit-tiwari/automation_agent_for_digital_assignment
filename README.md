# ByteXL Automation Copilot

An autonomous agent and Web UI built to solve ByteXL practice modules, quizzes (MCQs), and coding labs (SQL, Python, etc.) with Google Gemini integration.

---

## 🚀 Key Features

1. **Autonomous Course & Module Navigation**:
   - Scans "My Courses" and skips any course $\ge 95\%$ completed.
   - Automatically navigates into uncompleted courses (e.g. *RDBMS*, *Cloud Security*, *System Design*).
   - Enters uncompleted units (e.g. *SQL Essentials*).
   - **Skips reading materials** (book icon topics) and targets only uncompleted **Quizzes** and **Labs** (empty checkboxes).

2. **MCQ Quiz Solver**:
   - Queries Google Gemini in the strict format (`Question`, `Options A-D`, asking for option letter and short reason).
   - Selects the correct radio option on ByteXL.
   - Automatically takes a screenshot and advances.
   - When all 20 questions are done (or Next is disabled), submits if available, closes the test tab, and advances.

3. **Coding & SQL Lab Solver**:
   - **Simulates natural character-by-character keyboard typing** (`delay=20ms`) to satisfy anti-cheat and keystroke recording.
   - Automatically strips leading markdown language identifiers (e.g. `SQL`, `PostgreSQL`, `python`).
   - Clicks **Submit** and verifies test results.
   - Automatically re-prompts Gemini with error details on test failures (up to 3 retries).
   - **Auto-Close ("Cut Tab")**: Once all challenges are finished, checks for overarching Submit/Finish, closes all `/test/` tabs, and advances to the next sub-topic.

4. **100% Module Gating**:
   - After completing all quizzes and labs in a module, pauses and presents a 100% completion report with interactive buttons:
     - `[Proceed with Next Module]`
     - `[Stop & End Session]`

5. **Storage Management**:
   - Rolling screenshot cleanup permanently keeps only the **latest 30 screenshots**, capping folder size under **~2 to 3 MB**.

---

## 💻 Quick Start

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Launch Browser with Remote Debugging
Double-click:
`launch_debug_browser.bat`
*(Or in PowerShell)*:
```powershell
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
```
In that window, make sure you are logged into **ByteXL** (`https://app.bytexl.ai/courses`) and **Gemini** (`https://gemini.google.com/app`).

### 3. Start the Web UI
Double-click:
`start_ui.bat`
*(Or in PowerShell)*:
```powershell
python app.py
```
Your browser will open to: **http://localhost:5000**

Click **`▶ proceed with bytexl`** in the chat and let the agent work!

---

## 📁 File Structure

- `app.py` - Flask server, Web UI backend, and autonomous agent controller.
- `bytexl_agent.py` - Core Playwright CDP automation engine with Gemini integration.
- `templates/index.html` - Modern dark-mode web chat dashboard with live logs and screenshot feed.
- `launch_debug_browser.bat` - 1-Click launcher for Brave/Chrome with remote debugging on port 9222.
- `start_ui.bat` - 1-Click launcher for the Web UI.
- `run_agent.bat` - 1-Click CLI runner for the automation agent.
- `screenshots/` - Rolling folder holding the latest verification screenshots.

