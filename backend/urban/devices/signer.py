"""The device's identity key, whichever kind it is: one Signer for the unit, the district client and the lighting cabinet.

Two kinds, both first-class:
- FileSigner: Ed25519, a file on the unit (<pki>/edge/<unit>.key), as before;
- TpmSigner: ECDSA P-256 whose private half is born inside the unit's TPM 2.0 and never leaves it (the TPM cannot do
  Ed25519). What lies on disk, <pki>/edge/<unit>.tpm/, is the key wrapped by the TPM: key.pub and key.priv are of no
  use on any other machine, and root on this one can sign only while sitting on this very unit.

The TPM is driven with tpm2-tools 5.x (Debian 12: apt install tpm2-tools) through /dev/tpmrm0, the kernel's resource
manager. The primary key under the owner hierarchy is made from the same template every time, so the same TPM always
gives the same primary and the wrapped key loads under it.

    python -m urban.devices.signer --pki DIR --unit NAME --public     the public key text (64 or 130 hex)
    python -m urban.devices.signer --pki DIR --unit NAME --kind       ed25519 | tpm-p256
    python -m urban.devices.signer --pki DIR --unit NAME --sign TEXT  signature hex of the JSON string TEXT, i.e. of
                                                                      crypto.canonical(TEXT); verify with
                                                                      crypto.verify_text(public, TEXT, signature)
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from . import crypto

TPM_DEVICE = "/dev/tpmrm0"
DEFAULT_TCTI = f"device:{TPM_DEVICE}"
TPM_TOOL = "tpm2_createprimary"   # the tool whose presence on PATH says tpm2-tools is installed
TPM_TIMEOUT = 30                  # seconds any single tpm2 command may take
KEY_ATTRIBUTES = "fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign"  # a signing key that cannot be duplicated
PRIMARY = ["tpm2_createprimary", "-C", "o", "-g", "sha256", "-G", "ecc256", "-c", "primary.ctx"]
TPM_FILES = ("key.pub", "key.priv", "public.hex")


class SignerError(RuntimeError):
    """The device key cannot be used right now (no TPM, a tool failed, a blob of another machine). Worded for the log."""


class Signer:
    kind = ""

    def public(self) -> str:
        raise NotImplementedError

    def sign(self, obj) -> str:
        """Hex signature of crypto.canonical(obj), in the form the platform verifies for this kind (see crypto.py)."""
        raise NotImplementedError


class FileSigner(Signer):
    """An Ed25519 key kept as 64 hex characters in a file (or, for demos and tests, handed over as a key object)."""
    kind = crypto.ED25519

    def __init__(self, path: Path | None = None, key=None):
        if key is None:
            key = crypto.load_private(Path(path).read_text())  # FileNotFoundError / ValueError for a missing or bad file
        self._key = key
        self._public = crypto.public_hex(key)

    def public(self) -> str:
        return self._public

    def sign(self, obj) -> str:
        return crypto.sign(self._key, obj)


def tpm_present() -> bool:
    """Is there a TPM we can use here: its resource-manager device and tpm2-tools."""
    return Path(TPM_DEVICE).exists() and shutil.which(TPM_TOOL) is not None


def _tail(data) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data or "")
    return " ".join(text.split())[-300:]


def _run(runner, argv: list[str], cwd: Path, tcti: str):
    """One tpm2 command; SignerError with the tool's own words if it cannot run, hangs or fails."""
    env = {**os.environ, "TPM2TOOLS_TCTI": tcti}
    try:
        done = runner(argv, cwd=str(cwd), env=env, capture_output=True, timeout=TPM_TIMEOUT, check=False)
    except FileNotFoundError:
        raise SignerError(f"{argv[0]} is not installed (apt install tpm2-tools)")
    except subprocess.TimeoutExpired:
        raise SignerError(f"{argv[0]} gave no answer in {TPM_TIMEOUT} s: the TPM is busy or stuck")
    except OSError as err:
        raise SignerError(f"{argv[0]} cannot run: {err}")
    if done.returncode != 0:
        raise SignerError(f"{argv[0]} failed ({done.returncode}): {_tail(done.stderr) or _tail(done.stdout) or 'no message'}")
    return done


def _read(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as err:
        raise SignerError(f"{what}: {path.name} was not written ({err})")


def _der(signature: bytes) -> bytes:
    """An ECDSA P-256 signature as DER, from what tpm2_sign -f plain wrote: DER already (tools 5.x) or r||s (64 bytes)."""
    try:
        r, s = decode_dss_signature(signature)
        if encode_dss_signature(r, s) == signature:  # all of it, nothing trailing
            return signature
    except ValueError:
        pass
    if len(signature) == 64:
        return encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
    raise SignerError(f"tpm2_sign wrote a signature in a form that is neither DER nor r||s ({len(signature)} bytes)")


def _chmod(path: Path, mode: int):
    try:
        path.chmod(mode)
    except OSError:
        pass


class TpmSigner(Signer):
    """ECDSA P-256 with a key inside the TPM. `dir` holds key.pub and key.priv (from tpm2_create: the key wrapped by this
    TPM) and public.hex (the public key text, 130 hex). Each signature loads the key under a fresh primary in a private
    temporary directory, signs, and cleans up; nothing secret ever exists as a file."""
    kind = crypto.P256

    def __init__(self, dir: Path, runner=subprocess.run, tcti: str | None = None):
        self.dir, self.runner = Path(dir), runner
        self.tcti = tcti or os.environ.get("TPM2TOOLS_TCTI") or DEFAULT_TCTI
        missing = [n for n in TPM_FILES if not (self.dir / n).is_file()]
        if missing:
            raise SignerError(f"the TPM key folder {self.dir} lacks {', '.join(missing)}: make the key again (--keygen)")
        self._public = None

    def public(self) -> str:
        if self._public is None:
            text = (self.dir / "public.hex").read_text().strip().lower()
            try:
                if crypto.public_kind(text) != crypto.P256:
                    raise ValueError("not a P-256 key")
                crypto.load_device_public(text)
            except ValueError as err:
                raise SignerError(f"{self.dir / 'public.hex'} does not hold a P-256 public key: {err}")
            self._public = text
        return self._public

    def sign(self, obj) -> str:
        public = self.public()
        work = Path(tempfile.mkdtemp(prefix="urban-tpm-"))  # 0700, ours alone
        try:
            _run(self.runner, list(PRIMARY), work, self.tcti)
            _run(self.runner, ["tpm2_load", "-C", "primary.ctx", "-u", str(self.dir / "key.pub"),
                               "-r", str(self.dir / "key.priv"), "-c", "key.ctx"], work, self.tcti)
            (work / "msg.bin").write_bytes(crypto.canonical(obj))
            _run(self.runner, ["tpm2_sign", "-c", "key.ctx", "-g", "sha256", "-s", "ecdsa", "-f", "plain",
                               "-o", "sig.bin", "msg.bin"], work, self.tcti)
            signature = _der(_read(work / "sig.bin", "tpm2_sign")).hex()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not crypto.verify_text(public, obj, signature):
            raise SignerError("the TPM's signature does not verify against public.hex: the key blob is damaged "
                              "or does not belong to this key")
        return signature


def create_tpm_key(dir: Path, runner=subprocess.run, tcti: str | None = None) -> str:
    """Make a P-256 signing key inside the TPM and keep its wrapped blob in `dir` (key.pub, key.priv, public.hex; the
    folder 0700, the files 0600). Returns the public key text (130 hex). `dir` must not exist; nothing is left behind if
    any step fails."""
    dir = Path(dir)
    tcti = tcti or os.environ.get("TPM2TOOLS_TCTI") or DEFAULT_TCTI
    if dir.exists():
        raise SignerError(f"{dir} exists already: one key per unit")
    dir.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=dir.name + ".new-", dir=dir.parent))  # same file system, so the move is one step
    try:
        _run(runner, list(PRIMARY), work, tcti)
        _run(runner, ["tpm2_create", "-C", "primary.ctx", "-G", "ecc256:ecdsa-sha256", "-a", KEY_ATTRIBUTES,
                      "-u", "key.pub", "-r", "key.priv"], work, tcti)
        _run(runner, ["tpm2_load", "-C", "primary.ctx", "-u", "key.pub", "-r", "key.priv", "-c", "key.ctx"], work, tcti)
        _run(runner, ["tpm2_readpublic", "-c", "key.ctx", "-f", "der", "-o", "pub.der"], work, tcti)
        try:
            point = serialization.load_der_public_key(_read(work / "pub.der", "tpm2_readpublic"))
            if not isinstance(point, ec.EllipticCurvePublicKey) or not isinstance(point.curve, ec.SECP256R1):
                raise ValueError("not a P-256 key")
            public = crypto.p256_point_hex(point)
        except ValueError as err:
            raise SignerError(f"tpm2_readpublic gave no P-256 public key ({err})")
        for name in ("key.pub", "key.priv"):
            if not (work / name).is_file():
                raise SignerError(f"tpm2_create did not write {name}")
        (work / "public.hex").write_text(public + "\n")
        for name in ("primary.ctx", "key.ctx", "pub.der"):  # contexts of this run only
            (work / name).unlink(missing_ok=True)
        for name in TPM_FILES:
            _chmod(work / name, 0o600)
        _chmod(work, 0o700)
        os.replace(work, dir)
        return public
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def load_signer(pki: Path, unit: str, key_path: Path | None = None, runner=None) -> Signer:
    """The signer of this unit: its TPM key if <pki>/edge/<unit>.tpm/ exists, else the Ed25519 file
    <pki>/edge/<unit>.key (or key_path). FileNotFoundError if there is no key; SignerError if the TPM folder is damaged."""
    tpm = Path(pki) / "edge" / f"{unit}.tpm"
    if tpm.is_dir():
        return TpmSigner(tpm, **({"runner": runner} if runner is not None else {}))
    return FileSigner(Path(key_path) if key_path else Path(pki) / "edge" / f"{unit}.key")


def main(argv=None, runner=None) -> int:
    ap = argparse.ArgumentParser(description="the device key of a unit: its public key and signatures")
    ap.add_argument("--pki", required=True, help="folder with edge/<unit>.tpm/ or edge/<unit>.key")
    ap.add_argument("--unit", required=True)
    ap.add_argument("--key", help="the Ed25519 key file, if it is not <pki>/edge/<unit>.key")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--public", action="store_true", help="print the public key text")
    what.add_argument("--kind", action="store_true", help="print ed25519 or tpm-p256")
    what.add_argument("--sign", metavar="TEXT", help="print the signature hex of the JSON string TEXT")
    args = ap.parse_args(argv)
    try:
        signer = load_signer(Path(args.pki), args.unit, args.key, runner)
        print(signer.public() if args.public else signer.kind if args.kind else signer.sign(args.sign))
    except FileNotFoundError as err:
        print(f"signer: no key for {args.unit}: {err}", file=sys.stderr)
        return 1
    except (SignerError, ValueError, OSError) as err:
        print(f"signer: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
