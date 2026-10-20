# Gates: the centre delivers updates to its units and districts by itself; both decks say what changed, each for its audience

OWNS: os/**, backend/urban/**, backend/tests/**, web/src/**, deploy/**, docs/**, GATES.md, CONTEXT.md, README.md, tools/**

Scope: step 3 of URBAN OS. Until now a new system version reached a junction unit only by copying the update package onto it over SSH. Now the control centre keeps the released packages and hands them out itself, over the link every unit and district already keeps to it (through the district's relay for units behind a district): the centre offers a release in a signed message to each device on an older version, the device downloads the package over the same TLS listener, checks its size and hash against the signed offer, and a root helper on the device installs it only if the package is signed by URBAN's build key and is newer than the running system; then the device reboots into it, the automatic rollback stays as it was, and the centre shows each device's system version and update state. A hijacked unit service or a lying centre can at most cause a genuine, newer URBAN release to be installed: no foreign package, no downgrade, no file outside the update inbox. Then both presentations: the team deck shows what was actually changed in steps 2 and 3, with the demonstration; the public deck says only what it gives the city and its people, in plain words. WireGuard, ARM, Secure Boot and the key inside the TPM stay out of this step and the docs keep saying so. The step-2 ledger is archived in docs/gates/2026-10-19-three-levels-step2.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=c860da6e7fac317a5df10fb23e504d1d98b223319157e44b2af497a3819983e4; output-bytes=598; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the centre keeps releases (added with their version, size and hash; listed; withdrawn), offers a release in a gateway-signed message only to a targeted unit or district whose reported system version is older, never to one on that version or newer nor after the release is withdrawn, serves the package only for a valid unexpired one-time link of that device, and shows each device's system version and update state in Devices
  CHECK: node tools/gates/pytest.mjs tests/test_release.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=02ecee0ef10731bc8e6aee5cb94e07d3d9bd271463f595d423605d411b53d0c6; exit=0; EXPECT=matched; output-sha256=e7926cf9f65150b6b3cdce826c0936fc5e4ddaeb75ac1b6bc47db5cce1832b5a; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: a unit's and a district's client report their system version, ignore an offer that is not newer than the running system, download an offered package, refuse it (and report why) when its size or hash differs from the signed offer, and hand a verified package to the root helper through the update inbox only
  CHECK: node tools/gates/pytest.mjs tests/test_update_client.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=295fd5d680cab97e7bb7d627f01148bc3ea276363d16795c110ae774abed9f9e; exit=0; EXPECT=matched; output-sha256=60479547467e1cff569f2700404496b5960c24348674f606754419ed6ab8b767; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=4bf6f78d7b2c2bb76184356c28cd70417764b0ed0d5ad2e1d21d702f985f54af; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: over the air in QEMU: with the centre, a district and a unit behind it, one release command on the centre brings both the district and the unit from version 1 to 2 by themselves (each boots slot B, passes its self-check, and the centre's Devices shows both on version 2), while the root helper on the unit refuses a package signed by another key, a package older than the running system claimed as newer, and a file name outside the inbox, each time leaving the running slot as it was
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=ddc13a38085a33db6611ed04c2cbd9167798ba963cc508a92bd3cf2fefa60440; output-bytes=25885; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: an intruder on a junction unit still gets nothing, and the hijacked unit service can now write only its key folder and the update inbox, and can neither install a package nor reboot the machine
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=a39952cea312c29627722d3c694ec5eb6784fb70a7d458be1ea426c00edcb281; output-bytes=2990; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: a signed update copied by hand still installs into the other slot and boots
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=f872caf084b2594c98731067703898c3654ec701686c6ba66eea9c29b20035b2; output-bytes=4020; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: a broken update still rolls back by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=edca5ede724519924ae52341785e9a077a8ff4257b009dc6e9e84e0b84b1a4ab; output-bytes=6693; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the chain unit -> district -> centre still works as in step 2 (kernel relay, district as a device, reports through an outage)
  CHECK: node os/tests/check-chain.mjs
  EXPECT: CHAIN CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f61d67b26b99dc7e18faa0aa4311a190ab3a0f0bdba4cd3f4cad6ecd2957659d; exit=0; EXPECT=matched; output-sha256=20d8160fff09d368f5b43ff8ebf96d18e33d54eeb833e608d9d219347808bbc9; output-bytes=10095; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: the docs describe delivery of updates from the centre (the release command, what the device checks, what an intruder can and cannot do with it) and no longer list it as missing, and CONTEXT.md records this step's decisions
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: the public deck speaks plainly (none of the technical words the team deck uses, such as TPM, QEMU, RAUC, SSH, root, ядро) and says that the system updates itself without a trip to the junction; the team deck has slides naming what changed in steps 2 and 3, updates from the centre among them
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=6889725d71d04ab49837794fdd2e77a1af449c59006a5a50eb8598cae36f0875; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G11: both decks (team https://claude.ai/artifact/Qd3ZnqpQXXw3Rrdh7RWnGt, public https://claude.ai/artifact/4puh3qy188fsVhSZnoaXjX) are right on every slide: rendered at 1920×1080 with no text past the 920 px line, clipped or broken mid-word, every number and claim matching the repository's logs and docs, and the "not verified" lists matching this ledger's final state
  EVIDENCE: manual, 2026-10-19. Team deck version 12 (16 slides, sources docs/deck/project), public deck version 4 (12 slides, docs/deck-public/project). All 28 slides rendered at 1920x1080 and measured in the browser: no text past 920 px (lowest flow bottoms 901 px demo, 887 px screen), no overflowing or clipped element; the new slides (changes, changes3, status, new) also looked at by eye. Facts checked against the logs: OTA = data/os/logs/ota-unit.log and ota-district.log (URBAN-UPDATE installed version=2 on both; six refusals on the unit: signature, version mismatch, not newer, hash, two bad requests), 293 tests = PYTEST PASSED (293 tests), speed figures = docs/edge-os.md; the status slide claims 11/11 only once G5 and G7 are met; «not verified» lists = docs/edge-os.md «Что не проверено».
