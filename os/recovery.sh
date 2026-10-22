#!/bin/bash
# The owner's half of a unit's data recovery (step 7), run in the builder container by `node os/recovery.mjs decrypt` and by
# the QEMU check (os/tests/vm_pcr7.py): opens a unit's urban-recovery.enc with the escrow private key and prints the
# recovery key to type at the unit's DATA LOCKED prompt.
#
#   recovery.sh decrypt FILE [ESCROW_KEY]     ESCROW_KEY: default /out/keys/escrow/escrow.key (data/os/keys/escrow)
#
# urban-recovery.enc (written by urban-grow on the unit's ESP): a few header lines (unit, volume, escrow-key, made) and
# between -----BEGIN/END URBAN RECOVERY KEY----- the base64 of an RSA-OAEP (SHA-256) ciphertext of
# "URBAN-RECOVERY-KEY v1 volume=<LUKS UUID> key=<recovery key>". Nothing is written anywhere but a scratch folder that is
# removed at the end.
set -euo pipefail
cmd=${1:-}; file=${2:-}; key=${3:-/out/keys/escrow/escrow.key}
[ "$cmd" = decrypt ] && [ -n "$file" ] || { echo "usage: recovery.sh decrypt FILE [ESCROW_KEY]" >&2; exit 2; }
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 2; }
[ -f "$key" ] || { echo "the escrow private key is not here ($key): it is kept offline, give its path" >&2; exit 2; }
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
chmod 700 "$W"
tr -d '\r' < "$file" > "$W/file"  # copied through Windows tools it may have CRLF line ends
field() { sed -n "s/^$1: *//p" "$W/file" | head -n 1; }
sed -n '/^-----BEGIN URBAN RECOVERY KEY-----$/,/^-----END URBAN RECOVERY KEY-----$/p' "$W/file" | sed '1d;$d' > "$W/b64"
[ -s "$W/b64" ] && base64 -d "$W/b64" > "$W/ct" 2>/dev/null && [ -s "$W/ct" ] \
  || { echo "$file is not an urban-recovery.enc (no BEGIN/END URBAN RECOVERY KEY block)" >&2; exit 2; }
pub=$(openssl pkey -in "$key" -pubout -outform DER 2>/dev/null | base64 -w0) || true
[ -n "$pub" ] || { echo "cannot read the escrow private key $key (not a private key, or protected by a passphrase)" >&2; exit 3; }
have="sha256:$(printf '%s' "$pub" | base64 -d | sha256sum | cut -d' ' -f1)"
want=$(field escrow-key)
if [ -n "$want" ] && [ "$want" != "$have" ]; then
  echo "this file was encrypted to escrow key $want, the key given is $have: use the escrow key of that unit's image" >&2
  exit 3
fi
if ! plain=$(openssl pkeyutl -decrypt -inkey "$key" -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
               -pkeyopt rsa_mgf1_md:sha256 -in "$W/ct" 2>/dev/null); then
  echo "the decryption failed: the file is damaged, or it was not made for this escrow key" >&2
  exit 3
fi
re='^URBAN-RECOVERY-KEY v1 volume=([0-9a-fA-F-]+) key=([cbdefghijklnrtuv]{8}(-[cbdefghijklnrtuv]{8}){7})$'
[[ "$plain" =~ $re ]] || { echo "what the file holds is not an URBAN recovery key" >&2; exit 3; }
volume=${BASH_REMATCH[1]}; rk=${BASH_REMATCH[2]}
if [ "$volume" != "$(field volume)" ]; then
  echo "WARNING: the header says volume $(field volume), the key inside is for volume $volume (the header was changed)" >&2
fi
echo "unit:          $(field unit)"
echo "data volume:   $volume"
echo "escrow key:    $have"
echo "made:          $(field made)"
echo
echo "recovery key: $rk"
echo
echo "Type it at the unit's DATA LOCKED prompt (its keyboard and screen, or the serial line); dashes and capitals do not"
echo "matter. The unit opens its data, seals it again to its current Secure Boot state and boots on."
