# Term Budget Planner

A local tool for planning how a fixed term total gets spent month by month.
Planning only: no bank connections, no network calls, nothing leaves your machine.

## Run

```
python3 budget.py
```

Python 3.9+ and nothing to install. It starts a server on `127.0.0.1` with a free
port and opens your browser. Ctrl+C stops it. Add `--no-browser` to skip opening a tab.

### Desktop app

`launcher/install-desktop-app.sh` builds **Budget Planner.app** on the Desktop
with the dollar-sign icon. Double-click it to start the planner in a Terminal
window (close the window to stop it); if it's already running, it just reopens
the page. Run the script again if you move this folder.
`launcher/Start Budget Planner.command` does the same start by itself.

### Sharing it

`python3 make_package.py` builds `dist/Budget Planner.zip` to send someone.
Your saved budget is never in it. Opened, it's one folder with the
installers, `INSTALL.txt` and an `app` folder. Send it as a Google Drive or
Dropbox link, not an attachment: Gmail blocks the Windows `.bat`, even
zipped. On Windows they double-click "Install on Windows", which installs
Python first if it's missing (through winget, for their account only).
On a Mac they double-click "Install on Mac" (Linux: `sh "Install on Linux.sh"`);
`INSTALL.txt` inside walks them through it. The installer copies the app to
`~/Library/Application Support/Budget Planner` (Mac),
`%LOCALAPPDATA%\Budget Planner` (Windows) or `~/.local/share/budget-planner`
(Linux) and puts a Budget Planner shortcut on their Desktop (plus the Start
menu or app menu). Installing again updates the app and keeps their budget.
To send your budget along, send `data/budget.json` too: they save it into
Downloads and the installer loads it (the newest `budget*.json` there, so a
browser's "budget (1).json" works). Send a newer one any time; running the
installer again loads it and backs up theirs. A file already loaded is skipped.
Don't run the installer on your own Mac: it replaces your Desktop app with one
pointing at a fresh, empty install.

## Website (budget.lucaguiga.com)

The same page, online, with accounts. Three parts:

- **The page** (`site/`, built by `python3 make_site.py`) is hosted on GitHub
  Pages. It's `static/index.html` plus `static/web.js`, which shows a login
  screen and talks to the budget API instead of a local server. No budget
  math happens in the browser.
- **The budget API** (`https://budget-api.lucaguiga.com`, on
  AWS) stores data and checks logins. Each person can only reach entries
  named `user-<their id>:...`. It runs no budget logic.
- **The planner server** (`server/`, on the Linux home lab) does all the
  logic. Every couple of seconds `server/sync.py` checks the API for new
  edits (`user-<id>:edits`), runs them in order through `budget_actions.py`
  (the same code as the local app), saves each budget on the server under
  `data/web/users/<id>/`, and writes the result to `user-<id>:view` for the
  page. A copy of each budget also goes to `user-<id>:state` as a backup.

An edit shows "Updating..." and lands in a second or two. If the server is
down, edits wait and go through when it's back. Previews the page asks for on
its own ("left as of today", the request periods) are worked out ahead and
show at once.

**Server setup** (Python 3.9+ built with OpenSSL; nothing to install):

```
cp server/config.example.ini server/config.ini   # fill in the token, then:
chmod 600 server/config.ini
python3 -m server.admin add <username>             # asks for their password
python3 -m server.admin add luca --import data/budget.json   # an account that starts from a saved budget
python3 -m server.sync --once                      # one check, to try it
```

Then install `server/budget-sync.service` (instructions in its header) so it
runs all the time. Other commands: `passwd`, `remove`, `list`, `push`.
The password settings in `config.ini` must match the API's login check.

**Trying it on one computer:** `python3 -m server.fake_api` is a stand-in for
the budget API, used by `test_web_sync.py`.

## How it works

- **Setup**: enter the term total (everything you have, tuition included), the
  tuition per quarter, the first and last quarter, and the month the plan starts
  (defaults to Fall 2026 to Summer 2027, starting Sep). Tuition is set aside
  first; the rest is split evenly across the months, with leftover cents going
  to the earliest months.
- **Quarters**: Fall is Sep-Dec, Winter Jan-Mar, Spring Apr-Jun, Summer Jul-Aug.
  Fall, Winter and Spring each carry one tuition payment; Summer has none. A plan
  can start partway into its first quarter and still includes that quarter's
  tuition.
- **Tuition**: UCSC's 2026-27 undergraduate due dates are filled in (Fall Sep 17,
  Winter Dec 16, Spring Mar 23, from the Registrar's academic calendar). Later
  quarters start with no due date. Edit any quarter's amount or due date in place;
  due dates can be anywhere, even before the plan starts. Raising tuition takes
  the difference evenly from the unlocked months; lowering it gives it back. It's
  blocked, with the highest allowed amount, if a month would drop below $0 or its
  category total.
- **Layout**: a tree. The term sits on top, then the quarters, then the months.
  Click the arrow by a quarter's name to fold it into one row (name, tuition,
  unallocated, quarter total); click the row to open it. Folds are remembered
  in your browser.
- **Where you are now**: the current quarter's border shifts color day by day
  through cyan, yellow, blue, green, brown and crimson. The quarter's days are
  divided evenly by the six colors (about 20 days each for Fall) and blended in
  between, with a strip showing today's spot. The current month gets a sunset
  border that travels around the card (it stays still with Reduce motion on).
  Both update on their own at midnight.
- **Edit term**: change the quarters and start month, the term total, the total
  tuition (split evenly) or the per-quarter tuition, without starting over.
  Changing quarters re-splits spending evenly across unlocked months; new
  quarters get the same tuition as the others.
- **Adjust a month**: click a card, type a new allocation, Apply. The difference
  is spread evenly across the other unlocked months, so the term total never moves.
  Locked months are never touched. A change is blocked, with the reason and the
  highest allowed amount, if it would push any month below $0 or below its
  category total.
- **Unallocated**: shown once per quarter, on the quarter tile: what's assigned to
  its months minus everything planned in them. New expenses in the quarter come
  out of it. Month tiles only show a line when they're over ("Over by").
- **Individual expenses**: one-off charges like team dues or tournament travel.
  Each quarter has its own list (name, month, amount) with a subtotal. They use
  up part of their month's budget like a category, show on the month card with a
  diamond, and count toward Total left. The lists start closed, showing just
  the quarter's expense total; click to open one. Changing the term can't drop a month that
  still has expenses; move or delete them first.
- **Paid** (in Edit term): a log of money received. Each payment has whether
  it's received and when, the amount, the dates it's for, who paid (from a names
  list you manage there) and whether they were reimbursed. It's checked against
  what the plan needs for those dates (each month's budget scaled to the days
  covered, plus tuition due in the range) and shows "Enough, $X extra" or
  "Short $X", live while you type. It's a record only and never changes the plan.
- **Request** (in Edit term): how much to ask for. Pick whole periods only: "as
  of" the start of a month or quarter (this month by default) through the end
  of a month, a quarter or the term. Each payment in
  the Paid log covers its dates evenly by day, so whatever of it falls in the
  window still counts. Shows the plan's need, what's left of each payment, what
  to request, the date you're paid through and any uncovered dates. "Add to
  Paid log as expected" logs the request for the uncovered dates. Expected
  payments only count if you tick the box. Untick any part to leave it out of
  the request: Tuition, each category, Individual expenses or Unallocated money.
  The breakdown shows every part and what was left out; your choices are
  remembered in your browser.
- **Left as of a date**: each quarter header has a date (today by default) and
  shows how much of that quarter's budget is still ahead from that day, plus its
  tuition if it isn't due yet.
- **Adds to total**: check it on a category paid by money on top of the term
  total, like gas. Its amounts raise Total available and each month's budget
  instead of using them up, so Total left and Unallocated don't change. The
  optimizer skips it. Shown with + on the cards.
- **Static categories**: check Static on anything fixed, like rent. Its amounts
  are locked (no edits or delete until unchecked) and the optimizer skips it.
- **Optimize** (in Edit term): shows static categories next to dynamic ones, each
  dynamic one with a 0 to 1 slider. Each quarter's free money (allocations minus
  your own category amounts and expenses) is pooled and split evenly across its
  months, then across the dynamic categories by slider (Food 0.5 and Projects
  0.3 means 62.5% and 37.5%), stored as extra on top of your amounts and marked
  with an arrow on the cards. To make the even split fit, allocation shifts
  between months of the same quarter; quarters keep their totals and locked
  months keep their allocation (and just get their own free money). Run it again
  any time: it recalculates from scratch, so a new expense shrinks the quarter's
  extra. It never goes below your own amounts; if a whole quarter is short, it
  gets no extra and the months still over are listed. Every allocation shift is
  recorded, so Un-optimize removes all the extra and moves every shift back,
  leaving each month exactly where it was. A banner on the main page offers re-optimizing when months go over.
- **Categories**: set a default per month on the main screen. Every month uses the
  defaults unless you override a category inside a month (shown with `*`).
  Clear an override with "Use default" or by emptying the field.
- **Reset**: Settings > Reset everything, then type RESET.

## Files

- `budget.py`: server, storage, launch
- `budget_core.py`: all split and redistribution logic (integer cents)
- `budget_actions.py`: turns a request from the page into a change or an answer (shared by the local app and the website sync)
- `static/index.html`: the page
- `data/budget.json`: your saved plan, written on every change
- `data/server.json`: where the running planner is, while it runs (for the Desktop app)
- `launcher/`: the Desktop app builder, its start script and icons
- `launch.py`: what an installed shortcut runs: reopens the page, or starts the planner in a terminal window
- `install.py`: the installer for Mac, Windows and Linux
- `installer/`: the double-click installers and `INSTALL.txt` that go in the zip
- `make_package.py`: builds `dist/Budget Planner.zip`
- `make_site.py`, `static/web.js`: the website build (`site/`)
- `server/`: the website sync, account commands, settings and service file for the planner server
- `data/budget.backup.json`: the version before the last change (also kept after a reset)

## Tests

```
python -m unittest -v
```
