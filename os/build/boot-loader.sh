#!/bin/bash
# The boot loader of URBAN OS and the config it loads, made from the boot keys. Sourced by build.sh (the fleet image)
# and by os/unit-image.mjs (every unit's image gets its own GRUB maintenance password): one recipe for both, so the
# boot loader of a unit's image cannot drift from the fleet's.
#
#   . /work/os/build/boot-loader.sh
#   boot_gpg_init KEYS            the GnuPG boot key from KEYS/boot-gpg, checked against KEYS/boot.pub.gpg
#   boot_gpg ARGS...              gpg with that key ring
#   boot_sign FILE                FILE.sig, a detached signature by the boot key
#   grub_password_hash PASSWORD   prints GRUB's PBKDF2 hash of the maintenance password
#   boot_loader HASH ESP KEYS     ESP/EFI/BOOT/BOOTX64.EFI, signed with the Secure Boot db key (KEYS/sb/db), and
#                                 ESP/EFI/BOOT/grub.cfg with its .sig: the password hash HASH in both
#
# The caller runs under set -euo pipefail; a step that fails stops it with the reason on stderr.

# What GRUB loads is signed with the GnuPG boot key. gpg-agent needs a socket in its home, which the folder shared from
# the host cannot hold, so the key ring is used from a copy in the container.
boot_gpg_init() {  # KEYS
  GPGW=/tmp/boot-gpg
  rm -rf "$GPGW"; mkdir -p "$GPGW"; cp -r "$1/boot-gpg/." "$GPGW/"; chmod -R go-rwx "$GPGW"; chmod 700 "$GPGW"
  boot_gpg --export | cmp -s - "$1/boot.pub.gpg" || { echo "boot-gpg and boot.pub.gpg are not the same key" >&2; exit 1; }
}
boot_gpg() { gpg --homedir "$GPGW" --batch --yes --quiet "$@"; }
boot_sign() {  # FILE -> FILE.sig, a detached binary signature (what GRUB's pgp module reads)
  rm -f "$1.sig"
  boot_gpg --digest-algo SHA256 --detach-sign --output "$1.sig" "$1" && [ -s "$1.sig" ] || { echo "could not sign $1" >&2; exit 1; }
}

# the password goes to grub-mkpasswd-pbkdf2 on its standard input (twice: it asks again), never on a command line
grub_password_hash() {  # PASSWORD
  local hash
  # (called as $(grub_password_hash ...), where bash does not keep set -e: every failure is caught here)
  hash=$(printf '%s\n%s\n' "$1" "$1" | grub-mkpasswd-pbkdf2 | sed -n 's/.*is \(grub\.pbkdf2\..*\)$/\1/p') && [ -n "$hash" ] \
    || { echo "grub-mkpasswd-pbkdf2 gave no hash" >&2; exit 1; }
  echo "$hash"
}

# The boot loader: a standalone GRUB with URBAN's boot key built in. Its embedded config runs first, before anything
# on the disk is read: signatures are enforced from then on and the maintenance password is set (so a config that
# fails its signature leaves a locked GRUB prompt, not an open one), then the signed grub.cfg is loaded. The boot
# loader itself is signed with the Secure Boot db key; the firmware runs nothing else. Plain commands only: the
# embedded config is read by GRUB's simple parser.
boot_loader() {  # HASH ESP KEYS
  local hash="$1" esp="$2" keys="$3"
  mkdir -p "$esp/EFI/BOOT"
  cat > /tmp/early.cfg <<EOF
set check_signatures=enforce
export check_signatures
set superusers="urban"
export superusers
password_pbkdf2 urban $hash
configfile /EFI/BOOT/grub.cfg
EOF
  grub-mkimage -O x86_64-efi -o /tmp/BOOTX64.unsigned.EFI -p /EFI/BOOT -c /tmp/early.cfg \
    --pubkey "$keys/boot.pub.gpg" --disable-shim-lock \
    part_gpt fat squash4 normal linux echo test loadenv configfile regexp search all_video efi_gop gzio \
    boot cat sleep reboot halt password_pbkdf2 pbkdf2 gcry_sha512 pgp gcry_rsa gcry_sha256 hashsum probe
  sbsign --key "$keys/sb/db.key" --cert "$keys/sb/db.crt" --output "$esp/EFI/BOOT/BOOTX64.EFI" /tmp/BOOTX64.unsigned.EFI >/dev/null
  sbverify --cert "$keys/sb/db.crt" "$esp/EFI/BOOT/BOOTX64.EFI" >/dev/null || { echo "BOOTX64.EFI does not verify against db.crt" >&2; exit 1; }
  rm -f /tmp/BOOTX64.unsigned.EFI
  # grub.cfg: the build's template with the password filled in, signed with the boot key
  sed "s|^# URBAN-GRUB-PASSWORD\$|set superusers=\"urban\"\npassword_pbkdf2 urban $hash|" /work/os/build/grub.cfg > "$esp/EFI/BOOT/grub.cfg"
  grep -q '^password_pbkdf2 urban grub.pbkdf2' "$esp/EFI/BOOT/grub.cfg" || { echo "grub.cfg has no password" >&2; exit 1; }
  boot_sign "$esp/EFI/BOOT/grub.cfg"
}
