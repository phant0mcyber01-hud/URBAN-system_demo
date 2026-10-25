# Gates: the pilot machine's own cameras reach the centre - the district on the centre's machine becomes a real district

OWNS: os/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 14 closes "the pilot machine's cameras give the centre nothing". On the pilot (ROLE=district,center: the
district server and the control centre on one machine) the vision of the machine's own cameras measures queues, but
nothing carries them to the platform: the district reporter needs a district identity (a key, an enrolment as
district-...), and the pilot has none. Now the pilot machine gives its district side an identity of its own at first
boot - its own key, enrolled at its own centre as an ordinary district (shown in Devices, audited like any enrolment, no
code typed by anyone) - and its district reporter connects to its own centre over the machine's loopback, so the
queues of its cameras reach the centre exactly as a separate district's would: only for the approaches of junctions
the admin routed through this district. Nothing changes for a separate district, a unit or a plain centre. The step-13
ledger is archived in docs/gates/2026-10-24-center-update-step13.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=a258b8ffa936769b76c81579eda0427ac6de2e4274fd52bf5bf393e3677afe0f; output-bytes=340; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the pilot's own district is an ordinary district: it is enrolled once with the machine's own key (the same identity after a restart, never a second one), shows in Devices and the audit like any district, its reports are accepted only over its signed channel, and the centre takes from them only the queues of junctions routed through it; a separate district, a unit and a centre without the district role are unchanged
  CHECK: node tools/gates/pytest.mjs tests/test_pilot_district.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=bfbb3c39cd49c506bd035101b2d6ed1b1425f8a4cb89c7f53598c4c63f345b4f; exit=0; EXPECT=matched; output-sha256=2daa08e2db6e801e340075b6aea04520e8cb5a10cb1c64768cfd91b37eaebfeb; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=12101a31d2447f2401a761f616afe16af5a3bca28a7ad61c6430726f8e3ad9b9; output-bytes=57; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: in QEMU, a pilot machine (district,center) with one camera of real video (data/videos) on an approach of a junction routed through its district: with no enrolment typed by anyone, its district is enrolled at its own centre, connects over loopback, and the centre shows that approach's queue measured by the machine's own vision; after a restart it is the same district, not a second one; the queue of a junction not routed through it is not taken (control)
  CHECK: node os/tests/check-pilot-cameras.mjs
  EXPECT: PILOT CAMERAS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=4ca786bd9e62098753aa1a78fb762685467c176a440c598864ec0ad4aeb4a22c; exit=0; EXPECT=matched; output-sha256=12933b8c25de9e95fc94d09f385b2da3578cf22febcd3cc884460a488dbff845; output-bytes=10390; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the earlier role checks hold: unit, district, centre and the pilot machine boot and run their services
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d9661fad1e0797a91d47afbe4bb4b62f9874a1b396749c208280915de8f7c6fd; exit=0; EXPECT=matched; output-sha256=70418b123679c43d509804dfdbec955f69a3c162ab1e171f4689f908a4a700f1; output-bytes=4311; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the docs describe how the pilot machine's cameras reach the centre (its own district, what is automatic, what the admin still does: routing junctions and naming the cameras' approaches) and CONTEXT.md records step 14
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the team deck shows what step 14 changed; the public deck says plainly that on the pilot one machine both sees the queues and runs the centre
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=e698f452c84e5a0eacbf351491debd97825899244d83d15998ebd4ea053c9e4b; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: 2026-10-25, docs/deck/project (24 slides) and docs/deck-public/project (13) rendered at 1920×1080 in a browser and measured slide by slide: all 37 keep their flow within 920 px from the top and 1792 px from the left, no pinned line wraps, nothing scrolls; changes14, status and the public pilot.html captured with headless Edge at 1920×1080 and looked at. Claims checked: status "7/7 PASSED · шаг 14" is G0-G6 met in data/os/logs/gates-step14*.log; its checked lines are os/tests/vm_pilot.py's steps that G3 passed (district-center enrolled by the machine itself, key in the TPM, reporter over 127.0.0.1:8443, the J-01-SW queue taken at the centre after routing, J-02 not routed gets nothing, same district after a restart) plus G4 and G2; its open lines are the honest limits in docs/edge-os.md «Камеры пилотной машины (шаг 14)» (0.0 m under emulation, a junction needs a unit to be routed). changes14's command and words match backend/urban/node/pilot.py, provision.py, urban-prepare. The public sentence «На пилоте одна машина и считает очереди по своим камерам, и работает центром управления» matches G3.
