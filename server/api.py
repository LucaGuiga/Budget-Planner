"""Calls to the budget API with the machine token.

Only the routes that already exist are used: /data and /data/<name>, plus
PUT /users for the account list. Requests are spaced out to stay under the
API's rate limit, and a 429 (too many requests) is waited out and retried.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    """The API couldn't be reached or refused a request."""


class Api:
    def __init__(self, url, token, gap=0.3, timeout=15):
        self.url, self.token, self.gap, self.timeout = url.rstrip("/"), token, gap, timeout
        self._last = 0.0

    def _request(self, method, path, body=None):
        for attempt in range(6):
            wait = self._last + self.gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            data = None if body is None else json.dumps(body).encode("utf-8")
            req = urllib.request.Request(self.url + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": "budget-planner-sync"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    text = r.read().decode("utf-8")
                    return json.loads(text) if text else None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 429 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"{method} {path}: HTTP {e.code}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise ApiError(f"{method} {path}: {e}") from None
        raise ApiError(f"{method} {path}: still rate limited")

    def _path(self, name):
        return "/data/" + urllib.parse.quote(name, safe="")

    def list(self):
        """{name: updated_at} for every entry."""
        found = self._request("GET", "/data")
        if isinstance(found, dict):  # accept {"items": [...]} or similar wrappers
            found = next((v for v in found.values() if isinstance(v, list)), [])
        out = {}
        for row in found or []:
            name = row.get("key") or row.get("name") if isinstance(row, dict) else None
            if name:
                out[name] = row.get("updated_at") or ""
        return out

    def get(self, name):
        found = self._request("GET", self._path(name))
        return found.get("payload") if isinstance(found, dict) else None

    def put(self, name, payload):
        self._request("PUT", self._path(name), payload)

    def delete(self, name):
        self._request("DELETE", self._path(name))

    def put_users(self, users):
        self._request("PUT", "/users", {"users": users})
