#!/bin/sh
# RAUC post-install hook of an URBAN OS bundle (manifest: [hooks] filename=hook.sh, [image.rootfs] hooks=post-install).
#
# The slot image has just been written to the target slot. It is first checked: its dm-verity superblock must describe
# the layout of the bundle's slot.cfg (veritysetup dump) and the slot must verify against the root hash in slot.cfg
# (veritysetup verify). Then the bundle's signed slot files go onto the ESP: slot.cfg with the new image's
# dm-verity root hash and slot.sha256 with the sha256 of its kernel and initrd, each with its detached signature by the
# URBAN boot key (.sig), as
#   /boot/efi/EFI/URBAN/slot-<A|B>.cfg, slot-<A|B>.sha256  and their  .sig
# GRUB boots the slot only with these (check_signatures=enforce). Each file is written under a temporary name and
# moved into place, then read back. Any failure fails the installation: RAUC then does not switch to the new slot.
set -eu

case "${1:-}" in
  slot-pre-install|slot-install|install-check) exit 0 ;;
esac

fail() { echo "urban bundle hook: $*" >&2; exit 1; }

# which slot is being written: A or B
slot="${RAUC_SLOT_BOOTNAME:-}"
if [ -z "$slot" ]; then
  case "${RAUC_SLOT_DEVICE:-}" in
    *rootfs-a) slot=A ;;
    *rootfs-b) slot=B ;;
  esac
fi
case "$slot" in A|B) ;; *) fail "cannot tell which slot is being installed (RAUC_SLOT_BOOTNAME='${RAUC_SLOT_BOOTNAME:-}')" ;; esac

# where the bundle is mounted: this script runs from there; the environment names it as well
here=$(cd "$(dirname "$0")" && pwd)
src=""
for d in "$here" "${RAUC_BUNDLE_MOUNT_POINT:-}" "${RAUC_UPDATE_SOURCE:-}" "$(dirname "${RAUC_IMAGE_NAME:-/nonexistent/x}")"; do
  if [ -n "$d" ] && [ -f "$d/slot.cfg" ] && [ -f "$d/slot.cfg.sig" ]; then src="$d"; break; fi
done
[ -n "$src" ] || fail "the bundle carries no slot.cfg and slot.cfg.sig"
for n in sha256 sha256.sig; do [ -s "$src/slot.$n" ] || fail "the bundle carries no slot.$n (the signed kernel/initrd hash list)"; done
[ "$(grep -c '^[0-9a-f]\{64\}  boot/\(vmlinuz\|initrd\.img\)$' "$src/slot.sha256")" = 2 ] || fail "slot.sha256 does not list the kernel and the initrd"

grep -q '^set URBAN_ROOTHASH=[0-9a-f]\{64\}$' "$src/slot.cfg" || fail "slot.cfg has no root hash"
grep -q '^set URBAN_HASHOFFSET=[0-9]\+$' "$src/slot.cfg" || fail "slot.cfg has no hash offset"
[ -s "$src/slot.cfg.sig" ] || fail "slot.cfg.sig is empty"

# The image just written must be the one slot.cfg speaks for: its dm-verity tree and data must agree with the root
# hash in the signed file. Otherwise the mismatch would show only at the next boot (safe through the fallback, but a
# wasted boot, and the slot would be marked for it).
roothash=$(sed -n 's/^set URBAN_ROOTHASH=//p' "$src/slot.cfg")
hashoff=$(sed -n 's/^set URBAN_HASHOFFSET=//p' "$src/slot.cfg")
[ $((hashoff % 4096)) -eq 0 ] && [ "$hashoff" -gt 0 ] || fail "slot.cfg: the hash offset $hashoff is not a multiple of 4096"
dev="${RAUC_SLOT_DEVICE:-}"
if [ -z "$dev" ]; then
  case "$slot" in A) dev=/dev/disk/by-partlabel/rootfs-a ;; B) dev=/dev/disk/by-partlabel/rootfs-b ;; esac
fi
[ -b "$dev" ] || fail "the slot device $dev is not there to check the written image"
sync
blockdev --flushbufs "$dev" 2>/dev/null || true
# The tree's own superblock is not signed, and veritysetup takes the length of the data and the block sizes from it
# (with a superblock, --data-blocks is ignored). It must describe exactly the signed layout - hashoff / 4096 data
# blocks, 4096-byte data and hash blocks, hash type 1, sha256 - as the initramfs requires before it opens the slot
# (scripts/local-top/urban-verity, the same check): a slot that would not boot is not installed.
info=$(veritysetup dump --hash-offset="$hashoff" "$dev" 2>&1) || fail "no dm-verity superblock at the hash offset $hashoff of $dev - not installing it"
blocks=""; dbs=""; hbs=""; htype=""; halg=""
while read -r w1 w2 w3 w4; do
  case "$w1 $w2" in
    "Data blocks:") blocks="$w3" ;;
    "Data block") if [ "$w3" = "size:" ]; then dbs="$w4"; fi ;;
    "Hash block") if [ "$w3" = "size:" ]; then hbs="$w4"; fi ;;
    "Hash type:") htype="$w3" ;;
    "Hash algorithm:") halg="$w3" ;;
  esac
done <<EOF
$info
EOF
if [ "$blocks" != "$((hashoff / 4096))" ] || [ "$dbs" != 4096 ] || [ "$hbs" != 4096 ] || [ "$htype" != 1 ] || [ "$halg" != sha256 ]; then
  fail "the dm-verity superblock on $dev does not match slot.cfg (data blocks '$blocks', want $((hashoff / 4096)); block sizes '$dbs'/'$hbs', hash type '$htype', '$halg', want 4096/4096, 1, sha256) - not installing it"
fi
veritysetup verify --hash-offset="$hashoff" --data-blocks="$((hashoff / 4096))" "$dev" "$dev" "$roothash" >/dev/null 2>&1 \
  || fail "the image written to $dev does not match the root hash in slot.cfg - not installing it"

esp=/boot/efi
if ! mountpoint -q "$esp"; then mount "$esp" 2>/dev/null || fail "the ESP is not mounted at $esp"; fi
dir="$esp/EFI/URBAN"
mkdir -p "$dir" || fail "cannot make $dir"

# Two signed pairs per slot: slot-X.sha256 (the sha256 of this build's kernel and initrd, which GRUB checks them against)
# and slot-X.cfg (the root hash). The hash list goes first and the boot file last: the boot file is what makes GRUB
# take the slot, and the slot is not booted while RAUC has it marked bad (it is, until this install succeeds).
install_pair() {  # NAME: slot.NAME and slot.NAME.sig of the bundle -> slot-<X>.NAME and its .sig on the ESP
  f="$dir/slot-$slot.$1"
  cp "$src/slot.$1" "$f.new" || fail "cannot write $f.new"
  cp "$src/slot.$1.sig" "$f.sig.new" || fail "cannot write $f.sig.new"
  sync
  cmp -s "$src/slot.$1" "$f.new" || fail "$f.new does not read back"
  cmp -s "$src/slot.$1.sig" "$f.sig.new" || fail "$f.sig.new does not read back"
  mv -f "$f.new" "$f" || fail "cannot move $f into place"
  mv -f "$f.sig.new" "$f.sig" || fail "cannot move $f.sig into place"
  sync
  cmp -s "$src/slot.$1" "$f" && cmp -s "$src/slot.$1.sig" "$f.sig" || fail "$f does not read back"
}
cleanup() { rm -f "$dir/slot-$slot.cfg.new" "$dir/slot-$slot.cfg.sig.new" "$dir/slot-$slot.sha256.new" "$dir/slot-$slot.sha256.sig.new"; }
trap cleanup EXIT

install_pair sha256
install_pair cfg
cfg="$dir/slot-$slot.cfg"
# a new image starts with no failed tries counted against it (GRUB's X_FAIL); not fatal: the counter only limits retries
grub-editenv "$esp/EFI/BOOT/grubenv" set "${slot}_FAIL=0" 2>/dev/null || echo "urban bundle hook: ${slot}_FAIL not reset" >&2
echo "urban bundle hook: slot $slot boot file and kernel/initrd hash list installed ($(sed -n 's/^set URBAN_VERSION=//p' "$cfg"))"
