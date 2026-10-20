# Gates: City OS core and modules; street lighting as the second module

OWNS: backend/**, web/src/**, os/**, deploy/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 4. URBAN is a City OS (the owner's wording): one core and modules for city services. The core gets a
module interface (backend/urban/modules/base.py) and a module registry (MODULES, GET /api/modules); traffic is
registered as the first module without rewriting it; street lighting (lighting) is the second, built on the core
only: lighting cabinets enrol like units, report their state, and obey signed, time-limited commands from operators
(set a level for 1-720 minutes), returning to their own astronomical schedule when a lease runs out or the link is
lost. The design is docs/city-os-modules.md. Utilities and emergency modules stay plans. The step-3 ledger is
archived in docs/gates/2026-10-19-ota-step3.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=c05ea7298e62ec2d13e7423382e035436184366558f6c59966d4c946ec5c9952; output-bytes=404; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the core loads the modules named in MODULES (traffic and lighting by default, an unknown name refused), GET /api/modules lists them with their summaries, a device report reaches only the module of its device kind, and lighting cabinets enrol, get a kit and are listed like units (provision enroll-lighting)
  CHECK: node tools/gates/pytest.mjs tests/test_modules.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=369f0027522087b788e4e43030842017670f62ccdb53ffbb9923141ca561789f; exit=0; EXPECT=matched; output-sha256=a9df4afcfcbb8557d727b8cbb5446f2ae7d003be1e8336b16e4b2a471112dc6b; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the lighting module at the centre: an operator's override (level, 1-720 minutes, reason) is sent signed to the cabinet and renewed while wanted, then released and audited; a viewer cannot set one; bad input is refused; alerts for failed lamps, an open door, a cabinet offline and a refused command; a cabinet's report is accepted only for its own cabinet and only with sane values
  CHECK: node tools/gates/pytest.mjs tests/test_lighting.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=07857d9a56c82f4c2e7ccd58c60b129e8f4c9e79b926853dbba8e545141b8f72; exit=0; EXPECT=matched; output-sha256=4a26941edd903e513f3c81e73e5465565603031849052178eb11aa894e78491e; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the lighting cabinet's program obeys only a verified command for its own cabinet with a sane level and lease, refuses forged, replayed, stale and foreign ones and says why, returns to its astronomical schedule when the lease runs out or the centre goes silent, and computes sunset and sunrise for Fergana correctly within a few minutes
  CHECK: node tools/gates/pytest.mjs tests/test_lighting_device.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=b74761dcd5864fd583008530ad19e7d0901bec95a6eafa9fdf2af99de22eae6d; exit=0; EXPECT=matched; output-sha256=fe8ec3e86c766b7916faf2d26cb77961261f975296df3d66e45ef7a310264119; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=a663a7a5a07c5bf3bf1d62bd58f93935b97d454fe250806fa576c268c044db70; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the interface builds and shows the modules: a module switch, a lighting panel listing cabinets with their state and alerts, and override and release controls for operators only
  CHECK: node tools/gates/web-modules.mjs
  EXPECT: WEB MODULES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=87fc3849028ece93eedfc6ec1b750406ac0048588116bbf84a455ffbca968a3a; exit=0; EXPECT=matched; output-sha256=0497566382a9ec999c7e4ab73756317b91dedb8c5aca05bd603f03f23ddffffd; output-bytes=25; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: in QEMU a lighting cabinet (URBAN OS, MODULE=lighting) enrolled at a centre comes online, obeys an operator's override through the centre's API and shows it on its screen, refuses a command meant for another cabinet, and returns to its schedule by itself within 3 minutes after the centre's device listener stops
  CHECK: node os/tests/check-lighting.mjs
  EXPECT: LIGHTING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d6a8f8d60e9fad19c73e794c91926382ba13eabf27a9a86d93968a6a2ddaa705; exit=0; EXPECT=matched; output-sha256=fc58590387a86aa7a0fef2c4e5c5b60feef2892bf9ba76fd111c6be0ecf01524; output-bytes=13094; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the chain unit -> district -> centre and updates from the centre still work (traffic as a module changed nothing for them)
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=95814fe0986d9ea596e3aa00493bd64f4ddbf4e12e66143fb06c32317867ea80; output-bytes=27212; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the docs describe the core, the module rules and the lighting module (docs/city-os-modules.md is the guide for writing a module), README and CONTEXT.md record step 4
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: the public deck says, in plain words, that street lighting now runs on the same system as the second service; the team deck shows the module core and the lighting module
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=266a968aa37274d7edc7b004146d73729250097ac90df59ce69a5770115acfd8; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: both decks are right on every slide, rendered at 1920×1080: no text past the 920 px line, nothing clipped or overflowing, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-19. Team deck version 16 (17 slides, docs/deck/project), public deck version 8 (13 slides, docs/deck-public/project). All 30 slides rendered at 1920x1080 and measured in the browser: no text past 920 px (highest flow bottoms: city 913, demo 901, screen 887), nothing clipped or overflowing. Claims checked: «Уличное освещение — работает» / lighting «работает» = G6 passed in QEMU (data/os/logs/gates-step4-b.log) and G2/G3 tests; «Core.send, роли и аудит, обновления из центра» = backend/urban/modules/base.py and docs/city-os-modules.md; water, heat and emergency services are marked as next modules, as in CONTEXT.md.
