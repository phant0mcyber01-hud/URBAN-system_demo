// Step 14, G3: the pilot machine's own cameras reach its centre, in QEMU. One pilot machine (ROLE=district,center) with
// one camera of real video (data/videos/chiang-mai-720p.webm, copied into the VM for the check only) through its real
// vision on the approach J-01-SW: with no enrolment code typed by anyone its district (district-center) is enrolled at
// its own centre (Devices, district-enrolled once) and reports over the loopback; the centre keeps J-01-SW's queue only
// once J-01 is routed through that district (urban-center provision assign), not before and not after it is routed back
// (the controls); after a restart it is the same district, not a second one        -> PILOT CAMERAS CHECK PASSED
// (os/tests/vm_pilot.py; the build is redone only when its inputs changed)
import { existsSync } from "node:fs";
import { join } from "node:path";
import { ROOT, build, fail, scenario } from "./lib.mjs";

if (!existsSync(join(ROOT, "data", "videos", "chiang-mai-720p.webm")))
  fail("data/videos/chiang-mai-720p.webm is missing: the camera's real video (see data/videos/SOURCES.txt)");
build("1", { image: true });
if (scenario("pilot-cameras") !== 0) fail("pilot-cameras scenario");
