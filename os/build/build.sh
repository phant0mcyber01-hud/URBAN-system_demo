#!/bin/bash
# Builds URBAN OS inside the builder container (os/build/Dockerfile): one image for the junction unit, the
# district server and the control centre; the role is chosen on the ESP (urban-unit.txt).
#
#   build.sh --version 1 [--image] [--break firewall]
#
# Always makes the signed update bundle out/urban-edge-<version>.raucb; with --image also the disk image
# out/urban-edge-<version>.img (slot A filled, slot B empty) to write onto the unit's disk.
# --break firewall makes a deliberately broken system (a firewall that does not load) for the rollback test.
#
# /work is the repository, /out is data/os (keys, cache, results). Nothing here needs a loop device:
# file systems are made from directories and written into the disk image at their offsets.
set -euo pipefail

VERSION=""; IMAGE=0; BREAK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --image) IMAGE=1; shift ;;
    --break) BREAK="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$VERSION" ] || { echo "--version is required" >&2; exit 2; }

OUT=/out
COMPATIBLE=urban-edge-x86_64
PACKAGES="systemd,systemd-sysv,systemd-timesyncd,systemd-resolved,udev,dbus,linux-image-amd64,initramfs-tools,\
rauc,rauc-service,netbase,grub-common,openssh-server,nftables,python3,python3-venv,ca-certificates,iproute2,iputils-ping,kmod,\
e2fsprogs,dosfstools,util-linux,fdisk,procps,libglib2.0-0,libgomp1,firmware-realtek,intel-microcode,amd64-microcode"
# packages added later go on top of the cached base (a few MB to fetch, not the whole system again):
# cryptsetup and the TPM 2.0 libraries - the data partition is LUKS2 with its key sealed in the machine's TPM
# (systemd-cryptenroll / systemd-cryptsetup load libtss2 at run time); tpm2-tools: the device key of every unit,
# district and lighting cabinet is made inside the TPM and signs there (urban.devices.signer drives tpm2_createprimary,
# tpm2_create, tpm2_load, tpm2_sign). Its other libraries (libtss2-sys1, -fapi1, -tctildr0, libcurl4, ...) are Depends
# of the package and come with it; libtss2-tcti-device0 (the /dev/tpmrm0 transport) is listed above
EXTRA_PACKAGES="cryptsetup-bin libcryptsetup12 libtss2-esys-3.0.2-0 libtss2-mu0 libtss2-rc0 libtss2-tcti-device0 tpm2-tools"
# real mini-PCs: Realtek network chips want their firmware, and the CPU its microcode fixes (Debian's
# non-free-firmware section, redistributable); Intel network chips need nothing
ESP_MB=128; SLOT_MB=1536; DATA_MB=2048  # data: keys, kit, cameras and room for one update bundle
mkdir -p "$OUT/cache/pip" "$OUT/keys" "$OUT/build"
log() { echo ">> $*"; }

# ---- update signing keys: a CA whose certificate goes into the image, and a signing certificate from it
bash /work/os/build/keys.sh "$OUT/keys"  # made once; valid from 2020 so a unit's wrong clock cannot block updates

# ---- base system, cached by its package list; the extras on top, cached again
COMPONENTS=main,non-free-firmware
BASE_ID=$(echo "bookworm $COMPONENTS $PACKAGES" | sha256sum | cut -c1-12)
BASE="$OUT/cache/base-$BASE_ID.tar"
FULL_ID=$(echo "$BASE_ID $EXTRA_PACKAGES" | sha256sum | cut -c1-12)
FULL="$OUT/cache/base-$BASE_ID-$FULL_ID.tar"
APT="apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30"  # a stalled mirror must not hang the build
ROOT=/tmp/root
rm -rf "$ROOT"; mkdir -p "$ROOT"
if [ -f "$FULL" ]; then
  log "base system from cache ($BASE_ID+$FULL_ID)"
  tar -C "$ROOT" -xf "$FULL"
else
  if [ ! -f "$BASE" ]; then
    log "debootstrap bookworm (first build takes a while)"
    debootstrap --variant=minbase --components="$COMPONENTS" --include="$PACKAGES" bookworm "$ROOT" http://deb.debian.org/debian
    tar -C "$ROOT" -cf "$BASE.tmp" . && mv "$BASE.tmp" "$BASE"
  else
    log "base system from cache ($BASE_ID)"
    tar -C "$ROOT" -xf "$BASE"
  fi
  log "extra packages: $EXTRA_PACKAGES"
  mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"; mount --bind /dev "$ROOT/dev"
  rm -f "$ROOT/etc/resolv.conf"; cp /etc/resolv.conf "$ROOT/etc/resolv.conf"  # the base's is a link into /run: no DNS in a chroot
  chroot "$ROOT" $APT update -qq
  DEBIAN_FRONTEND=noninteractive chroot "$ROOT" $APT install -y -qq --no-install-recommends $EXTRA_PACKAGES >/dev/null
  chroot "$ROOT" apt-get clean
  rm -rf "$ROOT"/var/lib/apt/lists/*
  for m in dev sys proc; do umount "$ROOT/$m"; done
  tar -C "$ROOT" -cf "$FULL.tmp" . && mv "$FULL.tmp" "$FULL"
fi

mount_chroot() {
  mount -t proc proc "$ROOT/proc"; mount -t sysfs sys "$ROOT/sys"; mount --bind /dev "$ROOT/dev"
  mkdir -p "$ROOT/root/.cache/pip"; mount --bind "$OUT/cache/pip" "$ROOT/root/.cache/pip"
}
umount_chroot() {
  for m in root/.cache/pip dev sys proc; do umount "$ROOT/$m" 2>/dev/null || true; done
}
trap umount_chroot EXIT
mount_chroot
rm -f "$ROOT/etc/resolv.conf"; cp /etc/resolv.conf "$ROOT/etc/resolv.conf"  # a link to resolved in the image

# ---- the URBAN software: the unit (urban.edge) and its vision (urban.vision), as on a server install
log "URBAN software"
mkdir -p "$ROOT/opt/urban/models"
tar -C /work --exclude='__pycache__' --exclude='*.egg-info' --exclude='.pytest_cache' -cf - backend | tar -C "$ROOT/opt/urban" -xf -
chroot "$ROOT" python3 -m venv /opt/urban/.venv
chroot "$ROOT" /opt/urban/.venv/bin/pip install --quiet --upgrade pip
chroot "$ROOT" /opt/urban/.venv/bin/pip install --quiet -e "/opt/urban/backend[edge,vision]"
chroot "$ROOT" /opt/urban/.venv/bin/python -m compileall -q /opt/urban/backend/urban >/dev/null
chroot "$ROOT" /opt/urban/.venv/bin/python -c "import urban.edge.__main__, urban.vision.__main__, urban.modules.lighting.__main__, urban.devices.signer, cv2, onnxruntime"
# the TPM tools the signer drives must be in the image and start (no TPM here: --version never opens a device)
for t in tpm2_createprimary tpm2_create tpm2_load tpm2_sign; do
  chroot "$ROOT" "$t" --version >/dev/null || { echo "tpm2-tools: $t does not run in the image" >&2; exit 1; }
done
# the control centre's interface (role center): built on the host with `npm run build` in web/
[ -f /work/web/dist/index.html ] || { echo "web/dist is missing: run npm run build in web/ first" >&2; exit 1; }
mkdir -p "$ROOT/opt/urban/web"; cp -r /work/web/dist "$ROOT/opt/urban/web/dist"; chmod -R a+rX,go-w "$ROOT/opt/urban/web"
ln -s /var/lib/urban/platform "$ROOT/opt/urban/data"  # the platform's data/ (city, keys, accounts, history)
if [ -f /work/data/models/yolox_nano.onnx ]; then
  cp /work/data/models/yolox_nano.onnx "$ROOT/opt/urban/models/"
else
  log "no data/models/yolox_nano.onnx: the image ships without a vision model (see docs/vision.md)"
fi

# ---- the operating system around it
log "system configuration"
cp -r /work/os/rootfs/. "$ROOT/"
cp /work/deploy/urban-edge@.service /work/deploy/urban-vision@.service /work/deploy/urban-lighting@.service "$ROOT/etc/systemd/system/"
# the data partition is mounted by label; systemd-cryptsetup needs the dm-crypt module early
echo dm_crypt >> "$ROOT/etc/modules"
cp "$OUT/keys/ca.pem" "$ROOT/etc/rauc/keyring.pem"
# the repository may sit on a file system without Unix modes (Windows): set them, never copy them
(cd /work/os/rootfs && find . -type d) | while read -r d; do chmod 755 "$ROOT/$d"; done
(cd /work/os/rootfs && find . -type f) | while read -r f; do chmod 644 "$ROOT/$f"; done
chmod 644 "$ROOT"/etc/systemd/system/urban-*.service "$ROOT/etc/rauc/keyring.pem"
chmod 755 "$ROOT"/usr/local/bin/urban-*
chroot "$ROOT" useradd --system --uid 990 --home-dir /var/lib/urban --no-create-home --shell /usr/sbin/nologin urban
# the camera vision has its own user: same group (it shares its queue file), different uid (no reach into the unit)
chroot "$ROOT" useradd --system --uid 991 --gid urban --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin urban-vision
# the group of the TPM's resource manager (/dev/tpmrm0, set by etc/udev/rules.d/60-urban-tpm.rules): the unit, district and
# cabinet services join it (SupplementaryGroups=tss) to sign with the device key. Whether Debian's TPM packages made it or
# not, it exists now.
chroot "$ROOT" getent group tss >/dev/null || chroot "$ROOT" groupadd --system tss
chroot "$ROOT" getent group tss >/dev/null || { echo "group tss was not made" >&2; exit 1; }
rm -rf "$ROOT/etc/urban"; ln -s /var/lib/urban/etc "$ROOT/etc/urban"
mkdir -p "$ROOT/var/lib/urban" "$ROOT/boot/efi"
rm -f "$ROOT"/etc/ssh/ssh_host_*                       # every unit makes its own at first boot
ln -sf /run/systemd/resolve/stub-resolv.conf "$ROOT/etc/resolv.conf"
# the unit's screen: getty reads /etc/issue.d (not /run) when /etc/issue exists, and the root is read-only,
# so the live part is a link into /run that urban-console rewrites
mkdir -p "$ROOT/etc/issue.d"; ln -sf /run/urban/console.issue "$ROOT/etc/issue.d/50-urban.issue"
: > "$ROOT/etc/machine-id"                               # empty: systemd keeps a transient one on a read-only root
echo urban-edge > "$ROOT/etc/hostname"
printf "127.0.0.1 localhost\n127.0.1.1 urban-edge\n" > "$ROOT/etc/hosts"
chroot "$ROOT" passwd -l root >/dev/null                 # no password logins at all; maintenance by SSH key only
printf "VERSION=%s\nBUILT=%s\nCOMPATIBLE=%s\n" "$VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$COMPATIBLE" > "$ROOT/etc/urban-os-release"
# the root file system, and the watchdog drivers, so the timer is armed before systemd starts
for m in squashfs i6300esb iTCO_wdt sp5100_tco; do echo "$m" >> "$ROOT/etc/initramfs-tools/modules"; done
chroot "$ROOT" systemctl enable systemd-networkd systemd-resolved systemd-timesyncd ssh nftables \
  urban-grow.service urban-prepare.service urban-os-check.service urban-health.service urban-power.service >/dev/null 2>&1
chroot "$ROOT" systemctl mask apt-daily.timer apt-daily-upgrade.timer e2scrub_all.timer >/dev/null 2>&1 || true
if [ "$BREAK" = "firewall" ]; then
  log "BREAK: firewall rules that do not load (rollback test only)"
  echo "this is not an nftables rule" >> "$ROOT/etc/nftables.conf"
fi

log "kernel and initramfs"
chroot "$ROOT" update-initramfs -u -k all >/dev/null
cp "$(ls "$ROOT"/boot/vmlinuz-* | sort -V | tail -1)" "$ROOT/boot/vmlinuz"
cp "$(ls "$ROOT"/boot/initrd.img-* | sort -V | tail -1)" "$ROOT/boot/initrd.img"

chroot "$ROOT" apt-get clean
umount_chroot  # the pip cache is the host's: let go of it before cleaning up
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/apt/* "$ROOT"/tmp/* "$ROOT"/root/.cache
find "$ROOT/usr/share/doc" -type f ! -name copyright -delete 2>/dev/null || true
rm -rf "$ROOT"/usr/share/man/* "$ROOT"/usr/share/info/*
umount_chroot
trap - EXIT

# ---- slot image: a read-only squashfs; GRUB reads the kernel straight from it
SLOT="$OUT/build/rootfs-$VERSION.img"
log "rootfs slot image"
rm -f "$SLOT"
mksquashfs "$ROOT" "$SLOT" -comp gzip -noappend -quiet -no-progress
size=$(stat -c %s "$SLOT")
[ "$size" -lt $((SLOT_MB * 1024 * 1024)) ] || { echo "rootfs ($size bytes) does not fit a ${SLOT_MB} MiB slot" >&2; exit 1; }

# ---- signed update bundle
log "update bundle"
B=/tmp/bundle; rm -rf "$B"; mkdir -p "$B"
cp "$SLOT" "$B/rootfs.img"
printf "[update]\ncompatible=%s\nversion=%s\n\n[bundle]\nformat=verity\n\n[image.rootfs]\nfilename=rootfs.img\n" \
  "$COMPATIBLE" "$VERSION" > "$B/manifest.raucm"
rm -f "$OUT/urban-edge-$VERSION.raucb"
rauc bundle --cert "$OUT/keys/signing.pem" --key "$OUT/keys/signing.key" "$B" "$OUT/urban-edge-$VERSION.raucb" >/dev/null
printf "[system]\ncompatible=%s\nbootloader=noop\n[keyring]\npath=%s\ncheck-purpose=codesign\n" \
  "$COMPATIBLE" "$OUT/keys/ca.pem" > /tmp/verify.conf
rauc --conf /tmp/verify.conf info "$OUT/urban-edge-$VERSION.raucb" >/dev/null  # as the unit will check it

if [ "$IMAGE" = 1 ]; then
  log "disk image"
  E=/tmp/esp; rm -rf "$E"; mkdir -p "$E/EFI/BOOT"
  grub-mkimage -O x86_64-efi -o "$E/EFI/BOOT/BOOTX64.EFI" -p /EFI/BOOT \
    part_gpt fat squash4 normal linux echo test loadenv configfile regexp search all_video efi_gop gzio \
    boot cat sleep reboot halt password_pbkdf2 pbkdf2 gcry_sha512
  # the GRUB maintenance password: made once, kept with the other keys (data/os/keys, never in git); only its
  # PBKDF2 hash goes into the image
  if [ ! -f "$OUT/keys/grub-password" ]; then
    ( umask 077; head -c 18 /dev/urandom | base64 > "$OUT/keys/grub-password" )
  fi
  pw=$(cat "$OUT/keys/grub-password")
  hash=$(printf '%s\n%s\n' "$pw" "$pw" | grub-mkpasswd-pbkdf2 | sed -n 's/.*is \(grub\.pbkdf2\..*\)$/\1/p')
  [ -n "$hash" ] || { echo "grub-mkpasswd-pbkdf2 gave no hash" >&2; exit 1; }
  sed "s|^# URBAN-GRUB-PASSWORD\$|set superusers=\"urban\"\npassword_pbkdf2 urban $hash|" /work/os/build/grub.cfg > "$E/EFI/BOOT/grub.cfg"
  grep -q '^password_pbkdf2 urban grub.pbkdf2' "$E/EFI/BOOT/grub.cfg" || { echo "grub.cfg has no password" >&2; exit 1; }
  grub-editenv "$E/EFI/BOOT/grubenv" create
  grub-editenv "$E/EFI/BOOT/grubenv" set ORDER="A B" A_OK=1 A_TRY=0 B_OK=0 B_TRY=0
  cp /work/os/build/urban-unit.txt "$E/urban-unit.txt"
  rm -f /tmp/esp.img; mkfs.vfat -C -n URBAN-ESP /tmp/esp.img $((ESP_MB * 1024)) >/dev/null
  mcopy -s -i /tmp/esp.img "$E"/* ::/

  rm -f /tmp/data.img; mkdir -p /tmp/data-root
  mkfs.ext4 -q -L urban-data -d /tmp/data-root /tmp/data.img ${DATA_MB}M

  # assembled inside the container with large writes, then copied out once (sparse): writing it in place over a
  # shared folder takes millions of small writes
  DISK=/tmp/disk.img
  rm -f "$DISK"
  truncate -s $(( (1 + ESP_MB + 2 * SLOT_MB + DATA_MB + 1) * 1024 * 1024 )) "$DISK"
  sfdisk --quiet "$DISK" <<EOF
label: gpt
start=1MiB, size=${ESP_MB}MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="esp"
size=${SLOT_MB}MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="rootfs-a"
size=${SLOT_MB}MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="rootfs-b"
size=${DATA_MB}MiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="urban-data"
EOF
  offset() { sfdisk -J "$DISK" | python3 -c "import json,sys; print(json.load(sys.stdin)['partitiontable']['partitions'][$1]['start'])"; }
  put() { dd if="$1" of="$DISK" bs=4M seek=$(( $(offset "$2") * 512 )) oflag=seek_bytes conv=notrunc,sparse status=none; }
  put /tmp/esp.img 0
  put "$SLOT" 1
  put /tmp/data.img 3
  rm -f "$OUT/urban-edge-$VERSION.img"
  cp --sparse=always "$DISK" "$OUT/urban-edge-$VERSION.img"
  rm -f "$DISK"
  log "image: $OUT/urban-edge-$VERSION.img"
fi

python3 - "$OUT" "$VERSION" "$IMAGE" "$BREAK" <<'PY'
import hashlib, json, os, sys
out, version, image, broken = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
files = {"bundle": f"urban-edge-{version}.raucb", "rootfs": f"build/rootfs-{version}.img"}
if image:
    files["image"] = f"urban-edge-{version}.img"
manifest = {"version": version, "broken": broken or None,
            "files": {k: {"path": v, "sha256": sha(os.path.join(out, v)), "bytes": os.path.getsize(os.path.join(out, v))}
                      for k, v in files.items()}}
json.dump(manifest, open(os.path.join(out, f"manifest-{version}.json"), "w"), indent=1)
print(">> done:", ", ".join(f"{k} {v['bytes'] // (1 << 20)} MiB" for k, v in manifest["files"].items()))
PY
