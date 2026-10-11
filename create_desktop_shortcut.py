import os
import sys

def create_shortcut():
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if not os.path.exists(desktop):
        print(f"[!] Desktop directory not found at {desktop}")
        return False

    shortcut_path = os.path.join(desktop, "Brave (ByteXL Agent Ready).lnk")

    brave_candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
    ]

    brave_exe = None
    for cand in brave_candidates:
        if os.path.exists(cand):
            brave_exe = cand
            break

    if not brave_exe:
        print("[!] Brave executable not found on system.")
        return False

    flags = "--remote-debugging-port=9222 --remote-allow-origins=* --restore-last-session --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding"

    vbs_script = f"""
Set oWS = WScript.CreateObject("WScript.Shell")
sLinkFile = "{shortcut_path}"
Set oLink = oWS.CreateShortcut(sLinkFile)
oLink.TargetPath = "{brave_exe}"
oLink.Arguments = "{flags}"
oLink.IconLocation = "{brave_exe},0"
oLink.Description = "Brave Browser with ByteXL Automation Agent Ready (Port 9222)"
oLink.WorkingDirectory = "{os.path.dirname(brave_exe)}"
oLink.Save
"""
    vbs_path = os.path.join(os.path.dirname(__file__), "_temp_create_shortcut.vbs")
    try:
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_script)
        ret = os.system(f'cscript //nologo "{vbs_path}"')
        if os.path.exists(vbs_path):
            os.remove(vbs_path)
        if os.path.exists(shortcut_path):
            print(f"[OK] Successfully created shortcut on Desktop: {shortcut_path}")
            return True
        else:
            print("[!] Shortcut creation failed.")
            return False
    except Exception as e:
        print(f"[!] Error creating shortcut: {e}")
        return False

if __name__ == "__main__":
    create_shortcut()
