"""A stand-in for the budget API, for tests and trying the site on this computer.

It follows the budget API's spec: /data for
the machine token, logins with a session cookie, sessions limited to their
own "user-<id>:" entries, PUT /users, and CORS for the site's address.
Not for real use: everything is kept in memory.

    python3 -m server.fake_api --port 8765 --token test --origin http://localhost:8766
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")


class FakeApi:
    def __init__(self, token, origin, scrypt, encoding="hex"):
        self.token, self.origin, self.scrypt, self.encoding = token, origin, scrypt, encoding
        self.data = {}        # name -> (payload, updated_at)
        self.users = []       # [{user_id, username, salt, hash}]
        self.sessions = {}    # session id -> user_id
        self.fails = {}       # ip -> [times]
        self.lock = threading.Lock()
        self.requests = 0

    def _decode(self, text):
        return bytes.fromhex(text) if self.encoding == "hex" else base64.b64decode(text)

    def check_login(self, username, password):
        for u in self.users:
            if u["username"] == (username or "").lower():
                p = self.scrypt
                digest = hashlib.scrypt(password.encode(), salt=self._decode(u["salt"]), n=p["n"], r=p["r"],
                                        p=p["p"], dklen=p["length"], maxmem=256 * 1024 * 1024)
                return u if hmac.compare_digest(digest, self._decode(u["hash"])) else None
        return None


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None, headers=()):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            if self.headers.get("Origin") == api.origin:
                self.send_header("Access-Control-Allow-Origin", api.origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE")
                self.send_header("Access-Control-Allow-Headers", "content-type")
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def _who(self):
            """'machine', a user id, or None."""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], api.token):
                return "machine"
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session" and v in api.sessions:
                    return api.sessions[v]
            return None

        def do_OPTIONS(self):
            self._send(204 if self.headers.get("Origin") == api.origin else 403)

        def _route(self, method):
            api.requests += 1
            path = urlsplit(self.path).path
            if path == "/health":
                return self._send(200, {"ok": True})
            if method != "GET" and self._who() != "machine" and self.headers.get("Origin") not in (None, api.origin):
                return self._send(403, {"error": "forbidden"})
            with api.lock:
                if path == "/login" and method == "POST":
                    ip = self.client_address[0]
                    recent = [t for t in api.fails.get(ip, []) if t > time.time() - 900]
                    if len(recent) >= 5:
                        return self._send(429, {"error": "too many attempts"})
                    b = self._body() or {}
                    u = api.check_login(b.get("username"), b.get("password") or "")
                    if not u:
                        api.fails[ip] = recent + [time.time()]
                        return self._send(401, {"error": "unauthorized"})
                    sid = secrets.token_urlsafe(32)
                    api.sessions[sid] = u["user_id"]
                    return self._send(200, {"user_id": u["user_id"], "username": u["username"]}, [
                        ("Set-Cookie", f"session={sid}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000")])
                who = self._who()
                if who is None:
                    return self._send(401, {"error": "unauthorized"})
                if path == "/logout" and method == "POST":
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        k, _, v = part.strip().partition("=")
                        if k == "session":
                            api.sessions.pop(v, None)
                    return self._send(200, {"ok": True})
                if path == "/session" and method == "GET":
                    if who == "machine":
                        return self._send(401, {"error": "unauthorized"})
                    u = next(x for x in api.users if x["user_id"] == who)
                    return self._send(200, {"user_id": u["user_id"], "username": u["username"]})
                if path == "/users" and method == "PUT":
                    if who != "machine":
                        return self._send(403, {"error": "forbidden"})
                    new = self._body()["users"]
                    old = {u["user_id"]: u for u in api.users}
                    keep = {u["user_id"] for u in new if old.get(u["user_id"], {}).get("hash") == u["hash"]}
                    api.sessions = {s: uid for s, uid in api.sessions.items() if uid in keep}
                    api.users = new
                    return self._send(200, {"ok": True})
                prefix = None if who == "machine" else f"user-{who}:"
                if path == "/data" and method == "GET":
                    items = [{"key": k, "updated_at": v[1]} for k, v in sorted(api.data.items())
                             if prefix is None or k.startswith(prefix)]
                    return self._send(200, {"items": items})
                if path.startswith("/data/"):
                    name = unquote(path[6:])
                    if not NAME_RE.fullmatch(name):
                        return self._send(400, {"error": "bad name"})
                    if prefix is not None and not name.startswith(prefix):
                        return self._send(403, {"error": "forbidden"})
                    if method == "GET":
                        if name not in api.data:
                            return self._send(404, {"error": "not found"})
                        payload, stamp = api.data[name]
                        return self._send(200, {"key": name, "payload": payload, "updated_at": stamp})
                    if method in ("PUT", "POST"):
                        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
                        api.data[name] = (self._body(), stamp)
                        return self._send(200, {"key": name, "updated_at": stamp})
                    if method == "DELETE":
                        api.data.pop(name, None)
                        return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})

        def do_GET(self): self._route("GET")
        def do_POST(self): self._route("POST")
        def do_PUT(self): self._route("PUT")
        def do_DELETE(self): self._route("DELETE")

    return Handler


def serve(port, token, origin, scrypt, encoding="hex"):
    """Start in a background thread. Returns (FakeApi, server)."""
    api = FakeApi(token, origin, scrypt, encoding)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return api, server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="test")
    ap.add_argument("--origin", default="http://localhost:8766")
    a = ap.parse_args()
    serve(a.port, a.token, a.origin, {"n": 16384, "r": 8, "p": 1, "length": 64})
    print(f"Fake budget API on http://127.0.0.1:{a.port} (token {a.token!r}, site origin {a.origin})")
    threading.Event().wait()
