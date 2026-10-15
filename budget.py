"""Local term budget planner. Run with: python budget.py

Serves a single page on 127.0.0.1 and saves to data/budget.json.
Standard library only. No network calls beyond the local loopback server.
"""

import json
import os
import shutil
import signal
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import budget_core as core
from budget_actions import answer_query, apply_action

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
INDEX_HTML = HERE / "static" / "index.html"
MAX_BODY = 64 * 1024
CODE_FILES = [HERE / "budget.py", HERE / "budget_core.py"]


def _code_stamp():
    return [f.stat().st_mtime for f in CODE_FILES if f.exists()]


STARTED_WITH = _code_stamp()  # if these files change while running, the page asks for a restart


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.main = self.dir / "budget.json"
        self.backup = self.dir / "budget.backup.json"

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return core.validate(core.migrate(json.load(f)))

    def load(self):
        """Return the saved state, or None if there is nothing usable."""
        if not self.main.exists():
            return None
        try:
            return self._read(self.main)
        except (OSError, ValueError) as e:
            print(f"Warning: {self.main.name} is unreadable ({e}).", file=sys.stderr)
        aside = self.dir / f"budget.unreadable-{int(time.time())}.json"
        os.replace(self.main, aside)
        print(f"Moved it to {aside.name}.", file=sys.stderr)
        if self.backup.exists():
            try:
                state = self._read(self.backup)
                print("Loaded the backup instead.", file=sys.stderr)
                self.save(state)
                return state
            except (OSError, ValueError) as e:
                print(f"Backup is unreadable too ({e}).", file=sys.stderr)
        return None

    def _keep_backup(self):
        if self.main.exists():
            tmp = self.backup.with_suffix(".json.tmp")
            shutil.copy2(self.main, tmp)
            os.replace(tmp, self.backup)

    def save(self, state):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._keep_backup()
        tmp = self.main.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.main)

    def clear(self):
        """Reset: the old budget stays recoverable in the backup file."""
        self._keep_backup()
        if self.main.exists():
            self.main.unlink()


class App:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.state = store.load()

    def view(self):
        return {"ok": True, "budget": core.summarize(self.state) if self.state else None,
                "quarter_options": core.quarter_options(),
                "needs_restart": _code_stamp() != STARTED_WITH}

    def query(self, q):
        with self.lock:
            return {"ok": True, "answer": answer_query(self.state, q)}

    def handle(self, action):
        with self.lock:
            new = apply_action(self.state, action)
            if new is None:
                self.store.clear()
            else:
                self.store.save(new)
            self.state = new
            return self.view()


# ---------------------------------------------------------------- http

def make_handler(app, port_holder):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BudgetPlanner"

        def log_message(self, fmt, *args):
            pass  # keep the terminal quiet

        def _allowed(self):
            # Refuse requests aimed at us through other hostnames (DNS rebinding)
            # or posted from other sites.
            port = port_holder[0]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in hosts}

        def _send(self, status, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj), "application/json")

        def do_GET(self):
            if not self._allowed():
                return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden"})
            if self.path == "/":
                return self._send(HTTPStatus.OK, INDEX_HTML.read_bytes(),
                                  "text/html; charset=utf-8")
            if self.path == "/api/state":
                with app.lock:
                    return self._json(HTTPStatus.OK, app.view())
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        def do_POST(self):
            if not self._allowed() or self.path not in ("/api/action", "/api/query") \
                    or self.headers.get("Content-Type") != "application/json":
                return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Bad request"})
            try:
                action = json.loads(self.rfile.read(length))
                if not isinstance(action, dict):
                    raise ValueError
            except ValueError:
                return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Bad JSON"})
            try:
                if self.path == "/api/query":
                    return self._json(HTTPStatus.OK, app.query(action))
                self._json(HTTPStatus.OK, app.handle(action))
            except core.BudgetError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(e)})
            except OSError as e:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"ok": False, "error": f"Could not save: {e}. Nothing was changed."})

    return Handler


def main():
    app = App(Store(DATA_DIR))
    port_holder = [0]
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, port_holder))
    port_holder[0] = server.server_address[1]
    url = f"http://127.0.0.1:{port_holder[0]}/"
    print(f"Budget planner running at {url}")
    print("Close this window (or press Ctrl+C) to stop it.")
    # Tell the desktop launcher where we are, so a second click reopens this
    # page instead of starting another copy.
    running = DATA_DIR / "server.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    running.write_text(json.dumps({"url": url, "pid": os.getpid()}))

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGHUP"):        # not on Windows
        signal.signal(signal.SIGHUP, stop)   # the Terminal window was closed
    if "--no-browser" not in sys.argv:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        try:
            if json.loads(running.read_text()).get("pid") == os.getpid():
                running.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
