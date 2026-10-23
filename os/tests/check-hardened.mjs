// G8 / step 8 (first G18 / step 5): an intruder on a junction unit gets nothing, and the device key is inside the TPM.
// As root: no kernel memory, no memory of the unit's process, lockdown and ptrace off, no private key of the unit
// anywhere on the machine (the key folder holds TPM-wrapped blobs; copied to another machine they cannot sign there,
// while the unit itself still signs: the control); as the hijacked unit service: no keys, no boot files, no writes
// outside its key folder, no other process, and it can sign with the TPM (it is the unit) while an urban process without
// the TPM group cannot; every network-facing URBAN service sandboxed to systemd's "OK" or better; SSH modern only.
// Since step 8 the junction unit also has its tunnel (a link signed as the centre signs it, a camera in its cabinet
// network): the hijacked service cannot read the tunnel key, change wg0 or add a peer; a forged link, or a genuine one
// naming another endpoint than the kit's district, that it writes into its inbox is refused by the root helper and wg0
// stays as it was (a genuine newer link is applied: the control); forwarding is on for wg0 and the cabinet network
// only, never globally; beside SSH only wg0's own UDP port listens; the helper's service is sandboxed to "OK" as well.
//
//   node os/tests/check-hardened.mjs            the junction unit (edge-J-09)
//   node os/tests/check-hardened.mjs lighting   the street-lighting cabinet (light-C-09, MODULE=lighting): the same
//                                               scenario, its own service urban-lighting@ in the jail
import { build, fail, scenario } from "./lib.mjs";

const kind = process.argv[2] ?? "unit";
if (!["unit", "lighting"].includes(kind)) fail(`unknown scenario "${kind}" (unit or lighting)`);
build("1", { image: true });
if (scenario("hardened", kind) !== 0) fail(`hardening scenario (${kind})`);
