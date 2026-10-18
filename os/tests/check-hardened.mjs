// G18: an intruder on a junction unit gets nothing. As root: no kernel memory, no memory of the unit's process,
// lockdown and ptrace off; as the hijacked unit service: no keys, no boot files, no writes outside its key folder,
// no other process; every network-facing URBAN service sandboxed to systemd's "OK" or better; SSH modern only.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("hardened") !== 0) fail("hardening scenario");
