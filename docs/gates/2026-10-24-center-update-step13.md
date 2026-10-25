# Gates: the centre updates itself - one admin command, the same signed path, a bad version rolls back with the data kept

OWNS: os/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 13 closes "the centre is updated by hand". Until now `urban-unit update BUNDLE` on the centre was the only
way. Now the admin runs one command on the centre, `urban-center release self-update N`: the centre installs release N
of its own platform from its own release store through the same root update helper as every unit (URBAN's signature,
newer than the running version, the compatible of this machine), reboots into the other slot, and the boot after that
says how it went: on the new version, or back on the old one by itself (the existing self-check and A/B fallback) - both
audited and shown in Devices. The centre refuses a release that is halted or withdrawn, not newer, or for another
platform; the admin must name the version (no automatic self-update). The centre's data partition (its database,
enrolments, keys, audit) is the same before and after, so the devices come back by themselves. The step-12 ledger is
archived in docs/gates/2026-10-24-rollout-step12.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=a258b8ffa936769b76c81579eda0427ac6de2e4274fd52bf5bf393e3677afe0f; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: self-update refuses what it must and records what happened: a halted, withdrawn, not newer or other-platform release is refused with the reason and nothing is staged; an accepted one is handed to the root update helper (not installed by the platform itself) and audited as centre-update-started; on the next start the platform compares the running version with the one it started and audits centre-updated or centre-rolled-back (with the version it is back on), once; Devices shows the centre's own system and its last self-update; the release, wave and floor rules of steps 3-12 still hold
  CHECK: node tools/gates/pytest.mjs tests/test_self_update.py tests/test_release.py tests/test_rollout.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f6693722a410dd412e33233eb0ca9461a8aa3d6dd41369ad3186dc0118b4711c; exit=0; EXPECT=matched; output-sha256=7d85ad7a04315acce321a214c8d6dfa139579b23d507c68dbe54f01ef6c5cdfc; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=636ab21a50eb9bba2bd64aafc867570c4f600746d1948a45ac20417a10178979; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: in QEMU, a centre with one unit enrolled: release 2 added, `urban-center release self-update 2` on the centre installs it by the root helper, the centre reboots into version 2 in the other slot, its database, the unit's enrolment and the audit are the same as before, the unit comes back online by itself, and the audit says centre-updated; then a broken release 3 by the same command: the centre fails its self-check, comes back on version 2 by itself with the same data, the audit says centre-rolled-back; a halted release is refused by the command
  CHECK: node os/tests/check-center-update.mjs
  EXPECT: CENTER UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=ac9b39a2648f14d9834468d8d28bf24e96e590c76f8e01983ebfd24ab97efa9d; exit=0; EXPECT=matched; output-sha256=597ca2d427840f1ece56e44d4234c9db7feb38cf01e590df7e3e9ad31dd5baf3; output-bytes=32173; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=44df3951c6c2c06069dc64c0074e589a68a0887fab11f745b9790b5edd3aad5f; output-bytes=40829; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the docs describe the centre's self-update (the command, what it refuses, what happens on a bad version, what is not covered) and CONTEXT.md records step 13
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the team deck shows what step 13 changed; the public deck says plainly that the control centre is updated the same safe way as the junctions
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=d9dbcd6e25101a784ece7baac8f1966fca41bfae9caef6635b0f880a1c4a1ee7; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: 2026-10-24, docs/deck/project (23 slides) and docs/deck-public/project (13) rendered at 1920×1080 in a browser and measured slide by slide: all 36 keep their flow within 920 px from the top and 1792 px from the left, no pinned top or bottom line wraps, nothing scrolls; changes13, status and the public new.html captured with headless Edge at 1920×1080 and looked at. Claims checked: status "7/7 PASSED · шаг 13" is the --reverify run of G0-G6 on the final code (data/os/logs/gates-step13c.log, after the review fixes); its six checked lines are the steps of os/tests/vm_center_update.py that G3 passed (self-update 2 into slot B, devices/audit/platform files unchanged and the unit back online, broken 3 back on 2 with centre-rolled-back, release 3 halted after the rollback, the halted release refused) and G4; its open lines and changes13's «Открыто» match docs/edge-os.md «Самообновление центра (шаг 13)»; changes13's command and words (refused: halted/withdrawn/not newer; the root helper; centre-rolled-back) match backend/urban/release.py and self_update.py. The public sentence «Так же и центр: при сбое сам вернёт прежнюю, ничего не потеряв» matches G3 (a version that fails its self-check, back on the old one with the same data). Live artifacts read before publishing: both are the step-12 versions published from this repository, no in-app edits.
