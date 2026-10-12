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

### 2. Connect to Your Existing Browser
- **Option A (Automatic)**: Just start the Web UI (`start_ui.bat`) and click **"▶ proceed with brave browser"** (or Chrome/Edge). If your browser is already open, the agent seamlessly enables remote debugging on port 9222 and restores all your open tabs, saved passwords, and ByteXL login!
- **Option B (Batch Script)**: Double-click `launch_existing_brave.bat` or `launch_with_existing_profile.bat`. It checks if port 9222 is active, gracefully restores all open tabs, and starts your browser with remote debugging on your default profile.
- **Option C (Command Line)**:
```powershell
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222 --restore-last-session
```
Once connected, you can minimize the browser anytime; the agent runs in the background while you do your other work!

### 3. Start the Web UI
Double-click:
`start_ui.bat`
*(Or in PowerShell)*:
```powershell
python app.py
```
Your browser will open to: **http://localhost:5000**

- To solve a specific course, type: `proceed with RDBMS` or `proceed with Cloud Security` (or use the target input bar). Acronyms like `RDBMS`, `DBMS`, `DSA`, `AZ-900` are automatically resolved to full course names.
- Or simply click **`▶ proceed with bytexl`** / **`▶ proceed with brave browser`** to solve uncompleted modules automatically!

---

## 📁 File Structure

- `app.py` - Flask server, Web UI backend, course resolution, and autonomous agent controller.
- `bytexl_agent.py` - Core Playwright CDP automation engine with Gemini integration.
- `gemini_brain.py` - Gemini AI Brain for quiz solving, vision, and acronym/course catalog matching.
- `launch_existing_brave.bat` - Launches Brave with remote debugging on default profile with session restoration.
- `launch_with_existing_profile.bat` - Launches any chosen browser (Chrome, Edge, Brave) with existing profile.
- `templates/index.html` - Modern dark-mode web chat dashboard with live logs, target course bar, and screenshot feed.
- `start_ui.bat` - 1-Click launcher for the Web UI.
- `run_agent.bat` - 1-Click CLI runner for the automation agent.
- `screenshots/` - Rolling folder holding the latest verification screenshots.

