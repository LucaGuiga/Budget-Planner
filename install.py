"""Install Budget Planner for this user and put a shortcut on the Desktop.

In the zip it sits in the "app" folder with the rest of the app, and the
"Install on ..." files next to that folder run it. It works out whether this
is macOS, Windows or Linux, copies the app into the usual place for programs
on that system, and makes a shortcut that starts it. Installing again updates
the app and keeps the saved budget.

To load a budget someone sent you, save their budget .json file into
Downloads (or next to the installer) and run the installer. It picks the
newest file named budget*.json, backs up the budget you have, and loads it.
A file that was already loaded is skipped, so reinstalling later never
undoes your own changes.
"""

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

NAME = "Budget Planner"
SRC = Path(__file__).resolve().parent   # the app files are next to this one
INSTALLER_DIR = SRC.parent if SRC.name == "app" else SRC   # where the "Install on ..." files are
sys.path.insert(0, str(SRC))   # for budget_core and launch, however this was started
COMMON_FILES = [
    "budget.py", "budget_core.py", "budget_actions.py", "launch.py", "README.md", "static/index.html",
]
ICON_FILES = {  # each system only needs its own icon
    "darwin": ["launcher/BudgetPlanner.icns"],
    "win32": ["launcher/icon.ico"],
    "linux": ["launcher/icon-256.png"],
}
APP_FILES = COMMON_FILES + ICON_FILES.get(sys.platform, ICON_FILES["linux"])


def pause():
    """Double-clicking the installer on Windows opens a window that would
    close the moment it finishes; keep it up so the result can be read.
    The .bat installer pauses on its own, so it turns this off."""
    if sys.platform == "win32" and sys.stdin and sys.stdin.isatty() \
            and not os.environ.get("BUDGET_PLANNER_NO_PAUSE"):
        input("\nPress Enter to close.")


def fail(message):
    print(f"\nCould not install: {message}")
    pause()
    sys.exit(1)


def install_dir():
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / NAME
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / NAME
    data_home = os.environ.get("XDG_DATA_HOME") or home / ".local" / "share"
    return Path(data_home) / "budget-planner"


def copy_app(dest):
    missing = [f for f in APP_FILES if not (SRC / f).exists()]
    if missing:
        fail("these files are missing: " + ", ".join(missing)
             + '. Unzip the whole "Budget Planner" folder, then run the installer inside it.')
    for f in APP_FILES:
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SRC / f, target)
    (dest / "data").mkdir(exist_ok=True)


# ---------------------------------------------------------------- loading a budget

def find_budget_file(installer_dir):
    """The newest budget*.json next to the installer, one folder up (the
    unzipped folder may sit inside another), or in Downloads. None if there's none."""
    folders = [installer_dir, installer_dir.parent, Path.home() / "Downloads"]
    found = {f.resolve() for d in folders if d.is_dir() for f in d.glob("budget*.json")
             if f.is_file() and not f.name.startswith("budget.backup")}
    return max(found, key=lambda f: f.stat().st_mtime, default=None)


def check_budget_file(path):
    """Read and check a budget file. Returns its bytes, or stops with why it can't be used."""
    import budget_core as core
    try:
        raw = path.read_bytes()
        core.validate(core.migrate(json.loads(raw.decode("utf-8"))))
    except (OSError, ValueError) as e:
        fail(f"{path.name} isn't a budget this app can read ({e}). Nothing was changed.")
    return raw


def load_budget(raw, dest):
    """Make raw the saved budget, keeping a copy of the one it replaces.
    Returns False if this exact file was loaded before."""
    data = dest / "data"
    stamp = data / "last-loaded.txt"
    digest = hashlib.sha256(raw).hexdigest()
    if stamp.exists() and stamp.read_text().strip() == digest:
        return False
    current = data / "budget.json"
    if current.exists():
        keep = data / f"budget.before-load-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
        shutil.copyfile(current, keep)
        print(f"Saved your previous budget as {keep.name}")
    tmp = current.with_suffix(".json.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, current)
    stamp.write_text(digest)
    return True


def make_executable(path):
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------- macOS

def install_mac(dest):
    py = shlex.quote(sys.executable)
    # The terminal window the planner runs in. Closing it stops the planner.
    command = dest / "launcher" / "Start Budget Planner.command"
    command.write_text(f"#!/bin/zsh\ncd {shlex.quote(str(dest))} || exit 1\nexec {py} budget.py\n")
    make_executable(command)

    app = Path.home() / "Desktop" / f"{NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    shutil.copyfile(dest / "launcher" / "BudgetPlanner.icns",
                    app / "Contents" / "Resources" / "BudgetPlanner.icns")
    (app / "Contents" / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{NAME}</string>
  <key>CFBundleDisplayName</key><string>{NAME}</string>
  <key>CFBundleIdentifier</key><string>local.budget-planner.launcher</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>BudgetPlanner</string>
  <key>CFBundleIconFile</key><string>BudgetPlanner</string>
  <key>LSUIElement</key><true/>
</dict></plist>
""")
    exe = app / "Contents" / "MacOS" / "BudgetPlanner"
    exe.write_text(f"#!/bin/zsh\nexec {py} {shlex.quote(str(dest / 'launch.py'))}\n")
    make_executable(exe)
    # Downloaded files are flagged by macOS; the installed copy shouldn't be.
    subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(dest)],
                   stderr=subprocess.DEVNULL)
    # Ad-hoc signature so macOS runs it cleanly, then refresh the Finder icon.
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["/usr/bin/touch", str(app)])
    return [app]


# ---------------------------------------------------------------- Windows

def _powershell(script, env):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        env={**os.environ, **env}, capture_output=True, text=True)


def install_windows(dest):
    exe = Path(sys.executable)
    pythonw = exe.parent / "pythonw.exe"   # runs without a blank console window
    target = pythonw if pythonw.exists() else exe
    # Ask Windows where the Desktop is: it can be moved, for example into OneDrive.
    found = _powershell("[Environment]::GetFolderPath('Desktop')", {})
    desktop = Path(found.stdout.strip() or Path.home() / "Desktop")
    start_menu = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") \
        / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    # Paths travel as environment variables so no quoting can go wrong.
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BP_LNK); "
              "$s.TargetPath = $env:BP_TARGET; $s.Arguments = $env:BP_ARGS; "
              "$s.WorkingDirectory = $env:BP_DIR; $s.IconLocation = $env:BP_ICON; "
              "$s.Description = 'Plan a term budget month by month'; $s.Save()")
    made = []
    for folder in (desktop, start_menu):
        lnk = folder / f"{NAME}.lnk"
        result = _powershell(script, {
            "BP_LNK": str(lnk), "BP_TARGET": str(target),
            "BP_ARGS": f'"{dest / "launch.py"}"', "BP_DIR": str(dest),
            "BP_ICON": str(dest / "launcher" / "icon.ico")})
        if result.returncode == 0 and lnk.exists():
            made.append(lnk)
        else:
            print(f"Couldn't make the shortcut in {folder}: {result.stderr.strip()}")
    return made


# ---------------------------------------------------------------- Linux

def _desktop_quote(arg):
    """Quote one argument for the Exec line of a .desktop file."""
    for ch in '\\"`$':
        arg = arg.replace(ch, "\\" + ch)
    return f'"{arg}"'


def install_linux(dest):
    entry = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={NAME}",
        "Comment=Plan a term budget month by month",
        f"Exec={_desktop_quote(sys.executable)} {_desktop_quote(str(dest / 'launch.py'))}",
        f"Icon={dest / 'launcher' / 'icon-256.png'}",
        "Terminal=false",
        "Categories=Office;Finance;",
        "",
    ])
    try:
        found = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
        desktop = Path(found.stdout.strip()) if found.returncode == 0 and found.stdout.strip() \
            else Path.home() / "Desktop"
    except OSError:
        desktop = Path.home() / "Desktop"
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    made = []
    for folder in (data_home / "applications", desktop):
        if folder == desktop and not desktop.is_dir():
            print(f"No Desktop folder at {desktop}, so the shortcut is only in the app menu.")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "budget-planner.desktop"
        path.write_text(entry)
        make_executable(path)
        made.append(path)
        try:  # GNOME won't launch Desktop shortcuts it doesn't trust
            subprocess.run(["gio", "set", str(path), "metadata::trusted", "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    return made


# ---------------------------------------------------------------- main

def main():
    if sys.version_info < (3, 9):
        fail("Budget Planner needs Python 3.9 or newer.")
    dest = install_dir()
    if dest.resolve() == SRC:
        fail("this is already the installed copy. Run the installer from the download.")
    installer_dir = INSTALLER_DIR
    budget_file = find_budget_file(installer_dir)
    raw = check_budget_file(budget_file) if budget_file else None
    if raw is not None:
        import launch
        if launch.running_url(dest / "data" / "server.json"):
            fail(f"{NAME} is open. Close its window, then run this installer again.")
    system = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
    print(f"Installing {NAME} for {system} into:\n  {dest}")
    try:
        copy_app(dest)
        shortcuts = {"darwin": install_mac, "win32": install_windows}.get(
            sys.platform, install_linux)(dest)
        if raw is not None:
            if load_budget(raw, dest):
                print(f"\nLoaded the budget from {budget_file}")
            else:
                print(f"\n{budget_file.name} was already loaded, so your budget was left as is.")
        elif not (dest / "data" / "budget.json").exists():
            print("\nNo budget.json found in Downloads, so the planner starts empty.\n"
                  "To load one, save it into Downloads and run this installer again.")
    except OSError as e:
        fail(str(e))
    if not shortcuts:
        fail("the app was copied, but no shortcut could be made. "
             f'You can still start it by running: "{sys.executable}" "{dest / "launch.py"}"')
    print("\nShortcut made:")
    for s in shortcuts:
        print(f"  {s}")
    print(f"\nDone. Double-click {NAME} on your Desktop to start it.")
    pause()


if __name__ == "__main__":
    main()
