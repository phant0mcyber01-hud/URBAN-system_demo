# Gates: measured boot built into URBAN OS - the data key goes only to this unit's approved, current release

OWNS: os/**, deploy/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 11 builds the mechanism of docs/measured-boot-design.md (step 10) into URBAN OS. The data key is sealed under
PolicyAuthorize(owner) then PolicyPCR(7); each release brings an owner-signed approval bound to this unit's TPM (EK name):
PCR 11 = the release's security version, PolicyNV(floor < base+S+1), PolicyCommandCode(Unseal). The signed initramfs
measures the version into PCR 11 before the unseal and refuses a dangerous kernel command line; right after the unlock a
phase lock extends PCR 11 so any root obtained later is refused, enforced by its own unit before every access path; GRUB
has no password (no editing); root is locked and debug-shell masked. After an update is marked good the floor counter is
raised under the owner's approval and the other slot marked bad. The boot key and the Secure Boot db certificate rotate
(the old one into dbx). The owner's approval keys stay off every unit; the centre only carries the signed approvals.
Units written before step 11 are written again. Every claim is checked in QEMU with a control. The step-10 ledger is
archived in docs/gates/2026-10-23-measured-boot-design-step10.md.

- [ ] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK

- [ ] G1: the TPM mechanism still holds on swtpm (step 10's proof, unchanged or extended)
  CHECK: node os/tests/check-tpm-policy.mjs
  EXPECT: TPM POLICY CHECK PASSED

- [ ] G2: the whole test suite passes, none skipped (including the release path's approvals and the centre's per-unit base)
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED

- [ ] G3: the image carries the measured-boot pieces and the earlier checks hold (cmdline allowlist and measurement in the initramfs, phase-lock unit ordered before every access path, root locked, debug-shell masked, GRUB with superusers and no password, the new db certificate, the old one in the enrolment's dbx)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED

- [ ] G4: in QEMU a unit seals its data under the policy at first boot and opens it by itself at every later boot of its approved release; an approval signed for another unit gives DATA LOCKED and its own opens (S9); an ordinary update N to N+1 opens the data without the recovery key, then the floor rises and the other slot is marked bad; release N is refused afterwards with DATA LOCKED (S1, S3, S8); a power cut between the unlock of N+1 and the floor raise, and between the raise and the mark, leaves the unit opening its data without the recovery key and the floor not lowered (S4); Secure Boot off gives DATA LOCKED and the recovery key opens and re-seals (S5)
  CHECK: node os/tests/check-measured.mjs
  EXPECT: MEASURED BOOT CHECK PASSED

- [ ] G5: in QEMU the old boot loader is refused: with the new enrolment (old db certificate in dbx) the firmware does not start it, and with an enrolment from before dbx its PCR 7 differs and the data stays locked; the new loader on the same unit opens (S1)
  CHECK: node os/tests/check-measured.mjs dbx
  EXPECT: MEASURED DBX CHECK PASSED

- [ ] G6: in QEMU no root ever gets the key: each forbidden kernel parameter (init=, rdinit=, break=top, break, single, emergency, systemd.unit=rescue.target, systemd.debug_shell) panics the initramfs before the unseal; with urban-grow killed right after the unseal, timed out, or the data mount failing, the phase lock still ran before sshd, getty and emergency, emergency and rescue give no shell, and an unseal tried afterwards is refused by the TPM; control: with the lock unit disabled in a test build the same unseal succeeds (S6, S7)
  CHECK: node os/tests/check-measured.mjs window
  EXPECT: MEASURED WINDOW CHECK PASSED

- [ ] G7: in QEMU a genuine image booted from USB on the same board with a foreign maintenance.pub and a foreign authorized_keys gives no shell before the unseal and no key after it; the internal disk's own release opens (S2)
  CHECK: node os/tests/check-measured.mjs usb
  EXPECT: MEASURED USB CHECK PASSED

- [ ] G8: Secure Boot, updates from the centre, the tunnel and the intruder check still hold
  CHECK: node os/tests/check-secureboot.mjs
  EXPECT: SECURE BOOT CHECK PASSED

- [ ] G9: units and districts still enrol, come online and update from the centre (now with the release's approvals)
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED

- [ ] G10: the intruder check holds on the measured-boot image
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED

- [ ] G11: the docs describe measured boot as built: the owner's keys and how a release is approved, the BIOS enrolment of the new db certificate with the old one in dbx, re-flashing units, maintenance without a GRUB password, what stays open; CONTEXT.md records step 11
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED

- [ ] G12: the team deck shows what step 11 changed and what stays open; the public deck stays plain
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED

- [ ] G13: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: pending
