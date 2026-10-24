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
#   boot_loader HASH ESP KEYS arm64 ROOT DEB
#                                 the same for arm64: ESP/EFI/BOOT/BOOTAA64.EFI, made by the grub-mkimage of the arm64
#                                 system in ROOT (run under emulation) from the arm64-efi modules of DEB (the package
#                                 grub-efi-arm64-bin), and a grub.cfg whose kernel line also names ARM's serial console
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
boot_loader() {  # HASH ESP KEYS [arm64 ROOT DEB]
  local hash="$1" esp="$2" keys="$3" arch="${4:-x86}" root="${5:-}" deb="${6:-}"
  local efi=BOOTX64.EFI
  [ "$arch" = arm64 ] && efi=BOOTAA64.EFI
  mkdir -p "$esp/EFI/BOOT"
  cat > /tmp/early.cfg <<EOF
set check_signatures=enforce
export check_signatures
set superusers="urban"
export superusers
password_pbkdf2 urban $hash
configfile /EFI/BOOT/grub.cfg
EOF
  local modules="part_gpt fat squash4 normal linux echo test loadenv configfile regexp search all_video efi_gop gzio
    boot cat sleep reboot halt password_pbkdf2 pbkdf2 gcry_sha512 pgp gcry_rsa gcry_sha256 hashsum probe"
  if [ "$arch" = arm64 ]; then
    # GRUB's arm64-efi modules come only in an arm64 package: unpacked into the arm64 system for the moment, where its
    # own grub-mkimage (the same GRUB version) builds the image, emulated
    [ -x "$root/usr/bin/grub-mkimage" ] && [ -f "$deb" ] || { echo "arm64 boot loader: no grub-mkimage in '$root' or no '$deb'" >&2; exit 1; }
    rm -rf "$root/tmp/grub-arm64"; mkdir -p "$root/tmp/grub-arm64"
    dpkg-deb -x "$deb" "$root/tmp/grub-arm64"
    cp /tmp/early.cfg "$keys/boot.pub.gpg" "$root/tmp/grub-arm64/"
    # shellcheck disable=SC2086
    chroot "$root" grub-mkimage -O arm64-efi -d /tmp/grub-arm64/usr/lib/grub/arm64-efi -o /tmp/grub-arm64/BOOTAA64.EFI \
      -p /EFI/BOOT -c /tmp/grub-arm64/early.cfg --pubkey /tmp/grub-arm64/boot.pub.gpg --disable-shim-lock $modules
    mv "$root/tmp/grub-arm64/BOOTAA64.EFI" /tmp/BOOTAA64.unsigned.EFI
    rm -rf "$root/tmp/grub-arm64"
  else
    # shellcheck disable=SC2086
    grub-mkimage -O x86_64-efi -o /tmp/BOOTX64.unsigned.EFI -p /EFI/BOOT -c /tmp/early.cfg \
      --pubkey "$keys/boot.pub.gpg" --disable-shim-lock $modules
  fi
  sbsign --key "$keys/sb/db.key" --cert "$keys/sb/db.crt" --output "$esp/EFI/BOOT/$efi" "/tmp/${efi%.EFI}.unsigned.EFI" >/dev/null
  sbverify --cert "$keys/sb/db.crt" "$esp/EFI/BOOT/$efi" >/dev/null || { echo "$efi does not verify against db.crt" >&2; exit 1; }
  rm -f "/tmp/${efi%.EFI}.unsigned.EFI"
  # grub.cfg: the build's template with the password filled in, signed with the boot key
  sed "s|^# URBAN-GRUB-PASSWORD\$|set superusers=\"urban\"\npassword_pbkdf2 urban $hash|" /work/os/build/grub.cfg > "$esp/EFI/BOOT/grub.cfg"
  grep -q '^password_pbkdf2 urban grub.pbkdf2' "$esp/EFI/BOOT/grub.cfg" || { echo "grub.cfg has no password" >&2; exit 1; }
  if [ "$arch" = arm64 ]; then
    # the kernel's console also on ARM's serial port (ttyAMA0: the PL011 of QEMU's virt machine and of many boards), named
    # last so that it is the system's console; vsyscall=none is x86's only
    sed -i '/^set CMDLINE=/{s| console=ttyS0,115200| console=ttyS0,115200 console=ttyAMA0,115200|; s| vsyscall=none||}' "$esp/EFI/BOOT/grub.cfg"
    grep -q '^set CMDLINE=".* console=ttyAMA0,115200 ' "$esp/EFI/BOOT/grub.cfg" && ! grep -q 'vsyscall=' "$esp/EFI/BOOT/grub.cfg" \
      || { echo "grub.cfg: the arm64 kernel line was not made" >&2; exit 1; }
  fi
  boot_sign "$esp/EFI/BOOT/grub.cfg"
}
