// G6: a street-lighting cabinet on URBAN OS (MODULE=lighting, light-C-01) enrolled at a control centre (role center,
// no second factor for the test's admin): the centre lists it online on its schedule, an operator's override
// through the centre's API reaches the cabinet and its own screen, a command for an unknown cabinet is refused
// and changes nothing, and with the centre's platform stopped the cabinet returns to its schedule by itself within
// 180 s (and joins again when the platform is back).
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("lighting") !== 0) fail("lighting scenario");
