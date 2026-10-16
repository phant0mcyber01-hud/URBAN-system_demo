// G3: a unit without an identity makes no key; with one it makes its key and enrolment code, kept across reboots.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("firstboot") !== 0) fail("first boot scenario");
