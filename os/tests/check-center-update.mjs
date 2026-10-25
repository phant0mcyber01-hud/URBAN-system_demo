// Step 13, G3: the control centre updates itself, in QEMU. A centre (district,center) with one x86 unit enrolled: release
// 2 added with `urban-center release add`, then `urban-center release self-update 2` on the centre hands it to the root
// update helper, which installs it; the centre restarts into version 2 in the other slot with its database, the unit's
// enrolment and the audit as before, the unit comes back online by itself, and the audit says centre-updated; a broken
// release 3 by the same command fails its self-check and the centre is back on 2 by itself with the same data
// (centre-rolled-back); the halted release is refused by the command              -> CENTER UPDATE CHECK PASSED
// (os/tests/vm_center_update.py; the builds are redone only when their inputs changed)
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
build("2");
build("3", { broken: "firewall" });
if (scenario("center-update") !== 0) fail("center-update scenario");
