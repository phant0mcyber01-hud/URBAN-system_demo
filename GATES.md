# Gates: review of steps 12-14 - the code and security review's fixes hold, and every check they touch passes again

OWNS: os/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: a code review and a security review of steps 12-14 (releases by platform and waves, the centre's self-update,
the pilot's own district) found and fixed: (1) releases.json was read, changed and written by the command line and the
platform with no lock, so a halt written while the admin promoted a release could be lost and the broken release
offered to the whole city - every change now runs under a file lock and reads the file afresh; (2) a rollback on a
device outside the release's wave halted it - now only the wave counts, as for a failure and as the docs say; (3) the
pilot's own-district marker was written in place - now whole or not at all; (4) root put the pilot's district kit in
place through any symlink planted in the folders the district service may write, and took any text after a
certificate's first line as its CA - kit files are now new files moved over the name (env files 0600 from the start),
the CA must parse as an X.509 certificate, and urban-prepare never changes the owner or mode of a key through a link. And
(5), found by the pilot check after a restart: camera vision timed its rhythm (frames per camera, the queue file, its
stats, a file's playing pace) by the wall clock, so a step of the clock backwards (NTP correcting a clock that ran ahead;
in QEMU the restart) stopped the analysis of every camera for as long as the step - now that rhythm runs on the
monotonic clock and the samples keep their wall time.
The image changed (urban-prepare, the platform), so every QEMU check of steps 12-14 and the ones whose test harness
changed in step 14 (chain, OTA) run again. The step-14 ledger is archived in
docs/gates/2026-10-25-pilot-cameras-step14.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the fixes have tests that fail without them: a halt written while the admin promotes is kept, a rollback outside the wave halts nothing, a wall clock stepping back does not hold a camera back, a kit whose certificate is not a certificate is refused and kit files are replaced whole, the self-update and pilot-district rules still hold
  CHECK: node tools/gates/pytest.mjs tests/test_rollout.py tests/test_enrolment.py tests/test_self_update.py tests/test_pilot_district.py tests/test_release.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=b71f546fe4c6f1fee3c9bca15d1119d736af0dc886717900f9cea970a28bb33a; exit=0; EXPECT=matched; output-sha256=f9436e143b2e390e46d75cffbff4a568e5c89b376ec3f9d737e67c37cece69c4; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=82496a38f650a75a31bcb6712029ad8a30ac640034f17e43c08a0cd338378779; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: releases by platform and the self-halting rollout still work in QEMU (an x86 and an arm64 unit, a broken release halts by itself)
  CHECK: node os/tests/check-rollout.mjs
  EXPECT: ROLLOUT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=e7ec984039a6c2a563feb61bf535908e5bc87bd190f94a0909d46467a9b12af4; exit=0; EXPECT=matched; output-sha256=ac286932251b34d5bbac411c4f9fce29ae7323aa72f666d7a0c322adb8739cb9; output-bytes=50777; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the centre's self-update still works in QEMU (version 2 with the same data, a broken 3 rolled back, a halted release refused)
  CHECK: node os/tests/check-center-update.mjs
  EXPECT: CENTER UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=ac9b39a2648f14d9834468d8d28bf24e96e590c76f8e01983ebfd24ab97efa9d; exit=0; EXPECT=matched; output-sha256=15c85815038c1a3b1f442fd79aed27fc574817e6f6f4eadad897f72049ab1328; output-bytes=29411; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the pilot machine's own cameras still reach its centre in QEMU (its own district, enrolled with no code, the same after a restart)
  CHECK: node os/tests/check-pilot-cameras.mjs
  EXPECT: PILOT CAMERAS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=4ca786bd9e62098753aa1a78fb762685467c176a440c598864ec0ad4aeb4a22c; exit=0; EXPECT=matched; output-sha256=98175fa38cf61b09ecc1983498cd41192a33cc6c2fca69dc031c290f2fde2bb8; output-bytes=9718; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: units and districts still enrol, come online through the district and update from the centre (the pilot centre's own district now a third device)
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=219361a468d5d895040382f98acc04d69b7848761254df98ba74dac5da935989; output-bytes=38090; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the unit -> district -> centre chain still works (the pilot centre's own district now a third device)
  CHECK: node os/tests/check-chain.mjs
  EXPECT: CHAIN CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f61d67b26b99dc7e18faa0aa4311a190ab3a0f0bdba4cd3f4cad6ecd2957659d; exit=0; EXPECT=matched; output-sha256=a91e6d8af905139a7b476615a6ccd1d081c6ac44285f57810691d94825ed8010; output-bytes=19221; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: unit, district, centre and the pilot machine still boot and run their services
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d9661fad1e0797a91d47afbe4bb4b62f9874a1b396749c208280915de8f7c6fd; exit=0; EXPECT=matched; output-sha256=2cb5f2c5b7f8fbf489ed6a122b2d745cd7a855f4714f1cb92bf166fa2f916fd1; output-bytes=4793; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: the docs say what the review changed (the lock on releases.json, only the wave counts) and CONTEXT.md records the review
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: both decks still pass their checks
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=e698f452c84e5a0eacbf351491debd97825899244d83d15998ebd4ea053c9e4b; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries
