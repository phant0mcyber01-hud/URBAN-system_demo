// G2: the image boots in QEMU; the OS self-check, SSH, the firewall and the watchdog are looked at from outside.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("boot") !== 0) fail("boot scenario");
