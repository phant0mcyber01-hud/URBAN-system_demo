// G4-G6: updates on a running unit. good: version 2 into slot B. foreign: version 2 signed by another key.
// broken: version 3 whose firewall does not load - the unit must fall back to version 1 by itself.
import { build, fail, scenario } from "./lib.mjs";

const kind = process.argv[2];
if (!["good", "foreign", "broken"].includes(kind)) fail("usage: check-update.mjs good|foreign|broken");
build("1", { image: true });
if (kind === "broken") build("3", { broken: "firewall" });
else build("2");
if (scenario("update", kind) !== 0) fail(`update scenario ${kind}`);
