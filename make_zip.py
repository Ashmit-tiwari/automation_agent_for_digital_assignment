import os
import zipfile

ZIP_NAME = "ByteXL_Automation_Agent.zip"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ZIP_PATH = os.path.join(BASE_DIR, ZIP_NAME)

INCLUDE_FILES = [
    "app.py",
    "bytexl_agent.py",
    "requirements.txt",
    "README.md",
    "start_ui.bat",
    "run_agent.bat",
    "launch_debug_browser.bat",
]

INCLUDE_DIRS = [
    "templates"
]

print(f"Creating {ZIP_PATH}...")
with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
    for f in INCLUDE_FILES:
        fp = os.path.join(BASE_DIR, f)
        if os.path.exists(fp):
            zf.write(fp, arcname=f)
            print(f"  + Added file: {f}")

    for d in INCLUDE_DIRS:
        dp = os.path.join(BASE_DIR, d)
        if os.path.exists(dp):
            for root, dirs, files in os.walk(dp):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, BASE_DIR)
                    zf.write(full_path, arcname=rel_path)
                    print(f"  + Added file: {rel_path}")

print(f"\n[OK] Successfully generated {ZIP_PATH} ({os.path.getsize(ZIP_PATH)} bytes)")

