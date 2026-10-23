// G4 / step 8 (the image check of every step): the image builds from the repository and has the A/B layout, the signed
// boot chain, the boot-disk and escrow pieces and the three roles' pieces; since step 8 also the tunnel's: wireguard-tools,
// the WireGuard module loaded at boot, the root helper urban-wg with its path unit, timer and service, urban-prepare's
// inbox, key and firewall fragment, and the relay's mode only. Rebuilds only when its inputs changed.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("image") !== 0) fail("image layout");
