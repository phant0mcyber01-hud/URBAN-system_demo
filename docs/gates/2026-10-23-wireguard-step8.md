# Gates: WireGuard between each junction unit and its district; camera video only inside the tunnel

OWNS: os/**, deploy/**, backend/**, web/src/**, docs/**, tools/**, GATES.md, CONTEXT.md, README.md

Scope: step 8 builds the tunnel designed in docs/wireguard-design.md. Today the unit's link to the centre is end-to-end
TLS through the district relay, but camera video (RTSP, with the camera's credentials) between the junction cabinet
and the district server would travel in the clear. After this step: the district listens on UDP 51820, each unit dials
it with its own WireGuard key (made on the unit, root-only, on the encrypted data partition); the centre numbers the
district and the unit's tunnel address once and for ever, takes a unit's first tunnel key from its authenticated
health report (a changed key waits for an admin), and sends a signed peer list to the district and a signed link to
the unit; a root helper on each machine re-checks the signature and applies only that. Video reaches the district only
through the tunnel (kernel DNAT on the unit to the camera in the cabinet network); there is no fallback path. Once a
unit has shaken hands it is "proven" and reaches the centre through the tunnel; the admin closes the district's open
relay port (mode only). Owner's defaults (2026-10-21): cameras sit in the unit's cabinet, the unit has two networks
(two NICs or VLANs), any link to the district, 10.77.0.0/16, the first key accepted by itself and a changed key
confirmed by an admin. The pilot machine (district and centre on one) needs no tunnel. The step-7 ledger is archived
in docs/gates/2026-10-21-boot-gaps-step7.md.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=41ff4497ce930838cd872a74ce33aca9ec838cddb6fb4cfb367cd931e74ed4c9; output-bytes=342; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the pure tunnel pieces hold: tunnel keys, unit addresses (stable, no collisions, retired ones never reused), signed peer list and link (forged, wrongly addressed, older or with extra fields refused), wg texts, handshake freshness, camera ports; the capture judge catches a plain exchange and passes WireGuard
  CHECK: node tools/gates/pytest.mjs tests/test_wireguard.py tests/test_wireguard_capture.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=c5d1fc4af75a8d87a13fa09ce79b0952881a0a36196977bcc70d50ec13368946; exit=0; EXPECT=matched; output-sha256=a4dc8ce3c8acf10d8d73c41ef65dcc44237b393d1d8204a3aa9c8208f6defdbc; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the centre numbers a district and a unit's tunnel address once and for ever, takes a unit's first tunnel key from its health report and holds a changed key until an admin confirms it, sends the signed peer list to the district and the signed link to the unit (at session start and on change), drops a revoked unit from the next list, and switches a district to mode only on an admin's request; the district and the unit queue only messages that verify and are not older than the one applied
  CHECK: node tools/gates/pytest.mjs tests/test_wireguard_hub.py tests/test_wireguard_devices.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=a490d25d8a96a9994070d16f1d07c7440bebed27ce1b1725a95dbfd918169984; exit=0; EXPECT=matched; output-sha256=10e884a07d47918b395dfa224913702eba9e49d801649b12b413151f652c9cfe; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: the whole test suite passes, none skipped
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=723c85bf891dc7875d7610a9d9e262887a9c9ef26f54d1a4b5aafb340eb3f58a; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the image carries the tunnel pieces and the earlier checks hold (wireguard-tools, the root helper and its path unit, the firewall pieces; signed chain, escrow and boot-disk pieces as before)
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=361909109ed0a7962fae5780d006fe59d8d7864441fcfa1c183644aa9bbba7c9; output-bytes=1759; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: in QEMU with a centre, a district and a unit behind it on a link of their own, under Secure Boot: the tunnel comes up from the centre's signed messages alone (handshake, 10.77.1.1 and 10.77.1.2 reach each other), the unit becomes proven and its TLS to the centre runs inside the tunnel; a fake camera in the unit's cabinet network hands a marked stream to the district through the tunnel whole, while a capture of the link holds nothing but WireGuard messages (no mark, no RTSP, no TLS, no server name) and the control (the same exchange sent in the clear) is caught; a key the district does not know and random packets get no answer; the district's relay port answers from the link in mode allow and not in mode only, from the tunnel in both; SSH to the district from the tunnel is closed; a revoked unit loses the tunnel within seconds; with the district's tunnel down the unit's service keeps its process, the camera is unreachable and the link still carries nothing but WireGuard; with the unit's clock a year back the tunnel does not come up and comes up by itself once the clock is right
  CHECK: node os/tests/check-wireguard.mjs
  EXPECT: WIREGUARD CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=468e89d89620715272a52670996af457cbecd6099f2e70a88d13513aaeab1864; exit=0; EXPECT=matched; output-sha256=9e7891538916c3fd9564ca1ea874b59cd010d11296c43403037f26893ba01179; output-bytes=27199; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: the chain unit - district - centre still works without the tunnel configured (pilot path)
  CHECK: node os/tests/check-chain.mjs
  EXPECT: CHAIN CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f61d67b26b99dc7e18faa0aa4311a190ab3a0f0bdba4cd3f4cad6ecd2957659d; exit=0; EXPECT=matched; output-sha256=6b84df7d80a9adc87bb923b9364e2c442bacf79b2cd9e17089d4fa78fe8dc54b; output-bytes=17643; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: units and districts still enrol, come online through the district and update from the centre
  CHECK: node os/tests/check-ota.mjs
  EXPECT: OTA CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=65fadcc4013f3dec2aad4214afa9e14da33aa108667ce997846958fc56075d83; exit=0; EXPECT=matched; output-sha256=c0377afa572260e43e9ba98002099a796228df24e6bd39c56e8f25fa2e9eeb4c; output-bytes=38776; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: the intruder check holds with the tunnel pieces in: the unit's service cannot read the tunnel key, change the interface, its peers or the inbox beyond its own messages; on the unit only SSH listens on TCP; forwarding is on for wg0 and the cabinet network only, not the global ip_forward
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=2494ca569a00c1107719c2a496cf06ae9a96bbb2c8e3430b9ca1ba3177050375; output-bytes=12946; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: a signed update still installs into the other slot and boots, and a pre-step-7 unit is still refused
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=ffea597c8605f9cf33a0fa393e940b0fb9556a5482a14ec91aed528438af4e7c; output-bytes=6829; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: the docs describe the tunnel, its row in the intruder table and its honest limits; «WireGuard до района» is no longer listed as missing; CONTEXT.md records step 8
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G11: the team deck shows what step 8 changed and what stays open; the public deck says plainly that camera video between the junction and the district is now closed, with no technical words
  CHECK: node tools/gates/decks.mjs
  EXPECT: DECKS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=52f2c6f03f53d11f16bdf987b5ed61d8a47459962ce35f248b4224940e7fc0e7; exit=0; EXPECT=matched; output-sha256=945c2eac3f98d60b2490688e3bcfdb019b4573498075c6287612e593a2eac74b; output-bytes=138; shell=C:\WINDOWS\system32\cmd.exe; cwd=D:\Claude\URBAN-system; path=88406f20ec23/51 entries

- [x] G12: both decks are right on every slide, rendered at 1920×1080, every claim matching the repository and its logs
  EVIDENCE: manual, 2026-10-23. Team deck published as version 23 (19 slides: new changes8 «Шаг 8: туннель до района», gaps without «WireGuard до района», intruder and asks updated, status rewritten for step 8, every footer N / 19); public deck version 14 (13 slides: privacy.html says video between the junction and the district now goes only by a closed channel, no technical words; decks.mjs checks it). The live team deck (v21) and public privacy.html were byte-identical to the committed sources before publishing. All 32 slides rendered at 1920x1080 and measured: no flowing content past 920 px (lowest 913, public city), nothing wider than 1792 px, no pinned line wrapped. Claims against logs: «STATUS: 11/11 PASSED · шаг 8» = G1-G11 met above (gates-step8f.log --reverify on the final image, G3 re-run after the test fix: PYTEST PASSED (692 tests)); «видео камеры — только в WireGuard», «чужой ключ: ни одного ответа», «туннель упал — открытого пути нет», «отозванный блок выпал за 3 с», «часы назад — туннель ждёт» = G5 (check-wireguard-direct.log: OK wire 879-897 WireGuard frames, 0 leaks; OK outsider; OK down; OK revoke 'in 3 s'; OK clock); «692 теста платформы» = G3. Open items (hardware and a real camera, Secure Boot keys in a real BIOS, an old signed release with the disk in hand, tunnel speed on district hardware) match docs/edge-os.md and CONTEXT.md step 8.
