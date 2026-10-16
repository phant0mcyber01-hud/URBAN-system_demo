# Gates: URBAN Edge OS - a bootable, read-only, self-updating Linux image for the unit

OWNS: os/**, docs/edge-os.md, GATES.md, CONTEXT.md, README.md, deploy/README.md, .gitignore

Scope: a Debian-based disk image for the URBAN Edge unit (x86_64, UEFI) that boots with a read-only root, keeps keys and settings on a separate data partition, makes the unit's key on first boot and prints its enrolment code, locks the network down, arms a hardware watchdog, and updates itself from signed A/B bundles with automatic rollback - proven by booting it in QEMU.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the image builds from the repository and has the A/B layout (ESP, rootfs-A, rootfs-B, data) with a squashfs root in slot A
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=11b441036d85d9025d97b0bd4ff4b8b987595b96dd2e30a3207d6e129f6677e9; output-bytes=868; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the image boots in QEMU to a running system whose root is read-only, with the unit services, firewall and watchdog in place, as reported by the OS self-check
  CHECK: node os/tests/check-boot.mjs
  EXPECT: BOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d5b9334d0e6615f40753d1fe05cffcbeedf7d256a3eebdff580ddaf006fa9398; exit=0; EXPECT=matched; output-sha256=e738ecde09a81ad4aac4a2635bfe2561bd34bfe809f754a485ca21a3067dd1d6; output-bytes=1205; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: first boot makes the unit key on the data partition and prints the enrolment code; a second boot keeps the same key
  CHECK: node os/tests/check-firstboot.mjs
  EXPECT: FIRSTBOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d884b261b8b2b557f51e80f0801a933c0f4fa0f8a5a29082d34b17526458f617; exit=0; EXPECT=matched; output-sha256=2a8389753cd22b88f3a9e865591b56ebe3fc1e220cc2acbd347c44ea7b02af0e; output-bytes=3441; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: a signed update bundle installs into the other slot, the unit reboots into it and marks it good
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=b4b8df67cb80641ecaa5b89b2963f4369ab35fcb496ee1bf1445cf883fcde020; output-bytes=3327; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: a bundle signed with a foreign key is refused and the installed slots stay untouched
  CHECK: node os/tests/check-update.mjs foreign
  EXPECT: FOREIGN BUNDLE REFUSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=495c200fb7c41952b90d2d28aa68ff239d9c2ff0da46feddc3359d72b8f460fc; exit=0; EXPECT=matched; output-sha256=4f3b20579e0480a74c4ff7ef7157b34c051df994d7927bebf4cfdefa65bb3bef; output-bytes=1216; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: an update whose system fails its health check is rolled back to the previous slot without anyone touching the unit
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=30d23f0fafb7019ceb6dcf3a7d84dbf3422d41e15a411c67005a81f4efc90b41; output-bytes=4972; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: the platform and unit test suite still passes
  CHECK: node os/tests/check-pytest.mjs
  EXPECT: PYTEST PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=38682ea0912cbaa234bf95e33f8aa0aa37891cfe9a35f34e54bd1bb1a9d5f2c3; exit=0; EXPECT=matched; output-sha256=8f134172c78fdac3c59937d4e79cf1e1b15498f601b4ebbae3383dfeeeae013e; output-bytes=47; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G8: docs/edge-os.md explains building, flashing, first boot, updates and rollback, and states what was not tried on real hardware
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries
