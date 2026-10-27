# Gates: step 15 - the water module: a pump station's unit reads its automation and the centre sees it, and nothing can write to a pump

OWNS: os/**, backend/**, web/**, deploy/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: the third City OS module, `water` (docs/water-module.md): a unit `pump-<site>` in a pump station's cabinet
reads the station's automation over Modbus TCP or RTU through a register map (pressures, flow, power, energy counter,
level, pumps running and failed, door), works out its specific energy (kWh per m3) and last night's minimum flow
against its 7-night median, and reports every 5 s. The centre accepts the report only for the unit's own site, field
by field, raises alerts (no link, no link to the automation, pressure outside the centre's thresholds, pump fault,
door, possible leak), audits each alert's start and end once, and shows the stations in their own tab and on the map.
Read-only by construction: the unit's Modbus client sends only functions 01-04, the station refuses every command,
the module has no POST route and the interface no control. On URBAN OS the station is MODULE=water, its own hardened
service urban-water@, no tunnel, no cameras. The review ledger of steps 12-14 is archived in
docs/gates/2026-10-25-review-steps12-14.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the water module's own tests pass: a report about another site or with any field out of range is dropped whole, alerts start and end with one audit line each, the leak and specific-energy figures come out right, a register map that is wrong stops the start with a clear error, the read-only client refuses any function but 01-04 before sending a byte and a fake PLC sees only 01-04, the station refuses a signed command and reports why, the module has no route but GET, enrolment by CLI and by code makes a water record with its site and thresholds
  CHECK: node tools/gates/pytest.mjs tests/test_water.py tests/test_water_device.py tests/test_modules.py tests/test_lighting.py tests/test_enrolment.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=0372bd0aab9e5b5e26c69abbcd337210b260c7af3b91b71a52e89d7aa9467308; exit=0; EXPECT=matched; output-sha256=52b6fea3e2e859ce9f7a907e915924f30cb6b08d928ca84990706b7fc52e47f8; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=f79239bba81073bd0017bd298d4a6d03cc94c182b4efda37e8a5b85bfa8a327d; output-bytes=57; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the interface builds and its water tab says what it must: stations, readings, alerts, no control
  CHECK: node tools/gates/web-modules.mjs
  EXPECT: WEB MODULES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=87fc3849028ece93eedfc6ec1b750406ac0048588116bbf84a455ffbca968a3a; exit=0; EXPECT=matched; output-sha256=0497566382a9ec999c7e4ab73756317b91dedb8c5aca05bd603f03f23ddffffd; output-bytes=25; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: a pump station on URBAN OS in QEMU (MODULE=water) enrolled at a centre: the centre shows the readings of a fake PLC, a pump fault set in the PLC becomes an alert and its clearing clears it, a PLC that stops answering becomes a source alert while the station stays online, the PLC saw no function but 01-04 for the whole run, the station's service is hardened and no junction or vision service runs
  CHECK: node os/tests/check-water.mjs
  EXPECT: WATER CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=cc9281eba58f14022470f15a834118d9bd83fff7e8ff1bb90f6202268ac5072b; exit=0; EXPECT=matched; output-sha256=1c7ec8978380a417d77816f4fd0742a2596c2201ba6300d517fb284963ec33ef; output-bytes=12135; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the street-lighting cabinet still works in QEMU (the enrolment and boot code it shares with the water station changed)
  CHECK: node os/tests/check-lighting.mjs
  EXPECT: LIGHTING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d6a8f8d60e9fad19c73e794c91926382ba13eabf27a9a86d93968a6a2ddaa705; exit=0; EXPECT=matched; output-sha256=2a1985c05ed50c0416e664ab03185a6175f2df7c289b7cf257e404cd71be66b4; output-bytes=12399; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: unit, district, centre and the pilot machine still boot and run their services
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d9661fad1e0797a91d47afbe4bb4b62f9874a1b396749c208280915de8f7c6fd; exit=0; EXPECT=matched; output-sha256=0eff188cf812d372ac03cf3d48b6722a92aeeb9cec2df3cb70c9eafbb68c3810; output-bytes=4972; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the docs describe the water module (read-only, MODULE=water, enroll-water, urban-water@) and CONTEXT.md records step 15
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: both decks still pass their checks
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=e698f452c84e5a0eacbf351491debd97825899244d83d15998ebd4ea053c9e4b; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries
