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

- [ ] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK

- [ ] G1: the release store and the hub keep releases by architecture and roll them out in waves: the compatible is read from the signed package (a package whose compatible is neither of ours is refused); a device is offered only the newest release for its own compatible and only if it is in the current wave; a device that reports no compatible gets x86 releases only; promotion widens the wave; a failure or rollback reported by any device of the wave halts the rollout (no new offers, audited, shown in Devices with the device and the reason), a halted release can be resumed or withdrawn by the admin; the version floor and earlier release rules still hold
  CHECK: node tools/gates/pytest.mjs tests/test_release.py tests/test_rollout.py
  EXPECT: PYTEST PASSED

- [ ] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED

- [ ] G3: the images report their compatible in their health and the earlier image checks hold (x86 and arm64)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED

- [ ] G4: in QEMU, one centre with an x86 unit and an arm64 unit: the centre holds release 2 for x86 and release 2 for arm64; each unit is offered and installs only its own platform's package and boots it; then release 3 (a broken one) goes out in a first wave of the x86 unit only: it rolls back, the rollout halts by itself, the arm64 unit's release 3 is never offered while halted, Devices shows the halt with the device and the reason
  CHECK: node os/tests/check-rollout.mjs
  EXPECT: ROLLOUT CHECK PASSED

- [ ] G5: units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED

- [ ] G6: the docs describe releases by architecture and the waves (commands, what halts a rollout, how to resume or withdraw), and CONTEXT.md records step 12
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED

- [ ] G7: the team deck shows what step 12 changed; the public deck says plainly that a bad update stops itself before it reaches the whole city
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED

- [ ] G8: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: pending
