// G3: the stand unit's image as it is shipped (os/unit-image.mjs), unpacked, checked against its checksums and
// booted: name and maintenance key from its ESP, the enrolment code on its screen, no other key let in.
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { OUT, ROOT, fail, scenario } from "./lib.mjs";

const unit = "edge-J-07", serial = "URB-STAND-01", name = `urban-edge-1-${unit}`;
const made = spawnSync(process.execPath, [join(ROOT, "os", "unit-image.mjs"), "--unit", unit, "--serial", serial],
  { cwd: ROOT, stdio: "inherit" });
if (made.status !== 0) fail("os/unit-image.mjs");
const status = scenario("unitimage", `/release/${name}.img.xz`, `/release/${name}.sha256`,
  "/out/keys/maintenance_ed25519", unit, serial, { extra: ["-v", `${join(OUT, "release")}:/release:ro`] });
if (status !== 0) fail("unit image scenario");
