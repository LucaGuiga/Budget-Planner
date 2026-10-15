"""Tests for the website sync (server/), against the stand-in API in server/fake_api.py.

A small client plays the page's part the way web.js does: log in, write
edits to "user-<id>:edits", read results from "user-<id>:view".
"""

import datetime
import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import budget_core as core
from budget_actions import apply_action
from server import admin, fake_api
from server.accounts import AccountError
from server.api import Api, ApiError
from server.sync import Syncer, answer_key

TOKEN = "machine-token-for-tests"
ORIGIN = "https://budget.lucaguiga.com"
SCRYPT = {"n": 1024, "r": 8, "p": 1, "length": 64}   # small n keeps tests fast
SETUP = {"type": "setup", "total": "11000", "tuition": "2000", "first_quarter": "Fall 2026",
         "last_quarter": "Summer 2027", "start": "2026-10"}


class Page:
    """Does what web.js does, over plain HTTP."""

    def __init__(self, url):
        self.url, self.cookie, self.user = url, None, None

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, headers={
            "Content-Type": "application/json", "Origin": ORIGIN, **({"Cookie": self.cookie} if self.cookie else {})},
            data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req) as r:
                if r.headers.get("Set-Cookie"):
                    self.cookie = r.headers["Set-Cookie"].split(";")[0]
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def login(self, username, password):
        code, body = self.call("POST", "/login", {"username": username, "password": password})
        self.user = body
        return code

    def view(self):
        code, body = self.call("GET", f"/data/user-{self.user['user_id']}:view")
        return body["payload"] if code == 200 else None

    def send(self, *edits):
        """edits: (kind, body) pairs, numbered after what the view says is done."""
        done = self.view()["seq_done"]
        rows = [{"seq": done + i, "id": f"e{done + i}", "kind": k, "body": b, "at": "now"}
                for i, (k, b) in enumerate(edits, 1)]
        self.call("PUT", f"/data/user-{self.user['user_id']}:edits", {"edits": rows})
        return [r["seq"] for r in rows]


class WebSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake, self.server = fake_api.serve(0, TOKEN, ORIGIN, SCRYPT)
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cfg = {"api_url": url, "token": TOKEN, "request_gap": 0, "poll_seconds": 0,
                    "data_dir": self.tmp, "scrypt": SCRYPT, "encoding": "hex"}
        self.api = Api(url, TOKEN, gap=0)
        self.sync = Syncer(self.cfg, self.api)
        self.page = Page(url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp)

    def make_user(self, name="alex", password="correct horse", **kw):
        user = admin.add(self.cfg, self.api, name, password, **kw)
        self.assertEqual(self.page.login(name, password), 200)
        return user

    def test_new_account_gets_an_empty_view_and_can_log_in(self):
        self.make_user()
        view = self.page.view()
        self.assertIsNone(view["budget"])
        self.assertEqual(view["seq_done"], 0)
        self.assertTrue(view["quarter_options"])
        self.assertEqual(Page(self.page.url).login("alex", "wrong password"), 401)

    def test_passwords_are_never_stored_or_sent_plain(self):
        self.make_user(password="correct horse")
        on_disk = (self.tmp / "accounts.json").read_text()
        self.assertNotIn("correct horse", on_disk)
        self.assertNotIn("correct horse", json.dumps(self.fake.users))

    def test_an_edit_is_processed_saved_and_backed_up(self):
        user = self.make_user()
        [seq] = self.page.send(("action", SETUP))
        self.assertEqual(self.sync.round(), 1)
        view = self.page.view()
        self.assertEqual(view["seq_done"], seq)
        self.assertEqual(view["results"][str(seq)], {"ok": True})
        self.assertEqual(view["budget"]["total"], 1100000)
        saved = json.loads((self.tmp / "users" / str(user["user_id"]) / "budget.json").read_text())
        self.assertEqual(saved["total"], 1100000)
        self.assertEqual(self.api.get(f"user-{user['user_id']}:state")["budget"], saved)

    def test_a_refused_edit_reports_why_and_changes_nothing(self):
        self.make_user()
        self.page.send(("action", SETUP))
        self.sync.round()
        before = self.page.view()
        [seq] = self.page.send(("action", {"type": "set_alloc", "index": 0, "amount": "999999"}))
        self.sync.round()
        view = self.page.view()
        self.assertFalse(view["results"][str(seq)]["ok"])
        self.assertIn("more than all the spending money", view["results"][str(seq)]["error"])
        self.assertEqual(view["budget"], before["budget"])
        self.assertEqual(view["state_rev"], before["state_rev"])

    def test_edits_run_once_and_in_order(self):
        self.make_user()
        self.page.send(("action", SETUP), ("action", {"type": "add_category", "name": "Food", "default": "250"}),
                       ("action", {"type": "add_category", "name": "Food", "default": "100"}))
        self.assertEqual(self.sync.round(), 1)
        self.assertEqual(self.sync.round(), 0)   # nothing new: nothing runs twice
        view = self.page.view()
        self.assertEqual([c["name"] for c in view["budget"]["categories"]], ["Food"])
        self.assertFalse(view["results"]["3"]["ok"])   # the duplicate name was refused

    def test_results_match_the_local_app(self):
        self.make_user()
        steps = [SETUP, {"type": "add_category", "name": "Rent", "default": "300"},
                 {"type": "set_alloc", "index": 2, "amount": "1000"}, {"type": "set_lock", "index": 2, "locked": True},
                 {"type": "add_item", "name": "Flight", "month": "2027-01", "amount": "300"}, {"type": "optimize"}]
        self.page.send(*[("action", s) for s in steps])
        self.sync.round()
        local = None
        for s in steps:
            local = apply_action(local, s)
        self.assertEqual(self.page.view()["budget"], core.summarize(local))

    def test_previews_are_answered_and_common_ones_worked_out_ahead(self):
        self.make_user()
        self.page.send(("action", SETUP))
        self.sync.round()
        [seq] = self.page.send(("query", {"type": "coverage", "amount": "500", "from": "2026-11-01", "to": "2026-11-30"}))
        self.sync.round()
        view = self.page.view()
        self.assertTrue(view["results"][str(seq)]["ok"])
        self.assertIn("diff", view["results"][str(seq)]["answer"])
        today = datetime.date.today().isoformat()
        self.assertIn(answer_key({"type": "quarter_left", "quarter": "Fall 2026", "date": today}), view["answers"])
        self.assertIn(answer_key({"type": "starts", "today": today}), view["answers"])

    def test_importing_an_existing_budget(self):
        local = apply_action(None, SETUP)
        path = self.tmp / "mine.json"
        path.write_text(json.dumps(local))
        self.make_user(import_path=path)
        self.assertEqual(self.page.view()["budget"], core.summarize(local))
        with self.assertRaises(AccountError):
            admin.add(self.cfg, self.api, "sam", "long enough", import_path=self.tmp / "missing.json")

    def test_people_only_reach_their_own_entries(self):
        self.make_user("alex")
        other = Page(self.page.url)
        admin.add(self.cfg, self.api, "sam", "another password")
        other.login("sam", "another password")
        code, _ = other.call("GET", f"/data/user-{self.page.user['user_id']}:view")
        self.assertEqual(code, 403)
        code, _ = other.call("PUT", f"/data/user-{self.page.user['user_id']}:edits", {"edits": []})
        self.assertEqual(code, 403)

    def test_edits_for_unknown_or_removed_accounts_are_ignored(self):
        self.api.put("user-99:edits", {"edits": [{"seq": 1, "id": "x", "kind": "action", "body": SETUP}]})
        self.assertEqual(self.sync.round(), 0)
        self.assertFalse((self.tmp / "users" / "99").exists())

    def test_password_change_and_removal_end_logins(self):
        self.make_user()
        admin.passwd(self.cfg, self.api, "alex", "a new password")
        self.assertIsNone(self.page.view())       # old session ended
        self.assertEqual(self.page.login("alex", "a new password"), 200)
        self.page.send(("action", SETUP))
        self.sync.round()
        archive = admin.remove(self.cfg, self.api, "alex")
        self.assertTrue((archive / "budget.json").exists())
        self.assertEqual(self.page.login("alex", "a new password"), 401)
        self.assertEqual(self.api.list(), {})

    def test_a_failed_publish_is_retried(self):
        self.make_user()
        self.page.send(("action", SETUP))
        real_put, calls = self.api.put, []

        def flaky_put(name, payload):
            calls.append(name)
            if len(calls) == 1:
                raise ApiError("network down")
            real_put(name, payload)
        self.api.put = flaky_put
        self.sync.round()
        self.assertEqual(self.page.view()["seq_done"], 0)   # the view didn't go out
        self.sync.round()
        self.assertEqual(self.page.view()["budget"]["total"], 1100000)

    def test_bad_edits_are_refused_not_crashing(self):
        self.make_user()
        self.page.call("PUT", f"/data/user-{self.page.user['user_id']}:edits", {"edits": [
            {"seq": 1, "id": "a", "kind": "action", "body": "not a dict"},
            {"seq": 2, "id": "b", "kind": "teleport", "body": {}},
            {"seq": "3", "id": "c", "kind": "action", "body": SETUP},
            {"seq": 4, "id": "d", "kind": "action", "body": {"type": "nope"}}]})
        self.sync.round()
        view = self.page.view()
        self.assertEqual(view["seq_done"], 4)
        self.assertEqual([view["results"][k]["ok"] for k in ("1", "2", "4")], [False, False, False])
        self.assertIsNone(view["budget"])


if __name__ == "__main__":
    unittest.main()
