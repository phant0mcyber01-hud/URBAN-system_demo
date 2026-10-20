"""Device keys of two kinds: an Ed25519 file or an ECDSA P-256 key inside the TPM. Both sign and verify, the centre
authenticates both and refuses a signature of the wrong kind or a forged one, enrolment carries either public key, every
client signs through one Signer (the TPM's when the unit has one), the TPM signer drives tpm2-tools exactly as specified
(checked against a fake that behaves like tpm2-tools and keeps a real key in its 'chip'), and `--keygen --tpm` does what
the docs say. There is no TPM in the test machine: the fake stands for it."""
import asyncio
import functools
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from fastapi import WebSocketDisconnect

from urban.devices import crypto, signer
from urban.devices.enrolment import install_kit, make_code, make_kit, parse_code
from urban.devices.hub import DeviceHub
from urban.devices.registry import ACTIVE, Registry, kind_of, provision
from urban.devices.signer import FileSigner, SignerError, TpmSigner, create_tpm_key, load_signer
from urban.edge.__main__ import keygen
from urban.edge.device import EdgeDevice
from urban.equipment.adapter import NoController
from urban.node.district import DistrictDevice
from urban.modules.lighting.device import EmulatedAdapter, LightingCabinet

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]
POSIX = os.name == "posix"
OBJ = {"device": "edge-J-01", "nonce": "abc"}

CREATEPRIMARY = ["tpm2_createprimary", "-C", "o", "-g", "sha256", "-G", "ecc256", "-c", "primary.ctx"]
CREATE = ["tpm2_create", "-C", "primary.ctx", "-G", "ecc256:ecdsa-sha256", "-a",
          "fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign", "-u", "key.pub", "-r", "key.priv"]
LOAD_NEW = ["tpm2_load", "-C", "primary.ctx", "-u", "key.pub", "-r", "key.priv", "-c", "key.ctx"]
READPUBLIC = ["tpm2_readpublic", "-c", "key.ctx", "-f", "der", "-o", "pub.der"]
SIGN = ["tpm2_sign", "-c", "key.ctx", "-g", "sha256", "-s", "ecdsa", "-f", "plain", "-o", "sig.bin", "msg.bin"]


# ---------------------------------------------------------------- a fake that behaves like tpm2-tools 5.x

class FakeTpm:
    """Stands for /dev/tpmrm0 and the tpm2 command line tools. The 'chip' keeps real P-256 keys in memory; files the tools
    write are opaque handles and wrapped blobs, never key bytes (every call checks that no file in its directory holds a
    private scalar). A key wrapped by one chip does not load under the primary of another, as in a real TPM."""

    def __init__(self, chip="chip-A", plain="der", fail=None, hang=None):
        self.chip, self.plain, self.fail, self.hang = chip, plain, fail, hang
        self.keys = {}      # blob id -> private key, the chip's secret memory
        self.calls = []     # (argv, cwd, env) of every command
        self.dirs = []      # the working directories used

    def secrets(self):
        return [k.private_numbers().private_value.to_bytes(32, "big") for k in self.keys.values()]

    def __call__(self, argv, cwd=None, env=None, capture_output=False, timeout=None, check=True):
        assert capture_output is True and check is False and timeout and 0 < timeout <= 60, "timeouts on every call"
        assert env["TPM2TOOLS_TCTI"].startswith("device:"), "the TCTI goes through the environment"
        work = Path(cwd)
        self.calls.append((list(argv), work, env))
        if work not in self.dirs:
            self.dirs.append(work)
        if self.hang == argv[0]:
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.fail and self.fail[0] == argv[0]:
            return SimpleNamespace(returncode=self.fail[1], stdout=b"", stderr=self.fail[2].encode())
        out = self._run(list(argv), work)
        for path in work.iterdir():  # nothing the tools leave behind may hold a private key
            data = path.read_bytes()
            assert not any(s in data for s in self.secrets()), f"private key bytes in {path}"
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    def _fail(self, text):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=text.encode())

    def _run(self, argv, work):
        tool, args = argv[0], argv[1:]
        if tool == "tpm2_createprimary":
            assert argv == CREATEPRIMARY
            (work / "primary.ctx").write_bytes(f"CTX:primary:{self.chip}".encode())
        elif tool == "tpm2_create":
            assert argv == CREATE
            assert (work / "primary.ctx").read_bytes() == f"CTX:primary:{self.chip}".encode()
            key = ec.generate_private_key(ec.SECP256R1())
            blob = os.urandom(8).hex()
            self.keys[blob] = key
            (work / "key.pub").write_bytes(key.public_key().public_bytes(*crypto.POINT))
            (work / "key.priv").write_bytes(f"WRAPPED:{self.chip}:{blob}".encode())
        elif tool == "tpm2_load":
            parent = (work / args[args.index("-C") + 1]).read_bytes().decode().split(":")[-1]
            wrapped = Path(args[args.index("-r") + 1])
            wrapped = wrapped if wrapped.is_absolute() else work / wrapped
            _, chip, blob = wrapped.read_bytes().decode().split(":")
            if chip != parent or blob not in self.keys:
                raise _Refused("Error: integrity check failed: the key does not belong under this primary")
            (work / args[args.index("-c") + 1]).write_bytes(f"CTX:key:{blob}".encode())
        elif tool == "tpm2_readpublic":
            assert argv == READPUBLIC
            key = self.keys[(work / "key.ctx").read_bytes().decode().split(":")[-1]]
            (work / "pub.der").write_bytes(key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        elif tool == "tpm2_sign":
            assert argv == SIGN
            key = self.keys[(work / "key.ctx").read_bytes().decode().split(":")[-1]]
            der = key.sign((work / "msg.bin").read_bytes(), ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            (work / "sig.bin").write_bytes(der if self.plain == "der" else r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        else:
            raise AssertionError(f"unexpected command {argv}")
        return b""


class _Refused(Exception):
    pass


def _run_fake(fake):
    """The fake as a runner: a refused command is a non-zero exit, like the real tool."""
    def runner(argv, **kw):
        try:
            return fake(argv, **kw)
        except _Refused as err:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=str(err).encode())
    runner.fake = fake
    return runner


def tpm_key(pki, unit="edge-J-01", chip="chip-A", **kw):
    """A unit with a TPM key made through the fake: (runner, public key text)."""
    fake = FakeTpm(chip, **kw)
    runner = _run_fake(fake)
    return runner, create_tpm_key(Path(pki) / "edge" / f"{unit}.tpm", runner=runner)


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path, demo_devices=False)
    return tmp_path


def ed():
    k = crypto.new_key()
    return k, crypto.public_hex(k)


def p256():
    k = crypto.new_p256_key()
    return k, crypto.public_hex(k)


# ---------------------------------------------------------------- the two kinds sign and verify

def test_both_kinds_sign_and_verify_and_say_what_they_are():
    ek, epub = ed()
    pk, ppub = p256()
    assert len(epub) == 64 and crypto.public_kind(epub) == "ed25519"
    assert len(ppub) == 130 and ppub.startswith("04") and crypto.public_kind(ppub) == "tpm-p256"
    assert ppub == ppub.lower() and crypto.public_kind(ppub.upper()) == "tpm-p256"
    for key, pub in ((ek, epub), (pk, ppub)):
        sig = crypto.sign(key, OBJ)
        assert crypto.verify_text(pub, OBJ, sig) and crypto.verify_text(pub.upper(), OBJ, sig)
        assert not crypto.verify_text(pub, {**OBJ, "nonce": "other"}, sig)
        assert crypto.check_public(f" {pub.upper()}\n") == pub
        fp = crypto.fingerprint(pub)
        assert len(fp) == 19 and fp.count("-") == 3 and fp == crypto.fingerprint(pub + "\n")
    assert crypto.fingerprint(epub) != crypto.fingerprint(ppub)
    der = bytes.fromhex(crypto.sign(pk, OBJ))
    assert der[0] == 0x30 and encode_dss_signature(*decode_dss_signature(der)) == der  # hex of a DER signature
    assert len(bytes.fromhex(crypto.sign(ek, OBJ))) == 64
    # the P-256 signature is ECDSA over SHA-256 of the canonical bytes, whatever made it
    pk.public_key().verify(der, crypto.canonical(OBJ), ec.ECDSA(hashes.SHA256()))


def test_a_signature_of_the_wrong_kind_or_a_forged_one_fails_cleanly():
    ek, epub = ed()
    pk, ppub = p256()
    other_e, other_epub = ed()
    other_p, other_ppub = p256()
    esig, psig = crypto.sign(ek, OBJ), crypto.sign(pk, OBJ)
    assert not crypto.verify_text(epub, OBJ, psig) and not crypto.verify_text(ppub, OBJ, esig)  # cross-kind
    assert not crypto.verify_text(other_epub, OBJ, esig) and not crypto.verify_text(other_ppub, OBJ, psig)  # forged
    off_curve = "04" + "01" * 64
    for pub in (epub, ppub):
        for bad in ("", "zz", "not hex at all", "0", esig[:-2], psig[:-2], esig + "00", psig + "00", "30" * 40, "00" * 64,
                    None, 5, "None"):
            assert crypto.verify_text(pub, OBJ, bad) is False
    for bad_pub in ("", "zz", epub[:-2], ppub[:-2], ppub + "00", off_curve, "05" + ppub[2:], "04" + "00" * 64,
                    "ff" * 65, None, 7):
        assert crypto.verify_text(bad_pub, OBJ, psig) is False and crypto.verify_text(bad_pub, OBJ, esig) is False
        with pytest.raises(ValueError):
            crypto.check_public(str(bad_pub))
    # a P-256 public key that is not on the curve is no key, by the library and by us
    with pytest.raises(ValueError):
        crypto.load_device_public(off_curve)
    # a signature over a different message, a tampered r, and a high-S twin (still valid ECDSA) behave as expected
    r, s = decode_dss_signature(bytes.fromhex(psig))
    assert crypto.verify_text(ppub, OBJ, encode_dss_signature(r, s).hex())
    assert not crypto.verify_text(ppub, OBJ, encode_dss_signature(r ^ 1, s).hex())
    assert not crypto.verify_text(ppub, {"device": "edge-J-02", "nonce": "abc"}, psig)


def test_the_gateway_and_root_stay_ed25519():
    key = crypto.new_key()
    pub = crypto.public_hex(key)
    assert crypto.verify(crypto.load_public(pub), OBJ, crypto.sign(key, OBJ))  # unchanged helpers
    with pytest.raises(ValueError):
        crypto.load_public(p256()[1])  # a P-256 key is no gateway key


# ---------------------------------------------------------------- enrolment

def test_enrolment_codes_carry_either_public_key():
    _, epub = ed()
    _, ppub = p256()
    for pub in (epub, ppub):
        code = make_code("edge-J-07", "URB-1", pub)
        assert code.startswith("URBAN1:edge-J-07:URB-1:" + pub + ":") and code.count(":") == 4
        assert parse_code(code) == {"device": "edge-J-07", "serial": "URB-1", "public": pub}
        assert parse_code(" \n".join([code[:20], code[20:60], code[60:]]))["public"] == pub  # line breaks from copying
        damaged = code.replace(pub[:8], pub[:7] + ("0" if pub[7] != "0" else "1"), 1)
        with pytest.raises(ValueError, match="контрольная сумма"):
            parse_code(damaged)
    # a well-formed code (right check sum) around something that is not a key is refused
    for junk in ("04" + "01" * 64, "04" + ppub[2:-2], ppub[2:], "ab" * 40):
        with pytest.raises(ValueError, match="нет открытого ключа"):
            parse_code(make_code("edge-J-07", "", junk))


def test_the_registry_enrols_and_authenticates_either_kind(pki):
    reg = Registry(pki / "devices.json")
    ek, epub = ed()
    pk, ppub = p256()
    qk, qpub = p256()
    rec = reg.enroll("edge-J-01", JUNCTIONS[0], ppub.upper(), "URB-P")
    assert rec["public_key"] == ppub and rec["status"] == ACTIVE
    reg.enroll("edge-J-02", JUNCTIONS[1], epub, "URB-E")
    reg.enroll_district("district-1", "10.1.0.2", qpub, "SRV-1")
    lk, lpub = p256()
    reg.enroll_lighting("light-C-01", "C-01", lpub, "LGT-1")
    assert [kind_of(reg.devices[d]) for d in ("edge-J-01", "district-1", "light-C-01")] == ["unit", "district", "lighting"]
    for device, key in (("edge-J-01", pk), ("edge-J-02", ek), ("district-1", qk), ("light-C-01", lk)):
        sig = crypto.sign(key, {"device": device, "nonce": "n1"})
        assert reg.authenticate(device, "n1", sig)["id"] == device
        assert reg.authenticate(device, "n2", sig) is None  # another nonce: a replayed answer
    assert reg.authenticate("edge-J-01", "n1", crypto.sign(ek, {"device": "edge-J-01", "nonce": "n1"})) is None  # wrong kind
    assert reg.authenticate("edge-J-02", "n1", crypto.sign(pk, {"device": "edge-J-02", "nonce": "n1"})) is None
    assert reg.authenticate("edge-J-01", "n1", crypto.sign(qk, {"device": "edge-J-01", "nonce": "n1"})) is None  # forged
    assert reg.authenticate("edge-J-01", "n1", "zz") is None and reg.authenticate("nobody", "n1", "zz") is None
    # one key, one device, in either form of the text; and a key that is not a key is refused by every enrolment
    with pytest.raises(ValueError, match="already enrolled as edge-J-01"):
        reg.enroll_district("district-2", "10.1.0.3", ppub.upper())
    with pytest.raises(ValueError, match="already enrolled"):
        reg.enroll_lighting("light-C-02", "C-02", ppub)
    for bad in ("04" + "01" * 64, "zz", ppub[:-2], ""):
        with pytest.raises(ValueError):
            reg.enroll("edge-J-03", {"id": "J-03", "tls": "T3"}, bad)
        with pytest.raises(ValueError):
            reg.enroll_district("district-3", "10.1.0.4", bad)
        with pytest.raises(ValueError):
            reg.enroll_lighting("light-C-03", "C-03", bad)
    assert set(Registry(pki / "devices.json").devices) == {"edge-J-01", "edge-J-02", "district-1", "light-C-01"}
    assert Registry(pki / "devices.json").devices["edge-J-01"]["public_key"] == ppub  # survives the file


class FakeSocket:
    """The device's end of the device socket: answers the challenge with `hello(nonce)`, then sends the reports."""

    def __init__(self, hello, reports=()):
        self.hello, self.todo, self.sent, self.closed = hello, list(reports), [], None

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def receive_text(self):
        if len(self.sent) == 1:
            return json.dumps(self.hello(self.sent[0]["nonce"]))
        if self.todo:
            return json.dumps(self.todo.pop(0))
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = code


def hub_for(pki, seen=None):
    return DeviceHub(Registry(pki / "devices.json"), crypto.load_private((pki / "gateway.key").read_text()),
                     on_report=lambda rec, report: (seen if seen is not None else []).append(rec), clock=lambda: 100,
                     plans={"T1": {1, 2}})


def test_a_p256_device_authenticates_through_the_hub_and_a_wrong_kind_does_not(pki):
    pk, ppub = p256()
    ek, _ = ed()
    seen = []
    hub = hub_for(pki, seen)
    hub.registry.enroll("edge-J-01", JUNCTIONS[0], ppub)

    def hello(key):
        return lambda nonce: {"type": "hello", "device": "edge-J-01", "version": "0.1.0", "epoch": 1,
                              "sig": crypto.sign(key, {"device": "edge-J-01", "nonce": nonce})}
    ws = FakeSocket(hello(pk), [{"type": "report", "t": 1, "health": {}}])
    asyncio.run(hub.serve(ws))
    assert ws.sent[1]["type"] == "welcome" and ws.sent[1]["tls"] == "T1" and ws.sent[1]["plans"] == [1, 2]
    assert [r["id"] for r in seen] == ["edge-J-01"] and hub.registry.devices["edge-J-01"].get("first_seen")
    assert hub.status()[0]["fingerprint"] == crypto.fingerprint(ppub)
    wrong = FakeSocket(hello(ek))  # an Ed25519 key signing for a P-256 device
    asyncio.run(hub.serve(wrong))
    assert wrong.closed == 4401 and len(wrong.sent) == 1


# ---------------------------------------------------------------- the TPM signer against tpm2-tools

def tree(path):
    return sorted(str(p.relative_to(path)).replace("\\", "/") for p in Path(path).rglob("*"))


def test_creating_a_tpm_key_runs_exactly_the_specified_commands_and_leaves_no_private_key(tmp_path):
    runner = _run_fake(FakeTpm())
    fake = runner.fake
    target = tmp_path / "pki" / "edge" / "edge-J-01.tpm"
    public = create_tpm_key(target, runner=runner, tcti="device:/dev/tpmrm0")
    assert [argv for argv, _, _ in fake.calls] == [CREATEPRIMARY, CREATE, LOAD_NEW, READPUBLIC]
    assert all(env["TPM2TOOLS_TCTI"] == "device:/dev/tpmrm0" for _, _, env in fake.calls)
    assert len(public) == 130 and public.startswith("04") and crypto.public_kind(public) == "tpm-p256"
    assert tree(target) == ["key.priv", "key.pub", "public.hex"]  # nothing else: no contexts, no DER
    assert (target / "public.hex").read_text().strip() == public
    assert list(fake.keys.values())[0].public_key().public_bytes(*crypto.POINT).hex() == public
    for path in target.iterdir():
        assert not any(s in path.read_bytes() for s in fake.secrets())
        if POSIX:
            assert path.stat().st_mode & 0o777 == 0o600
    if POSIX:
        assert target.stat().st_mode & 0o777 == 0o700
    assert not any(p.exists() for p in fake.dirs)  # the working directory is gone
    assert tree(target.parent) == ["edge-J-01.tpm", "edge-J-01.tpm/key.priv", "edge-J-01.tpm/key.pub",
                                   "edge-J-01.tpm/public.hex"]
    with pytest.raises(SignerError, match="exists already"):
        create_tpm_key(target, runner=runner)  # one key per unit, never replaced


def test_signing_with_the_tpm_loads_the_key_under_a_fresh_primary_and_verifies(tmp_path):
    runner, public = tpm_key(tmp_path)
    fake = runner.fake
    fake.calls.clear()
    signer_ = TpmSigner(tmp_path / "edge" / "edge-J-01.tpm", runner=runner, tcti="device:/dev/tpmrm0")
    assert signer_.kind == "tpm-p256" and signer_.public() == public
    sig = signer_.sign(OBJ)
    assert crypto.verify_text(public, OBJ, sig) and crypto.sign(signer_, OBJ) != "" and bytes.fromhex(sig)[0] == 0x30
    argvs = [argv for argv, _, _ in fake.calls]
    key_dir = tmp_path / "edge" / "edge-J-01.tpm"
    assert argvs[0] == CREATEPRIMARY
    assert argvs[1] == ["tpm2_load", "-C", "primary.ctx", "-u", str(key_dir / "key.pub"), "-r", str(key_dir / "key.priv"),
                        "-c", "key.ctx"]
    assert argvs[2] == SIGN
    work = fake.calls[0][1]
    assert all(cwd == work for _, cwd, _ in fake.calls[:3])           # one private working directory per signature
    assert work.name.startswith("urban-tpm-") and not work.exists()  # and it is cleaned afterwards
    assert not any(p.exists() for p in fake.dirs)
    assert tree(key_dir) == ["key.priv", "key.pub", "public.hex"]     # signing writes nothing next to the key
    assert not crypto.verify_text(public, {**OBJ, "nonce": "x"}, sig)


@pytest.mark.parametrize("plain", ["der", "raw"])
def test_either_form_of_plain_signature_comes_out_as_der_hex(tmp_path, plain):
    runner, public = tpm_key(tmp_path, plain=plain)
    sig = TpmSigner(tmp_path / "edge" / "edge-J-01.tpm", runner=runner).sign({"x": [1, 2, "я"]})
    assert crypto.verify_text(public, {"x": [1, 2, "я"]}, sig)
    der = bytes.fromhex(sig)
    assert der[0] == 0x30 and encode_dss_signature(*decode_dss_signature(der)) == der


def test_tpm_failures_are_clear_signer_errors_and_leave_nothing_behind(tmp_path):
    key_dir = tmp_path / "edge" / "edge-J-01.tpm"
    runner, public = tpm_key(tmp_path)
    fake = runner.fake
    # a tool that fails: its own words reach the log
    fake.fail = ("tpm2_sign", 1, "ERROR: Esys_Sign(0x9a2) - TPM2_RC_AUTH_FAIL")
    with pytest.raises(SignerError, match=r"tpm2_sign failed \(1\).*TPM2_RC_AUTH_FAIL"):
        TpmSigner(key_dir, runner=runner).sign(OBJ)
    # a TPM that hangs
    fake.fail, fake.hang = None, "tpm2_createprimary"
    with pytest.raises(SignerError, match="no answer"):
        TpmSigner(key_dir, runner=runner).sign(OBJ)
    # tpm2-tools not installed
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    with pytest.raises(SignerError, match="not installed"):
        TpmSigner(key_dir, runner=missing).sign(OBJ)
    # the blob of another machine's TPM does not load here
    other, _ = tpm_key(tmp_path / "other", chip="chip-B")
    stolen = tmp_path / "stolen"
    stolen.mkdir()
    for name in ("key.pub", "key.priv", "public.hex"):
        (stolen / name).write_bytes((key_dir / name).read_bytes())
    with pytest.raises(SignerError, match="integrity check failed"):
        TpmSigner(stolen, runner=other).sign(OBJ)
    assert not any(p.exists() for p in fake.dirs + other.fake.dirs)
    # a signature that does not verify against public.hex is never returned
    fake.hang = None
    other_public = tpm_key(tmp_path / "third", chip="chip-A")[1]
    liar = tmp_path / "liar"
    liar.mkdir()
    for name in ("key.pub", "key.priv"):
        (liar / name).write_bytes((key_dir / name).read_bytes())
    (liar / "public.hex").write_text(other_public)
    with pytest.raises(SignerError, match="does not verify"):
        TpmSigner(liar, runner=runner).sign(OBJ)
    # creating: a failing step leaves neither the folder nor a half-made one
    broken = tmp_path / "pki2" / "edge" / "edge-J-09.tpm"
    for step in ("tpm2_createprimary", "tpm2_create", "tpm2_load", "tpm2_readpublic"):
        with pytest.raises(SignerError, match=step):
            create_tpm_key(broken, runner=_run_fake(FakeTpm(fail=(step, 2, "no TPM"))))
        assert not broken.exists() and tree(broken.parent) == []
    # a damaged key folder is refused when the signer is made, before anything runs
    (key_dir / "key.priv").unlink()
    with pytest.raises(SignerError, match="key.priv"):
        TpmSigner(key_dir, runner=runner)
    (key_dir / "key.priv").write_bytes(b"x")
    (key_dir / "public.hex").write_text("ab" * 32)
    with pytest.raises(SignerError, match="P-256"):
        TpmSigner(key_dir, runner=runner).public()


def test_the_signer_cli_prints_the_public_key_kind_and_signature(tmp_path, capsys):
    key = crypto.new_key()
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "light-C-01.key").write_text(crypto.private_hex(key))
    base = ["--pki", str(tmp_path), "--unit", "light-C-01"]
    assert signer.main([*base, "--kind"]) == 0 and capsys.readouterr().out.strip() == "ed25519"
    assert signer.main([*base, "--public"]) == 0 and capsys.readouterr().out.strip() == crypto.public_hex(key)
    assert signer.main([*base, "--sign", "hello"]) == 0
    assert crypto.verify_text(crypto.public_hex(key), "hello", capsys.readouterr().out.strip())
    runner, public = tpm_key(tmp_path, "edge-J-01")
    base = ["--pki", str(tmp_path), "--unit", "edge-J-01"]
    assert signer.main([*base, "--kind"], runner=runner) == 0 and capsys.readouterr().out.strip() == "tpm-p256"
    assert signer.main([*base, "--public"], runner=runner) == 0 and capsys.readouterr().out.strip() == public
    assert signer.main([*base, "--sign", "hello"], runner=runner) == 0
    assert crypto.verify_text(public, "hello", capsys.readouterr().out.strip())
    assert signer.main([*base, "--sign", "hello"], runner=_run_fake(FakeTpm(chip="chip-B"))) == 1  # the other machine
    assert "integrity check failed" in capsys.readouterr().err
    assert signer.main(["--pki", str(tmp_path), "--unit", "edge-none", "--public"]) == 1
    assert "no key for edge-none" in capsys.readouterr().err


# ---------------------------------------------------------------- one signer for every client

def test_load_signer_picks_the_tpm_key_when_the_unit_has_one_else_the_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_signer(tmp_path, "edge-J-01")
    key = crypto.new_key()
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge" / "edge-J-01.key").write_text(crypto.private_hex(key))
    s = load_signer(tmp_path, "edge-J-01")
    assert isinstance(s, FileSigner) and s.kind == "ed25519" and s.public() == crypto.public_hex(key)
    assert crypto.verify_text(s.public(), OBJ, s.sign(OBJ)) and crypto.verify_text(s.public(), OBJ, crypto.sign(s, OBJ))
    elsewhere = tmp_path / "elsewhere.key"
    elsewhere.write_text(crypto.private_hex(other := crypto.new_key()))
    assert load_signer(tmp_path, "edge-J-01", elsewhere).public() == crypto.public_hex(other)
    assert load_signer(tmp_path, "edge-J-01", str(elsewhere)).public() == crypto.public_hex(other)
    runner, public = tpm_key(tmp_path, "edge-J-02")
    t = load_signer(tmp_path, "edge-J-02", runner=runner)
    assert isinstance(t, TpmSigner) and t.kind == "tpm-p256" and t.public() == public
    assert crypto.verify_text(public, OBJ, t.sign(OBJ)) and crypto.verify_text(public, OBJ, crypto.sign(t, OBJ))
    both = tmp_path / "edge" / "edge-J-02.key"  # a TPM folder next to a file: the TPM wins, nobody signs from the file
    both.write_text(crypto.private_hex(crypto.new_key()))
    assert isinstance(load_signer(tmp_path, "edge-J-02"), TpmSigner)
    (tmp_path / "edge" / "edge-J-02.tpm" / "key.pub").unlink()
    with pytest.raises(SignerError):
        load_signer(tmp_path, "edge-J-02")  # damaged: refused, not silently replaced by the file
    bad = tmp_path / "edge" / "edge-J-03.key"
    bad.write_text("nothex")
    with pytest.raises(ValueError):
        load_signer(tmp_path, "edge-J-03")


def test_a_kit_goes_in_place_for_either_kind_of_key_and_only_on_its_own_unit(tmp_path, pki):
    unit_pki = tmp_path / "unit"
    (unit_pki / "edge").mkdir(parents=True)
    runner, public = tpm_key(unit_pki, "edge-J-01")
    reg = Registry(pki / "devices.json")
    rec = reg.enroll("edge-J-01", JUNCTIONS[0], public, "URB-P")
    kit = make_kit(rec, pki, "city.example", 8443, [])
    assert kit["fingerprint"] == crypto.fingerprint(public)
    env = tmp_path / "unit.env"
    key_path = unit_pki / "edge" / "edge-J-01.key"  # absent: the TPM folder is the key
    written = install_kit(kit, "edge-J-01", key_path, unit_pki, env)
    assert str(env) in written and (unit_pki / "ca.pem").exists()
    # a kit made for another key is refused, and nothing is written: a unit with another TPM key, one with a file key
    fresh = tmp_path / "fresh"
    (fresh / "edge").mkdir(parents=True)
    tpm_key(fresh, "edge-J-01", chip="chip-C")
    with pytest.raises(ValueError, match="kit was made for key"):
        install_kit(kit, "edge-J-01", fresh / "edge" / "edge-J-01.key", fresh, tmp_path / "fresh.env")
    filed = tmp_path / "filed"
    (filed / "edge").mkdir(parents=True)
    (filed / "edge" / "edge-J-01.key").write_text(crypto.private_hex(crypto.new_key()))
    with pytest.raises(ValueError, match="kit was made for key"):
        install_kit(kit, "edge-J-01", filed / "edge" / "edge-J-01.key", filed, tmp_path / "filed.env")
    assert not (fresh / "ca.pem").exists() and not (filed / "ca.pem").exists()
    assert not (tmp_path / "fresh.env").exists() and not (tmp_path / "filed.env").exists()
    with pytest.raises(ValueError, match="no key for edge-J-05"):
        install_kit({**kit, "unit": "edge-J-05"}, "edge-J-05", unit_pki / "edge" / "edge-J-05.key", unit_pki, env)
    # an Ed25519 unit as before
    (unit_pki / "edge" / "edge-J-02.key").write_text(crypto.private_hex(k := crypto.new_key()))
    rec3 = reg.enroll("edge-J-02", JUNCTIONS[1], crypto.public_hex(k), "URB-E")
    install_kit(make_kit(rec3, pki, "city.example", 8443, []), "edge-J-02", unit_pki / "edge" / "edge-J-02.key", unit_pki,
                tmp_path / "unit2.env")


# ---------------------------------------------------------------- keygen --tpm auto | require | off

@pytest.fixture
def tpm_env(monkeypatch):
    """A machine with a (fake) TPM: the existence check says yes, the TPM key is made through the fake runner."""
    fake = FakeTpm()
    runner = _run_fake(fake)
    monkeypatch.setattr(signer, "tpm_present", lambda: True)
    monkeypatch.setattr(signer, "create_tpm_key", functools.partial(create_tpm_key, runner=runner))
    return fake


def run_keygen(pki, capsys, tpm="auto", unit="edge-J-01", key=None):
    keygen(SimpleNamespace(unit=unit, key=key, pki=str(pki), serial="URB-0042", tpm=tpm))
    out = capsys.readouterr().out
    code = parse_code(next(line.strip() for line in out.splitlines() if "URBAN1:" in line))
    return out, code


def key_line(out):
    return [line for line in out.splitlines() if line.startswith("key: ")]


def test_keygen_auto_uses_the_tpm_when_there_is_one_and_reuses_the_key(tmp_path, capsys, tpm_env):
    out, code = run_keygen(tmp_path, capsys)
    assert key_line(out) == ["key: tpm"] and len(code["public"]) == 130 and code["serial"] == "URB-0042"
    assert (tmp_path / "edge" / "edge-J-01.tpm").is_dir() and not (tmp_path / "edge" / "edge-J-01.key").exists()
    assert crypto.fingerprint(code["public"]) in out and "private key: inside the TPM" in out
    assert len(tpm_env.keys) == 1
    again, code2 = run_keygen(tmp_path, capsys)  # idempotent: the same key, no second one, never both kinds
    assert code2 == code and key_line(again) == ["key: tpm"] and len(tpm_env.keys) == 1
    assert not (tmp_path / "edge" / "edge-J-01.key").exists()
    off, code3 = run_keygen(tmp_path, capsys, "off")  # an existing TPM key is reused even with --tpm off
    assert code3 == code and key_line(off) == ["key: tpm"] and not (tmp_path / "edge" / "edge-J-01.key").exists()
    assert load_signer(tmp_path, "edge-J-01").public() == code["public"]


def test_keygen_off_keeps_the_file_even_with_a_tpm(tmp_path, capsys, tpm_env):
    out, code = run_keygen(tmp_path, capsys, "off")
    assert key_line(out) == ["key: file"] and len(code["public"]) == 64 and tpm_env.calls == []
    assert (tmp_path / "edge" / "edge-J-01.key").exists() and not (tmp_path / "edge" / "edge-J-01.tpm").exists()
    assert f"private key: {tmp_path / 'edge' / 'edge-J-01.key'} (stays on this unit)" in out
    again, code2 = run_keygen(tmp_path, capsys, "auto")  # a file key that exists is reused: never a second, TPM one
    assert code2 == code and key_line(again) == ["key: file"] and tpm_env.calls == []
    assert not (tmp_path / "edge" / "edge-J-01.tpm").exists()
    with pytest.raises(SystemExit, match="require"):  # and --tpm require does not pretend a file key is a TPM one
        keygen(SimpleNamespace(unit="edge-J-01", key=None, pki=str(tmp_path), serial="", tpm="require"))
    # an explicit --key path is a file key
    explicit = tmp_path / "somewhere" / "k.key"
    out2, code4 = run_keygen(tmp_path / "other", capsys, "auto", key=str(explicit))
    assert key_line(out2) == ["key: file"] and explicit.exists() and not (tmp_path / "other" / "edge").exists()
    assert crypto.public_hex(crypto.load_private(explicit.read_text())) == code4["public"]


def test_keygen_without_a_tpm_makes_the_file_and_require_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(signer, "tpm_present", lambda: False)
    monkeypatch.setattr(signer, "create_tpm_key", lambda *a, **k: pytest.fail("no TPM: nothing to make"))
    for mode in ("auto", "off"):
        out, code = run_keygen(tmp_path / mode, capsys, mode)
        assert key_line(out) == ["key: file"] and len(code["public"]) == 64
        assert (tmp_path / mode / "edge" / "edge-J-01.key").exists()
    with pytest.raises(SystemExit, match="no TPM"):
        run_keygen(tmp_path / "require", capsys, "require")
    assert not (tmp_path / "require").exists() or tree(tmp_path / "require") == []
    # args from before --tpm existed (no attribute) behave as auto
    keygen(SimpleNamespace(unit="edge-J-02", key=None, pki=str(tmp_path / "old"), serial=""))
    assert (tmp_path / "old" / "edge" / "edge-J-02.key").exists() and "key: file" in capsys.readouterr().out


def test_keygen_auto_falls_back_to_the_file_in_one_line_and_require_fails(tmp_path, capsys, monkeypatch):
    fake = FakeTpm(fail=("tpm2_create", 1, "ERROR: Esys_Create(0x921)\nTPM2_RC_NV_UNAVAILABLE"))
    monkeypatch.setattr(signer, "tpm_present", lambda: True)
    monkeypatch.setattr(signer, "create_tpm_key", functools.partial(create_tpm_key, runner=_run_fake(fake)))
    out, code = run_keygen(tmp_path / "auto", capsys)
    assert len(key_line(out)) == 1 and len(code["public"]) == 64
    assert key_line(out)[0].startswith("key: file (TPM key failed: ") and "TPM2_RC_NV_UNAVAILABLE" in key_line(out)[0]
    assert "\n" not in key_line(out)[0] and key_line(out)[0].endswith(")")
    assert (tmp_path / "auto" / "edge" / "edge-J-01.key").exists() and not (tmp_path / "auto" / "edge" / "edge-J-01.tpm").exists()
    assert tree(tmp_path / "auto" / "edge") == ["edge-J-01.key"]  # no half-made TPM folder
    with pytest.raises(SystemExit, match="TPM2_RC_NV_UNAVAILABLE"):
        run_keygen(tmp_path / "require", capsys, "require")
    assert not (tmp_path / "require" / "edge").exists() or tree(tmp_path / "require" / "edge") == []


def test_the_enrolment_output_has_the_same_lines_for_both_kinds(tmp_path, capsys, tpm_env):
    tpm_out, _ = run_keygen(tmp_path / "t", capsys)
    file_out, _ = run_keygen(tmp_path / "f", capsys, "off")
    shape = lambda out: [line.strip().split(":")[0] for line in out.splitlines() if line.strip()]
    assert shape(tpm_out) == shape(file_out) == ["private key", "key", "fingerprint", "enrolment code (paste it in the UI",
                                                 "URBAN1", "then download the unit's kit there and run"]


# ---------------------------------------------------------------- every client signs through the Signer

def test_a_p256_unit_session_against_the_hub_end_to_end(pki, tmp_path):
    unit_pki = tmp_path / "unit"
    (unit_pki / "edge").mkdir(parents=True)
    runner, public = tpm_key(unit_pki, "edge-J-01")
    seen = []
    hub = hub_for(pki, seen)
    hub.registry.enroll("edge-J-01", JUNCTIONS[0], public, "URB-P")
    gateway = crypto.load_public((pki / "gateway.pub").read_text())
    dev = EdgeDevice("edge-J-01", "T1", load_signer(unit_pki, "edge-J-01", runner=runner), gateway, NoController(),
                     wall=lambda: 1_700_000_000)
    assert dev.key.kind == "tpm-p256"
    ws = FakeSocket(dev.hello, [dev.report({})])
    asyncio.run(hub.serve(ws))
    welcome = ws.sent[1]
    assert welcome["type"] == "welcome" and welcome["tls"] == "T1" and dev.on_welcome(welcome)
    assert [r["id"] for r in seen] == ["edge-J-01"] and not any(p.exists() for p in runner.fake.dirs)
    # the same unit with the key of another chip: not authenticated
    other = tmp_path / "other"
    (other / "edge").mkdir(parents=True)
    other_runner, _ = tpm_key(other, "edge-J-01", chip="chip-B")
    impostor = EdgeDevice("edge-J-01", "T1", load_signer(other, "edge-J-01", runner=other_runner), gateway, NoController())
    ws2 = FakeSocket(impostor.hello)
    asyncio.run(hub.serve(ws2))
    assert ws2.closed == 4401
    # an Ed25519 unit still works next to it
    ek = crypto.new_key()
    hub.registry.enroll("edge-J-02", JUNCTIONS[1], crypto.public_hex(ek), "URB-E")
    ws3 = FakeSocket(EdgeDevice("edge-J-02", "T2", FileSigner(key=ek), gateway, NoController()).hello)
    asyncio.run(hub.serve(ws3))
    assert ws3.sent[1]["type"] == "welcome"


def test_the_district_and_the_lighting_cabinet_sign_with_a_tpm_key_too(pki, tmp_path):
    gateway = crypto.load_public((pki / "gateway.pub").read_text())
    reg = Registry(pki / "devices.json")
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, r: None)
    unit_pki = tmp_path / "unit"
    (unit_pki / "edge").mkdir(parents=True)
    runner, dpub = tpm_key(unit_pki, "district-1")
    reg.enroll_district("district-1", "10.1.0.2", dpub)
    district = DistrictDevice("district-1", load_signer(unit_pki, "district-1", runner=runner), gateway_public=gateway)
    ws = FakeSocket(district.hello, [{"type": "report", "t": 5, "queues": {}, "events": [], "health": {}}])
    asyncio.run(hub.serve(ws))
    assert ws.sent[1]["type"] == "welcome" and ws.closed is None
    runner2, lpub = tpm_key(unit_pki, "light-C-01")
    reg.enroll_lighting("light-C-01", "C-01", lpub)
    cabinet = LightingCabinet("light-C-01", "C-01", load_signer(unit_pki, "light-C-01", runner=runner2), gateway,
                              EmulatedAdapter(), 71.78, 40.39)
    ws2 = FakeSocket(cabinet.hello, [{"type": "report", "t": 1, "health": {}}])
    asyncio.run(hub.serve(ws2))
    assert ws2.sent[1]["type"] == "welcome" and ws2.sent[1]["cabinet"] == "C-01"
    assert crypto.verify_text(lpub, {"device": "light-C-01", "nonce": "z"}, cabinet.hello("z")["sig"])


def test_the_demo_fleet_and_the_lighting_loader_read_file_keys_as_before(tmp_path):
    from urban.modules.lighting.session import load_trust
    provision(JUNCTIONS, tmp_path)  # the demo: every unit's key is a file under edge/
    s = load_signer(tmp_path, "edge-J-01")
    assert isinstance(s, FileSigner)
    rec = json.loads((tmp_path / "devices.json").read_text())
    assert any(d["id"] == "edge-J-01" and d["public_key"] == s.public() for d in rec)
    key, gateway_public, root_public = load_trust(tmp_path, "edge-J-01")
    assert key.public() == s.public() and gateway_public is not None and root_public is not None
    assert crypto.verify_text(s.public(), OBJ, crypto.sign(key, OBJ))
