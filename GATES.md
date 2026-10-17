# Gates: one URBAN OS for the junction unit, the district server and the control centre, each sizing its own power

OWNS: os/**, backend/urban/node/**, backend/urban/vision/**, backend/urban/__main__.py, backend/urban/config.py, backend/tests/**, deploy/**, docs/**, GATES.md, CONTEXT.md, README.md, tools/**

Scope: the three-level architecture agreed with the owner - a light unit at each junction, a district server that relays signed commands and takes the camera work, the most powerful machine in the office as the control centre; commands are signed only by the centre; for the pilot the district server and the centre are one machine. This step makes the OS universal: one image, the role (unit, district, center, or district,center) chosen on its ESP, the role's services started at boot, the data partition grown to the whole disk, and a power manager that keeps a unit from overheating (fewer frames, lower clock, never touching the control path) and lets the district server and the centre size their vision work and clock to their load. The district's relay and its reports to the centre are the next step.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the power manager decides per role from temperature, CPU load and camera load: a unit lowers frames and clock as it heats and pauses vision before it overheats, servers keep full work longer and size vision threads to their cores; levels come down only with hysteresis; missing sensors and missing cpufreq files are handled; the CPU settings it writes are the ones it decided
  CHECK: node tools/gates/pytest.mjs tests/test_power.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=011ae077dd785f47b2a4576d0228fccd5ba0fc090a4d6f1e068b57ce611e964c; exit=0; EXPECT=matched; output-sha256=c4591023f99594c4ec3a28821b45d9a5091ec3af42babdaca8827f345a9c600f; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: camera vision follows the power plan: it analyses no more frames per camera than the plan allows, none while paused, and takes its thread count from the plan
  CHECK: node tools/gates/pytest.mjs tests/test_vision_power.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=2d6108d56a5b6b920f287058b5d5d90ed38401bbb200e1015161795675803d5f; exit=0; EXPECT=matched; output-sha256=2be7820ec0683e44256b7dce9db270418d0acccde75f2bbbdd44a54e2396f268; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the whole test suite passes
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=0e1b6c808a6e4c1c9219efdbfcbb6ac84f0dc2a20ce92beda5acd324f2b7c0a4; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: one image carries all three roles: the power manager, the unit's and the vision services, the control centre's service and its built interface, and the role-dependent firewall
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=3481f670e7abe896ee68a0c08757f15f63061bc13a4bc87d6546f11dd4f47d69; output-bytes=261; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: booted as the pilot's district server and centre on a larger disk, the image grows its data partition to the disk, serves the control centre over HTTPS to the outside, opens only the interface and device ports, writes a power plan for its role, and still passes its self-check
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d9661fad1e0797a91d47afbe4bb4b62f9874a1b396749c208280915de8f7c6fd; exit=0; EXPECT=matched; output-sha256=d42ca60260bbfe1c33821164ea0aac4e484dee41aee52d27bad82555d3a938dd; output-bytes=2953; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: as a junction unit the image still takes its name and maintenance key from the ESP, sets up its kit and keeps its service running across a reboot, now with a unit power plan
  CHECK: node os/tests/check-unit-image.mjs
  EXPECT: UNIT IMAGE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d80e94bbbb7bec968ec8398b2fb386cab0493440208906891c5837967d1442ab; exit=0; EXPECT=matched; output-sha256=a25fd0599eb224c1c4418c585b9ffb67d88fc78ebd2ba147a697b24e83eb37c9; output-bytes=4993; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the first-boot behaviour is unchanged: no key without a name, one key kept across reboots
  CHECK: node os/tests/check-firstboot.mjs
  EXPECT: FIRSTBOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d884b261b8b2b557f51e80f0801a933c0f4fa0f8a5a29082d34b17526458f617; exit=0; EXPECT=matched; output-sha256=64d51604e75e7f4df26f6f8fc8fdfe74cf1ca835f4596b7b8fb4b60ded8dae7f; output-bytes=4581; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: a signed update still installs into the other slot and boots
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=1389cf1df88af7bd0622c46cdc5085ee754f9cff2f019cb82514798e10110804; output-bytes=4273; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: a broken update still rolls back to the working system by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=f2abc00db71853cfeb6c2fe9de30212fed11f7dd2d1df821ba361be15650b0df; output-bytes=6251; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: the image still boots from NVMe, SATA and a USB stick with an Intel network card
  CHECK: node os/tests/check-hardware.mjs
  EXPECT: HARDWARE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=cf4b5ebd31b9d786d1b5cd75acdc1212ce6a230332cc265e862108453979b75a; exit=0; EXPECT=matched; output-sha256=21a84b3dc82a68a419b311a1e109a188f50a85dbd7ad342c78e822c794177cde; output-bytes=5155; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G11: the docs describe the three levels, the roles, how power is sized and what is not built yet, and CONTEXT.md records the architecture and the owner's decisions with their reasons
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries
