"""Manage website accounts: python3 -m server.admin <command>

    add <username> [--import budget.json]   make an account (asks for its password)
    passwd <username>                       change a password (ends that person's logins)
    remove <username>                       delete an account; their budget is archived here
    list                                    show the accounts
    push                                    send the account list to the API again

Accounts live on this server; the API only ever gets the password hashes.
"""

import argparse
import datetime
import getpass
import json
import shutil
import sys
from pathlib import Path

from server import config as config_mod
from server.accounts import AccountError, Accounts, check_password, hash_password
from server.api import Api, ApiError
from server.sync import UserData, log, publish

import budget_core as core


def ask_password(username):
    while True:
        pw = getpass.getpass(f"Password for {username}: ")
        try:
            check_password(pw)
        except AccountError as e:
            print(e)
            continue
        if getpass.getpass("Type it again: ") == pw:
            return pw
        print("Those don't match. Try again.")


def push_users(api, accounts):
    api.put_users([{k: u[k] for k in ("user_id", "username", "salt", "hash")} for u in accounts.users])


def add(cfg, api, username, password, import_path=None):
    accounts = Accounts(cfg["data_dir"])
    state = None
    if import_path:
        try:
            state = core.validate(core.migrate(json.loads(Path(import_path).read_text(encoding="utf-8"))))
        except (OSError, ValueError) as e:
            raise AccountError(f"{import_path} isn't a budget this app can read ({e}).") from None
    check_password(password)
    user = accounts.add(username, *hash_password(password, cfg["scrypt"], cfg["encoding"]))
    accounts.save()
    data = UserData(cfg["data_dir"], user["user_id"])
    meta = {"seq_done": 0, "state_rev": 1 if state else 0, "results": {}}
    data.save(state, meta, state_changed=state is not None)
    push_users(api, accounts)
    publish(api, user["user_id"], state, meta, backup=state is not None)
    return user


def passwd(cfg, api, username, password):
    accounts = Accounts(cfg["data_dir"])
    check_password(password)
    user = accounts.find(username)
    user["salt"], user["hash"] = hash_password(password, cfg["scrypt"], cfg["encoding"])
    accounts.save()
    push_users(api, accounts)


def remove(cfg, api, username):
    accounts = Accounts(cfg["data_dir"])
    user = accounts.remove(username)
    accounts.save()
    push_users(api, accounts)
    for part in ("edits", "view", "state"):
        api.delete(f"user-{user['user_id']}:{part}")
    folder = Path(cfg["data_dir"]) / "users" / str(user["user_id"])
    if folder.exists():
        archive = Path(cfg["data_dir"]) / "removed" / f"{user['user_id']}-{username}-{datetime.date.today()}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(archive))
        return archive
    return None


def main():
    ap = argparse.ArgumentParser(description="Manage website accounts for the budget planner.")
    ap.add_argument("--config", help="path to config.ini")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--import", dest="import_path")
    sub.add_parser("passwd").add_argument("username")
    sub.add_parser("remove").add_argument("username")
    sub.add_parser("list")
    sub.add_parser("push")
    args = ap.parse_args()
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        sys.exit(str(e))
    api = Api(cfg["api_url"], cfg["token"], cfg["request_gap"])
    try:
        if args.cmd == "add":
            user = add(cfg, api, args.username, ask_password(args.username.lower()), args.import_path)
            print(f'Made "{user["username"]}" (user {user["user_id"]})'
                  + (", with the imported budget." if args.import_path else ". They set up their budget after logging in."))
        elif args.cmd == "passwd":
            passwd(cfg, api, args.username, ask_password(args.username.lower()))
            print(f"Changed. {args.username}'s other logins are ended.")
        elif args.cmd == "remove":
            if input(f'Type {args.username} to delete that account: ').strip() != args.username:
                sys.exit("Nothing was changed.")
            archive = remove(cfg, api, args.username)
            print("Deleted." + (f" Their budget is archived at {archive}" if archive else ""))
        elif args.cmd == "list":
            users = Accounts(cfg["data_dir"]).users
            for u in users:
                print(f'{u["user_id"]:>4}  {u["username"]}')
            print(f"{len(users)} account{'s' if len(users) != 1 else ''}.")
        elif args.cmd == "push":
            push_users(api, Accounts(cfg["data_dir"]))
            print("Sent the account list to the API.")
    except AccountError as e:
        sys.exit(str(e))
    except ApiError as e:
        log(f"The API refused or couldn't be reached: {e}")
        sys.exit("Saved here, but the API wasn't updated. Run: python3 -m server.admin push")


if __name__ == "__main__":
    main()
