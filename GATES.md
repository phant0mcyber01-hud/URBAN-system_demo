# Gates: URBAN Edge OS on a real mini-PC - an image to write and a guide to follow

OWNS: os/**, docs/**, GATES.md, CONTEXT.md, README.md, .gitignore

Scope: turn the QEMU-proven URBAN Edge OS into something a person can put on an ordinary x86-64 mini-PC: an image that carries the drivers and firmware such boxes need and boots from NVMe, SATA or a USB stick; a per-unit image with the unit's identity and maintenance key that can be prepared without Linux; a compressed release with its checksum; and a step-by-step guide in Russian that says plainly that no real mini-PC has been tried yet.

- [x] G0: this ledger states outcomes that can fail
  CHECK: node C:/Users/DilDa/.claude/skills/unlazy/scripts/gate-lint.mjs GATES.md
  EXPECT: LINT OK
  EVIDENCE: automatic-evidence=v1; definition-sha256=495aef5e665131d49faba79374b9a7b244c9abc642d22fb4f40ac5ea8b723f19; exit=0; EXPECT=matched; output-sha256=48630b7361dd44ee870917b12c3d19b9d7bdea738aaca16bb04d4cab83b772d2; output-bytes=8; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G1: the image carries Realtek network firmware, CPU microcode and the watchdog drivers of Intel and AMD boards, and keeps its A/B layout
  CHECK: node os/tests/check-image.mjs
  EXPECT: IMAGE LAYOUT VERIFIED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7ec66d313dd38ea5ed649b8cc003687c0767e121de748530d35504a23ac750cb; exit=0; EXPECT=matched; output-sha256=6df7a8904715852817725bb79ef634cb72acbefb4d88e599c3e8b2a2feab5e7b; output-bytes=962; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G2: the same image boots from an NVMe disk, a SATA disk and a USB stick with an Intel network card, and passes its self-check on each
  CHECK: node os/tests/check-hardware.mjs
  EXPECT: HARDWARE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=cf4b5ebd31b9d786d1b5cd75acdc1212ce6a230332cc265e862108453979b75a; exit=0; EXPECT=matched; output-sha256=275e6d07e9fbafd2ce82c95af18a881e5d2fa43d70ce526b0518c1a3ff296634; output-bytes=4249; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G3: a unit image made for one junction carries its name and maintenance key on the ESP only, unpacks to its published checksum, boots, shows its enrolment code, lets in that key and refuses any other
  CHECK: node os/tests/check-unit-image.mjs
  EXPECT: UNIT IMAGE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d80e94bbbb7bec968ec8398b2fb386cab0493440208906891c5837967d1442ab; exit=0; EXPECT=matched; output-sha256=ab13820c89397c075948023c01c15253d5d2bf61be7f241e754d7ef62d3596a7; output-bytes=3963; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G4: the first-boot behaviour is unchanged: no key without a name, one key kept across reboots
  CHECK: node os/tests/check-firstboot.mjs
  EXPECT: FIRSTBOOT CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d884b261b8b2b557f51e80f0801a933c0f4fa0f8a5a29082d34b17526458f617; exit=0; EXPECT=matched; output-sha256=ffb100f449921b0ef0965124e387c48b7a3e46ff2258ddfea0fbe7ad0f7b89a2; output-bytes=3806; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G5: a signed update still installs into the other slot and boots
  CHECK: node os/tests/check-update.mjs good
  EXPECT: UPDATE CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=d3d590f1d607e64055329e2b4c6dbd20ee080074930ac93276d27cdd4874814c; exit=0; EXPECT=matched; output-sha256=2b7ac6e054ef190d9192b45fd7abac76b0ea8f7353b8d6222d4e3a7082651bf8; output-bytes=3711; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G6: a broken update still rolls back to the working system by itself
  CHECK: node os/tests/check-update.mjs broken
  EXPECT: ROLLBACK CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=05b095097fa8787ce6e4c7f65f3f3565d1e9c840b6ee760706fdb3ca1b2106a0; exit=0; EXPECT=matched; output-sha256=08f9e87162694ab5bfb24b6de8c0ee1e8eb46106e5416c04ec975e0c3c00116a; output-bytes=5435; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries

- [x] G7: docs/mini-pc.md walks a person from a box to an enrolled unit (requirements, writing on Windows, firmware settings, first boot, enrolment, maintenance, faults, a stand checklist), says what was never tried on a real mini-PC, and CONTEXT.md records it
  CHECK: node os/tests/check-docs.mjs
  EXPECT: DOCS CHECK PASSED
  EVIDENCE: automatic-evidence=v1; definition-sha256=7a7eaf5b4aedd293d3eee5e14b8e0be9d4a112f9269b1e8e5579c7c3c552f63e; exit=0; EXPECT=matched; output-sha256=a646cb7131c6775144fa7ec37840cdefac1fd1f1fc2efa0f5726e39a3386d30e; output-bytes=18; shell=C:\WINDOWS\system32\cmd.exe; cwd=C:\Users\DilDa\Desktop\URBAN-system; path=88406f20ec23/51 entries
