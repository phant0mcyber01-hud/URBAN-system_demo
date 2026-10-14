"""Operator accounts, roles and sessions.

Users live in a JSON file (data/users.json, outside git) with scrypt password hashes. Roles:
  viewer    may look at everything;
  operator  may also decide on proposals, pull everything back and cut the demo link;
  admin     may also manage devices (revoke, restore) and users.
A session is a random bearer token kept in memory with an expiry; restarting the platform
signs everyone out. Failed logins are throttled per account and per address. The file is
re-read when another process (python -m urban.users) changes it; sessions of accounts whose
password changed or that were disabled end at once.
"""
import datetime
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

ROLES = ("viewer", "operator", "admin")
RANK = {role: i for i, role in enumerate(ROLES)}
SESSION_HOURS = 12
MAX_FAILURES, FAILURE_WINDOW = 5, 60  # per account and per address


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


class Accounts:
    def __init__(self, path: Path):
        self.path = path
        self.users: dict[str, dict] = self._load()
        self._mtime = self._stamp()
        self.sessions: dict[str, dict] = {}  # token -> {name, role, expires}
        self._failures: dict[str, list[float]] = {}

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
            if new is None or new["hash"] != u["hash"] or not new["enabled"]:
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
        return {"name": u["name"], "role": u["role"], "enabled": u["enabled"], "created": u.get("created", "")}

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

    def sign_in(self, name: str, password: str, address: str = "") -> dict | None:
        """A session {token, name, role, expires} or None."""
        if not self.check(name, password, address):
            return None
        u = self.users[name]
        token = secrets.token_urlsafe(32)
        session = {"token": token, "name": name, "role": u["role"], "expires": time.time() + SESSION_HOURS * 3600}
        self.sessions[token] = session
        return session

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
