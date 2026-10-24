// Step 11: the arm64 light unit in QEMU aarch64 (virt machine, AAVMF with URBAN's Secure Boot keys enrolled, swtpm),
// emulated on this x86 laptop, so a long run (os/tests/vm_arm.py).
//   no argument  G4: it boots under Secure Boot with a verified system, its data handled honestly for the TPM its kernel can reach (QEMU's virt TPM has no driver in Debian's arm64 kernel: plain, and said so), its
//                self-check OK (role unit, vision off), its name, key and enrolment code on its screen, within 1 GB of
//                memory; a foreign boot loader is refused by the firmware; the x86 bundle (version 2) is refused by its
//                RAUC as the wrong compatible                                                    -> ARM CHECK PASSED
//   update       G5: arm64 version 2 installs into slot B and boots; version 3 (a firewall that does not load) is
//                installed into the other slot and rolls back by itself                         -> ARM UPDATE CHECK PASSED
import { build, fail, scenario } from "./lib.mjs";

const kind = process.argv[2];
if (kind !== undefined && kind !== "update") fail("usage: check-arm.mjs [update]");
build("1", { image: true, arch: "arm64" });
if (kind === "update") {
  build("2", { arch: "arm64" });
  build("3", { arch: "arm64", broken: "firewall" });
  if (scenario("arm-update") !== 0) fail("arm64 update scenario");
} else {
  build("2");  // the x86 bundle the ARM unit must refuse
  if (scenario("arm") !== 0) fail("arm64 unit scenario");
}
