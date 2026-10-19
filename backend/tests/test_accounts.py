import os
import time

import pytest

from urban.accounts import MAX_FAILURES, SESSION_HOURS, Accounts, allows


@pytest.fixture
def acc(tmp_path):
    a = Accounts(tmp_path / "users.json")
    a.create("ops", "correct horse battery", "operator")
    return a


def test_bootstrap_creates_one_admin_once(tmp_path):
    a = Accounts(tmp_path / "users.json")
    pw = a.bootstrap()
    assert pw and a.users["admin"]["role"] == "admin"
    assert a.bootstrap() is None
    assert a.sign_in("admin", pw)["role"] == "admin"
    assert Accounts(tmp_path / "users.json").users["admin"]["hash"] == a.users["admin"]["hash"]   # persisted


def test_create_validates_and_never_stores_the_password(acc):
    for bad in [("", "longenoughpw", "operator"), ("x y", "longenoughpw", "operator"),
                ("ok", "short", "operator"), ("ok", "longenoughpw", "god"), ("ops", "longenoughpw", "viewer")]:
        with pytest.raises(ValueError):
            acc.create(*bad)
    assert "correct horse battery" not in acc.path.read_text(encoding="utf-8")


def test_sign_in_and_sessions(acc):
    assert acc.sign_in("ops", "wrong") is None
    s = acc.sign_in("ops", "correct horse battery")
    assert acc.session(s["token"])["name"] == "ops"
    assert acc.session("nope") is None and acc.session("") is None
    acc.sign_out(s["token"])
    assert acc.session(s["token"]) is None


def test_session_expires_and_disabling_ends_it(acc, monkeypatch):
    s = acc.sign_in("ops", "correct horse battery")
    acc.sessions[s["token"]]["expires"] = time.time() - 1
    assert acc.session(s["token"]) is None
    s = acc.sign_in("ops", "correct horse battery")
    acc.set_enabled("ops", False)
    assert acc.session(s["token"]) is None and acc.sign_in("ops", "correct horse battery") is None
    acc.set_enabled("ops", True)
    assert acc.sign_in("ops", "correct horse battery") is not None


def test_guessing_is_throttled_per_account_and_per_address(acc):
    for _ in range(MAX_FAILURES):
        assert acc.sign_in("ops", "wrong", "10.0.0.1") is None
    assert acc.sign_in("ops", "correct horse battery", "10.0.0.2") is None   # the account is throttled
    acc.create("other", "another long password", "viewer")
    assert acc.sign_in("other", "another long password", "10.0.0.1") is None  # so is the address
    assert acc.sign_in("other", "another long password", "10.0.0.3") is not None


def test_password_change_signs_the_user_out(acc):
    s = acc.sign_in("ops", "correct horse battery")
    acc.set_password("ops", "a brand new password")
    assert acc.session(s["token"]) is None
    assert acc.sign_in("ops", "correct horse battery") is None
    assert acc.sign_in("ops", "a brand new password") is not None


def test_roles_are_ordered():
    assert allows("admin", "viewer") and allows("operator", "operator") and not allows("viewer", "operator")
    assert not allows("nobody", "viewer")
    assert SESSION_HOURS > 0


def test_the_first_admin_password_is_also_kept_in_a_private_file(tmp_path):
    from urban.accounts import keep_first_password
    a = Accounts(tmp_path / "users.json")
    pw = a.bootstrap()
    path = keep_first_password(tmp_path / "state" / "first-admin-password", pw)
    assert path.read_text(encoding="utf-8") == pw + "\n"
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0  # owner only
    keep_first_password(path, "second-password")  # a new first password (users file wiped) replaces it
    assert path.read_text(encoding="utf-8") == "second-password\n"
