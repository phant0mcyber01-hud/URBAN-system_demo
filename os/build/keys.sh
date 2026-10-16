#!/bin/bash
# The update keys of URBAN Edge OS: a CA, whose certificate is built into every image, and a code-signing
# certificate from it that signs the releases.
#
# Both certificates are valid from 2020-01-01, whatever the clock of the machine that makes them says. A unit
# in a cabinet may start with a clock that is hours or years off (no battery, no network yet), and a release
# machine's clock may be wrong too: a certificate that is "not yet valid" for the unit would make it refuse
# every update. Expiry is what protects against old keys, not the start date.
#
#   keys.sh DIR    makes DIR/ca.{key,pem} and DIR/signing.{key,pem} unless DIR/ca.pem exists
set -euo pipefail
K="$1"
[ -f "$K/ca.pem" ] && exit 0
mkdir -p "$K"
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
