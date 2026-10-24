// The file to write onto one machine of URBAN OS: a junction unit, a district server or the control centre.
//
//   node os/unit-image.mjs --unit edge-J-07 --serial URB-0001 [--key my-key.pub] [--version 1]
//   node os/unit-image.mjs --role district --unit district-1 --serial SRV-0001 --center urban-center
//   node os/unit-image.mjs --unit edge-J-07 --serial URB-0001 --vision off     (a light unit: no cameras)
//   node os/unit-image.mjs --unit light-C-01 --serial LGT-0001 --module lighting    (a street-lighting cabinet)
//   node os/unit-image.mjs --role district,center --unit center --serial SRV-0002 --host urban-center
//   node os/unit-image.mjs --arch arm64 --unit light-C-01 --serial LGT-0001 --module lighting   (on an arm64 board)
//
// --arch arm64 (step 11): a unit on a 64-bit ARM board with UEFI firmware, from the arm64 image (urban-edge-arm64-<version>
// .img); its boot loader BOOTAA64.EFI is made again by the grub-mkimage of that image's own system, run under emulation,
// and the file is data/os/release/urban-edge-arm64-<version>-<unit>.img.xz.
// Takes the built disk image and makes it this machine's own: a random disk GUID and a random GUID for every partition
// (no two units, and no stick written from the fleet image, share them), its own GRUB maintenance password (its hash in
// a boot loader built and signed again with the Secure Boot db key and in a grub.cfg signed again with the boot key,
// by os/build/boot-loader.sh as the fleet build makes them), the machine's name, serial and role (urban-unit.txt) and the
// maintenance key (maintenance.pub) on its ESP. Packs it as data/os/release/urban-edge-<version>-<unit>.img.xz with a
// .sha256 file (the packed and the unpacked image) and the guide docs/mini-pc.md next to it. The GRUB password is
// written to data/os/release/urban-edge-<version>-<unit>.grub-password, never into the image: it goes with the
// unit's passport.
// Without --key the project's own maintenance key is used, made once in data/os/keys/maintenance_ed25519;
// its private half never leaves this machine and is never committed (data/ is ignored).
// The same could be done by hand on any laptop: the ESP is FAT. This only saves the installer the step.
import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { OUT, ROOT, build, fail, inBuilder } from "./tests/lib.mjs";

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const unit = opt("--unit", "");
const serial = opt("--serial", "");
const version = opt("--version", "1");
const role = opt("--role", "unit");
const host = opt("--host", "");
const center = opt("--center", "");
const vision = opt("--vision", "");
const module_ = opt("--module", "");
const arch = opt("--arch", "x86");
if (!["x86", "arm64"].includes(arch)) fail("--arch arm64 (an ARM board); leave it out for an x86 mini-PC");
if (arch === "arm64" && role !== "unit") fail("--arch arm64 is for a unit (a light unit or a street-lighting cabinet on an ARM board)");
const tag = arch === "x86" ? version : `${arch}-${version}`;  // the built image's name: urban-edge-<tag>.img
const efi = arch === "x86" ? "BOOTX64.EFI" : "BOOTAA64.EFI";
if (module_ && module_ !== "lighting") fail("--module lighting is the only choice (a street-lighting cabinet); leave it out for a junction unit");
if (module_ && role !== "unit") fail("--module lighting is for a unit (--role unit)");
const names = { unit: module_ ? /^light-[A-Za-z0-9._-]+$/ : /^edge-[A-Za-z0-9._-]+$/, district: /^district-[A-Za-z0-9._-]+$/,
  center: /^center(-[A-Za-z0-9._-]+)?$/, "district,center": /^center(-[A-Za-z0-9._-]+)?$/ };
if (!names[role]) fail("--role unit | district | center | district,center");
if (!names[role].test(unit) || unit === "edge-J-00") {
  fail({ unit: module_ ? "--unit light-<cabinet>, e.g. light-C-01" : "--unit edge-<junction>, e.g. edge-J-07", district: "--unit district-<name>, e.g. district-1" }[role]
    || "--unit center or center-<name>");
}
if (host && !/^[A-Za-z0-9.-]+$/.test(host)) fail("--host: a host name or an address");
if (center && !/^[A-Za-z0-9.-]+(:[0-9]+)?$/.test(center)) fail("--center: the centre's host name or address, host[:port]");
if (role === "district" && !center) fail("--role district needs --center <the control centre's address>");
if (vision && vision !== "off") fail("--vision off is the only choice (a light unit); leave it out for a unit with cameras");
if (vision && role !== "unit") fail("--vision off is for a junction unit");
if (!/^[A-Za-z0-9._-]+$/.test(serial)) fail("--serial: the number on the box (letters, digits, . _ -)");
if (!/^[0-9][0-9A-Za-z.+-]*$/.test(version)) fail("--version: digits first");

const keys = join(OUT, "keys");
let pub;
if (opt("--key")) {
  pub = readFileSync(opt("--key"), "utf8").trim();
} else {
  if (!existsSync(join(keys, "maintenance_ed25519"))) {
    const r = inBuilder("ssh-keygen -q -t ed25519 -N '' -C urban-maintenance -f /out/keys/maintenance_ed25519");
    if (r.status !== 0) fail("could not make the maintenance key");
    console.log(">> made the maintenance key data/os/keys/maintenance_ed25519 (keep it; it opens every unit made with it)");
  }
  pub = readFileSync(join(keys, "maintenance_ed25519.pub"), "utf8").trim();
}
if (!/^(ssh-ed25519|ecdsa-sha2-nistp\d+|ssh-rsa) [A-Za-z0-9+/=]+( [^\r\n]*)?$/.test(pub)) fail("that is not one SSH public key");

build(version, { image: true, arch });

const release = join(OUT, "release");
mkdirSync(release, { recursive: true });
const name = `urban-edge-${tag}-${unit}`;
// the installer's text file, as the image ships it but with this unit's lines; CRLF so Notepad shows it right
const unitTxt = readFileSync(join(ROOT, "os", "build", "urban-unit.txt"), "utf8")
  .replace(/^UNIT=.*$/m, `UNIT=${unit}`).replace(/^SERIAL=.*$/m, `SERIAL=${serial}`)
  .replace(/^ROLE=.*$/m, `ROLE=${role}`).replace(/^HOST=.*$/m, `HOST=${host}`)
  .replace(/^CENTER=.*$/m, `CENTER=${center}`).replace(/^VISION=.*$/m, `VISION=${vision}`).replace(/^MODULE=.*$/m, `MODULE=${module_}`).replace(/\r?\n/g, "\r\n");
// arm64: the boot loader is made by the grub-mkimage of the image's own arm64 system (its slot, unpacked here, run under
// emulation), with the arm64-efi modules of the same GRUB version, fetched into the volume's cache by the arm64 build
const armLoader = `root=/tmp/arm-root; rm -rf "$root"
unsquashfs -q -n -d "$root" /out/build/rootfs-${tag}.img
gv=$(chroot "$root" dpkg-query -W -f='\${Version}' grub-common)
deb=""
for d in /out/cache/*-grub-efi-arm64-bin.deb; do
  if [ -f "$d" ] && [ "$(dpkg-deb -f "$d" Version)" = "$gv" ]; then deb=$d; fi
done
[ -n "$deb" ] || { echo "no grub-efi-arm64-bin $gv in the build cache: build the arm64 image again" >&2; exit 1; }
boot_loader "$hash" "$E" /out/keys arm64 "$root" "$deb"
rm -rf "$root"`;
const script = `set -euo pipefail
. /work/os/build/boot-loader.sh
img=/tmp/${name}.img
pwfile=/release/${name}.grub-password
trap 'rm -f "$pwfile.tmp"' EXIT
cp --sparse=always /out/urban-edge-${tag}.img "$img"
# the unit's own disk GUID and partition GUIDs, on the copy (the fleet image stays as it is): random, so that no two
# units and no stick written from the fleet image carry the same ones. Nothing in the boot knows the fleet's: GRUB reads
# the slot's GUID from the disk it booted from (grub.cfg, probe --part-uuid) and hands it on (urban.bootpart).
sfdisk --disk-id "$img" "$(cat /proc/sys/kernel/random/uuid)" >/dev/null
nparts=$(sfdisk -J "$img" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['partitiontable']['partitions']))")
for n in $(seq 1 "$nparts"); do
  sfdisk --part-uuid "$img" "$n" "$(cat /proc/sys/kernel/random/uuid)" >/dev/null
done
python3 - /out/urban-edge-${tag}.img "$img" <<'PY'
import json, subprocess, sys
def guids(disk):
    t = json.loads(subprocess.run(["sfdisk", "-J", disk], check=True, capture_output=True, text=True).stdout)["partitiontable"]
    return [t["id"].lower()] + [p["uuid"].lower() for p in t["partitions"]]
fleet, unit = guids(sys.argv[1]), guids(sys.argv[2])
if len(unit) != len(fleet) or set(unit) & set(fleet) or len(set(unit)) != len(unit):
    sys.exit(f"the unit's disk and partition GUIDs are not new: {unit} (fleet image: {fleet})")
PY
esp=$(( $(sfdisk -J "$img" | python3 -c "import json,sys; print(json.load(sys.stdin)['partitiontable']['partitions'][0]['start'])") * 512 ))
# the unit's own GRUB maintenance password: its hash goes into a new boot loader and grub.cfg, made and signed as the
# fleet build makes them (boot-loader.sh); the password itself only into the file next to the image
boot_gpg_init /out/keys
pw=$(head -c 18 /dev/urandom | base64)
( umask 077; printf '%s\\n' "$pw" > "$pwfile.tmp" )
hash=$(grub_password_hash "$pw")
E=/tmp/esp-unit; rm -rf "$E"
${arch === "x86" ? `boot_loader "$hash" "$E" /out/keys` : armLoader}
for f in ${efi} grub.cfg grub.cfg.sig; do
  mcopy -o -i "$img@@$esp" "$E/EFI/BOOT/$f" "::/EFI/BOOT/$f"
  rm -f /tmp/readback; mcopy -n -o -i "$img@@$esp" "::/EFI/BOOT/$f" /tmp/readback
  cmp -s "$E/EFI/BOOT/$f" /tmp/readback || { echo "the ESP's EFI/BOOT/$f does not read back" >&2; exit 1; }
done
rm -rf "$E" /tmp/readback /tmp/early.cfg
printf '%s' "$UNIT_TXT" > /tmp/urban-unit.txt
printf '%s\\r\\n' "$MAINT_KEY" > /tmp/maintenance.pub
mcopy -o -i "$img@@$esp" /tmp/urban-unit.txt ::/urban-unit.txt
mcopy -o -i "$img@@$esp" /tmp/maintenance.pub ::/maintenance.pub
echo ">> packing (xz)"
xz -T0 -6 -c "$img" > /release/${name}.img.xz.tmp && mv /release/${name}.img.xz.tmp /release/${name}.img.xz
{ (cd /release && sha256sum ${name}.img.xz); echo "$(sha256sum "$img" | cut -d' ' -f1)  ${name}.img"; } > /release/${name}.sha256
mv -f "$pwfile.tmp" "$pwfile"
rm -f "$img"
ls -la /release`;
const r = inBuilder(script, {
  extra: ["-v", `${release}:/release`, "-e", `UNIT_TXT=${unitTxt}`, "-e", `MAINT_KEY=${pub}`],
});
if (r.status !== 0) fail("making the unit image failed");
const pwFile = join(release, `${name}.grub-password`);  // one line, the password (made in the builder; never printed)
if (!existsSync(pwFile) || !/^\S+\n$/.test(readFileSync(pwFile, "utf8"))) fail(`data/os/release/${name}.grub-password is missing or not one line`);
copyFileSync(join(ROOT, "docs", "mini-pc.md"), join(release, "mini-pc.md"));
mkdirSync(join(release, "img"), { recursive: true });  // the guide's picture of the unit's screen
copyFileSync(join(ROOT, "docs", "img", "mini-pc-unit-screen.png"), join(release, "img", "mini-pc-unit-screen.png"));
console.log(`>> ${join("data", "os", "release", name + ".img.xz")}  (write it with Rufus or balenaEtcher; guide: mini-pc.md)`);
console.log(`>> GRUB maintenance password: data/os/release/${name}.grub-password (keep it with the unit's passport; it is not in the image)`);
console.log("UNIT IMAGE READY");
