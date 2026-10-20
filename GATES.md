# Gates: the device key lives inside the TPM; the lighting cabinet faces the intruder check too

OWNS: backend/**, os/**, deploy/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 5. Until now a device's identity key was a file (Ed25519) on its encrypted data partition: root on a
running unit could copy it and impersonate that unit from anywhere until it was revoked (the red row of the intruder
table). Now a machine with a TPM 2.0 makes its key inside the TPM (ECDSA P-256; the TPM does not do Ed25519): the
private key never leaves the chip, what lies on disk is a TPM-wrapped blob that no other machine can use, and root can
sign as the unit only while sitting on that very unit. A machine without a TPM keeps the Ed25519 file. Both kinds
enrol with the same code (the public key's length tells them apart: 64 hex Ed25519, 130 hex P-256), the centre
verifies both, and every client (unit, district, lighting cabinet) signs through one signer. The lighting cabinet
also goes through the intruder check. The step-4 ledger is archived in docs/gates/2026-10-19-modules-step4.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=a21b784011640ac96051f109e2e6abec5f3959eb4c5f11937466f6dd49514c42; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: device keys of both kinds: Ed25519 files and TPM P-256 keys sign and verify, the centre authenticates both and refuses a wrong-kind or forged signature, enrolment codes and kits carry either, every client picks the TPM signer when the unit has one, the TPM signer drives tpm2-tools as specified (checked against a fake runner), and keygen with --tpm auto/require/off behaves as documented
  CHECK: node tools/gates/pytest.mjs tests/test_device_keys.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=6b8d99f8455153476ec3126cd87d13fec7d14bf724f12ceaefa2f05f1af998ee; exit=0; EXPECT=matched; output-sha256=3f3c8bde2ddf58dba37f9d079abd1d93468bb551d53ab150c33972263be51dce; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=7fcb2aefa8dfbf24ffeafd9df8c22d5e3b4ffc134505206cc053c5dd1b5ae2d1; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: in QEMU on a junction unit with a TPM the key is in the TPM: root finds no private key on the machine, the key blobs copied to another machine cannot sign there while the unit itself still signs (the control), and every earlier intruder probe still holds
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=10019d80f0d28ae2723ebebac7922afcecc71154c27373f8974e2c51fa807815; output-bytes=6327; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: in QEMU the intruder check also holds on a lighting cabinet: its service jail (no other files, writes only its key folder, the inbox and its status folder, cannot install or reboot), its key in the TPM, nothing listening but SSH
  CHECK: node os/tests/check-hardened.mjs lighting
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9e46cb776e9f2e7bed57f6d35a2dbdf7b03f9ec775904b675a95c140d2a5b96d; exit=0; EXPECT=matched; output-sha256=c1271bfb2457bba1d3e816cc14115b1df8a4ad8d03e9756ffa2a32888e7914b5; output-bytes=6166; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: units and districts with TPM keys enrol by code, come online through the district and update from the centre (the chain and updates end to end)
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=1973efa285f27801e32ad84c20f48b2c758f8a66ed629295ded81a886f4faccf; output-bytes=31079; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: a lighting cabinet with a TPM key enrols, obeys and falls back as in step 4
  CHECK: node os/tests/check-lighting.mjs
  EXPECT: LIGHTING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d6a8f8d60e9fad19c73e794c91926382ba13eabf27a9a86d93968a6a2ddaa705; exit=0; EXPECT=matched; output-sha256=682dc0f4ec31811c98e225cbbdd79a4b65fc2d76550c77b4174f6b5e1a9b6c3a; output-bytes=13323; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the docs say where the device key lives and what root on a unit can and cannot do now, and CONTEXT.md records step 5
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the team deck's intruder table no longer says root takes the unit's key away, and says the key is in the chip; the public deck stays plain
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=54cc1ca2c17d6d4b70e272f35a161400f6db53ab2b514f881c79da75c5c469b7; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-20. Team deck version 17 (17 slides), public deck version 8 (13 slides, unchanged in step 5). All 30 slides rendered at 1920x1080 and measured in the browser: no text past 920 px, nothing clipped or overflowing; the changed slide (intruder) ends at 747 px. Claim checked: the row «root на блоке — подпись только здесь, ключ в чипе TPM, его не вынести» = G3 passed in QEMU (data/os/logs/gates-step5-b.log: copied key blobs cannot sign on a second machine, the unit itself signs); the public deck has no technical words (G8).
