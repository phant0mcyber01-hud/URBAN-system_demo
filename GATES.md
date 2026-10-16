# Gates: controllers of every kind, and URBAN's own look

OWNS: backend/urban/equipment/**, backend/urban/edge/__main__.py, backend/urban/survey.py, backend/tests/**, web/**, docs/**, tools/**, CONTEXT.md, README.md, GATES.md, deploy/**

Scope: the unit can work with any signal controller that has plan-selection contacts (any encoding, with an enable contact and feedback, through a relay board on RS-485 or Ethernet or the unit's own outputs), with NTCIP controllers, and with any SNMP or Modbus controller described by a profile file; a probe tells what a controller in a cabinet supports; and the control centre gets its own visual style inspired by the ctOS of Watch Dogs, with nothing taken from Ubisoft; all of it recorded in CONTEXT.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=958052e1be78d9acdd6d8ab18bb3126f4a183151119a69de38c6f4b36cac193d; output-bytes=150; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the contact adapter selects plans in binary, one-hot and BCD codes, holds an enable contact, honours the controller's own plan numbers and its feedback, and every failure leaves all contacts open (the base plan)
  CHECK: node tools/gates/pytest.mjs tests/test_contacts.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=bf254bfeee1e8e884d05010fc08a4188595aae2d5431977a6d787404a6dfcd63; exit=0; EXPECT=matched; output-sha256=b6cd697eaf6e236a6861b317d311952593881e3ed1d0b0cb602a7bc6c5d8af4c; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: contacts are driven through a Modbus RTU board, a Modbus TCP board and the unit's own GPIO lines, each checked against a fake device that records what it was told
  CHECK: node tools/gates/pytest.mjs tests/test_contact_buses.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=c53e33a7021c54ee62278b1b00e5e3fc7b4355403c94a4ee9991c1f5f7dc6c92; exit=0; EXPECT=matched; output-sha256=0416d7d9bcaf23f68aa557c950b9d5e6aa7a73943a99844c87ae5899f078ff68; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: a controller described by a profile file (SNMP OIDs or Modbus registers) is driven like a built-in one; the built-in NTCIP profile writes exactly what the NTCIP adapter writes; malformed or unsafe profiles are refused
  CHECK: node tools/gates/pytest.mjs tests/test_profiles.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=6a40f7bbe666e4a2fee696f7366613cd4f2ab3db22f5d0cc803da766cda8f38a; exit=0; EXPECT=matched; output-sha256=01d5267bdab53f05941a573936f20e186d14d5a8314726039d4b75b67dc1d059; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the probe recognises an NTCIP controller and a Modbus device from fakes, reports an unknown or silent controller as such, and recommends an adapter
  CHECK: node tools/gates/pytest.mjs tests/test_probe.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=11ef00b2fe6016a2daf470f5534b372960359a3a290853bbb00ad1fe2053fbd7; exit=0; EXPECT=matched; output-sha256=9c5cb6a29497b23be125bc0bb7fa0ec8bd28109eb65bdbfdffe4dc2b9afcc8ce; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: docs/controllers.md lists every controller family with what works, what is untested and what needs a document or a device, and shows how to connect each
  CHECK: node tools/gates/docs.mjs controllers
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=6594a84a817474327bbc901e08cc276cc66d7909d32b7c486bbefb067f507275; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the interface builds and its style is one system: colours only from tokens, the new fonts bundled, the URBAN mark in place, and no Ubisoft name or logo in the product
  CHECK: node tools/gates/style.mjs
  EXPECT: STYLE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=83f357d270f856fb7e90704655b18d257d7c90525dd93fc5c04d1153a9bb3898; exit=0; EXPECT=matched; output-sha256=938318fc16e9599a9466c00a2725d0f96ca9630f0e1725cf2df95e820218d23d; output-bytes=66; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the new look was opened in the browser and reviewed on the login screen, the map with a junction selected, the junctions tab and the devices tab
  EVIDENCE: reviewed 2026-10-16 in the built-in browser, real mode, account demo, at 1440x860 and 375x812: sign-in (grid, URBAN mark, boot lines from /api/health), overview (steel city with cyan building edges, numbered rail, KPI tiles), junction J-07 and J-12 selected (cyan selection ring, card, passport tables), devices tab (enrol steps). Found and fixed during review: junction card now opens the passport; phone header overlapped (brand, revoke, user, clock now fit 375 px, no horizontal scroll); junction markers were hidden in real mode with the twin's camera layer

- [x] G8: the whole test suite passes
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=aec091a61879ab4385b91afdeb247ecac1e780efd7223519cf925375b94af4c6; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: CONTEXT.md records controller compatibility and the visual style with the reasons for both
  CHECK: node tools/gates/docs.mjs context
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=cfc16bd7c3d769cc77f1dfd87376d7fddea7fd46ec76c01ccac9369278802649; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries
