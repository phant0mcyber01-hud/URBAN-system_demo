# Gates: the stand kit - every unit's image its own, keys kept off-line

OWNS: os/**, deploy/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 9 makes the kit for the first real mini-PC on the stand. Until now every unit written from one image had the
same disk and partition GUIDs (a USB stick carrying the same image next to the unit's disk stopped its boot) and the
whole fleet shared one GRUB maintenance password (its PBKDF2 hash on one unit's ESP, cracked off-line, opened the boot
menu of every unit). After this step os/unit-image.mjs gives every unit's image a random disk GUID and random partition
GUIDs and its own GRUB maintenance password: a new hash in the boot loader's embedded config and in grub.cfg, the boot
loader signed again with the Secure Boot db key and grub.cfg with the boot key, the password kept next to the image on
the build machine only (data/os/release, never in git, never inside the image). The keys that must outlive this laptop
(update CA and signing key, Secure Boot PK/KEK/db, the GRUB boot key, the recovery escrow key) get an encrypted off-line
backup that opens only with the owner's passphrase and is proven to restore. The stand guide gets the installation order
step by step. Issuing production keys instead of the test keys stays the owner's decision and step; the tools for it are
documented. The step-8 ledger is archived in docs/gates/2026-10-23-wireguard-step8.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=e52fa8a9c53ccd96c03688e0959c79479af5ed6ef0dc7e4957f0cb4888eec7b7; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=585899d1912d7785635efa31a5ff93ed7e4326d27d9df0b9c403725771cc85cb; output-bytes=57; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the fleet image still carries every earlier piece (signed chain, escrow, boot disk, tunnel)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=1c1afb04b3960de96f432427a469ed17e7b56cb1cfcbfe4197af27a0fc6e844e; output-bytes=1759; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: two stand images made by os/unit-image.mjs differ from each other and from the fleet image in their disk GUID and every partition GUID, and in their GRUB password hash; each one's boot loader still verifies against the Secure Boot db key and its grub.cfg against the boot key; the password is not inside the image and sits next to it on the build machine only; the stand image as shipped (unpacked, checked against its checksums) boots in QEMU under Secure Boot from NVMe with its own name, maintenance key and enrolment code, its boot disk named by its own partition GUID
  CHECK: node os/tests/check-unit-image.mjs
  EXPECT: UNIT IMAGE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d80e94bbbb7bec968ec8398b2fb386cab0493440208906891c5837967d1442ab; exit=0; EXPECT=matched; output-sha256=40b315f6d0ff11859386b28f116a778a6f2ed680eccad03d845df5a575946ab3; output-bytes=11138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: Secure Boot and the verified system still hold on the fleet image (our loader boots, a foreign one is refused, a changed config is not run, a changed byte falls back)
  CHECK: node os/tests/check-secureboot.mjs
  EXPECT: SECURE BOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=fc5a7a57208fde3c3aaeb3c0cf82a89676b6e71f763c70b4d218ac5b09a90cd3; exit=0; EXPECT=matched; output-sha256=194358cad28187592ad5d2c144b477206138ac0c4d3d0fff068e2299498b4cfe; output-bytes=26324; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the key backup holds: it contains every key file the build needs, opens only with the right passphrase (a wrong one is refused and writes nothing), restores byte for byte into an empty folder, and never writes a key or the passphrase unencrypted outside data/os/keys
  CHECK: node os/tests/check-keys-backup.mjs
  EXPECT: KEYS BACKUP CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f34085c2d576b84199c8108c83eab4777061a66ad10bba52c8ded16b26140f56; exit=0; EXPECT=matched; output-sha256=f27bd13c4d7f56061c066b7eac254021fc4599fdb79dc7fbbe87abe7e2f78c36; output-bytes=539; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the docs give the stand's installation order step by step, the per-unit GRUB password and where it is kept, the key backup and restore, and the production-keys decision; the limits list no longer says one GRUB password for the fleet nor identical partition GUIDs; CONTEXT.md records step 9
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the team deck shows what step 9 changed and what stays open; the public deck stays plain
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=ce7ecb5b1adbf367841800a1d53888c620fe13a5ea6ce605d3bcbee58ed84b5c; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-23. Team deck published as version 25 (20 slides: new changes9 «Шаг 9: комплект стенда», status rewritten for step 9, footers N / 20); public deck version 16 (13 slides; safety.html changed in its speaker notes only: every box has its own maintenance code). The live public safety.html and privacy.html were byte-identical to ours before publishing (no edits made in the app were overwritten). All 33 slides rendered at 1920x1080 and measured: no flowing content past 920 px, nothing wider than 1792 px, no pinned line wrapped (changes9 623, status 824, public safety 852). Claims against logs: «STATUS: 7/7 PASSED · шаг 9» = G1-G7 met above (gates-step9.log); «у каждого блока свои GUID», «свой пароль GRUB», «пароля нет внутри образа», «образ стенда грузится под Secure Boot» = G3 (step9-check-unit-image.log: fleet disk fb9349bf, edge-J-07 9067b85d, edge-J-08 cc7e1f9e; each hash opens with its own password only; BOOTX64.EFI against db, grub.cfg against the boot key; no password in the raw image or ESP; URBAN-BOOTDISK rootfs-a d0d09a9d on nvme0n1); «копия ключей открывается и только паролем» = G5 (27 files restored byte for byte, a wrong passphrase writes nothing). Open items (hardware, Secure Boot keys in a real BIOS, production keys as the owner's step, an old signed release with the disk in hand) match docs/edge-os.md and CONTEXT.md step 9.
