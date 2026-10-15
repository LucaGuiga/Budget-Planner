"""Read server/config.ini."""

import configparser
import os
import stat
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT / "server" / "config.ini"


class ConfigError(Exception):
    """The config file is missing or has a bad value."""


def load(path=None):
    path = Path(path or os.environ.get("BUDGET_SYNC_CONFIG") or DEFAULT_PATH)
    if not path.exists():
        raise ConfigError(f"{path} doesn't exist. Copy server/config.example.ini to it and fill it in.")
    if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"Warning: {path} holds the API token but others can read it. "
              f"Run: chmod 600 {path}", file=sys.stderr)
    p = configparser.ConfigParser()
    p.read(path, encoding="utf-8")
    try:
        cfg = {
            "api_url": p.get("api", "url").rstrip("/"),
            "token": p.get("api", "token").strip(),
            "request_gap": p.getfloat("api", "request_gap", fallback=0.3),
            "poll_seconds": p.getfloat("sync", "poll_seconds", fallback=2),
            "data_dir": PROJECT / p.get("sync", "data_dir", fallback="data/web"),
            "scrypt": {"n": p.getint("passwords", "scrypt_n"), "r": p.getint("passwords", "scrypt_r"),
                       "p": p.getint("passwords", "scrypt_p"), "length": p.getint("passwords", "length")},
            "encoding": p.get("passwords", "encoding", fallback="hex"),
        }
    except (configparser.Error, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if not cfg["token"]:
        raise ConfigError(f"{path}: the API token is empty.")
    if cfg["encoding"] not in ("hex", "base64"):
        raise ConfigError(f"{path}: encoding must be hex or base64.")
    return cfg
