"""What the Desktop shortcut runs, on macOS, Windows and Linux.

If the planner is already running, reopen its page. Otherwise start it in a
terminal window, so closing that window stops it.
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_INFO = HERE / "data" / "server.json"
MAC_COMMAND = HERE / "launcher" / "Start Budget Planner.command"

# Linux terminals and the flag each one takes before the command to run.
LINUX_TERMINALS = [
    ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]), ("mate-terminal", ["-x"]), ("tilix", ["-e"]),
    ("kitty", []), ("alacritty", ["-e"]), ("xterm", ["-e"]),
]


def running_url(info=SERVER_INFO):
    """The page of a planner that's running and answering, or None."""
    try:
        url = json.loads(Path(info).read_text())["url"]
        with urllib.request.urlopen(url + "api/state", timeout=2) as r:
            return url if r.status == 200 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None  # no file, stale file, or nothing listening


def console_python():
    """The Windows shortcut runs pythonw (no window); the server wants python."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def start():
    server = [console_python(), str(HERE / "budget.py")]
    if sys.platform == "win32":
        subprocess.Popen(server, cwd=HERE, creationflags=subprocess.CREATE_NEW_CONSOLE)
    elif sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(MAC_COMMAND)])
    else:
        for name, flag in LINUX_TERMINALS:
            if shutil.which(name):
                subprocess.Popen([name, *flag, *server], cwd=HERE)
                return
        # No terminal found: run in the background. It stops at log out.
        subprocess.Popen(server, cwd=HERE, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    url = running_url()
    if url:
        webbrowser.open(url)
    else:
        start()


if __name__ == "__main__":
    os.chdir(HERE)
    main()
