// G8: three machines, unit -> district -> centre. The unit's TLS session reaches the centre through the district's
// relay (a client through the relay sees the centre's certificate), the district is a device of the centre with its
// address and health, its queues for its junction arrive, a 60 s outage of the centre loses none of its reports,
// and the district's firewall opens 8443 from private networks only.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("chain") !== 0) fail("chain scenario");
