"""Website accounts, kept on this server in <data_dir>/accounts.json.

Only a salted scrypt hash of each password is stored, and only the hashes
are sent to the API (which checks logins). Plain passwords are never saved.
"""

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
MIN_PASSWORD = 8


class AccountError(Exception):
    """A change to the accounts was refused. The message says why."""


def _encode(raw, encoding):
    return raw.hex() if encoding == "hex" else base64.b64encode(raw).decode("ascii")


def hash_password(password, params, encoding):
    if not hasattr(hashlib, "scrypt"):
        raise AccountError("This Python can't hash passwords (its hashlib has no scrypt). "
                           "Use a Python built with OpenSSL, like the standard python3 on Linux.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"],
                            p=params["p"], dklen=params["length"], maxmem=256 * 1024 * 1024)
    return _encode(salt, encoding), _encode(digest, encoding)


def check_password(password):
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")


class Accounts:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "accounts.json"
        self.data = {"next_id": 1, "users": []}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def users(self):
        return self.data["users"]

    def find(self, username):
        for u in self.users:
            if u["username"] == username.lower():
                return u
        raise AccountError(f'There is no user called "{username}".')

    def by_id(self, user_id):
        return next((u for u in self.users if u["user_id"] == user_id), None)

    def add(self, username, salt, digest):
        username = username.lower()
        if not USERNAME_RE.fullmatch(username):
            raise AccountError("Usernames are 3 to 32 characters: lowercase letters, numbers, "
                               "dots, dashes and underscores, starting with a letter or number.")
        if any(u["username"] == username for u in self.users):
            raise AccountError(f'There is already a user called "{username}".')
        user = {"user_id": self.data["next_id"], "username": username, "salt": salt, "hash": digest}
        self.data["next_id"] += 1
        self.users.append(user)
        return user

    def remove(self, username):
        user = self.find(username)
        self.users.remove(user)
        return user

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
