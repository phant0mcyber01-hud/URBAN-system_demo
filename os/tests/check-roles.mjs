// G5: the pilot's district server and control centre (ROLE=district,center) on a disk bigger than the image:
// the data partition grows, the control centre answers over HTTPS, only its ports open, a power plan for the role.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("roles") !== 0) fail("roles scenario");
