"""Step 11: URBAN OS for 64-bit ARM boards with UEFI firmware (arm64), gates G3-G5.

    python3 vm.py arm-image     G3: the arm64 image without booting it
    python3 vm.py arm           G4: the arm64 light unit in QEMU aarch64
    python3 vm.py arm-update    G5: a signed arm64 update into the other slot, then a broken one that rolls back
    (dispatched by vm.py; this module is not run on its own)

The arm64 image is built on this x86 laptop (os/build/build.sh --arch arm64: its system installed and its GRUB made
under emulation) as urban-edge-arm64-<version>.img / .raucb, next to the x86 files. It is checked the way the x86 image
is, through the same functions of vm.py where they do not depend on the processor: the disk layout, the bundle, the
signed chain (BOOTAA64.EFI, build/rootfs-arm64-1.img, aavmf-vars-urban.fd), the three roles' pieces (libraries under
aarch64-linux-gnu). What only ARM has is checked here: every program of the system is an aarch64 ELF, none x86; the boot
loader and the kernel are arm64 EFI binaries; the platform's Python imports inside the arm64 system (emulated).

The machine: QEMU's virt board with a Cortex-A72 (vm.VM(arch="arm64")), the ARM UEFI firmware AAVMF with URBAN's PK,
KEK and db enrolled and Secure Boot on, a software TPM 2.0 on its memory-mapped TIS, virtio disk and network, QEMU's
i6300esb watchdog (a PCI card), the console on the PL011 serial port (ttyAMA0), which is the screen of a board without
a monitor. Emulated on this host without KVM: every wait is ARM_PACE times the x86 one.

The TPM on this machine: QEMU's virt board offers its TPM only as the memory-mapped TIS device (tpm-tis-device; QEMU
7.2 here, 10.0 in bookworm-backports alike), the firmware uses it (its TPM event log is there), but Debian's arm64 kernel
has no driver for it (CONFIG_TCG_TIS is not set; CRB and the SPI/I2C TIS drivers are there, as real ARM boards with a TPM
use them). So in QEMU the arm64 unit finds no TPM and keeps its data plain, and says so everywhere: G4 accepts exactly that
(a kernel that sees a TPM and keeps the data plain fails; encryption on an ARM board is checked on hardware only); G5 checks that every system it boots opens the data as the first one did.

vm.py's names are taken from the running vm.py (`__main__`): importing it again as `vm` would make a second Fail class
that vm.py's `except Fail` does not catch.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time

vm = sys.modules["__main__"]

TAG = "arm64-1"  # the arm64 build of version 1: urban-edge-arm64-1.img, build/rootfs-arm64-1.img
COMPATIBLE = "urban-edge-aarch64"
EM_AARCH64, EM_X86 = 183, {3: "i386", 62: "x86-64"}  # ELF e_machine
PE_ARM64, EFI_APPLICATION = 0xAA64, 10  # PE machine and subsystem of an arm64 EFI application
# the platform's Python as the build checks it (build.sh): the unit, vision, the lighting cabinet, the device signer, the
# tunnel; and the vision stack's native wheels (OpenCV, ONNX Runtime), installed for aarch64
IMPORTS = ("urban.edge.__main__, urban.vision.__main__, urban.modules.lighting.__main__, urban.devices.signer, "
           "urban.net.wireguard, urban.net.wghelper, cv2, onnxruntime")
UNIT, SERIAL = "edge-J-11", "URB-11"
ENROLMENT = r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+) key=(\S+)"


def pe_header(path):
    """(machine, subsystem) of a PE/COFF file (an EFI binary), or None if it is not one."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head[:2] != b"MZ" or len(head) < 64:
        return None
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 24 + 70 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", head, pe + 4)[0]
    subsystem = struct.unpack_from("<H", head, pe + 24 + 68)[0]  # the same place in PE32 and PE32+ optional headers
    return machine, subsystem


def elf_machines(root):
    """Every ELF file of the unpacked system: {path relative to root: e_machine}. Links are not followed (what they point
    at is counted where it lies)."""
    found = {}
    for top, dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(top, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            with open(p, "rb") as f:
                head = f.read(20)
            if len(head) == 20 and head[:4] == b"\x7fELF":
                found[os.path.relpath(p, root)] = struct.unpack_from("<H" if head[5] == 1 else ">H", head, 18)[0]
    return found


def aarch64_only(root):
    """Every program, library and kernel module of the system is an aarch64 ELF and none is x86. Firmware for the
    devices' own processors (lib/firmware) is not a program of this system: it may be any other ELF, never x86."""
    elves = elf_machines(root)
    firmware = {p: m for p, m in elves.items() if p.startswith(("lib/firmware/", "usr/lib/firmware/"))}
    system = {p: m for p, m in elves.items() if p not in firmware}
    x86 = sorted(p for p, m in elves.items() if m in EM_X86)
    if x86:
        raise vm.Fail(f"{len(x86)} x86 ELF file(s) in the arm64 system, e.g. {x86[:5]}")
    other = sorted(f"{p} (e_machine {m})" for p, m in system.items() if m != EM_AARCH64)
    if other:
        raise vm.Fail(f"{len(other)} program(s) of the arm64 system are not aarch64: {other[:5]}")
    # the control: the walk does see the system's programs (an empty or wrong folder would pass the two checks above)
    for need in ("usr/bin/python3.11", "usr/lib/systemd/systemd", "usr/sbin/veritysetup", "usr/bin/rauc", "usr/bin/tpm2",
                 "usr/bin/wg", "usr/sbin/nft", "usr/sbin/sshd"):
        if system.get(need) != EM_AARCH64:
            raise vm.Fail(f"control failed: {need} was not found as an aarch64 ELF in the unpacked system")
    venv = [p for p in system if p.startswith("opt/urban/.venv/") and p.endswith(".so")]
    modules = [p for p in system if re.search(r"^(usr/)?lib/modules/.*\.ko$", p)]
    if not venv or not modules:
        raise vm.Fail(f"control failed: no native Python modules ({len(venv)}) or kernel modules ({len(modules)}) seen")
    print(f"   programs: {len(system)} ELF files of the system, all aarch64 ({len(venv)} native modules of the platform's "
          f"Python, {len(modules)} kernel modules), none x86; firmware: {len(firmware)} ELF file(s) for devices, none x86",
          flush=True)


def image():
    """G3: the arm64 image is what it says, checked in the builder without booting it."""
    disk = f"{vm.OUT}/urban-edge-{TAG}.img"
    parts, esp = vm.disk_layout(disk, "BOOTAA64.EFI")
    files = vm.sh(f"mdir -/ -b -i {disk}@@{esp} ::/").stdout
    if "BOOTX64.EFI" in files:
        raise vm.Fail("the arm64 image carries an x86 boot loader (BOOTX64.EFI)")
    vm.bundle_layout(f"{vm.OUT}/urban-edge-{TAG}.raucb", COMPATIBLE)
    print(f"   bundle: urban-edge-{TAG}.raucb verifies against the update CA, compatible={COMPATIBLE}, hook and signed slot files",
          flush=True)
    vm.signed_chain(disk, parts, esp, tag=TAG, loader="BOOTAA64.EFI", fd="aavmf-vars-urban.fd")

    work = "/tmp/arm-image"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    for name in ("BOOTAA64.EFI", "grub.cfg"):
        vm.sh(f"mcopy -n -o -i {disk}@@{esp} ::/EFI/BOOT/{name} {work}/{name}")
    loader = pe_header(f"{work}/BOOTAA64.EFI")
    if loader != (PE_ARM64, EFI_APPLICATION):
        raise vm.Fail(f"BOOTAA64.EFI is not an arm64 EFI application (machine, subsystem: {loader})")
    if b"part-uuid" not in open(f"{work}/BOOTAA64.EFI", "rb").read():
        raise vm.Fail("the boot loader has no probe --part-uuid (the module probe is not built into BOOTAA64.EFI)")
    cmdline = [l for l in open(f"{work}/grub.cfg").read().splitlines() if l.startswith("set CMDLINE=")]
    if len(cmdline) != 1 or "console=ttyAMA0,115200" not in cmdline[0] or "vsyscall" in cmdline[0]:
        raise vm.Fail(f"grub.cfg's kernel line is not the arm64 one (console=ttyAMA0, no vsyscall): {cmdline}")
    print("   boot loader: BOOTAA64.EFI is an arm64 EFI application (PE machine 0xAA64) with probe --part-uuid; "
          "the kernel's console on ttyAMA0 (the kernel: an arm64 EFI binary, the initrd: zstd, both checked below)", flush=True)

    slot = f"{vm.OUT}/build/rootfs-{TAG}.img"
    listing = vm.sh(["unsquashfs", "-l", slot]).stdout
    vm.roles_in(slot, listing, triplet="aarch64-linux-gnu")

    root = "/tmp/arm-root"
    shutil.rmtree(root, ignore_errors=True)
    vm.sh(["unsquashfs", "-q", "-n", "-d", root, slot])
    try:
        aarch64_only(root)
        kernel = pe_header(f"{root}/boot/vmlinuz")
        if not kernel or kernel[0] != PE_ARM64:
            raise vm.Fail(f"the slot's kernel is not an arm64 EFI binary (machine, subsystem: {kernel})")
        # GRUB checks the initrd's signature over the file as it reads it, and it reads a gzip, xz or lzop file unpacked
        # (squash4 brings those decompressors): the arm64 initrd is zstd, which GRUB leaves as it is
        with open(f"{root}/boot/initrd.img", "rb") as f:
            magic = f.read(4)
        if magic != b"\x28\xb5\x2f\xfd":
            raise vm.Fail(f"the slot's initrd is not zstd (starts with {magic.hex()}): GRUB would not verify its signature")
        conf = open(f"{root}/etc/rauc/system.conf").read()
        if not re.search(rf"^compatible={COMPATIBLE}$", conf, re.M):
            raise vm.Fail(f"the unit's RAUC does not say compatible={COMPATIBLE}")
        release = open(f"{root}/etc/urban-os-release").read()
        if f"COMPATIBLE={COMPATIBLE}" not in release or "VERSION=1" not in release:
            raise vm.Fail(f"etc/urban-os-release is not version 1 for {COMPATIBLE}: {release!r}")
        # the platform's Python, run inside the arm64 system (its own interpreter and wheels, emulated by the host)
        vm.sh(["mount", "-t", "proc", "proc", f"{root}/proc"])
        try:
            r = vm.sh(["chroot", root, "/opt/urban/.venv/bin/python", "-c",
                       f"import platform, sys; import {IMPORTS}; print(platform.machine(), sys.version.split()[0])"],
                      check=False, timeout=1800)
        finally:
            vm.sh(["umount", f"{root}/proc"], check=False)
        said = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode != 0 or not said.startswith("aarch64 "):
            raise vm.Fail(f"the platform's Python does not import in the arm64 system: {(r.stderr or r.stdout).strip()[-600:]}")
        print(f"   python: the platform imports in the arm64 system ({said}): {IMPORTS}", flush=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("ARM64 IMAGE LAYOUT VERIFIED", flush=True)


def boot_lines(v):
    """What the machine printed since its last power-on."""
    return v.lines[v.boots[-1]:]


def secure_and_verified(v, what):
    """Secure Boot and the verified root as the running arm64 system sees them: the firmware's SecureBoot variable, the
    kernel's EFI stub saying so on the console as it starts, the command line, the root device, the verity device's
    status, the unit's screen. (The x86 check's dmesg line is printed by x86 kernels only.)"""
    var = v.ssh(f"od -An -tu1 /sys/firmware/efi/efivars/SecureBoot-{vm.SB_GUID}").stdout.split()
    if not var or var[-1] != "1":
        raise vm.Fail(f"{what}: the firmware's SecureBoot variable is {var}, not 1")
    stub = [l.strip() for l in boot_lines(v) if re.search(r"EFI stub: UEFI Secure Boot is enabled", l)]
    if not stub:
        raise vm.Fail(f"{what}: the kernel's EFI stub never said 'UEFI Secure Boot is enabled' on the console")
    if "urban.verity=" not in v.ssh("cat /proc/cmdline").stdout:
        raise vm.Fail(f"{what}: no urban.verity on the kernel command line")
    src = v.ssh("findmnt -n -o SOURCE /").stdout.strip()
    if src != "/dev/mapper/urban-root":
        raise vm.Fail(f"{what}: the root is {src}, not /dev/mapper/urban-root")
    status = v.ssh("veritysetup status urban-root", check=False).stdout
    if not re.search(r"status:\s+verified", status):
        raise vm.Fail(f"{what}: veritysetup does not say the root is verified:\n{status.strip()[:400]}")
    if "Secure Boot on, system verified" not in v.ssh("cat /run/urban/console.issue").stdout:
        raise vm.Fail(f"{what}: the unit's screen does not say 'Secure Boot on, system verified'")
    return stub[0][stub[0].index("EFI stub"):]


def foreign_loaders(v, work):
    """The firmware refuses a boot loader not signed by URBAN's db on this machine's disk: one without a signature and one
    signed by a foreign key, each on a fresh power-on; the system never starts. The machine is left off with its original,
    signed boot loader back on the ESP. Returns the firmware's own words for each."""
    db = f"{vm.OUT}/keys/sb/db.crt"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    vm.esp_get(v.disk, "/EFI/BOOT/BOOTAA64.EFI", f"{work}/signed.efi")
    if vm.sh(["sbverify", "--cert", db, f"{work}/signed.efi"], check=False).returncode != 0:
        raise vm.Fail("control failed: the boot loader on the ESP is not signed by URBAN's db certificate")
    shutil.copy(f"{work}/signed.efi", f"{work}/unsigned.efi")
    vm.sh(["sbattach", "--remove", f"{work}/unsigned.efi"])
    vm.sh(f"openssl req -x509 -newkey rsa:2048 -nodes -keyout {work}/foreign.key -out {work}/foreign.crt -days 30 -subj /CN=not-urban-db")
    vm.sh(["sbsign", "--key", f"{work}/foreign.key", "--cert", f"{work}/foreign.crt", "--output", f"{work}/foreign.efi",
           f"{work}/unsigned.efi"])
    for f in ("unsigned", "foreign"):
        if vm.sh(["sbverify", "--cert", db, f"{work}/{f}.efi"], check=False).returncode == 0:
            raise vm.Fail(f"the {f} test loader verifies against URBAN's db, the test is void")
    said = {}
    for f, what in (("unsigned", "a boot loader without a signature"), ("foreign", "a boot loader signed by a foreign key")):
        vm.esp_put(v.disk, f"{work}/{f}.efi", "/EFI/BOOT/BOOTAA64.EFI")
        if v.proc is None:
            v.start()
        else:
            v.power_on()
        said[f] = vm.refused(v, what, f"{f}.efi")
        v.cut_power()
    vm.esp_put(v.disk, f"{work}/signed.efi", "/EFI/BOOT/BOOTAA64.EFI")
    return said


def light_unit(v, unit):
    """The running arm64 light unit: its kit, its unit service, no vision, and the memory it uses of its 1 GB."""
    plan = json.loads(v.ssh("cat /run/urban/power.json").stdout)
    if not plan["vision"].get("off") or plan["vision"]["fps"] != 0 or plan["vision"]["threads"] != 0:
        raise vm.Fail(f"a light unit's plan still has vision work: {plan['vision']}")
    v.ssh(vm.STAND_KIT.replace("UNIT", unit))
    r = v.ssh(f"urban-unit setup {vm.STAGE}/kit.json", check=False)
    if r.returncode != 0:
        raise vm.Fail(f"urban-unit setup: {(r.stderr or r.stdout).strip()[-300:]}")
    end = time.time() + 30 * v.pace
    while not vm.running(v, unit) and time.time() < end:
        time.sleep(3)
    if not vm.running(v, unit):
        raise vm.Fail(f"urban-edge@{unit} is not running after setup")
    time.sleep(10 * v.pace)  # let it settle: its memory is measured with it running
    if not vm.running(v, unit):
        raise vm.Fail(f"urban-edge@{unit} stopped after setup")
    vm.service_clean(v, "urban-power", "arm64 light unit")
    procs = v.ssh("pgrep -fc '[u]rban.vision' || true").stdout.strip()
    if procs not in ("", "0"):
        raise vm.Fail(f"a vision process is running on a light unit ({procs})")
    total, used = (int(x) for x in v.ssh("free -m | awk '/Mem:/{print $2, $3}'").stdout.split())
    if total > 1024 or used >= 400:
        raise vm.Fail(f"memory: {used} MB used of {total} MB (want at most 1024 MB in all and under 400 MB used)")
    return total, used


def unit():
    """G4: the arm64 light unit boots under Secure Boot with a verified system, its data LUKS2 with the key in the TPM, its
    self-check OK, its name, key and enrolment code on its screen, within 1 GB; a foreign boot loader is refused by the
    firmware; the x86 bundle is refused by the unit's RAUC (wrong compatible), whose signature it does accept."""
    v = vm.VM("arm-unit", arch="arm64", unit=UNIT, serial=SERIAL, vision="off", mem=1024)
    try:
        # ---- a boot loader not signed by URBAN's db, on the fresh machine before its first boot
        said = foreign_loaders(v, "/tmp/arm-loader")
        print(f"   OK the firmware refuses a boot loader not signed by URBAN's db, the system never starts: "
              f"unsigned -> '{said['unsigned'][0][:150]}'; foreign key -> '{said['foreign'][0][:150]}'", flush=True)

        # ---- the original signed boot loader on the same machine: the light unit comes up. What needs the TPM (the data
        # LUKS2 with its key in the TPM, the device key made in it) is noted and failed on at the end, after every other
        # fact of the gate was checked: a unit whose kernel cannot reach the TPM still shows everything else
        no_tpm = []
        v.mark = len(v.lines)
        v.power_on()
        # in the order the unit says them: the data partition (urban-grow), then its role and its key (urban-prepare)
        data = v.wait(r"URBAN-DATA (the partition is now LUKS2 with its key sealed in this machine's TPM|plain: no TPM on this machine.*)")
        if not data.group(1).startswith("the partition is now LUKS2"):
            no_tpm.append(f"the unit says: URBAN-DATA {data.group(1)}")
        v.wait(rf"URBAN-ROLE role=unit name={UNIT} vision=off")
        m = v.wait(ENROLMENT)
        fp, code, keyin = m.group(3), m.group(4), m.group(5)
        vm.check_code(code, fp, UNIT, SERIAL)
        if keyin != "tpm":
            no_tpm.append(f"its device key is {keyin!r}, not made in the TPM")
        line = v.booted("1", "A")
        check = line[line.index("URBAN-OS-CHECK"):]
        vm.sb_line(line, "arm64 unit")
        for need in ("firewall=on", "role=unit", f"unit={UNIT}"):
            if need not in line:
                raise vm.Fail(f"the self-check line lacks {need}: {line}")
        data_said = re.search(r"data=\S+", line)
        if "data=rw,luks2-tpm2" not in line:
            no_tpm.append(f"the self-check says {data_said.group(0) if data_said else 'nothing of its data'}, not data=rw,luks2-tpm2")
        stub = secure_and_verified(v, "arm64 unit")
        machine = v.ssh("uname -m; sed -n 's/^compatible=//p' /etc/rauc/system.conf").stdout.split()
        if machine != ["aarch64", COMPATIBLE]:
            raise vm.Fail(f"the unit is not an aarch64 system with RAUC compatible {COMPATIBLE}: {machine}")
        luks = v.ssh("cryptsetup status urban-data; cryptsetup luksDump /dev/disk/urban/urban-data", check=False).stdout
        if not re.search(r"type:\s+LUKS2", luks) or "systemd-tpm2" not in luks:
            no_tpm.append("the data partition is not LUKS2 with a TPM2 token")
        if no_tpm:  # why: what the firmware offered and what the kernel made of it
            seen = v.ssh("ls /sys/class/tpm 2>&1; grep -hE '^(# )?CONFIG_TCG_(TIS|CRB)[ =]' /boot/config-*; "
                         "dmesg | grep -iE 'tpm' | head -n 5", check=False).stdout.strip().replace("\n", " | ")
            no_tpm.append(f"the kernel's view of the TPM: {seen or 'nothing'}")
        print(f"   OK the original boot loader on the same machine: {check[:200]}", flush=True)
        print(f"   OK Secure Boot on (SecureBoot variable 1, '{stub[:60]}'), root /dev/mapper/urban-root verified, aarch64, "
              f"RAUC compatible {COMPATIBLE}", flush=True)
        if not no_tpm:
            print("   OK data LUKS2 with its key sealed in the TPM, the device key made in the TPM", flush=True)

        # its screen: the text getty shows above the login prompt, on the serial console (ttyAMA0) of the board
        screen = v.ssh("cat /run/urban/console.issue").stdout
        want = {"name": rf"^\s*name\s+{re.escape(UNIT)}\s+serial {re.escape(SERIAL)}\s*$",
                "role": r"^\s*role\s+junction unit, light \(no cameras\)\s*$",
                "key": rf"^\s*key\s+{re.escape(fp)} \((in TPM|file)\)\s*$",
                "enrolment code": rf"^\s*enrolment\s+{re.escape(code)}\s*$"}
        for what, rx in want.items():
            if not re.search(rx, screen, re.M):
                raise vm.Fail(f"the unit's screen does not show its {what}:\n{screen}")
        shown = boot_lines(v)
        for what in ("name", "key", "enrolment code"):
            if not any(re.search(want[what], l) for l in shown):
                raise vm.Fail(f"the serial console (ttyAMA0) never showed the unit's {what} (log: {vm.LOGS}/{v.name}.log)")
        where = re.search(want["key"], screen, re.M).group(1)
        print(f"   OK its screen (and the serial console ttyAMA0): name {UNIT}, key {fp} ({where}), enrolment {code[:32]}...",
              flush=True)

        total, used = light_unit(v, UNIT)
        print(f"   OK light unit: urban-edge@{UNIT} runs after its kit, no vision process; {used} MB used of {total} MB "
              f"(QEMU -m 1024)", flush=True)

        # ---- the x86 bundle: its signature is URBAN's (rauc info accepts it), its compatible is not this unit's
        stage = f"{vm.STAGE}/x86.raucb"
        v.put(f"{vm.OUT}/urban-edge-2.raucb", stage)
        info = v.ssh(f"rauc info --output-format=json {stage}", check=False)
        if info.returncode != 0 or json.loads(info.stdout or "{}").get("compatible") != "urban-edge-x86_64":
            raise vm.Fail(f"control failed: the unit does not read the x86 bundle as URBAN's, for urban-edge-x86_64: "
                          f"{(info.stderr or info.stdout).strip()[-300:]}")
        before = v.ssh("grub-editenv /boot/efi/EFI/BOOT/grubenv list; dd if=/dev/disk/urban/rootfs-b bs=1M count=4 status=none | sha256sum").stdout
        r = v.ssh(f"rauc install {stage}", check=False)
        said = (r.stdout + r.stderr).strip()
        after = v.ssh("grub-editenv /boot/efi/EFI/BOOT/grubenv list; dd if=/dev/disk/urban/rootfs-b bs=1M count=4 status=none | sha256sum").stdout
        if r.returncode == 0:
            raise vm.Fail("the arm64 unit installed the x86 bundle")
        # the refusal names both: RAUC's own words ("compatible ...") or the bundle hook's install-check, which makes RAUC's
        # compatible check ("this bundle is for 'urban-edge-x86_64', this system is 'urban-edge-aarch64'")
        if "urban-edge-x86_64" not in said or not ("urban-edge-aarch64" in said or "ompatible" in said):
            raise vm.Fail(f"the x86 bundle was refused, but not for its compatible: {said[-400:]}")
        booted, _ = vm.slots(v)
        if after != before or booted != "A" or "B_OK=0" not in after:
            raise vm.Fail(f"the refused x86 bundle still changed the boot state or slot B:\n{before}\n{after}")
        v.ssh(f"rm -f {stage}")
        reason = next((l.strip() for l in reversed(said.splitlines()) if "urban-edge-x86_64" in l), said.splitlines()[-1])
        print(f"   OK the x86 bundle (signed by URBAN, rauc info accepts it) is refused by the unit's RAUC: '{reason[:160]}'; "
              f"slot B and the boot state untouched", flush=True)
        if no_tpm:
            # QEMU's virt machine offers its TPM only as tpm-tis-device, which Debian's arm64 kernel has no driver for
            # (CONFIG_TCG_TIS is not set; CRB and the SPI/I2C TIS drivers real ARM boards use are there). Then, and only
            # then, the honest outcome is a plain data partition that says so everywhere: a unit whose kernel sees a TPM
            # and still keeps its data plain fails, and so does one that keeps it plain without saying it.
            tpm_seen = v.ssh("ls /sys/class/tpm 2>/dev/null", check=False).stdout.strip()
            config = v.ssh("grep -hE '^(# )?CONFIG_TCG_TIS[ =]' /boot/config-*", check=False).stdout
            if tpm_seen or "# CONFIG_TCG_TIS is not set" not in config:
                raise vm.Fail("the data partition is not encrypted with its key in the TPM although the kernel could reach "
                              "one: " + "; ".join(no_tpm))
            honest = ["plain: no TPM on this machine" in data.group(1), "data=rw,plain" in line, keyin == "file",
                      bool(re.search(r"NOT encrypted - this machine has no TPM", screen))]
            if not all(honest):
                raise vm.Fail(f"with no TPM the unit does not say plainly that its data is not encrypted "
                              f"(URBAN-DATA, self-check, key, screen: {honest}): " + "; ".join(no_tpm))
            print("   OK no TPM driver for QEMU's virt TPM in Debian's arm64 kernel (CONFIG_TCG_TIS is not set, "
                  "/sys/class/tpm empty): the unit keeps its data plain and says so - 'plain: no TPM on this machine', "
                  "self-check data=rw,plain, device key a file, screen 'NOT encrypted - this machine has no TPM'. "
                  "Encryption on an ARM board with a TPM is checked on hardware only", flush=True)
        print("ARM CHECK PASSED", flush=True)
    finally:
        vm.drop(v)


def update():
    """G5: on the arm64 unit a signed arm64 update (version 2) installs into slot B and boots; then a broken one (version
    3, a firewall that does not load) is installed into the other slot, A, fails its self-check and the unit is back on
    version 2 in slot B by itself."""
    v = vm.VM("arm-update", arch="arm64", unit=UNIT, serial=SERIAL, vision="off", mem=1024)
    try:
        v.start()
        # the data partition as slot A found it (LUKS2 with the TPM, or plain without one): every later system must open
        # it the same way, or the unit's keys and kit would be gone after the update or the rollback
        data = re.search(r"data=\S+", v.booted("1", "A")).group(0)
        v.put(f"{vm.OUT}/urban-edge-arm64-2.raucb", f"{vm.STAGE}/update.raucb")
        v.install(f"{vm.STAGE}/update.raucb")
        v.ssh(f"rm -f {vm.STAGE}/update.raucb")
        v.reboot()
        line = v.booted("2", "B")
        vm.sb_line(line, "arm64: slot B after the update")
        if data not in line.split():
            raise vm.Fail(f"slot B does not open the data partition as slot A did ({data}): {line}")
        booted, by_slot = vm.slots(v)
        if booted != "B" or by_slot["B"].get("boot_status") != "good":
            raise vm.Fail(f"after the update: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
        print(f"   OK version 2 installed into slot B and booted: {line[line.index('URBAN-OS-CHECK'):][:200]}", flush=True)

        v.put(f"{vm.OUT}/urban-edge-arm64-3.raucb", f"{vm.STAGE}/update.raucb")
        v.install(f"{vm.STAGE}/update.raucb")
        v.ssh(f"rm -f {vm.STAGE}/update.raucb")
        v.reboot()
        m = v.wait(r"URBAN-OS-CHECK (OK|FAIL) version=(\S+) slot=(\S+)")
        failed = v.lines[v.mark - 1]
        if m.group(1) != "FAIL" or m.group(2) != "3" or m.group(3).rstrip(":") != "A":
            raise vm.Fail(f"the broken version 3 did not fail its self-check in slot A: {failed}")
        v.wait(r"URBAN-HEALTH bad slot=A", 300)
        line = v.booted("2", "B")  # back on the previous system without anyone touching the unit
        if data not in line.split():
            raise vm.Fail(f"after the rollback the data partition is not opened as before ({data}): {line}")
        booted, by_slot = vm.slots(v)
        if booted != "B" or by_slot["A"].get("boot_status") != "bad":
            raise vm.Fail(f"after the rollback: booted {booted}, slot A {by_slot['A'].get('boot_status')}")
        print(f"   OK version 3 (broken firewall) in slot A: {failed[failed.index('URBAN-OS-CHECK'):][:160]}", flush=True)
        print(f"   OK rolled back by itself: {line[line.index('URBAN-OS-CHECK'):][:160]}; slot A bad", flush=True)
        print("ARM UPDATE CHECK PASSED", flush=True)
    finally:
        vm.drop(v)
