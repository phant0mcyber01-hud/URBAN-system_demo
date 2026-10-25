# Gates: releases by architecture and staged rollout - each unit gets only its own platform's release, a bad release stops itself

OWNS: os/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 12 makes the centre's update channel fit a fleet of two platforms and safe for a city. (1) Releases by
architecture: a release package says which systems it is for (RAUC compatible: urban-edge-x86_64 or urban-edge-aarch64);
`urban-center release add` reads it from the signed package itself; every unit and district reports its own compatible in
its health; the centre offers a device only releases for its own compatible, so x86 and ARM releases live side by side
under their own version numbers and no device is offered a package it would refuse. (2) Staged rollout: a release goes out
in waves - a first wave (named devices or a share of the fleet), then the rest only when the admin promotes it; if any
device of the wave reports the update failed or rolled back, the rollout halts by itself (no new device is offered the
release), the admin sees which device and why, and the devices that already updated are left as they are. Devices that
never reported a compatible (systems before step 12) keep today's behaviour: they are offered x86 releases only. The
centre's own self-update stays manual (a later step). The step-11 ledger is archived in docs/gates/2026-10-24-arm-step11.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=e52fa8a9c53ccd96c03688e0959c79479af5ed6ef0dc7e4957f0cb4888eec7b7; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the release store and the hub keep releases by architecture and roll them out in waves: the compatible is read from the signed package (a package whose compatible is neither of ours is refused); a device is offered only the newest release for its own compatible and only if it is in the current wave; a device that reports no compatible gets x86 releases only; promotion widens the wave; a failure or rollback reported by any device of the wave halts the rollout (no new offers, audited, shown in Devices with the device and the reason), a halted release can be resumed or withdrawn by the admin; the version floor and earlier release rules still hold
  CHECK: node tools/gates/pytest.mjs tests/test_release.py tests/test_rollout.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9d74695b8f7fe34747cfdb0ac1224665c73b62ba7ab1bcb8184af6d259d9b446; exit=0; EXPECT=matched; output-sha256=f9c306a379b94d3f25700812eca81481593cbd9794179d0fe37e246abb900e1e; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=d3bd131fa49243051090d9fe1e1424a53fab0cdda39bf532b99609a336a126d1; output-bytes=57; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the images report their compatible in their health and the earlier image checks hold (x86 and arm64)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=cbc39ecf3eba08f0ecc9b955bf08d39ca8c4545c6ad83aaba1e06e77f547a438; output-bytes=2080; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: in QEMU, one centre with an x86 unit and an arm64 unit: the centre holds release 2 for x86 and release 2 for arm64; each unit is offered and installs only its own platform's package and boots it; then release 3 (a broken one) goes out in a first wave of the x86 unit only: it rolls back, the rollout halts by itself, the arm64 unit's release 3 is never offered while halted, Devices shows the halt with the device and the reason
  CHECK: node os/tests/check-rollout.mjs
  EXPECT: ROLLOUT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=e7ec984039a6c2a563feb61bf535908e5bc87bd190f94a0909d46467a9b12af4; exit=0; EXPECT=matched; output-sha256=39518ee9a3bc2cb3315aa8f51208f2977c91040d84405f80819fa0f86efde753; output-bytes=42396; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=e1f55fd051c34ae30c1e1433ad8c08026a1ecd6676df232694fed5b0c3474769; output-bytes=43585; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the docs describe releases by architecture and the waves (commands, what halts a rollout, how to resume or withdraw), and CONTEXT.md records step 12
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the team deck shows what step 12 changed; the public deck says plainly that a bad update stops itself before it reaches the whole city
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=5abbab01b68a54f3f63c046c307cf8ab8c0cb0d5b71db6826281eb442c76da2a; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: 2026-10-24, both decks from docs/deck/project (22 slides) and docs/deck-public/project (13) rendered at 1920×1080 in a browser and measured slide by slide: all 35 keep their flow inside 920 px from the top and 1792 px from the left, no pinned top or bottom line wraps, nothing scrolls; status, changes12, the public new.html and cover were also captured with headless Edge at 1920×1080 and looked at. Claims checked against the repository and logs: status "8/8 PASSED · шаг 12" and its six checked lines are the G4 run's own OK lines (os/tests/vm_rollout.py: each unit offered only its own package, release 3 rolled back with "firewall not loaded", rollout-halted by edge-J-01, edge-J-02 not offered while halted after promote --all, offered at once after resume, withdrawn) plus G5 (check-ota); changes12's commands match backend/urban/release.py's CLI (add --devices/--wave, promote --wave P | --all, resume, withdraw); its open line matches docs/edge-os.md «Пределы выпусков и волн». The public new.html's sentence (a few junctions first, they return to the old version by themselves, the rollout stops itself) matches the halt on rollback shown in G4 and the docs' advice to start with a small wave. Before publishing, the live artifacts were read: the team deck's files equal git HEAD; the public cover.html had been re-laid out in the app by the owner (same text, the picture moved), and that version was taken into the repository so the publish keeps it.
