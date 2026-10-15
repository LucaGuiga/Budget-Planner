"""The planner server's sync loop: python3 -m server.sync

Every few seconds it asks the budget API which entries changed. For each
user whose edit list ("user-<id>:edits") changed, it runs the new edits in
order through the same code the local app uses (budget_actions), saves the
budget on this server, and writes the result to "user-<id>:view" for the
page to show. The website never runs budget logic; this does.

Each edit carries a number (seq). The view records the last number done
(seq_done) and the outcome of recent edits, so the page knows when its edit
went through and why one was refused. A copy of each budget also goes to
"user-<id>:state" as an off-site backup.

Per user, on this server (<data_dir>/users/<id>/):
    budget.json, budget.backup.json   the budget (same files as the local app)
    sync.json                         {seq_done, state_rev, results}
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import budget_core as core  # noqa: E402
from budget import Store  # noqa: E402
from budget_actions import answer_query, apply_action  # noqa: E402
from server import config as config_mod  # noqa: E402
from server.accounts import Accounts  # noqa: E402
from server.api import Api, ApiError  # noqa: E402

EDITS_RE = re.compile(r"user-(\d+):edits")
KEEP_RESULTS = 50        # outcomes kept in the view, newest first
MAX_EDITS_PER_ROUND = 100


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def answer_key(q):
    """Same text as web.js key(), so the page can find answers worked out ahead."""
    return json.dumps(q, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- one user

class UserData:
    def __init__(self, data_dir, user_id):
        self.user_id = user_id
        self.dir = Path(data_dir) / "users" / str(user_id)
        self.store = Store(self.dir)
        self.meta_path = self.dir / "sync.json"

    def load(self):
        state = self.store.load()
        meta = {"seq_done": 0, "state_rev": 0, "results": {}}
        if self.meta_path.exists():
            meta.update(json.loads(self.meta_path.read_text(encoding="utf-8")))
        return state, meta

    def save(self, state, meta, state_changed):
        self.dir.mkdir(parents=True, exist_ok=True)
        if state_changed:
            if state is None:
                self.store.clear()
            else:
                self.store.save(state)
        # Written right after the budget. If the machine stopped between the
        # two writes, the last edits would run again on restart.
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, self.meta_path)


def answers_ahead(state):
    """Previews the page asks for on its own, worked out now so they show at
    once: what's left of each quarter as of today, and the request periods."""
    if state is None:
        return {}
    today = datetime.date.today().isoformat()
    questions = [{"type": "quarter_left", "quarter": q["label"], "date": today}
                 for q in core.summarize(state)["quarters"]]
    questions.append({"type": "starts", "today": today})
    out = {}
    for q in questions:
        try:
            out[answer_key(q)] = answer_query(state, q)
        except core.BudgetError:
            pass
    for p in out.get(answer_key({"type": "starts", "today": today}), []):
        q = {"type": "periods", "as_of": p["date"]}
        try:
            out[answer_key(q)] = answer_query(state, q)
        except core.BudgetError:
            pass
    return out


def build_view(state, meta):
    return {
        "seq_done": meta["seq_done"],
        "state_rev": meta["state_rev"],
        "results": meta["results"],
        "budget": core.summarize(state) if state is not None else None,
        "quarter_options": core.quarter_options(),
        "answers": answers_ahead(state),
        "day": datetime.date.today().isoformat(),
    }


def run_edits(state, meta, edits):
    """Apply the new edits in order. Returns (state, changed)."""
    fresh = []
    for e in edits if isinstance(edits, list) else []:
        if isinstance(e, dict) and isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool) \
                and e["seq"] > meta["seq_done"]:
            fresh.append(e)
    fresh.sort(key=lambda e: (e["seq"], str(e.get("id"))))
    changed = False
    for e in fresh[:MAX_EDITS_PER_ROUND]:
        body, kind = e.get("body"), e.get("kind")
        try:
            if not isinstance(body, dict):
                raise core.BudgetError("That request was damaged. Try again.")
            if kind == "action":
                state = apply_action(state, body)
                changed = True
                result = {"ok": True}
            elif kind == "query":
                result = {"ok": True, "answer": answer_query(state, body)}
            else:
                raise core.BudgetError("That request was damaged. Try again.")
        except core.BudgetError as err:
            result = {"ok": False, "error": str(err)}
        meta["results"][str(e["seq"])] = result
        meta["seq_done"] = max(meta["seq_done"], e["seq"])
    newest = sorted(meta["results"], key=int)[-KEEP_RESULTS:]
    meta["results"] = {k: meta["results"][k] for k in newest}
    if changed:
        meta["state_rev"] += 1
    return state, changed


def process_user(api, data_dir, user_id):
    """Run a user's new edits and publish the result. Returns True if anything ran."""
    user = UserData(data_dir, user_id)
    state, meta = user.load()
    payload = api.get(f"user-{user_id}:edits")
    edits = payload.get("edits") if isinstance(payload, dict) else None
    before = meta["seq_done"]
    state, changed = run_edits(state, meta, edits)
    if meta["seq_done"] == before:
        return False
    user.save(state, meta, changed)
    publish(api, user_id, state, meta, changed)
    return True


def publish(api, user_id, state, meta, backup=True):
    api.put(f"user-{user_id}:view", build_view(state, meta))
    if backup:
        api.put(f"user-{user_id}:state", {"budget": state, "state_rev": meta["state_rev"]})


def publish_from_disk(api, data_dir, user_id, backup=False):
    state, meta = UserData(data_dir, user_id).load()
    publish(api, user_id, state, meta, backup)


# ---------------------------------------------------------------- the loop

class Syncer:
    def __init__(self, cfg, api=None):
        self.cfg = cfg
        self.api = api or Api(cfg["api_url"], cfg["token"], cfg["request_gap"])
        self.data_dir = Path(cfg["data_dir"])
        self.seen = {}          # entry name -> updated_at already handled
        self.retry = set()      # users whose view didn't get published
        self.day = None

    def known_users(self):
        return {u["user_id"] for u in Accounts(self.data_dir).users}

    def round(self):
        """One check. Returns how many users had edits run."""
        users = self.known_users()
        today = datetime.date.today().isoformat()
        if today != self.day:
            # "As of today" answers change at midnight, and on start-up the
            # views may be older than what's saved here.
            self.retry |= users
            self.day = today
        changed = self.api.list()
        todo = set(self.retry)
        for name, stamp in changed.items():
            m = EDITS_RE.fullmatch(name)
            if m and self.seen.get(name) != stamp:
                self.seen[name] = stamp
                todo.add(int(m.group(1)))
        ran = 0
        for user_id in sorted(todo):
            if user_id not in users:
                continue   # an edit list for an account that doesn't exist (any more)
            try:
                if process_user(self.api, self.data_dir, user_id):
                    ran += 1
                elif user_id in self.retry:
                    publish_from_disk(self.api, self.data_dir, user_id)
                self.retry.discard(user_id)
            except ApiError as e:
                log(f"user {user_id}: {e}. Trying again next round.")
                self.retry.add(user_id)
        return ran

    def run(self):
        log(f"Planner sync started. Checking {self.cfg['api_url']} every {self.cfg['poll_seconds']:g}s.")
        while True:
            try:
                ran = self.round()
                if ran:
                    log(f"Processed edits for {ran} user{'s' if ran != 1 else ''}.")
            except ApiError as e:
                log(f"Couldn't check for edits: {e}")
            time.sleep(self.cfg["poll_seconds"])


def main():
    ap = argparse.ArgumentParser(description="Process website edits for the budget planner.")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--config", help="path to config.ini")
    args = ap.parse_args()
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        sys.exit(str(e))
    syncer = Syncer(cfg)
    if args.once:
        print(f"Processed edits for {syncer.round()} user(s).")
    else:
        try:
            syncer.run()
        except KeyboardInterrupt:
            log("Stopped.")


if __name__ == "__main__":
    main()
