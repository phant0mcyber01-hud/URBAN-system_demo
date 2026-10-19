# Gates: the district server relays and reports, a light unit, an encrypted data partition, a three-machine demo; then speed and hardening

OWNS: os/**, backend/urban/node/**, backend/urban/devices/**, backend/urban/edge/**, backend/urban/platform.py, backend/urban/api/app.py, backend/urban/provision.py, backend/tests/**, web/src/**, deploy/**, docs/**, GATES.md, CONTEXT.md, README.md, tools/**

Scope: step 2 of the owner's three-level architecture, in time for the team presentation this week. The district server (ROLE=district) relays each junction unit's TLS connection to the control centre unchanged, so the centre still authenticates every unit and signs every command and the district can neither read nor forge them; the district is itself a device of the centre (enrolled with a code like a unit, shown in Devices with its address and health), reports the queues its cameras measure for its own junctions only, and keeps those reports through an outage of the link to the centre. A light unit (VISION=off) runs without camera work and is proven on 1 GB of memory. The data partition is encrypted (LUKS2) with a key sealed in the machine's TPM, so a disk taken out of its machine cannot be read; without a TPM it stays plain and the screen says so. One command brings up a three-machine demo (unit -> district -> centre) with every screen in a browser. WireGuard between junction and district and the ARM build are not in this step and the docs say so. Then, at the owner's request (any language, fast, excellent security, a break-in yields nothing): measured speed work (no URBAN service waits for the network, the district's relay moved into the kernel) and hardening (kernel lockdown, ptrace off for everyone, every network-facing service in a sandbox, modern-only SSH), proven by an intruder scenario with a control next to every probe.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=ea40c31feb8c965b045ae0f91581e77b15d598b22149e730a2fc143f4785f143; output-bytes=572; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the centre knows districts: a district enrols with its own code and address (no junction), a unit can be assigned to a district and its kit then points at the district's address while still naming the centre's certificate, a district's queues are accepted only for the junctions assigned to it, and a report replayed from a district's outage buffer is written to history at its own time without disturbing the live reading
  CHECK: node tools/gates/pytest.mjs tests/test_district.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=51cbbb66858180e8844f1152df19a89dc4ab28d2ace884c932d03c3c8d58b5f8; exit=0; EXPECT=matched; output-sha256=4a94713f3012a2e41d4f79bdfe8258f1f8201d7ac049d60c211a7d990cf69123; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the district's own client buffers reports while the centre is unreachable (bounded) and replays them in order after reconnecting, refuses commands and says so in its next report, and carries the relay's counts in its health
  CHECK: node tools/gates/pytest.mjs tests/test_district_client.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=fdad987bbee006631286d8d53894fec41138d0e7a2b1562a67164457f9038191; exit=0; EXPECT=matched; output-sha256=1292d0ad2957c93d17b7473310b160f308dec304c67cf613379a7bb078aff385; output-bytes=42; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: a light unit's power plan has no vision work and the power manager never starts counting cameras for it
  CHECK: node tools/gates/pytest.mjs tests/test_power.py
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=011ae077dd785f47b2a4576d0228fccd5ba0fc090a4d6f1e068b57ce611e964c; exit=0; EXPECT=matched; output-sha256=973b7277092fa03e0fd7a53aa5e9356579ad6d52064685e76a54d64c4954c92a; output-bytes=44; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the whole test suite passes
  CHECK: node tools/gates/pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=9bfd399191ec244f851e8fc0b2514b369bb16f3d32bef5d5bf99ba40e3f7aad4; exit=0; EXPECT=matched; output-sha256=501e92988a28353c23c8ae5255292743c746553b133cc3c9212059a042336f24; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: the image carries the relay and district services, the encryption tools (cryptsetup, the TPM libraries systemd needs) and the district's firewall opening
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=1d57307fddcaf117a19f4215f86beeadcc9cb4e845d018f0a536c3287d7ec3e8; output-bytes=322; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: with a TPM the first boot turns the data partition into LUKS2 unlocked by that TPM (the raw partition no longer holds the maintenance key in clear, which the plain control does), what the installer put on it before is kept, a reboot unlocks it again with the same unit key, and without a TPM the partition stays plain and the screen and self-check say so
  CHECK: node os/tests/check-encrypted.mjs
  EXPECT: ENCRYPTION CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=1e2834c4213eca9c4b2c2a9f5c6da3f39794461f7c7aacf149f33ebf0ba0b9a2; exit=0; EXPECT=matched; output-sha256=db8f20366484ef1ec912b6a42aedf26d772cdbbd744be894c334901455e26042; output-bytes=5693; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: a light unit (VISION=off) boots in 1024 MB, passes its self-check, runs its unit service after the kit with no vision process and under 400 MB in use, its plan says vision is off and its screen says it is a light unit
  CHECK: node os/tests/check-light.mjs
  EXPECT: LIGHT UNIT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=353389a729c83ad8b0d665052bf5003fb6e3679dc92b196a4de9d7414551fe3c; exit=0; EXPECT=matched; output-sha256=0e28c84dd167cc8833b39dcdb85d0ea498682f50151a7fe4d0e20e61582218f8; output-bytes=4299; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: three machines in a chain: a unit enrolled through a district reaches the centre through the district's kernel forwarding, no program on the district listening on 8443 (two devices online at the centre, the certificate seen through the district is the centre's), the district shows up in Devices with its address and health, the district's queues for its junction arrive at the centre, during a 60 s outage of the centre's device listener the district keeps its reports and the centre's history has samples from inside the outage afterwards, and the district forwards 8443 from private networks only
  CHECK: node os/tests/check-chain.mjs
  EXPECT: CHAIN CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=f61d67b26b99dc7e18faa0aa4311a190ab3a0f0bdba4cd3f4cad6ecd2957659d; exit=0; EXPECT=matched; output-sha256=310193f330872204be457e157e62544e89aa619da2d53035e70b92babc2eacb1; output-bytes=10150; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G9: the pilot machine (district,center) still grows its data partition, serves the centre over HTTPS and passes its self-check
  CHECK: node os/tests/check-roles.mjs
  EXPECT: ROLES CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d9661fad1e0797a91d47afbe4bb4b62f9874a1b396749c208280915de8f7c6fd; exit=0; EXPECT=matched; output-sha256=9f3606ed6d6ddd1c94f14489f729d907ba6f74637a2891059921a4a1c4b3af73; output-bytes=2785; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G10: a unit image still takes its name and maintenance key from the ESP and keeps its service across a reboot
  CHECK: node os/tests/check-unit-image.mjs
  EXPECT: UNIT IMAGE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d80e94bbbb7bec968ec8398b2fb386cab0493440208906891c5837967d1442ab; exit=0; EXPECT=matched; output-sha256=374ba99806f512596fde1d2b6c8f8c91de99febd6eb0290164d78c9542c43fb8; output-bytes=5459; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G11: first boot is unchanged: no key without a name, one key kept across reboots
  CHECK: node os/tests/check-firstboot.mjs
  EXPECT: FIRSTBOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d884b261b8b2b557f51e80f0801a933c0f4fa0f8a5a29082d34b17526458f617; exit=0; EXPECT=matched; output-sha256=4f69f57be72cb9b8fbb6800203c58766c0df6ca222b007cfc1b595253f349340; output-bytes=5203; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G12: a signed update still installs into the other slot and boots, and the new slot opens the encrypted data partition
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=0be4bd714fa54f162cddd405dfafc700a759a1e3881940d5cbe394fa05e44e0a; output-bytes=3979; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G13: a broken update still rolls back by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=2e1bc114c32bbccc39e635c0f9a36d31cc74de3f0ec89214051ae70860b39a89; output-bytes=6894; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G14: the image still boots from NVMe, SATA and a USB stick with an Intel network card
  CHECK: node os/tests/check-hardware.mjs
  EXPECT: HARDWARE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=cf4b5ebd31b9d786d1b5cd75acdc1212ce6a230332cc265e862108453979b75a; exit=0; EXPECT=matched; output-sha256=6f3cfc887e1b2880651db6068dfe7379b3e98f9c554ab3779839fbcd48f326ef; output-bytes=5948; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G15: one command brings the demo up - a unit, a district and the centre, three screens and the centre's interface reachable from this computer - and one command takes it down
  CHECK: node os/demo.mjs smoke
  EXPECT: DEMO SMOKE PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=927be46f096abe03f93d620b47b55a1b1ddee52203ecd4cf67b73d05f23df5d0; exit=0; EXPECT=matched; output-sha256=b7c976db02417bb37b98a7c82419197800d19daf1e26e3dd75caf1396488f716; output-bytes=1328; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G17: the unit's control service is up within 15 s of urban-prepare also with the network cable pulled, and booting costs less CPU than the previous image did in the same emulated VM, with no more CPU in use while it runs
  CHECK: node os/tests/check-speed.mjs
  EXPECT: SPEED CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=82455156ec915aeaf8cd166a1796341fc475b1436c1c9bc396a9224c182e91b7; exit=0; EXPECT=matched; output-sha256=58ce671c32270ca4e4a79c6fb8b46897de3129d5df8c6d91e2066cee70ba03df; output-bytes=7771; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G18: an intruder on a junction unit gets nothing: as root no kernel memory and no memory of the unit's process (own memory readable: the control), kernel lockdown confidentiality, ptrace off, no kexec, no core dumps; as the hijacked unit service no SSH keys, no boot files, no writes outside its key folder (inside it: the control), no other process; every network-facing URBAN service at systemd exposure 3.0 or better; SSH refuses CBC, SHA-1 key exchange and SHA-1 MACs while the modern default works; nothing but SSH listens
  CHECK: node os/tests/check-hardened.mjs
  EXPECT: HARDENING CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=534867ad00686c80de4108c8c7cf636e3653fad74d1273380248f3aba24bbec9; exit=0; EXPECT=matched; output-sha256=701f1927c52ffd6b345b0d3bdfcea27077f74cb929d580d081df7bcadb1b6fa9; output-bytes=3124; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G16: the docs describe the relay, districts as devices, the light unit, the encryption and its TPM rule, the demo, what an intruder gets at each level and how speed was measured, and what is still not built (WireGuard, ARM, Secure Boot), and CONTEXT.md records the decisions of this step with their reasons
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G19: the team deck (https://claude.ai/artifact/Qd3ZnqpQXXw3Rrdh7RWnGt) is right on every slide: viewed rendered at 1920×1080, no clipped, overlapping or mid-word-broken text, only the slide format's own properties, every number and claim matching the repository's logs and docs, the "not verified" slide matching the final state of this ledger
  EVIDENCE: manual, 2026-10-19, artifact version 9 (sources docs/deck/project). All 12 slides rendered at 1920×1080 and measured in the browser: every slide over=[] and clipped=[], lowest flow bottom 901 px (demo) against the 920 px limit; each slide also looked at by eye, no mid-word breaks. Only the format's own properties are used (sizes, colours, flex, table, x-icon, aside). Numbers checked against the logs: speed slide = docs/edge-os.md table from data/os/logs/gates-final.log (no cable 40.2 → 2.4 s, CPU −33 %, memory < 400 MB); "STATUS: 19/19 PASSED" = G0–G18 met above; the "not verified" column (real hardware, a real mini-PC TPM, heat, relay throughput) = the open items in CONTEXT.md and docs/edge-os.md; the intruder table = what G18 and G13 check.
