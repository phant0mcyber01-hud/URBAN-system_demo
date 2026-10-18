# Gates: the district server relays and reports, a light unit, an encrypted data partition, a three-machine demo; then speed and hardening

OWNS: os/**, backend/urban/node/**, backend/urban/devices/**, backend/urban/edge/**, backend/urban/platform.py, backend/urban/api/app.py, backend/urban/provision.py, backend/tests/**, web/src/**, deploy/**, docs/**, GATES.md, CONTEXT.md, README.md, tools/**

Scope: step 2 of the owner's three-level architecture, in time for the team presentation this week. The district server (ROLE=district) relays each junction unit's TLS connection to the control centre unchanged, so the centre still authenticates every unit and signs every command and the district can neither read nor forge them; the district is itself a device of the centre (enrolled with a code like a unit, shown in Devices with its address and health), reports the queues its cameras measure for its own junctions only, and keeps those reports through an outage of the link to the centre. A light unit (VISION=off) runs without camera work and is proven on 1 GB of memory. The data partition is encrypted (LUKS2) with a key sealed in the machine's TPM, so a disk taken out of its machine cannot be read; without a TPM it stays plain and the screen says so. One command brings up a three-machine demo (unit -> district -> centre) with every screen in a browser. WireGuard between junction and district and the ARM build are not in this step and the docs say so. Then, at the owner's request (any language, fast, excellent security, a break-in yields nothing): measured speed work (no URBAN service waits for the network, the district's relay moved into the kernel) and hardening (kernel lockdown, ptrace off for everyone, every network-facing service in a sandbox, modern-only SSH), proven by an intruder scenario with a control next to every probe.

- [ ] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: pending

- [ ] G1: the centre knows districts: a district enrols with its own code and address (no junction), a unit can be assigned to a district and its kit then points at the district's address while still naming the centre's certificate, a district's queues are accepted only for the junctions assigned to it, and a report replayed from a district's outage buffer is written to history at its own time without disturbing the live reading
  CHECK: node tools/gates/pytest.mjs tests/test_district.py
  EXPECT: PYTEST PASSED
  EVIDENCE: pending

- [ ] G2: the district's own client buffers reports while the centre is unreachable (bounded) and replays them in order after reconnecting, refuses commands and says so in its next report, and carries the relay's counts in its health
  CHECK: node tools/gates/pytest.mjs tests/test_district_client.py
  EXPECT: PYTEST PASSED
  EVIDENCE: pending

- [ ] G3: a light unit's power plan has no vision work and the power manager never starts counting cameras for it
  CHECK: node tools/gates/pytest.mjs tests/test_power.py
  EXPECT: PYTEST PASSED
  EVIDENCE: pending

- [ ] G4: the whole test suite passes
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: pending

- [ ] G5: the image carries the relay and district services, the encryption tools (cryptsetup, the TPM libraries systemd needs) and the district's firewall opening
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: pending

- [ ] G6: with a TPM the first boot turns the data partition into LUKS2 unlocked by that TPM (the raw partition no longer holds the maintenance key in clear, which the plain control does), what the installer put on it before is kept, a reboot unlocks it again with the same unit key, and without a TPM the partition stays plain and the screen and self-check say so
  CHECK: node os/tests/check-encrypted.mjs
  EXPECT: ENCRYPTION CHECK PASSED
  EVIDENCE: pending

- [ ] G7: a light unit (VISION=off) boots in 1024 MB, passes its self-check, runs its unit service after the kit with no vision process and under 400 MB in use, its plan says vision is off and its screen says it is a light unit
  CHECK: node os/tests/check-light.mjs
  EXPECT: LIGHT UNIT CHECK PASSED
  EVIDENCE: pending

- [ ] G8: three machines in a chain: a unit enrolled through a district reaches the centre through the district's kernel forwarding, no program on the district listening on 8443 (two devices online at the centre, the certificate seen through the district is the centre's), the district shows up in Devices with its address and health, the district's queues for its junction arrive at the centre, during a 60 s outage of the centre's device listener the district keeps its reports and the centre's history has samples from inside the outage afterwards, and the district forwards 8443 from private networks only
  CHECK: node os/tests/check-chain.mjs
  EXPECT: CHAIN CHECK PASSED
  EVIDENCE: pending

- [ ] G9: the pilot machine (district,center) still grows its data partition, serves the centre over HTTPS and passes its self-check
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: pending

- [ ] G10: a unit image still takes its name and maintenance key from the ESP and keeps its service across a reboot
  CHECK: node os/tests/check-unit-image.mjs
  EXPECT: UNIT IMAGE CHECK PASSED
  EVIDENCE: pending

- [ ] G11: first boot is unchanged: no key without a name, one key kept across reboots
  CHECK: node os/tests/check-firstboot.mjs
  EXPECT: FIRSTBOOT CHECK PASSED
  EVIDENCE: pending

- [ ] G12: a signed update still installs into the other slot and boots, and the new slot opens the encrypted data partition
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: pending

- [ ] G13: a broken update still rolls back by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: pending

- [ ] G14: the image still boots from NVMe, SATA and a USB stick with an Intel network card
  CHECK: node os/tests/check-hardware.mjs
  EXPECT: HARDWARE CHECK PASSED
  EVIDENCE: pending

- [ ] G15: one command brings the demo up - a unit, a district and the centre, three screens and the centre's interface reachable from this computer - and one command takes it down
  CHECK: node os/demo.mjs smoke
  EXPECT: DEMO SMOKE PASSED
  EVIDENCE: pending

- [ ] G17: the unit's control service is up within 15 s of urban-prepare also with the network cable pulled, and booting costs less CPU than the previous image did in the same emulated VM, with no more CPU in use while it runs
  CHECK: node os/tests/check-speed.mjs
  EXPECT: SPEED CHECK PASSED
  EVIDENCE: pending

- [ ] G18: an intruder on a junction unit gets nothing: as root no kernel memory and no memory of the unit's process (own memory readable: the control), kernel lockdown confidentiality, ptrace off, no kexec, no core dumps; as the hijacked unit service no SSH keys, no boot files, no writes outside its key folder (inside it: the control), no other process; every network-facing URBAN service at systemd exposure 3.0 or better; SSH refuses CBC, SHA-1 key exchange and SHA-1 MACs while the modern default works; nothing but SSH listens
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: pending

- [ ] G16: the docs describe the relay, districts as devices, the light unit, the encryption and its TPM rule, the demo, what an intruder gets at each level and how speed was measured, and what is still not built (WireGuard, ARM, Secure Boot), and CONTEXT.md records the decisions of this step with their reasons
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: pending

<!-- 2026-10-17: the owner asked to skip the full verification run ("скипни эту часть"). Checked one by one in this
session, on images of this step (not re-run after the last changes): speed (G17), intruder (G18), image layout (G5),
encryption (G6), light unit (G7), pilot machine (G9), docs (G16), 238 backend tests (G3/G4); the three-machine chain
(G8) failed on the district's queue reading, fixed in code (a light unit's empty report wiped it), not re-run.
Not run at all: G10-G15. -->
ABANDON: G0 owner skipped the verification run on 2026-10-17; run gate-check.mjs --approve GATES.md before relying on this step
ABANDON: G1 owner skipped the verification run on 2026-10-17 (district tests passed by hand, not through the gate)
ABANDON: G2 owner skipped the verification run on 2026-10-17 (district client tests passed by hand, not through the gate)
ABANDON: G3 owner skipped the verification run on 2026-10-17 (power tests passed by hand, not through the gate)
ABANDON: G4 owner skipped the verification run on 2026-10-17 (238 tests passed by hand, not through the gate)
ABANDON: G5 owner skipped the verification run on 2026-10-17 (image layout passed before the last rootfs changes)
ABANDON: G6 owner skipped the verification run on 2026-10-17 (encryption passed before the hardening changes)
ABANDON: G7 owner skipped the verification run on 2026-10-17 (light unit passed before the hardening changes)
ABANDON: G8 owner skipped the verification run on 2026-10-17 (chain failed once; fix in code not re-run on three machines)
ABANDON: G9 owner skipped the verification run on 2026-10-17 (pilot passed before the hardening changes)
ABANDON: G10 owner skipped the verification run on 2026-10-17 (unit image not run in this step)
ABANDON: G11 owner skipped the verification run on 2026-10-17 (first boot not run in this step)
ABANDON: G12 owner skipped the verification run on 2026-10-17 (update not run in this step)
ABANDON: G13 owner skipped the verification run on 2026-10-17 (rollback not run in this step)
ABANDON: G14 owner skipped the verification run on 2026-10-17 (hardware not run in this step)
ABANDON: G15 owner skipped the verification run on 2026-10-17 (demo smoke not run)
ABANDON: G16 owner skipped the verification run on 2026-10-17 (docs check passed by hand, not through the gate)
ABANDON: G17 owner skipped the verification run on 2026-10-17 (speed passed by hand on the final image of the speed work)
ABANDON: G18 owner skipped the verification run on 2026-10-17 (intruder check passed by hand on the final hardened image)
