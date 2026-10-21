#!/bin/bash
# The update keys of URBAN Edge OS: a CA, whose certificate is built into every image, and a code-signing
# certificate from it that signs the releases.
#
# Both certificates are valid from 2020-01-01, whatever the clock of the machine that makes them says. A unit
# in a cabinet may start with a clock that is hours or years off (no battery, no network yet), and a release
# machine's clock may be wrong too: a certificate that is "not yet valid" for the unit would make it refuse
# every update. Expiry is what protects against old keys, not the start date.
#
#   keys.sh DIR    makes DIR/ca.{key,pem} and DIR/signing.{key,pem} unless DIR/ca.pem exists, and (each only if
#                  missing, so older key folders get them on the next build) the boot keys below.
#
# The boot keys (step 6, Secure Boot) - never in git, kept with the update keys in data/os/keys:
#   DIR/sb/{PK,KEK,db}.{key,crt}   UEFI Secure Boot platform key, key-exchange key and signature database key: RSA-2048,
#                                  self-signed, 10 years. db signs the boot loader (BOOTX64.EFI).
#   DIR/sb/owner.guid              the signature owner GUID written into every enrolment
#   DIR/sb/{PK,KEK,db}.cer         the same certificates in DER, for the firmware setup of a real mini-PC
#   DIR/sb/{PK,KEK,db}.esl         EFI signature lists (one X.509 entry each), for firmware that wants a list
#   DIR/boot-gpg/                  GnuPG home (0700) with the RSA-3072 key "URBAN boot signing" (no passphrase): signs what
#                                  GRUB loads (its config, the kernel and initrd, the per-slot boot file)
#   DIR/boot.pub.gpg               its public key, binary: built into GRUB (grub-mkimage --pubkey)
# A boot key is made only when neither of its halves exists (a fresh key folder). One half without the other is a lost
# key, not a missing one: the units in the field trust the old key, and images and updates signed by a new one would
# not boot on them (every update would fail silently and fall back) - so the build stops and says what to restore.
set -euo pipefail
K="$1"
mkdir -p "$K"

lost() {  # WHAT: one half of a boot key is there without the other
  echo "keys.sh: $1. Restore the missing file from the off-line copy of $K and build again. Not making a new key:" \
    "the units in the field trust the old one, and images and updates signed by a new key would not boot on them." >&2
  exit 1
}

boot_keys() {
  local S="$K/sb" G="$K/boot-gpg" n made=0 pub=0 priv=0
  mkdir -p "$S"
  [ -f "$S/owner.guid" ] || cat /proc/sys/kernel/random/uuid > "$S/owner.guid"
  for n in PK KEK db; do
    if [ -f "$S/$n.crt" ] && [ ! -f "$S/$n.key" ]; then
      lost "$S/$n.crt exists without its private key $n.key"
    elif [ -f "$S/$n.key" ] && [ ! -f "$S/$n.crt" ]; then
      lost "$S/$n.key exists without its certificate $n.crt"
    elif [ ! -f "$S/$n.crt" ]; then
      openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -subj "/CN=URBAN Secure Boot $n" \
        -keyout "$S/$n.key" -out "$S/$n.crt" 2>/dev/null
      chmod 600 "$S/$n.key"; made=1
    fi
    [ -f "$S/$n.cer" ] && [ "$made" = 0 ] || openssl x509 -in "$S/$n.crt" -outform DER -out "$S/$n.cer"
  done
  # EFI_SIGNATURE_LIST with one EFI_CERT_X509 entry per certificate (efitools is not installed here)
  python3 - "$S" <<'PY'
import struct, subprocess, sys, uuid
s = sys.argv[1]
owner = uuid.UUID(open(f"{s}/owner.guid").read().strip()).bytes_le
x509 = uuid.UUID("a5c059a1-94e4-4aa7-87b5-ab155c2bf072").bytes_le
for n in ("PK", "KEK", "db"):
    der = subprocess.run(["openssl", "x509", "-in", f"{s}/{n}.crt", "-outform", "DER"], check=True, capture_output=True).stdout
    entry = owner + der
    open(f"{s}/{n}.esl", "wb").write(x509 + struct.pack("<III", 28 + len(entry), 0, len(entry)) + entry)
PY
  for n in PK KEK db; do
    openssl x509 -in "$S/$n.crt" -noout -subject | grep -q "URBAN Secure Boot $n" || { echo "$S/$n.crt is not our $n certificate" >&2; exit 1; }
  done
  [ -f "$K/boot.pub.gpg" ] && pub=1
  compgen -G "$G/private-keys-v1.d/*.key" > /dev/null && priv=1
  if [ "$pub" = 1 ] && [ "$priv" = 0 ]; then
    lost "$K/boot.pub.gpg exists but $G holds no private key (private-keys-v1.d/*.key)"
  elif [ "$pub" = 0 ] && [ "$priv" = 1 ]; then
    lost "$G holds a private key but its public key $K/boot.pub.gpg is missing"
  elif [ "$pub" = 0 ]; then
    # gpg-agent wants a Unix socket in the home, which a folder shared from Windows cannot hold: the key is made in a
    # scratch home and only its files (no sockets) are kept in DIR/boot-gpg
    local T; T=$(mktemp -d); chmod 700 "$T"
    cat > "$T/gen" <<EOF
%no-protection
Key-Type: RSA
Key-Length: 3072
Key-Usage: sign
Name-Real: URBAN boot signing
Expire-Date: 0
%commit
EOF
    gpg --homedir "$T" --batch --quiet --gen-key "$T/gen"
    gpg --homedir "$T" --batch --quiet --export > "$K/boot.pub.gpg"
    gpgconf --homedir "$T" --kill all 2>/dev/null || true
    rm -rf "$G"; mkdir -p "$G"; chmod 700 "$G"
    for f in private-keys-v1.d pubring.kbx pubring.gpg trustdb.gpg openpgp-revocs.d; do
      [ -e "$T/$f" ] && cp -r "$T/$f" "$G/"
    done
    rm -rf "$T"
    [ -s "$K/boot.pub.gpg" ] || { echo "the boot signing key was not made" >&2; exit 1; }
  fi
  echo ">> boot keys in $K (sb/ and boot-gpg/: never in git, keep PK.key/KEK.key/db.key and boot-gpg off networked machines)"
}
boot_keys

[ -f "$K/ca.pem" ] && exit 0
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/new"; : > "$W/index.txt"; echo 1000 > "$W/serial"
cat > "$W/ca.cnf" <<EOF
[ca]
default_ca = urban
[urban]
dir = $W
database = $W/index.txt
new_certs_dir = $W/new
serial = $W/serial
default_md = sha256
policy = anything
unique_subject = no
copy_extensions = none
[anything]
organizationName = optional
commonName = supplied
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[v3_signing]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF
openssl req -new -newkey rsa:4096 -nodes -keyout "$K/ca.key" -out "$W/ca.csr" -subj "/O=URBAN/CN=URBAN Edge OS update CA" 2>/dev/null
openssl ca -batch -config "$W/ca.cnf" -selfsign -keyfile "$K/ca.key" -in "$W/ca.csr" -out "$K/ca.pem" \
  -startdate 20200101000000Z -enddate 20460101000000Z -extensions v3_ca -notext 2>/dev/null
openssl req -new -newkey rsa:4096 -nodes -keyout "$K/signing.key" -out "$W/signing.csr" \
  -subj "/O=URBAN/CN=URBAN Edge OS release signing" 2>/dev/null
openssl ca -batch -config "$W/ca.cnf" -cert "$K/ca.pem" -keyfile "$K/ca.key" -in "$W/signing.csr" -out "$K/signing.pem" \
  -startdate 20200101000000Z -enddate 20360101000000Z -extensions v3_signing -notext 2>/dev/null
chmod 600 "$K/ca.key" "$K/signing.key"
openssl verify -CAfile "$K/ca.pem" -purpose any "$K/signing.pem" >/dev/null
echo ">> update keys made in $K (valid from 2020-01-01; keep ca.key off networked machines)"
