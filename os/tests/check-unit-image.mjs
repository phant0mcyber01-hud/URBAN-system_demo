// G3: every unit's image its own, as os/unit-image.mjs ships it. Two stand images are made (edge-J-07 and edge-J-08);
// vm.py unitimage compares them with each other and with the fleet image (disk and partition GUIDs, the GRUB password
// hash, the boot loader against the db key and grub.cfg against the boot key, the password nowhere inside), then boots
// the first as shipped: unpacked, checked against its checksums, its boot disk named by its own partition GUID, name and
// maintenance key from its ESP, the enrolment code on its screen, no other key let in.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { OUT, ROOT, build, fail, scenario } from "./lib.mjs";

const version = "1";
const stands = [["edge-J-07", "URB-STAND-01"], ["edge-J-08", "URB-STAND-02"]];
build(version, { image: true });  // here, with its output on the screen: unit-image.mjs then finds it up to date
const names = [];
for (const [unit, serial] of stands) {
  const name = `urban-edge-${version}-${unit}`;
  const made = spawnSync(process.execPath, [join(ROOT, "os", "unit-image.mjs"), "--unit", unit, "--serial", serial, "--version", version],
    { cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], maxBuffer: 64 << 20 });
  process.stdout.write(made.stdout || "");
  if (made.status !== 0) fail(`os/unit-image.mjs --unit ${unit}`);
  // where the unit's GRUB password is, said as the guide quotes it; the file one line, next to the image only
  const said = `>> GRUB maintenance password: data/os/release/${name}.grub-password (keep it with the unit's passport; it is not in the image)`;
  if (!made.stdout.split(/\r?\n/).includes(said)) fail(`os/unit-image.mjs did not say: ${said}`);
  const pwFile = join(OUT, "release", `${name}.grub-password`);
  const lines = existsSync(pwFile) ? readFileSync(pwFile, "utf8").split("\n") : [];
  if (lines.length !== 2 || lines[1] !== "" || !/^\S{16,}$/.test(lines[0])) fail(`${pwFile} is not one line holding a password`);
  if (readFileSync(join(OUT, "release", `${name}.sha256`), "utf8").includes(lines[0])) fail(`${name}.sha256 holds the GRUB password`);
  const ignored = spawnSync("git", ["check-ignore", "-q", `data/os/release/${name}.grub-password`], { cwd: ROOT });
  if (ignored.status !== 0) fail(`data/os/release/${name}.grub-password is not ignored by git`);
  names.push(name);
}
const [[unit, serial]] = stands;
const status = scenario("unitimage", `/release/${names[0]}.img.xz`, `/release/${names[0]}.sha256`,
  "/out/keys/maintenance_ed25519", unit, serial, `/release/${names[1]}.img.xz`, { extra: ["-v", `${join(OUT, "release")}:/release:ro`] });
if (status !== 0) fail("unit image scenario");
