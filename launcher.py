# launcher_ui.py
# Build: pyinstaller --onefile --noconsole --name "MyAppLauncher" launcher_ui.py
import json, os, shutil, subprocess, sys, tempfile, time, hashlib, threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

import tkinter as tk
from tkinter import ttk

# -------------- CONFIG --------------
APP_NAME = "main"
MAIN_EXE_NAME = "main.exe"   # the app you actually run after update
LOCAL_VERSION_FILE = "app.version"  # sidecar text file with version (e.g., "1.5.2")

# Host a tiny JSON manifest per platform/channel. Example schema further below.
MANIFEST_URL = "https://www.dropbox.com/scl/fi/8su28scnhsuoba929s39o/manifest.json?rlkey=rj1y29n9s5ujy7mwl7pan78x7&st=8y8catcx&dl=1"
REQUEST_TIMEOUT = 15  # seconds
USER_AGENT = f"{APP_NAME}Launcher/1.0"

LAUNCHER_LOG = "launcher.log"

from brightpearl.settings import get_settings

# -------------- LOGGING & PATHS --------------
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _log(msg: str):
    try:
        log_dir = get_settings().log_output_dir
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            p = os.path.join(log_dir, LAUNCHER_LOG)
        else:
            p = os.path.join(app_dir(), LAUNCHER_LOG)
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except Exception:
        pass

# -------------- VERSION UTILS --------------
def read_local_version() -> str:
    p = os.path.join(app_dir(), LOCAL_VERSION_FILE)
    try:
        with open(p, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or "0.0.0"
    except FileNotFoundError:
        return "0.0.0"
    except Exception:
        return "0.0.0"

def write_local_version(version: str):
    p = os.path.join(app_dir(), LOCAL_VERSION_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(version.strip())
    except Exception as e:
        _log(f"Failed to write local version: {e}")

def parse_version(v: str):
    parts = []
    for x in v.split("."):
        try:
            parts.append(int(x))
        except ValueError:
            num = "".join(c for c in x if c.isdigit())
            parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])

def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)

# -------------- HTTP / FILE UTILS --------------
def http_get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        data = r.read()
    text = data.decode("utf-8", errors="replace")
    return json.loads(text)

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def replace_file_atomic(src_path: str, dst_path: str):
    bak_path = dst_path + ".bak"
    try:
        if os.path.exists(bak_path):
            try: os.remove(bak_path)
            except Exception: pass
        if os.path.exists(dst_path):
            os.replace(dst_path, bak_path)
        os.replace(src_path, dst_path)
        try:
            if os.path.exists(bak_path):
                os.remove(bak_path)
        except Exception:
            pass
    finally:
        if os.path.exists(src_path):
            try: os.remove(src_path)
            except Exception: pass

def launch_main():
    target = os.path.join(app_dir(), MAIN_EXE_NAME)
    if not os.path.exists(target):
        _log(f"ERROR: {MAIN_EXE_NAME} not found; nothing to launch.")
        return False
    try:
        if sys.platform.startswith("win"):
            DETACHED_PROCESS = 0x00000008
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen([target], cwd=app_dir(),
                             creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
        else:
            subprocess.Popen([target], cwd=app_dir())
        return True
    except Exception as e:
        _log(f"Failed to launch main EXE: {e}")
        return False

def human_bytes(n: int) -> str:
    units = ["B","KB","MB","GB","TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units)-1:
        f /= 1024.0
        i += 1
    return f"{f:.1f} {units[i]}"

# -------------- UI --------------
class LauncherUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — Launcher")
        self.root.geometry("460x150")
        self.root.resizable(False, False)

        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        self.status = tk.StringVar(value="Starting…")
        self.detail = tk.StringVar(value="")
        self.percent = tk.StringVar(value="0%")

        ttk.Label(frm, textvariable=self.status, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frm, textvariable=self.detail).pack(anchor="w", pady=(2,8))

        self.pb = ttk.Progressbar(frm, orient="horizontal", mode="determinate", length=420, maximum=100)
        self.pb.pack()
        self.perc_label = ttk.Label(frm, textvariable=self.percent)
        self.perc_label.pack(anchor="e")

        self.cancel_btn = ttk.Button(frm, text="Cancel", command=self._cancel)
        self.cancel_btn.pack(anchor="e", pady=(8,0))

        self._cancelled = False

        # kick off work
        self.root.after(50, self.start)

    def _cancel(self):
        self._cancelled = True
        self.status.set("Cancelled.")
        self.detail.set("")
        self.root.after(450, self.root.destroy)

    def set_status(self, text: str):
        self.status.set(text); self.root.update_idletasks()

    def set_detail(self, text: str):
        self.detail.set(text); self.root.update_idletasks()

    def set_progress(self, pct: float, downloaded: int = None, total: int = None):
        pct = max(0.0, min(100.0, pct))
        self.pb["value"] = pct
        if downloaded is not None and total is not None and total > 0:
            self.percent.set(f"{pct:.0f}%  ({human_bytes(downloaded)} / {human_bytes(total)})")
        else:
            self.percent.set(f"{pct:.0f}%")
        self.root.update_idletasks()

    def start(self):
        t = threading.Thread(target=self._work, daemon=True)
        t.start()

    # ---- main worker thread ----
    def _work(self):
        local_version = read_local_version()
        _log(f"Local version: {local_version}")
        self.set_status("Checking for updates…")
        self.set_detail(f"Installed version: {local_version}")
        self.set_progress(5)

        manifest = {}
        try:
            manifest = http_get_json(MANIFEST_URL)
        except Exception as e:
            _log(f"Manifest fetch error: {e}")
            manifest = {}

        remote_version = manifest.get("version", "0.0.0")
        dl_url = manifest.get("url", "")
        expected_sha = (manifest.get("sha256") or "").lower()
        remote_filename = manifest.get("filename", MAIN_EXE_NAME)
        _log(f"Remote version: {remote_version}")

        if dl_url and is_newer(remote_version, local_version):
            self.set_status(f"Update available — {remote_version}")
            self.set_detail("Preparing download…")
            self.set_progress(10)

            tmp_dir = tempfile.mkdtemp(prefix="launcher_dl_")
            tmp_path = os.path.join(tmp_dir, remote_filename)

            # streamed download with progress
            try:
                req = Request(dl_url, headers={"User-Agent": USER_AGENT})
                with urlopen(req, timeout=REQUEST_TIMEOUT) as r, open(tmp_path, "wb") as f:
                    total = r.length  # content-length or None
                    downloaded = 0
                    chunk = 1024 * 128
                    last_update = time.time()
                    if total is None:
                        # indeterminate progress when no content-length
                        self.pb.configure(mode="indeterminate")
                        self.pb.start(10)
                        self.set_detail("Downloading (size unknown)…")
                    else:
                        self.set_detail(f"Downloading {human_bytes(total)}…")

                    while True:
                        if self._cancelled: raise RuntimeError("Download cancelled by user.")
                        data = r.read(chunk)
                        if not data: break
                        f.write(data)
                        downloaded += len(data)

                        # update at ~30ms intervals to keep smooth
                        if total:
                            now = time.time()
                            if now - last_update > 0.03:
                                pct = (downloaded / total) * 100.0
                                self.set_progress(pct, downloaded, total)
                                last_update = now

                    if total:
                        self.set_progress(100.0, downloaded, total)
                    else:
                        self.pb.stop()
                        self.pb.configure(mode="determinate")
                        self.set_progress(100.0)

                self.set_status("Verifying download…")
                self.set_detail("Checking file integrity (SHA-256)…")
                _log("Download complete. Verifying SHA-256...")
                actual = sha256_file(tmp_path).lower()
                if expected_sha and expected_sha != actual:
                    _log(f"Hash mismatch! expected={expected_sha} actual={actual}")
                    raise RuntimeError("Downloaded file hash mismatch")

                # Replace and relaunch
                target_path = os.path.join(app_dir(), MAIN_EXE_NAME)
                if os.path.basename(tmp_path) != os.path.basename(target_path):
                    new_tmp = os.path.join(tmp_dir, os.path.basename(target_path))
                    shutil.move(tmp_path, new_tmp)
                    tmp_path = new_tmp

                self.set_status("Installing update…")
                self.set_detail("Applying files…")
                self.set_progress(100.0)
                replace_file_atomic(tmp_path, target_path)
                write_local_version(remote_version)
                _log("Update applied successfully.")

            except Exception as e:
                _log(f"Update failed: {e}. Falling back to existing EXE.")
                # keep going and try to launch whatever we have
            finally:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
        else:
            self.set_status("You’re on the latest version.")
            self.set_detail(f"Latest: {remote_version}")
            self.set_progress(100.0)

        # Launch the app either way
        self.set_status("Launching application…")
        self.set_detail(MAIN_EXE_NAME)
        ok = launch_main()
        # Close the UI shortly after launch (or show error if nothing to run)
        if ok:
            self.root.after(600, self.root.destroy)
        else:
            self.set_status("Launch failed")
            self.set_detail(f"Could not start {MAIN_EXE_NAME}. See launcher.log.")
            # Leave window open for the user to read
            self.cancel_btn.configure(text="Close")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    ui = LauncherUI()
    ui.run()
