# Gates: the data stays locked off the signed boot, partitions only from the boot disk, a version floor at the centre

OWNS: os/**, deploy/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 7 closes the gaps the step-6 security review left open, where they can be closed honestly. (1) The data
partition's key is sealed in the TPM to PCR 7 (the Secure Boot state and its keys): someone at the cabinet who turns
Secure Boot off or boots their own system on the same board no longer gets the data. A recovery key, made at first
encryption and encrypted to an escrow key that only the owner keeps offline, lies on the ESP; a unit whose seal no
longer matches says so on its screen and opens its data again only with that recovery key typed on its console, then
re-seals to the new state. (2) Partitions are taken only from the disk the unit booted from: GRUB passes the boot
disk's identity, the initramfs, fstab, RAUC and the scripts use links made for that disk only, so a USB stick with
partitions named rootfs-a, esp or urban-data takes nothing over. (3) The centre keeps a minimum system version: a unit
or district reporting an older version is flagged and gets no commands until updated. Full protection against
booting an old signed release with the disk in hand needs measured boot with signed TPM policies and stays a later
step; a read-only ESP is not done because root on the unit can remount it (both said in the docs). The step-6 ledger
is archived in docs/gates/2026-10-21-secure-boot-step6.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=55c41c718c1f9b2cb91fa30a3652d89e5b8cbdbc8cf6d255913a06f63bc0ee67; output-bytes=342; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the centre's version floor: devices reporting a system version below the minimum are flagged in Devices with the reason, get no commands (a command to them is refused and audited), are offered the newest release as before, and are trusted again once they report a version at or above the minimum; the minimum is set and shown by the admin
  CHECK: node tools/gates/pytest.mjs tests/test_version_floor.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=752c9df8b22f6183e43ee08c71aa3da38c18a8e0deab21fbe5b6536815383ae4; exit=0; EXPECT=matched; output-sha256=b507b0b2cd8a285aa48553473e8a509314f8cdd9776f11b2641d8cf4aace6bd0; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=de4fb57f4a7c5650cb819cee9f8609fa32dac55d559b25ab5d253b102cba4b93; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the image carries the recovery escrow and boot-disk pieces and the earlier signed-chain checks hold (escrow public key in the image, no escrow private key anywhere in it, udev rule and boot-disk links, GRUB passes the boot disk identity)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=d33a524bfc5a8c1dcf2681b3df0c5c29505b3240ad34347dfe2cfd255c931652; output-bytes=1514; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: in QEMU the data key is sealed to PCR 7: with Secure Boot as enrolled the unit unlocks by itself across a power cycle with the TPM restarted on its kept state (the control, the same path as the locked case); after the firmware's Secure Boot state changes the data stays locked, the unit says so on its screen and runs none of its services; the recovery key decrypted with the offline escrow key and typed on its console opens the data, the unit re-seals to the new state and unlocks by itself at the next boot
  CHECK: node os/tests/check-pcr7.mjs
  EXPECT: PCR7 CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=51a0e33986189f5035d5a09e7c8d5459ecc3a157cbef8afe3d42fab0a502adc1; exit=0; EXPECT=matched; output-sha256=202a7f0f7ab2dd496bc4dab0fb2e2d62bb87279e9d49bbbde52d0d208d2663da; output-bytes=15385; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: in QEMU with a second disk attached that carries partitions named rootfs-a, rootfs-b, esp and urban-data, the unit boots, mounts and updates only from its own disk: its links point to the boot disk, the stranger disk is byte-for-byte unchanged after a boot and an update (the control: the old by-label links do resolve to the stranger disk)
  CHECK: node os/tests/check-bootdisk.mjs
  EXPECT: BOOT DISK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=679e1dcce707d73d5b4a1f826e557c7fe66e28eb6203fc2fb4da16511efe4b67; exit=0; EXPECT=matched; output-sha256=6fbd20d70dbd5e9e0369540ae976f63b0f4bf503ac884785a30575970d55e82b; output-bytes=8279; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: Secure Boot and the verified system still hold (our loader boots, a foreign one is refused, a changed config is not run, a changed byte falls back)
  CHECK: node os/tests/check-secureboot.mjs
  EXPECT: SECURE BOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=fc5a7a57208fde3c3aaeb3c0cf82a89676b6e71f763c70b4d218ac5b09a90cd3; exit=0; EXPECT=matched; output-sha256=b6a80db5ce029d89673f640efb57327b2d55376fb824191570abb6e70611241a; output-bytes=25728; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: a signed update still installs into the other slot and boots; a unit without the boot disk's links (as one written before step 7) is refused by the bundle's install-check with slot B and the boot state unchanged
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=b7992e2bd1c52316111697ad3aff619a8ced7cfabbb1ef3d47b1ac8a7a63e544; output-bytes=6680; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: a broken update still rolls back by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=3ce27ded590611607291bd9a460d5f5dc16e31db27d838bf7b9fdfe5c2d542ef; output-bytes=9353; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=a7a6be76f97d74898e6bcfcb09a0eeefa062719e8aff30ddba1bdaf1973d1454; output-bytes=34936; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: the intruder check on a junction unit still holds
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=e270cba32d7d38c9a77c59b68d1f5ea852dbafa36f23210e3481b71a5036e4fb; output-bytes=8945; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G11: the docs explain the PCR 7 seal and the recovery procedure step by step, the boot-disk rule, the version floor, and say plainly what stays open (an old signed release with the disk in hand, the ESP writable by root) and why; CONTEXT.md records step 7
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G12: the team deck shows what step 7 closed and what stays open; the public deck stays plain
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=543c4564854574b087ccf7c76ef43c0e2a995f267ac3ab07345bc0adc8a090f0; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G13: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-21. Team deck published as version 21 (18 slides: new changes7 «Шаг 7: данные, диск, версия», status, disk, gaps and asks rewritten, every page number renumbered); the published deck.json, changes7, status, asks, disk, gaps and exit read back and their sha256 equal the sources in docs/deck/project (8822cdbe…, 8016040c…, 4dcf7700…, 14122af0…, ff88ecd0…, faca4a07…, 8615c278…); before publishing, version 19 was read back and was byte-identical to the committed step-6 sources, so no edit made in the app was overwritten. Public deck version 8 (13 slides) unchanged: its claims ('Данные под замком', 'обновляется сам … без выезда') still hold. All 31 slides rendered at 1920x1080 in the browser (preview of both decks) and measured: no flowing content past 920 px (lowest 913, public city; team asks 802, status 779, changes7 677), the changes7 command line on one line after widening it to 1664 px. Claims against the ledger and logs: «STATUS: 12/12 PASSED · шаг 7» = G1-G12 met above on the final code (gates-step7e/f/g logs: G6/G8/G9 re-run after a 5-hour laptop sleep froze the first attempt; G9 also passed alone in check-ota-direct.log); «выключили Secure Boot — данные закрыты», «ключ восстановления открывает их» = G4 (pcr7.log: 'Current policy digest does not match', DATA LOCKED, stars only, re-sealed); «флешка с теми же разделами не тронута» = G5; «старый блок обновление не примет» = G7 (install-check refusal, slot A unchanged); «чужой загрузчик не стартует» = G6; «599 тестов платформы» = G2 (PYTEST PASSED (599 tests)); changes7 [5] version floor = G1; the open items (hardware, Secure Boot keys in a real BIOS, an old signed release with the disk in hand, the stop screens not exercised in QEMU) match docs/edge-os.md «Что не проверено» and CONTEXT.md step 7.
