// G18 / step 5: an intruder on a junction unit gets nothing, and the device key is inside the TPM. As root: no kernel
// memory, no memory of the unit's process, lockdown and ptrace off, no private key of the unit anywhere on the machine
// (the key folder holds TPM-wrapped blobs; copied to another machine they cannot sign there, while the unit itself
// still signs: the control); as the hijacked unit service: no keys, no boot files, no writes outside its key folder,
// no other process, and it can sign with the TPM (it is the unit) while an urban process without the TPM group cannot;
// every network-facing URBAN service sandboxed to systemd's "OK" or better; SSH modern only.
//
//   node os/tests/check-hardened.mjs            the junction unit (edge-J-09)
//   node os/tests/check-hardened.mjs lighting   the street-lighting cabinet (light-C-09, MODULE=lighting): the same
//                                               scenario, its own service urban-lighting@ in the jail
import { build, fail, scenario } from "./lib.mjs";

const kind = process.argv[2] ?? "unit";
if (!["unit", "lighting"].includes(kind)) fail(`unknown scenario "${kind}" (unit or lighting)`);
build("1", { image: true });
if (scenario("hardened", kind) !== 0) fail(`hardening scenario (${kind})`);
