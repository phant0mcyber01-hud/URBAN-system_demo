// G1: the image builds from the repository and has the A/B layout. Rebuilds only when its inputs changed.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("image") !== 0) fail("image layout");
