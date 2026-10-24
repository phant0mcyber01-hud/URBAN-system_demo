// G4 / step 8 (the image check of every step): the image builds from the repository and has the A/B layout, the signed
// boot chain, the boot-disk and escrow pieces and the three roles' pieces; since step 8 also the tunnel's: wireguard-tools,
// the WireGuard module loaded at boot, the root helper urban-wg with its path unit, timer and service, urban-prepare's
// inbox, key and firewall fragment, and the relay's mode only. Rebuilds only when its inputs changed.
// Step 11, with the argument arm64: the arm64 image (G3) - every program of its system an aarch64 ELF and none x86,
// BOOTAA64.EFI an arm64 EFI binary verifying against the db certificate, grub.cfg and the slot files against the boot key,
// the slot dm-verity with its root hash in the signed slot file, the bundle compatible=urban-edge-aarch64 verifying
// against the update CA, the platform's Python importing inside the arm64 system (os/tests/vm_arm.py).
import { build, fail, scenario } from "./lib.mjs";

const arch = process.argv[2];
if (arch === "arm64") {
  build("1", { image: true, arch });
  if (scenario("arm-image") !== 0) fail("arm64 image layout");
} else if (arch === undefined) {
  build("1", { image: true });
  if (scenario("image") !== 0) fail("image layout");
} else {
  fail("usage: check-image.mjs [arm64]");
}
