"""Build the website for GitHub Pages: python3 make_site.py

Writes site/ with the same page the local app uses, plus web.js, which logs
people in and talks to the budget API instead of a local server. Push the
contents of site/ to the GitHub repo that serves budget.lucaguiga.com.

    python3 make_site.py --api https://budget-api.lucaguiga.com --domain budget.lucaguiga.com
"""

import argparse
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TITLE = "Guiga School Budget"


def build(out, api, domain=None):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    page = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    hook = (f'<script>window.BUDGET_WEB = {json.dumps({"api": api})};</script>\n'
            '<script src="web.js"></script>\n<script>\n"use strict";')
    assert page.count('<script>\n"use strict";') == 1, "index.html's script tag changed; update make_site.py"
    page = page.replace('<script>\n"use strict";', hook)
    start, end = page.index("<title>"), page.index("</title>")
    page = page[:start] + f"<title>{TITLE}" + page[end:]
    (out / "index.html").write_text(page, encoding="utf-8")
    shutil.copyfile(HERE / "static" / "web.js", out / "web.js")
    (out / ".nojekyll").write_text("")
    if domain:
        (out / "CNAME").write_text(domain + "\n")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build the website for GitHub Pages.")
    ap.add_argument("--api", default="https://budget-api.lucaguiga.com")
    ap.add_argument("--domain", default="budget.lucaguiga.com")
    ap.add_argument("--out", default=str(HERE / "site"))
    a = ap.parse_args()
    print(f"Made {build(a.out, a.api, a.domain)}")


if __name__ == "__main__":
    main()
