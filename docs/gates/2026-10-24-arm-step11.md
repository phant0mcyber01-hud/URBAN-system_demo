# Gates: URBAN OS for ARM (arm64) - the light unit and the lighting cabinet on cheap boards

OWNS: os/**, deploy/**, backend/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 11 builds URBAN OS for 64-bit ARM boards with UEFI firmware (arm64), for the roles that go on cheap boards: the
light junction unit (ROLE=unit, VISION=off) and the street-lighting cabinet (MODULE=lighting). The same chain as on x86:
UEFI Secure Boot with URBAN's own keys, a standalone GRUB for arm64 (BOOTAA64.EFI) signed with the db key, signed GRUB
config and slot files, the system under dm-verity in A/B slots, RAUC updates with their own compatibility tag
(urban-edge-aarch64, so an x86 package never lands on an ARM unit nor the reverse), the data partition encrypted when the
board has a TPM 2.0 and plain with a clear screen line when it has none. The image is built on the x86 laptop (the ARM
system is installed and its GRUB made under emulation) and checked in QEMU aarch64 with ARM UEFI firmware (AAVMF) and
swtpm. Measured boot (designed in step 10) is parked: its build is a later step (docs/gates/2026-10-23-measured-boot-build-draft.md).
The step-10 ledger is archived in docs/gates/2026-10-23-measured-boot-design-step10.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=a21b784011640ac96051f109e2e6abec5f3959eb4c5f11937466f6dd49514c42; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=ed5b66eafba61fb7ef77a0d5db6297623cc69fdd278ff8e0dc59eea27a213716; output-bytes=57; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the x86 image is unchanged in what it carries (every earlier check of its layout holds)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=d45c4ce75a86bf3cf1b3adea0c28cf510ebb8a078bb0e9accc509323c856bab6; output-bytes=1759; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the arm64 image is what it says: every program of its system is an aarch64 ELF (none x86), BOOTAA64.EFI is an arm64 EFI binary that verifies against the db certificate, grub.cfg and the slot files verify against the boot key, the slot is dm-verity with its root hash in the signed slot file, the update bundle says compatible=urban-edge-aarch64 and verifies against the update CA, and the Python of the platform imports on it
  CHECK: node os/tests/check-image.mjs arm64
  EXPECT: ARM64 IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=8956589a2957cd2dc6b4fccfba75e18648af8c81823967a641c7b4e78171019b; exit=0; EXPECT=matched; output-sha256=3701db7067a13d2bb433273b64945f9f28872e115094a43e2fbfd259ba3d698c; output-bytes=1785; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: in QEMU aarch64 (virt machine, AAVMF with URBAN's Secure Boot keys enrolled, swtpm) the arm64 light unit boots under Secure Boot with a verified system, its data partition handled honestly for the TPM it can reach (QEMU's virt TPM is a tpm-tis-device, for which Debian's arm64 kernel has no driver: then the data stays plain and the unit says so in URBAN-DATA, the self-check and on the screen; a kernel that sees a TPM and keeps the data plain fails; encryption on an ARM board with a TPM is checked on hardware only), its self-check OK (secureboot=on verity=on firewall=on, role unit, vision off), its name, key and enrolment code on the screen, and it stays within 1 GB of memory; a foreign boot loader is refused by the firmware; control: the x86 bundle is refused by the ARM unit's RAUC (wrong compatible)
  CHECK: node os/tests/check-arm.mjs
  EXPECT: ARM CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3af7e301fc9d68ef0254e4b889886ff23989d654662c720df726f55264f4929; exit=0; EXPECT=matched; output-sha256=5f912999f1a729487abddcd5d3782ec9bddeacfb383aa5321d4b9b9dced54958; output-bytes=5250; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: in QEMU aarch64 a signed arm64 update installs into the other slot and boots, and a broken one rolls back by itself
  CHECK: node os/tests/check-arm.mjs update
  EXPECT: ARM UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=59f824fe8e36f4a45d4fb657dab8c05dd9d4fa0de9882c0ea8b673e52ce7e278; exit=0; EXPECT=matched; output-sha256=613cc3e347c1d44b606c6096c68432ae2600febe86bb50cbaf1d57c98f0cce33; output-bytes=11159; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: Secure Boot and the verified system still hold on x86
  CHECK: node os/tests/check-secureboot.mjs
  EXPECT: SECURE BOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=fc5a7a57208fde3c3aaeb3c0cf82a89676b6e71f763c70b4d218ac5b09a90cd3; exit=0; EXPECT=matched; output-sha256=8b0b3582f5878a9262445964141f9f8ad35eb13457e4cb3d077c7e76efe87901; output-bytes=27005; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the docs say how to build and write the ARM image, which boards it is for (arm64 with UEFI; no TPM means no encryption, said on the screen), how Secure Boot keys are enrolled on such a board, what was checked only in QEMU; «сборка под ARM» is no longer listed as missing; CONTEXT.md records step 11
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the team deck shows what step 11 changed and what stays open; the public deck says plainly that the system now also runs on small, cheap boards
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=64c79fa017ea718fd30505333fcc7373201fc75e05d745a42eb3cd19f94bf35c; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-24. Team deck published as version 32 (21 slides: new changes11 «Шаг 11: сборка под ARM», gaps without «Сборка под ARM», status rewritten for step 11, footers N / 21); public deck version 19 (13 slides: levels.html says plainly that the unit without cameras and the lighting cabinet now also run on small cheap boards, and that the data is encrypted where there is a protection chip). The live decks were byte-identical to the committed sources before publishing. All 34 slides rendered at 1920x1080 and measured: no flowing content past 920 px, nothing wider than 1792 px, no pinned line wrapped (changes11 665, status 779, gaps 612, public levels 859). Claims against logs: «STATUS: 8/8 PASSED · шаг 11» = G1-G8 met above (gates-step11.log); «образ arm64: только программы ARM» = G3 (4807 ELF, all aarch64); «ARM-блок грузится под Secure Boot», «чужой загрузчик не стартует», «пакет x86 на ARM не ставится», «169 МБ из 1 ГБ» = G4 (arm-unit.log); «обновление ARM и откат сами» = G5; the open items (encryption on an ARM board with a TPM, a real ARM board and its firmware, the measured boot build, real x86 hardware) match docs/edge-os.md and CONTEXT.md step 11.
