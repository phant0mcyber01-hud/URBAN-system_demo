// The file to write onto one unit's mini-PC:
//
//   node os/unit-image.mjs --unit edge-J-07 --serial URB-0001 [--key my-key.pub] [--version 1]
//
// Takes the built disk image, puts the unit's name and serial (urban-unit.txt) and the maintenance key
// (maintenance.pub) on its ESP, and packs it as data/os/release/urban-edge-<version>-<unit>.img.xz with a
// .sha256 file (the packed and the unpacked image) and the guide docs/mini-pc.md next to it.
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
if (!/^edge-[A-Za-z0-9._-]+$/.test(unit) || unit === "edge-J-00") fail("--unit edge-<junction>, e.g. edge-J-07");
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

build(version, { image: true });

const release = join(OUT, "release");
mkdirSync(release, { recursive: true });
const name = `urban-edge-${version}-${unit}`;
// the installer's text file, as the image ships it but with this unit's lines; CRLF so Notepad shows it right
const unitTxt = readFileSync(join(ROOT, "os", "build", "urban-unit.txt"), "utf8")
  .replace(/^UNIT=.*$/m, `UNIT=${unit}`).replace(/^SERIAL=.*$/m, `SERIAL=${serial}`).replace(/\r?\n/g, "\r\n");
const script = `set -euo pipefail
img=/tmp/${name}.img
cp --sparse=always /out/urban-edge-${version}.img "$img"
esp=$(( $(sfdisk -J "$img" | python3 -c "import json,sys; print(json.load(sys.stdin)['partitiontable']['partitions'][0]['start'])") * 512 ))
printf '%s' "$UNIT_TXT" > /tmp/urban-unit.txt
printf '%s\\r\\n' "$MAINT_KEY" > /tmp/maintenance.pub
mcopy -o -i "$img@@$esp" /tmp/urban-unit.txt ::/urban-unit.txt
mcopy -o -i "$img@@$esp" /tmp/maintenance.pub ::/maintenance.pub
echo ">> packing (xz)"
xz -T0 -6 -c "$img" > /release/${name}.img.xz.tmp && mv /release/${name}.img.xz.tmp /release/${name}.img.xz
{ (cd /release && sha256sum ${name}.img.xz); echo "$(sha256sum "$img" | cut -d' ' -f1)  ${name}.img"; } > /release/${name}.sha256
rm -f "$img"
ls -la /release`;
const r = inBuilder(script, {
  extra: ["-v", `${release}:/release`, "-e", `UNIT_TXT=${unitTxt}`, "-e", `MAINT_KEY=${pub}`],
});
if (r.status !== 0) fail("making the unit image failed");
copyFileSync(join(ROOT, "docs", "mini-pc.md"), join(release, "mini-pc.md"));
mkdirSync(join(release, "img"), { recursive: true });  // the guide's picture of the unit's screen
copyFileSync(join(ROOT, "docs", "img", "mini-pc-unit-screen.png"), join(release, "img", "mini-pc-unit-screen.png"));
console.log(`>> ${join("data", "os", "release", name + ".img.xz")}  (write it with Rufus or balenaEtcher; guide: mini-pc.md)`);
console.log("UNIT IMAGE READY");
