"""Manage operator accounts from the command line (the platform may be running or not).

    python -m urban.users list
    python -m urban.users add <name> --role operator          (asks for the password)
    python -m urban.users password <name>                     (asks for the new password)
    python -m urban.users disable <name> | enable <name>
    python -m urban.users mfa-reset <name>                    (lost phone: the next sign-in enrols anew)
"""
import argparse
import getpass

from . import config as c
from .accounts import ROLES, Accounts


def main():
    ap = argparse.ArgumentParser(description="URBAN operator accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--role", choices=ROLES, default="operator")
    sub.add_parser("password").add_argument("name")
    sub.add_parser("disable").add_argument("name")
    sub.add_parser("enable").add_argument("name")
    sub.add_parser("mfa-reset").add_argument("name")
    args = ap.parse_args()

    accounts = Accounts(c.USERS, mfa_roles=c.MFA_ROLES)
    if getattr(args, "name", None) and args.cmd != "add" and args.name not in accounts.users:
        raise SystemExit(f"no such user: {args.name}")
    if args.cmd == "list":
        for u in accounts.users.values():
            print(f"{u['name']:20} {u['role']:9} {'enabled' if u['enabled'] else 'DISABLED'}  "
                  f"{'2FA' if u.get('totp') else '   '}  since {u.get('created', '')}")
        return
    if args.cmd == "add":
        pw = getpass.getpass("password (10+ characters): ")
        if pw != getpass.getpass("again: "):
            raise SystemExit("passwords differ")
        print(accounts.create(args.name, pw, args.role, by="cli"))
    elif args.cmd == "password":
        pw = getpass.getpass("new password (10+ characters): ")
        accounts.set_password(args.name, pw)
        print("changed; a running platform picks it up within seconds and ends this user's open sessions")
    elif args.cmd == "mfa-reset":
        accounts.reset_mfa(args.name)
        print("second factor removed; the user signs in again and enrols a new one")
    else:
        accounts.set_enabled(args.name, args.cmd == "enable")
        print(accounts.public(args.name))


if __name__ == "__main__":
    main()
