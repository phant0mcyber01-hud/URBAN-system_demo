// Step 12, G4: releases by architecture and a staged rollout, in QEMU. One centre with an x86 unit and an arm64 unit
// (QEMU aarch64, emulated), both enrolled straight to the centre: the centre holds release 2 for x86 and release 2 for
// arm64 (urban-center release add reads each package's compatible); each unit is offered and installs only its own
// platform's package and boots it. Then release 3 (a broken system) goes out for both platforms in a first wave of the
// x86 unit only: it rolls back, the rollout halts by itself, the arm64 unit's release 3 is never offered while halted
// (not even once the wave is widened to every device), and Devices (GET /api/releases) shows the halt with the device and
// the reason; the control: resumed, the arm64 unit is offered it at once        -> ROLLOUT CHECK PASSED
// (os/tests/vm_rollout.py; the builds are redone only when their inputs changed)
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
build("2");
build("3", { broken: "firewall" });
build("1", { image: true, arch: "arm64" });
build("2", { arch: "arm64" });
build("3", { arch: "arm64", broken: "firewall" });
if (scenario("rollout") !== 0) fail("rollout scenario");
