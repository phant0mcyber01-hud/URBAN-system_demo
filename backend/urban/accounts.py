"""Operator accounts, roles and sessions.

Users live in a JSON file (data/users.json, outside git) with scrypt password hashes. Roles:
  viewer    may look at everything;
  operator  may also decide on proposals, pull everything back and cut the demo link;
  admin     may also manage devices (revoke, restore) and users.
A session is a random bearer token kept in memory with an expiry; restarting the platform
signs everyone out. Failed logins are throttled per account and per address. The file is
re-read when another process (python -m urban.users) changes it; sessions of accounts whose
password changed, whose second factor was reset or that were disabled end at once.

Second factor: one-time codes from an authenticator app (TOTP, RFC 6238: HMAC-SHA1, 30 s,
6 digits). Any account may turn it on; for the roles in `mfa_roles` it is required: until such
an account has enrolled, its sessions only get viewer rights, so nothing it can do changes the
city. A code is accepted once (no replay) within one step either side of the clock.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import urllib.parse
from pathlib import Path

ROLES = ("viewer", "operator", "admin")
RANK = {role: i for i, role in enumerate(ROLES)}
SESSION_HOURS = 12
MAX_FAILURES, FAILURE_WINDOW = 5, 60  # per account and per address


STEP, DIGITS = 30, 6
ISSUER = "URBAN"


def keep_first_password(path: Path, password: str) -> Path:
    """The first admin password also goes to a file only the service's user can read: on URBAN OS the journal it
    is printed to is volatile, and a password lost on reboot would leave nobody able to sign in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    try:
        path.chmod(0o600)  # an older file keeps its mode when it is overwritten
    except OSError:
        pass
    return path


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def totp(secret: bytes, step: int, digits: int = DIGITS) -> str:
    """RFC 6238 / RFC 4226 code for one time step."""
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def b32(secret: bytes) -> str:
    return base64.b32encode(secret).decode().rstrip("=")


def unb32(text: str) -> bytes:
    text = text.strip().replace(" ", "").upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


class CodeRequired(Exception):
    """The password was right; the account also needs a one-time code."""


class Accounts:
    def __init__(self, path: Path, mfa_roles: set[str] | frozenset = frozenset(), clock=time.time):
        """mfa_roles: roles that must use a second factor before they get their rights."""
        self.path = path
        self.mfa_roles = set(mfa_roles)
        self.clock = clock
        self.users: dict[str, dict] = self._load()
        self._mtime = self._stamp()
        self.sessions: dict[str, dict] = {}  # token -> {name, role, expires, mfa_setup}
        self._failures: dict[str, list[float]] = {}
        self._pending: dict[str, tuple[bytes, float]] = {}  # name -> (secret being enrolled, when started)

    # ---- users
    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return {u["name"]: u for u in json.loads(self.path.read_text(encoding="utf-8"))}

    def _stamp(self):
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def refresh(self):
        """Re-read the file if another process changed it; end the sessions that change invalidates."""
        stamp = self._stamp()
        if stamp == self._mtime:
            return
        try:
            users = self._load()
        except (OSError, ValueError):
            return  # half-written: try again on the next call
        self._mtime = stamp
        old, self.users = self.users, users
        for name, u in old.items():
            new = users.get(name)
            if (new is None or new["hash"] != u["hash"] or not new["enabled"]
                    or bool(new.get("totp")) != bool(u.get("totp")) or new["role"] != u["role"]):
                self.sign_out_all(name)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(list(self.users.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        self._mtime = self._stamp()

    def create(self, name: str, password: str, role: str, by: str = "system") -> dict:
        name = name.strip()
        if not name or len(name) > 40 or not name.replace("_", "").replace(".", "").replace("-", "").isalnum():
            raise ValueError("name must be 1-40 letters, digits, dots, dashes or underscores")
        if name in self.users:
            raise ValueError("user already exists")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        salt = secrets.token_bytes(16)
        self.users[name] = {"name": name, "role": role, "salt": salt.hex(), "hash": _hash(password, salt),
                            "enabled": True, "created": datetime.date.today().isoformat(), "by": by}
        self.save()
        return self.public(name)

    def set_password(self, name: str, password: str):
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        u = self.users[name]
        salt = secrets.token_bytes(16)
        u["salt"], u["hash"] = salt.hex(), _hash(password, salt)
        self.save()
        self.sign_out_all(name)

    def set_enabled(self, name: str, enabled: bool):
        self.users[name]["enabled"] = enabled
        self.save()
        if not enabled:
            self.sign_out_all(name)

    def public(self, name: str) -> dict:
        u = self.users[name]
        return {"name": u["name"], "role": u["role"], "enabled": u["enabled"], "created": u.get("created", ""),
                "mfa": bool(u.get("totp")), "mfa_required": u["role"] in self.mfa_roles}

    def bootstrap(self) -> str | None:
        """With no users at all, create an admin with a one-time password and return it."""
        if self.users:
            return None
        password = secrets.token_urlsafe(12)
        self.create("admin", password, "admin")
        return password

    # ---- sessions
    def _throttled(self, key: str) -> bool:
        now = time.time()
        recent = [f for f in self._failures.get(key, []) if now - f < FAILURE_WINDOW]
        self._failures[key] = recent
        return len(recent) >= MAX_FAILURES

    def check(self, name: str, password: str, address: str = "") -> bool:
        """Is this the password of an enabled account? Wrong attempts count against name and address."""
        self.refresh()
        keys = [f"u:{name}"] + ([f"a:{address}"] if address else [])
        if any(self._throttled(k) for k in keys):
            return False
        u = self.users.get(name)
        ok = (u is not None and u["enabled"]
              and hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"]))
        if not ok:
            for k in keys:
                self._failures.setdefault(k, []).append(time.time())
        return ok

    def _fail(self, name: str, address: str):
        for k in [f"u:{name}"] + ([f"a:{address}"] if address else []):
            self._failures.setdefault(k, []).append(time.time())

    def sign_in(self, name: str, password: str, address: str = "", code: str = "") -> dict | None:
        """A session {token, name, role, expires, mfa_setup} or None.
        Raises CodeRequired when the password is right but the account's one-time code is missing."""
        if not self.check(name, password, address):
            return None
        u = self.users[name]
        if u.get("totp"):
            if not code:
                raise CodeRequired()
            if not self._accept_code(name, code):
                self._fail(name, address)
                return None
        setup = u["role"] in self.mfa_roles and not u.get("totp")
        token = secrets.token_urlsafe(32)
        session = {"token": token, "name": name, "role": "viewer" if setup else u["role"], "mfa_setup": setup,
                   "expires": time.time() + SESSION_HOURS * 3600}
        self.sessions[token] = session
        return session

    # ---- second factor
    def _accept_code(self, name: str, code: str) -> bool:
        u = self.users[name]
        code = code.strip().replace(" ", "")
        if not code.isdigit() or len(code) != DIGITS:
            return False
        now = int(self.clock() // STEP)
        secret = unb32(u["totp"])
        for step in (now - 1, now, now + 1):
            if step > u.get("totp_last", -1) and hmac.compare_digest(totp(secret, step), code):
                u["totp_last"] = step  # each code works once
                self.save()
                return True
        return False

    def start_mfa(self, name: str) -> dict:
        """A new secret for this account, not active until confirm_mfa sees a code from it."""
        secret = secrets.token_bytes(20)
        self._pending[name] = (secret, self.clock())
        label = urllib.parse.quote(f"{ISSUER}:{name}")
        uri = (f"otpauth://totp/{label}?secret={b32(secret)}&issuer={ISSUER}"
               f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")
        return {"secret": b32(secret), "uri": uri}

    def confirm_mfa(self, name: str, code: str) -> bool:
        """Turn the second factor on with a code from the pending secret. Ends the account's sessions:
        the next sign-in asks for a code and gets the account's full rights."""
        pending = self._pending.get(name)
        if pending is None or self.clock() - pending[1] > 600:
            self._pending.pop(name, None)
            return False
        secret = pending[0]
        now = int(self.clock() // STEP)
        code = code.strip().replace(" ", "")
        matched = next((s for s in (now - 1, now, now + 1) if hmac.compare_digest(totp(secret, s), code)), None)
        if matched is None:
            return False
        u = self.users[name]
        u["totp"], u["totp_last"] = b32(secret), matched  # the confirming code cannot sign in as well
        del self._pending[name]
        self.save()
        self.sign_out_all(name)
        return True

    def reset_mfa(self, name: str):
        """Remove the second factor (lost phone). The account signs in again and, if its role needs one, enrols anew."""
        u = self.users[name]
        u.pop("totp", None)
        u.pop("totp_last", None)
        self.save()
        self.sign_out_all(name)

    def session(self, token: str) -> dict | None:
        self.refresh()
        s = self.sessions.get(token or "")
        if s is None:
            return None
        if time.time() > s["expires"] or not self.users.get(s["name"], {}).get("enabled"):
            self.sessions.pop(token, None)
            return None
        return s

    def role_of(self, name: str) -> str:
        return self.users.get(name, {}).get("role", "")

    def sign_out(self, token: str):
        self.sessions.pop(token, None)

    def sign_out_all(self, name: str):
        for token in [t for t, s in self.sessions.items() if s["name"] == name]:
            del self.sessions[token]


def allows(role: str, needed: str) -> bool:
    return RANK.get(role, -1) >= RANK[needed]
