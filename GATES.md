# Gates: measured boot - the design, and its TPM mechanics proven on a software TPM

OWNS: docs/**, os/tests/**, tools/**, backend/tests/**, GATES.md, CONTEXT.md

Scope: step 10 designs the last open hole of the boot chain and proves, before any unit runs it, that the TPM does what
the design needs. Today the data key is sealed to PCR 7 (the Secure Boot state) only. Two attacks with the disk in hand
still get it: (1) booting an old, genuinely signed URBAN release (its signatures still verify, PCR 7 is the same), then
exploiting what that release did not fix yet; (2) booting a genuine URBAN image from a USB stick on the same board: it
never booted before, so it takes the maintenance key from its own ESP, and root on it asks the TPM for the internal
disk's key under the same PCR 7. The design must close both without making an ordinary update ask for the recovery key,
must keep the recovery path, and must say plainly what it does not close. It must work with what Debian bookworm ships
(systemd 252, tpm2-tools 5.4, GRUB 2.06) or say exactly what it adds. The mechanics it relies on (sealing under a
policy, a release that is allowed and one that is refused, the update path, the recovery path) are proven on swtpm in a
container, not assumed. Building it into URBAN OS is step 11. The step-9 ledger is archived in
docs/gates/2026-10-23-stand-kit-step9.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=45f2f0b23659d603aebd358a717e9093af5a69c8ff812ab8199510941e171ecc; output-bytes=150; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: on a software TPM (swtpm, tpm2-tools as bookworm ships them) the design's mechanism holds: a secret sealed under the design's policy is released to the current release on this installation; refused to an older release after the floor was raised; refused to a genuine image of the same release booted from another disk or not yet provisioned; released again after an ordinary update to a newer release without the recovery key; and the recovery path still opens it; each refusal has a control that the same steps with the right state succeed
  CHECK: node os/tests/check-tpm-policy.mjs
  EXPECT: TPM POLICY CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=5a80785b8a5961f3e92a724b2738875c800054b85b0ba64a8485038abfc54824; exit=0; EXPECT=matched; output-sha256=d22ba1dc2442e2a23629d46803af67092530149e0dfea9398588636065260877; output-bytes=4950; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: docs/measured-boot-design.md gives the threat model (both attacks), the mechanism with every PCR, NV index and key it uses and who writes each, the boot-time and update-time sequence, the recovery and the failure cases (TPM cleared, firmware update, power cut mid-update), what bookworm lacks and how it is covered, what stays open, and the step-11 gates; CONTEXT.md records step 10
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=1579108c766384265aef421b1c594c15c2778d9eba02f51e55166c023233658c; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: an independent review of the design found no way for either attack to get the key that the design does not name as open, and every issue it raised is answered in the design
  EVIDENCE: manual, 2026-10-23. Two rounds of independent review (an Opus reviewer, read only, testing claims on swtpm). Round 1 broke the first design: B1 boot paths that never measure PCR 11/12 (shown on swtpm: root extended them and unsealed), B2 no sound source for PCR 12 (owner's public key on the USB ESP plus the attacker's authorized_keys), B3 unmeasured kernel command line, M1 an unauthenticated floor counter (permanent lockout; maxCounter survives undefine and clear; absolute first value), M2 a locked fallback slot marked good, F weak tests. The design was rewritten: PCR 12 dropped for a phase lock in PCR 11, boot-key and db-cert rotation (old cert to dbx) so old loaders give another PCR 7, GRUB with no password and an init-top cmdline allowlist, a capped owner-approved counter with a per-unit base, the other slot marked bad, a real LUKS2 recovery slot; the proof grew to 10 scenario groups, every refusal tied to its step. Round 2 verdict: within the design neither attack gets the key through a path the design does not name, on three conditions for step 11; its findings N1 (phase-lock window on every failure path), N2 (owner-key compromise), N3 (base storage, per-unit approvals: a real cross-unit rollback, now closed by policyRef = SHA-256(label || EK name), scenario 10), N4 (test gaps) are answered in «Ответы на второе ревью» and folded into the step-11 gates S1, S6-S9. Owner-key compromise is named as an open limit. After the last revision G1-G3 were re-run (--reverify): TPM POLICY CHECK PASSED (27 ok, 0 BAD), DOCS CHECK PASSED, PYTEST PASSED (692 tests).
