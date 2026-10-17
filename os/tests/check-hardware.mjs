// G2: the image boots on what mini-PCs have - an NVMe disk, a SATA disk, a USB stick - with an Intel network card.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("hardware") !== 0) fail("hardware scenario");
