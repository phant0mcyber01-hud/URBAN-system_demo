"""Second factor: TOTP codes, enrolment, replay, roles that require it, reset."""
import pytest

from urban.accounts import Accounts, CodeRequired, b32, totp, unb32

PASSWORD = "correct horse battery"


def test_rfc6238_vector():
    secret = b"12345678901234567890"
    assert totp(secret, 59 // 30, digits=8) == "94287082"
    assert totp(secret, 1111111109 // 30, digits=8) == "07081804"
    assert totp(secret, 59 // 30) == "287082"
    assert unb32(b32(secret)) == secret and unb32("gezd gnbv") == unb32("GEZDGNBV")


@pytest.fixture
def clock():
    return [1_800_000_000.0]


def enrol(acc, name, clock):
    secret = unb32(acc.start_mfa(name)["secret"])
    assert acc.confirm_mfa(name, totp(secret, int(clock[0] // 30)))
    return secret


def test_enrolment_then_sign_in_needs_a_fresh_code(tmp_path, clock):
    acc = Accounts(tmp_path / "u.json", clock=lambda: clock[0])
    acc.create("ops", PASSWORD, "operator")
    before = acc.sign_in("ops", PASSWORD)
    assert before["role"] == "operator" and not before["mfa_setup"]
    started = acc.start_mfa("ops")
    assert started["uri"].startswith("otpauth://totp/URBAN%3Aops?secret=") and "issuer=URBAN" in started["uri"]
    assert not acc.confirm_mfa("ops", "000000")
    secret = unb32(started["secret"])
    assert acc.confirm_mfa("ops", totp(secret, int(clock[0] // 30)))
    assert acc.session(before["token"]) is None                      # enrolling ends old sessions
    assert acc.public("ops")["mfa"]

    with pytest.raises(CodeRequired):
        acc.sign_in("ops", PASSWORD)
    assert acc.sign_in("ops", "wrong", code="123456") is None
    clock[0] += 60
    code = totp(secret, int(clock[0] // 30))
    s = acc.sign_in("ops", PASSWORD, code=code)
    assert s and s["role"] == "operator"
    assert acc.sign_in("ops", PASSWORD, code=code) is None           # the same code twice: refused
    assert acc.sign_in("ops", PASSWORD, code="12345") is None         # malformed


def test_confirmation_code_cannot_be_used_to_sign_in(tmp_path, clock):
    acc = Accounts(tmp_path / "u.json", clock=lambda: clock[0])
    acc.create("ops", PASSWORD, "operator")
    started = acc.start_mfa("ops")
    code = totp(unb32(started["secret"]), int(clock[0] // 30))
    assert acc.confirm_mfa("ops", code)
    assert acc.sign_in("ops", PASSWORD, code=code) is None


def test_codes_within_one_step_of_the_clock(tmp_path, clock):
    acc = Accounts(tmp_path / "u.json", clock=lambda: clock[0])
    acc.create("ops", PASSWORD, "operator")
    secret = enrol(acc, "ops", clock)
    clock[0] += 300
    now = int(clock[0] // 30)
    assert acc.sign_in("ops", PASSWORD, code=totp(secret, now - 3)) is None
    assert acc.sign_in("ops", PASSWORD, code=totp(secret, now + 1)) is not None
    assert acc.sign_in("ops", PASSWORD, code=totp(secret, now)) is None  # older than the last accepted step


def test_required_roles_get_viewer_rights_until_enrolled(tmp_path, clock):
    acc = Accounts(tmp_path / "u.json", mfa_roles={"operator", "admin"}, clock=lambda: clock[0])
    acc.create("ops", PASSWORD, "operator")
    acc.create("watch", PASSWORD, "viewer")
    s = acc.sign_in("ops", PASSWORD)
    assert s["role"] == "viewer" and s["mfa_setup"]
    assert acc.sign_in("watch", PASSWORD)["mfa_setup"] is False
    assert acc.public("ops")["mfa_required"] and not acc.public("watch")["mfa_required"]
    enrol(acc, "ops", clock)
    clock[0] += 30
    secret = unb32(acc.users["ops"]["totp"])
    s = acc.sign_in("ops", PASSWORD, code=totp(secret, int(clock[0] // 30)))
    assert s["role"] == "operator" and not s["mfa_setup"]


def test_pending_enrolment_expires(tmp_path, clock):
    acc = Accounts(tmp_path / "u.json", clock=lambda: clock[0])
    acc.create("ops", PASSWORD, "operator")
    secret = unb32(acc.start_mfa("ops")["secret"])
    clock[0] += 601
    assert not acc.confirm_mfa("ops", totp(secret, int(clock[0] // 30)))
    assert not acc.confirm_mfa("nobody", "123456")


def test_reset_from_another_process_ends_sessions(tmp_path, clock):
    path = tmp_path / "u.json"
    server = Accounts(path, clock=lambda: clock[0])
    server.create("ops", PASSWORD, "operator")
    secret = enrol(server, "ops", clock)
    clock[0] += 30
    s = server.sign_in("ops", PASSWORD, code=totp(secret, int(clock[0] // 30)))
    import time
    time.sleep(0.01)
    Accounts(path).reset_mfa("ops")                 # python -m urban.users mfa-reset ops
    assert server.session(s["token"]) is None
    assert server.sign_in("ops", PASSWORD) is not None  # no code needed any more
