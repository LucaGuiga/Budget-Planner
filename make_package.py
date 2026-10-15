"""Build the zip to send someone: python3 make_package.py

Writes dist/Budget Planner.zip. Opened, it's one folder holding the
installers, INSTALL.txt and an "app" folder with the app itself. Everything
is plain files, nothing packed or encoded. Gmail blocks the Windows .bat
installer, even zipped, so share the zip as a link (Google Drive, Dropbox)
rather than an attachment. Your saved budget (the data folder) is never included.
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP = "Budget Planner"
APP_FILES = [
    "budget.py", "budget_core.py", "budget_actions.py", "launch.py", "install.py", "README.md",
    "static/index.html",
    "launcher/BudgetPlanner.icns", "launcher/icon.ico", "launcher/icon-256.png",
]
INSTALLERS = ["Install on Windows.bat", "Install on Mac.command",
              "Install on Linux.sh", "INSTALL.txt"]


def add(z, source, name, executable=False):
    info = zipfile.ZipInfo(f"{TOP}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100755 if executable else 0o100644) << 16  # keeps Mac/Linux run permission
    z.writestr(info, source.read_bytes())


def main():
    out = HERE / "dist" / f"{TOP}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        for f in INSTALLERS:
            add(z, HERE / "installer" / f, f, executable=f.endswith((".command", ".sh")))
        for f in APP_FILES:
            add(z, HERE / f, f"app/{f}")
    print(f"Made {out}")


if __name__ == "__main__":
    main()
