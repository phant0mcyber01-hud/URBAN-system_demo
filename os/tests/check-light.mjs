// G7: a light unit (VISION=off) in 1024 MB: self-check OK, no vision process, a plan without vision work, under
// 400 MB in use, its unit service running after the kit and after a reboot, its screen saying it is light.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("light") !== 0) fail("light unit scenario");
