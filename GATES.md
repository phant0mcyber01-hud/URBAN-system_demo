# Gates: Secure Boot and a verified system - the machine runs only what URBAN signed

OWNS: os/**, deploy/**, docs/**, tools/**, backend/**, GATES.md, CONTEXT.md, README.md

Scope: step 6. Until now the data partition was encrypted but the system itself (boot loader, kernel, the read-only
slot) could be replaced by anyone holding the disk. Now the boot chain is signed with URBAN's own keys: UEFI Secure
Boot (our PK/KEK/db, enrolled by the owner) starts only our signed GRUB; GRUB loads only a config, a slot file, a
kernel and an initrd signed with our GPG boot key (check_signatures=enforce; kernel and initrd are also bound to the slot
by a signed hash list; grubenv is read with only its whitelisted variables); the slot is a dm-verity device whose
root hash and length come from the signed slot file, so a changed block of the system stops the kernel and the unit
falls back to the other slot when a good one exists. Updates carry the new slot's signed files (a RAUC hook checks and
writes them to the ESP); the boot loader itself is not updated over the air. All of this holds only on a machine with
our keys enrolled and Secure Boot on (checked in QEMU with a prepared NVRAM image; enrolling keys in a real firmware is
a stand task). Keys live with the other keys in data/os/keys (never in git). The step-5 ledger is archived in
docs/gates/2026-10-20-tpm-key-step5.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=32cfd4c33c38c7beed1eb6398526102122c253d1d80f678ce49f6c45436c7ad3; output-bytes=342; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the image is signed through: the boot loader verifies against our db certificate (and fails against another), the boot config, kernel and initrd carry valid signatures of our boot key, the slot's dm-verity tree verifies against the root hash in its signed slot file, the firmware variables for the VMs hold our keys with Secure Boot on, and the earlier layout checks hold
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=34481a3ed60851032d11e1c782199ad7f8818bce64c84a8d398682efd72ad182; output-bytes=884; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: in QEMU with Secure Boot enforced: the unit boots with our keys (the firmware and the kernel report Secure Boot, the root is the verity device); a boot loader not signed by our db is refused by the firmware; a changed boot config on the ESP is not executed; one changed byte in an installed slot makes that slot fail and the unit falls back to the other slot by itself
  CHECK: node os/tests/check-secureboot.mjs
  EXPECT: SECURE BOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=fc5a7a57208fde3c3aaeb3c0cf82a89676b6e71f763c70b4d218ac5b09a90cd3; exit=0; EXPECT=matched; output-sha256=89f5970276fc647545ca4f9f0853c8d0e9156f771992cc677bd08b61ecb0ae9a; output-bytes=23569; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: with Secure Boot on, a signed update still installs into the other slot (its signed slot file goes to the ESP) and boots
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=46a2ccac12495ff3d00f42a6356e526871a7fc5bbc55d6e9bef2a77768e59ca8; output-bytes=5920; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: with Secure Boot on, a broken update still rolls back by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=776c987e8b7fcc07136334a8113f8b78b54fb4fab59c7f538bfafa8728f90611; output-bytes=8849; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: with Secure Boot on, units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=050544739c6d4bf2ce875ee2d91d61ed1b31475f4643a89ac5c59066053574bb; output-bytes=32005; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: with Secure Boot on, the intruder check on a junction unit still holds (key in the TPM, the jails, SSH)
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=a0dfd065ad5566ca179441e7daeb48ce52d86a02b35a98aab39f7faa8152c253; output-bytes=8910; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=5349c96610b1e02a80dbe0bd5a84b903f7195f99be4c98bfa9529ad520307a85; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the docs explain the signed boot chain, how the owner enrols the keys on a mini-PC, what is verified at each step and what an intruder with the disk can no longer do, and no longer list Secure Boot as missing; CONTEXT.md records step 6
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: the team deck says the system is signed and verified at boot (no longer in its gaps); the public deck stays plain
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=54cc1ca2c17d6d4b70e272f35a161400f6db53ab2b514f881c79da75c5c469b7; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-21. Team deck version 19 (17 slides), public deck version 8 (13 slides; unchanged in step 6). All 30 slides rendered at 1920x1080 and measured in the browser: no text past 920 px (highest 913), nothing clipped or overflowing. Claims checked against data/os/logs/gates-step6c.log and the VM consoles: status «10/10 PASSED · шаг 6» = G0-G9 met above; «свой загрузчик грузится, чужой — нет» = sb-loader.log `Boot0002 "UEFI Misc Device" ... Access Denied` for the unsigned and the foreign-key loader; «байт системы испорчен — откат сам» = sb-slot.log `metadata block 100510 is corrupted`, `dm-verity device corrupted`, then `URBAN-OS-CHECK OK version=2 slot=B`; 583 tests = PYTEST PASSED (583 tests); the «not verified» items match docs/edge-os.md «Что не проверено».
