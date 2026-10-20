// G4: over the air. With the centre, a district and a unit behind it: one release command on the centre brings the
// district and the unit from version 1 to 2 by themselves (each boots slot B and passes its self-check, and the centre's
// Devices shows both on version 2); then the root helper on the unit, attacked as the user urban, refuses a package
// signed by another key, an older package claimed to be newer, a wrong hash, a file name outside the inbox and a link,
// each time leaving the running slot as it was.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
build("2");
if (scenario("ota") !== 0) fail("ota scenario");
