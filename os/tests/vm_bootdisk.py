"""Step 7: partitions only from the disk the unit booted from (gate G5; the image's pieces for G3).

    python3 vm.py bootdisk     (dispatched by vm.py; this module is not run on its own)

A stranger's USB stick carries GPT partitions named esp (FAT: a bogus grub.cfg, an identity, a maintenance key),
rootfs-a, rootfs-b and urban-data (ext4 labelled urban-data: a marker and the stranger's SSH key). It is attached to a
unit from power-on, Secure Boot on as always. The unit must boot, mount and update only from its own disk:

  the control   the old lookups (/dev/disk/by-partlabel/<name>, by-label/urban-data) do resolve to the stick once its
                partitions are announced again (what plugging it in does): the old system would have mounted its ESP
                and data and written the update to its rootfs-b;
  the proof     GRUB passed the slot's own partition GUID (urban.bootpart) and the initramfs opened that partition;
                /dev/disk/urban/* all lie on the boot disk, also after the stick is announced again; /boot/efi and
                /var/lib/urban are mounted from them; the stranger's SSH key gets nowhere; the unit passes its
                self-check; `urban-unit update` installs version 2 into slot B of its own disk and the unit runs it;
                the stick is byte for byte what it was (sha256 of its image before and after, in this container).

vm.py's names are taken from the running vm.py (`__main__`): importing it again as `vm` would make a second Fail class
that vm.py's `except Fail` does not catch.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

vm = sys.modules["__main__"]

NAMES = ("esp", "rootfs-a", "rootfs-b", "urban-data")
LINKS = NAMES + ("data-fs",)
STRANGER = "/tmp/bootdisk-stranger.img"
STRANGER_KEY = "/tmp/bootdisk-stranger-key"
LINUX_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
ESP_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
# what the image must not use any more, outside comments: a link or a mount by label or partition name (or GUID)
BY_NAME = re.compile(r"/dev/disk/by-(partlabel|label|partuuid)/|by\\x2d(partlabel|label|partuuid)|\bPARTLABEL=|^\s*LABEL=")


# ---------------------------------------------------------------- the image's pieces (G3)

def image_pieces(disk, parts, esp):  # parts: the image's partitions (sfdisk), esp: the ESP's byte offset in disk
    """In the built image: GRUB hands over the slot's partition GUID (urban.bootpart, read with probe, which is in the
    signed boot loader), the initramfs opens the slot by it, the udev rule and its helper make /dev/disk/urban/*, and
    fstab, RAUC, the services and the scripts use those links only, never a label or a partition name."""
    work = "/tmp/bootdisk-image"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    vm.sh(f"mcopy -n -o -i {disk}@@{esp} ::/EFI/BOOT/grub.cfg {work}/grub.cfg")
    vm.sh(f"mcopy -n -o -i {disk}@@{esp} ::/EFI/BOOT/BOOTX64.EFI {work}/BOOTX64.EFI")
    grub = open(f"{work}/grub.cfg").read()
    for slot, n in (("A", 2), ("B", 3)):
        if f"probe --set=URBAN_BOOTPART --part-uuid ($disk,gpt{n})" not in grub:
            raise vm.Fail(f"grub.cfg does not read slot {slot}'s partition GUID from the boot disk (probe --part-uuid ($disk,gpt{n}))")
    linux = [l for l in grub.splitlines() if l.strip().startswith("linux ")]
    if len(linux) != 2 or not all("urban.bootpart=$URBAN_BOOTPART" in l for l in linux):
        raise vm.Fail(f"grub.cfg does not pass urban.bootpart on every kernel line: {linux}")
    if b"part-uuid" not in open(f"{work}/BOOTX64.EFI", "rb").read():
        raise vm.Fail("the boot loader has no probe --part-uuid (the module probe is not built into BOOTX64.EFI)")

    slot = f"{vm.OUT}/build/rootfs-1.img"
    sq = f"{work}/sq"
    files = ["etc/udev/rules.d/60-urban-bootdisk.rules", "usr/local/bin/urban-bootdisk", "etc/fstab",
             "etc/rauc/system.conf", "etc/initramfs-tools/scripts/local-top/urban-verity", "usr/local/bin",
             "etc/systemd/system"]
    vm.sh(["unsquashfs", "-q", "-n", "-d", sq, slot, *files])

    def read(rel):
        if not os.path.isfile(f"{sq}/{rel}"):
            raise vm.Fail(f"the image lacks {rel}")
        return open(f"{sq}/{rel}", errors="replace").read()

    rule = read("etc/udev/rules.d/60-urban-bootdisk.rules")
    for need in ('PROGRAM=="/usr/local/bin/urban-bootdisk part %k", SYMLINK+="disk/urban/$env{ID_PART_ENTRY_NAME}"',
                 'PROGRAM=="/usr/local/bin/urban-bootdisk crypt %k", SYMLINK+="disk/urban/data-fs"'):
        if need not in rule:
            raise vm.Fail(f"the udev rule 60-urban-bootdisk.rules lacks {need}")
    listing = vm.sh(["unsquashfs", "-lls", slot, "usr/local/bin/urban-bootdisk"]).stdout
    if not re.search(r"^-rwxr-xr-x\s.*usr/local/bin/urban-bootdisk$", listing, re.M):
        raise vm.Fail(f"urban-bootdisk is not an executable in the image (udev could not run it): {listing.strip()[-200:]}")
    verity = read("etc/initramfs-tools/scripts/local-top/urban-verity")
    for need in ("urban.bootpart=*", "blkid -p -o export", "partitions carry this unit's boot partition GUID",
                 "/run/urban-boot/disk"):
        if need not in verity:
            raise vm.Fail(f"the initramfs script does not open the slot by its GUID ({need!r} missing)")
    fstab = read("etc/fstab")
    for dev, where in (("/dev/disk/urban/data-fs", "/var/lib/urban"), ("/dev/disk/urban/esp", "/boot/efi")):
        if not re.search(rf"^{re.escape(dev)}\s+{re.escape(where)}\s", fstab, re.M):
            raise vm.Fail(f"fstab does not mount {where} from {dev}")
    conf = read("etc/rauc/system.conf")
    for x in "ab":
        if f"device=/dev/disk/urban/rootfs-{x}" not in conf:
            raise vm.Fail(f"RAUC's slot rootfs-{x} is not /dev/disk/urban/rootfs-{x}")
    scanned = [f"{sq}/etc/fstab", f"{sq}/etc/rauc/system.conf",
               f"{sq}/etc/initramfs-tools/scripts/local-top/urban-verity"]
    scanned += sorted(f"{sq}/usr/local/bin/{n}" for n in os.listdir(f"{sq}/usr/local/bin") if n.startswith("urban-"))
    scanned += sorted(f"{sq}/etc/systemd/system/{n}" for n in os.listdir(f"{sq}/etc/systemd/system")
                      if n.endswith((".service", ".mount", ".path", ".timer")))
    for path in scanned:
        if os.path.islink(path) or not os.path.isfile(path):  # aliases and masks point outside the extracted tree
            continue
        for i, line in enumerate(open(path, errors="replace").read().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if BY_NAME.search(line):
                raise vm.Fail(f"{path[len(sq) + 1:]}:{i} still finds a partition by its label or name: {line.strip()[:160]}")
    shutil.rmtree(work, ignore_errors=True)
    print(f"   boot disk: GRUB passes the slot's partition GUID (urban.bootpart; probe in the signed loader), the initramfs "
          f"opens the slot by it, udev makes /dev/disk/urban/* for the boot disk only; fstab, RAUC and {len(scanned)} "
          f"scripts and units use no label or partition name", flush=True)


# ---------------------------------------------------------------- the stranger's disk (G5)

def stranger_disk(path):
    """The stranger's USB stick, made here (sfdisk, mkfs, mtools; no loop device): 256 MiB, GPT, partitions named like
    the unit's. Returns its partitions (sfdisk -J) by name."""
    for f in (path, STRANGER_KEY, STRANGER_KEY + ".pub"):
        if os.path.exists(f):
            os.remove(f)
    vm.sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "stranger", "-f", STRANGER_KEY])
    stranger_pub = open(STRANGER_KEY + ".pub").read()
    vm.sh(["truncate", "-s", "256M", path])
    vm.sh(["sfdisk", "--quiet", path], input=(
        "label: gpt\n"
        f'start=1MiB, size=32MiB, type={ESP_TYPE}, name="esp"\n'
        f'size=64MiB, type={LINUX_TYPE}, name="rootfs-a"\n'
        f'size=64MiB, type={LINUX_TYPE}, name="rootfs-b"\n'
        f'size=64MiB, type={LINUX_TYPE}, name="urban-data"\n'))
    parts = vm.partitions(path)
    if sorted(parts) != sorted(NAMES):
        raise vm.Fail(f"the stranger's disk has partitions {sorted(parts)}")
    work = "/tmp/bootdisk-stranger"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(f"{work}/data/ssh")

    def put(img, name):
        vm.sh(f"dd if={img} of={path} bs=1M seek={parts[name]['start'] * 512} oflag=seek_bytes conv=notrunc status=none")

    # esp: what a unit's ESP holds, the stranger's way: a boot config that is not URBAN's, an identity, a maintenance key
    esp = f"{work}/esp.img"
    vm.sh(["mkfs.vfat", "-C", "-n", "STRANGER", esp, str(32 * 1024 - 64)])
    with open(f"{work}/grub.cfg", "w") as f:
        f.write("echo URBAN-STRANGER-GRUB\nset superusers=\"\"\nlinux /vmlinuz init=/bin/sh\nboot\n")
    with open(f"{work}/urban-unit.txt", "w") as f:
        f.write("UNIT=stranger-J-99\nSERIAL=STRANGER\n")
    with open(f"{work}/maintenance.pub", "w") as f:
        f.write(stranger_pub)
    with open(f"{work}/STRANGER-ESP", "w") as f:
        f.write("the stranger's ESP\n")
    vm.sh(f"mmd -i {esp} ::/EFI ::/EFI/BOOT ::/EFI/URBAN")
    vm.sh(f"mcopy -o -i {esp} {work}/grub.cfg ::/EFI/BOOT/grub.cfg")
    vm.sh(f"mcopy -o -i {esp} {work}/urban-unit.txt {work}/maintenance.pub {work}/STRANGER-ESP ::/")
    put(esp, "esp")
    # rootfs-a, rootfs-b: a marked start, the rest empty
    for name in ("rootfs-a", "rootfs-b"):
        mark = f"{work}/{name}.mark"
        with open(mark, "wb") as f:
            f.write((f"URBAN-STRANGER-{name.upper()}\n" * 256).encode())
        put(mark, name)
    # urban-data: an ext4 labelled urban-data with a marker and the stranger's SSH key where the unit keeps its own
    with open(f"{work}/data/STRANGER-DATA", "w") as f:
        f.write("the stranger's data partition\n")
    with open(f"{work}/data/ssh/authorized_keys", "w") as f:
        f.write(stranger_pub)
    data = f"{work}/data.img"
    vm.sh(["mkfs.ext4", "-q", "-F", "-L", "urban-data", "-d", f"{work}/data", data, "60M"])
    put(data, "urban-data")
    shutil.rmtree(work, ignore_errors=True)
    if vm.partitions(path) != parts:
        raise vm.Fail("writing the stranger's file systems changed its partition table")
    return parts


def image_sums(path, parts):
    """sha256 of the whole image and of each partition."""
    whole = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            whole.update(chunk)
    each = {}
    with open(path, "rb") as f:
        for name, p in parts.items():
            f.seek(p["start"] * 512)
            each[name] = hashlib.sha256(f.read(p["size"] * 512)).hexdigest()
    return whole.hexdigest(), each


# ---------------------------------------------------------------- what the unit says about its disks

FACTS = r"""
echo "BOOTDISK $(/usr/local/bin/urban-bootdisk)"
echo "ROOTPART $(/usr/local/bin/urban-bootdisk root)"
for l in esp rootfs-a rootfs-b urban-data data-fs; do echo "URBAN $l $(readlink -e /dev/disk/urban/$l)"; done
echo "URBANDIR $(ls /dev/disk/urban 2>/dev/null | tr '\n' ' ')"
for l in esp rootfs-a rootfs-b urban-data; do echo "PARTLABEL $l $(readlink -e /dev/disk/by-partlabel/$l)"; done
echo "FSLABEL urban-data $(readlink -e /dev/disk/by-label/urban-data)"
for m in /boot/efi /var/lib/urban; do echo "MOUNT $m $(readlink -e "$(findmnt -n -o SOURCE --mountpoint $m)")"; done
echo "RECORD $(tr '\n' ' ' < /run/urban-boot/disk)"
echo "CMDLINE $(cat /proc/cmdline)"
for f in /boot/efi/STRANGER-ESP /var/lib/urban/STRANGER-DATA; do [ -e "$f" ] && echo "FOUND $f"; done
grep -l URBAN-STRANGER /boot/efi/EFI/BOOT/grub.cfg >/dev/null 2>&1 && echo "FOUND the stranger's grub.cfg"
grep -q ' stranger$' /var/lib/urban/ssh/authorized_keys 2>/dev/null && echo "FOUND the stranger's key in authorized_keys"
true
"""


def facts(unit):
    """The unit's disks as it sees them: the block devices (lsblk) and the lines of FACTS."""
    rows = json.loads(unit.ssh("lsblk -J -l -o KNAME,PKNAME,PARTUUID,PARTLABEL,TYPE").stdout)["blockdevices"]
    out = unit.ssh(FACTS).stdout
    f = {"rows": {r["kname"]: r for r in rows}, "urban": {}, "partlabel": {}, "mount": {}, "found": [], "fslabel": ""}
    for line in out.splitlines():
        w = line.split()
        if not w:
            continue
        if w[0] in ("BOOTDISK", "ROOTPART"):
            f[w[0].lower()] = w[1] if len(w) > 1 else ""
        elif w[0] == "URBAN":
            f["urban"][w[1]] = w[2] if len(w) > 2 else ""
        elif w[0] == "URBANDIR":
            f["urbandir"] = sorted(w[1:])
        elif w[0] == "PARTLABEL":
            f["partlabel"][w[1]] = w[2] if len(w) > 2 else ""
        elif w[0] == "FSLABEL":
            f["fslabel"] = w[2] if len(w) > 2 else ""
        elif w[0] == "MOUNT":
            f["mount"][w[1]] = w[2] if len(w) > 2 else ""
        elif w[0] == "RECORD":
            f["record"] = dict(x.split("=", 1) for x in w[1:] if "=" in x)
        elif w[0] == "CMDLINE":
            f["cmdline"] = line[len("CMDLINE "):]
        elif w[0] == "FOUND":
            f["found"].append(line[len("FOUND "):])
    return f


def disk_of(f, dev):
    """The disk a device (a /dev path or a kernel name) lies on: up through lsblk's parents (crypt -> part -> disk)."""
    k = os.path.basename(dev or "")
    for _ in range(5):
        r = f["rows"].get(k)
        if r is None:
            return None
        if r["type"] == "disk":
            return k
        k = r["pkname"]
    return None


def by_partuuid(f, uuids):
    """The disk whose partitions carry exactly these GUIDs (and its kernel name per partition name)."""
    found = {}
    for k, r in f["rows"].items():
        if r["type"] == "part" and (r.get("partuuid") or "").lower() in uuids.values():
            found.setdefault(disk_of(f, k), {})[next(n for n, u in uuids.items() if u == r["partuuid"].lower())] = k
    whole = [d for d, names in found.items() if sorted(names) == sorted(uuids)]
    if len(whole) != 1:
        raise vm.Fail(f"no single disk in the unit carries the partitions {sorted(uuids)} with these GUIDs: {found}")
    return whole[0], found[whole[0]]


def own_disk_holds(f, own, slot, what):
    """The boot disk is the unit's own disk; every /dev/disk/urban link, the root and both mounts are on it."""
    mine, kname = by_partuuid(f, own)
    if f.get("bootdisk") != mine:
        raise vm.Fail(f"{what}: urban-bootdisk says the boot disk is {f.get('bootdisk')!r}, the unit's own disk is {mine}")
    name = f"rootfs-{slot.lower()}"
    if f"urban.bootpart={own[name]}" not in f.get("cmdline", ""):
        raise vm.Fail(f"{what}: GRUB did not pass slot {slot}'s partition GUID {own[name]}: {f.get('cmdline')}")
    if f.get("rootpart") != kname[name]:
        raise vm.Fail(f"{what}: the verified root runs from {f.get('rootpart')!r}, not from {kname[name]} (slot {slot} of the boot disk)")
    if f.get("record", {}).get("DISK") != mine or f["record"].get("PART") != kname[name]:
        raise vm.Fail(f"{what}: the initramfs recorded {f.get('record')}, not {name} = {kname[name]} on {mine}")
    if f.get("urbandir") != sorted(LINKS):
        raise vm.Fail(f"{what}: /dev/disk/urban holds {f.get('urbandir')}, expected {sorted(LINKS)}")
    for n in NAMES:
        if f["urban"].get(n) != f"/dev/{kname[n]}":
            raise vm.Fail(f"{what}: /dev/disk/urban/{n} is {f['urban'].get(n)!r}, not the boot disk's {n} /dev/{kname[n]}")
    data = os.path.basename(f["urban"].get("data-fs", ""))
    row = f["rows"].get(data, {})
    if not (data == kname["urban-data"] or (row.get("type") == "crypt" and row.get("pkname") == kname["urban-data"])):
        raise vm.Fail(f"{what}: /dev/disk/urban/data-fs is {f['urban'].get('data-fs')!r}, neither the boot disk's urban-data "
                      f"partition nor a LUKS volume opened from it ({row})")
    for where, link in (("/boot/efi", "esp"), ("/var/lib/urban", "data-fs")):
        if not f["mount"].get(where) or f["mount"][where] != f["urban"][link]:
            raise vm.Fail(f"{what}: {where} is mounted from {f['mount'].get(where)!r}, not /dev/disk/urban/{link} "
                          f"({f['urban'][link]})")
        if disk_of(f, f["mount"][where]) != mine:
            raise vm.Fail(f"{what}: {where} is not on the boot disk {mine}")
    if f["found"]:
        raise vm.Fail(f"{what}: the stranger's files reached the unit: {f['found']}")
    return mine, kname


def announce(unit, kname):
    """The stranger's partitions announced again, as plugging the stick in does: udev processes them last."""
    devs = " ".join(f"/dev/{k}" for k in sorted(kname.values()))
    unit.ssh(f"udevadm trigger --action=change --settle {devs} && udevadm settle")


def old_lookup_hijacked(unit, f, theirs_kname, stranger, what):
    """The control: the lookups the unit used before step 7 resolve to the stranger's partitions."""
    hijacked = {n: f["partlabel"].get(n) for n in NAMES if f["partlabel"].get(n) == f"/dev/{theirs_kname[n]}"}
    if sorted(hijacked) != sorted(NAMES):
        stacks = unit.ssh(r"for d in /run/udev/links/*by-partlabel*; do echo $d: $(ls $d); done", check=False).stdout
        raise vm.Fail(f"{what}: control failed: the old /dev/disk/by-partlabel links do not all resolve to the stranger's "
                      f"disk {stranger} ({f['partlabel']}), so the test would prove nothing; udev's claims on them: "
                      f"{' | '.join(stacks.split(chr(10)))}")
    label = "the stranger's" if disk_of(f, f["fslabel"]) == stranger else "the unit's"
    return (f"by-partlabel/{{esp,rootfs-a,rootfs-b,urban-data}} -> {', '.join(hijacked[n] for n in NAMES)} on the "
            f"stranger's {stranger}; by-label/urban-data -> {f['fslabel'] or 'nothing'} ({label})")


# ---------------------------------------------------------------- the scenario (G5)

def bootdisk():
    unit = None
    try:
        theirs = stranger_disk(STRANGER)
        before, before_each = image_sums(STRANGER, theirs)
        their_uuids = {n: p["uuid"].lower() for n, p in theirs.items()}
        print(f"   the stranger's USB stick: partitions {', '.join(NAMES)} (ext4 labelled urban-data with an SSH key, a FAT "
              f"ESP with a bogus grub.cfg and a maintenance key), sha256 {before[:16]}...", flush=True)
        try:
            unit = vm.VM("bootdisk", extra_disk=STRANGER, extra_bus="usb")
            own = {n: p["uuid"].lower() for n, p in vm.partitions(unit.disk).items()}
            if set(own.values()) & set(their_uuids.values()):
                raise vm.Fail("the stranger's partition GUIDs are the unit's: not a stranger")
            unit.start()
            line = unit.booted("1", "A")
            vm.sb_line(line, "slot A with the stranger's stick attached")
            said = [l for l in unit.lines[unit.boots[-1]:] if "URBAN-BOOTDISK:" in l]
            f = facts(unit)
            mine, kname = own_disk_holds(f, own, "A", "slot A, the stick attached from power-on")
            stranger, their_kname = by_partuuid(f, their_uuids)
            if stranger == mine:
                raise vm.Fail("the stranger's disk and the boot disk are the same disk")
            if not said or f"on disk {mine}" not in said[-1] or own["rootfs-a"] not in said[-1]:
                raise vm.Fail(f"the initramfs did not say it opened slot A by its GUID on {mine}: {said}")
            print(f"OK the unit booted from its own disk {mine} with the stranger's stick {stranger} attached: "
                  f"{said[-1][said[-1].index('URBAN-BOOTDISK'):]}", flush=True)
            print(f"OK self-check with the stick attached: {line[line.index('URBAN-OS-CHECK'):][:150]}", flush=True)
            # the stick plugged in, as far as udev can tell: the old links go to it, the boot disk's links stay
            announce(unit, their_kname)
            f = facts(unit)
            control = old_lookup_hijacked(unit, f, their_kname, stranger, "slot A")
            print(f"OK control: the old lookups are the stranger's once its partitions are announced: {control}", flush=True)
            own_disk_holds(f, own, "A", "slot A, after the stick's partitions were announced again")
            print(f"OK /dev/disk/urban/{{{','.join(LINKS)}}} -> {', '.join(f['urban'][n] for n in LINKS)}: all on the "
                  f"boot disk {mine}, also after the stick was announced again", flush=True)
            data_type = f["rows"][os.path.basename(f["mount"]["/var/lib/urban"])]["type"]
            print(f"OK /boot/efi from {f['mount']['/boot/efi']} and /var/lib/urban from {f['mount']['/var/lib/urban']} "
                  f"({data_type}), both on {mine}; none of the stranger's files (ESP marker, grub.cfg, data marker, SSH "
                  f"key) on the unit", flush=True)
            r = vm.remote_ssh(unit.ssh_port, STRANGER_KEY, "true", check=False, timeout=120)
            if r.returncode == 0:
                raise vm.Fail("the stranger's SSH key (on the stick's ESP and data partition) logs in as root")
            print("OK the stranger's SSH key is refused (the unit's own maintenance key logs in)", flush=True)

            # the update, with the old rootfs-b link on the stick at that very moment
            unit.put(f"{vm.OUT}/urban-edge-2.raucb", f"{vm.STAGE}/update.raucb")
            announce(unit, their_kname)
            f = facts(unit)
            if f["partlabel"].get("rootfs-b") != f"/dev/{their_kname['rootfs-b']}":
                raise vm.Fail(f"control failed: right before the update /dev/disk/by-partlabel/rootfs-b is "
                              f"{f['partlabel'].get('rootfs-b')!r}, not the stranger's /dev/{their_kname['rootfs-b']}")
            own_disk_holds(f, own, "A", "right before the update")
            try:
                r = unit.ssh(f"urban-unit update {vm.STAGE}/update.raucb", check=False, timeout=2700)
            except subprocess.TimeoutExpired:
                raise vm.Fail("urban-unit update did not finish within 2700 s")
            if "installed into the other slot" not in r.stdout:
                log = unit.ssh("journalctl -u rauc --no-pager -n 25 -o cat", check=False).stdout
                raise vm.Fail(f"urban-unit update failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}\n{log}")
            print(f"OK urban-unit update installed version 2 while by-partlabel/rootfs-b was the stranger's "
                  f"/dev/{their_kname['rootfs-b']}; the unit restarts", flush=True)
            line = unit.booted("2", "B")
            vm.sb_line(line, "slot B after the update, the stick attached")
            booted, by_slot = vm.slots(unit)
            if booted != "B" or by_slot["B"].get("boot_status") != "good":
                raise vm.Fail(f"after the update: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
            devices = {x: by_slot[x].get("device") for x in ("A", "B")}
            if devices != {"A": "/dev/disk/urban/rootfs-a", "B": "/dev/disk/urban/rootfs-b"}:
                raise vm.Fail(f"RAUC's slots are {devices}, not /dev/disk/urban/rootfs-a and -b")
            said = [l for l in unit.lines[unit.boots[-1]:] if "URBAN-BOOTDISK:" in l]
            if not said or f"on disk {mine}" not in said[-1] or own["rootfs-b"] not in said[-1]:
                raise vm.Fail(f"the initramfs did not say it opened slot B by its GUID on {mine}: {said}")
            f = facts(unit)
            own_disk_holds(f, own, "B", "slot B after the update")
            print(f"OK version 2 runs from slot B of the boot disk {mine} ({f['rootpart']}, GUID {own['rootfs-b']}); "
                  f"RAUC's slots are {devices['A']} and {devices['B']}; {line[line.index('URBAN-OS-CHECK'):][:110]}",
                  flush=True)
        finally:
            if unit is not None:
                vm.drop(unit)  # QEMU and the software TPM stopped, the unit's disk removed
        after, after_each = image_sums(STRANGER, theirs)
        changed = [n for n in NAMES if before_each[n] != after_each[n]]
        if after != before:
            raise vm.Fail(f"the stranger's disk was written to (sha256 {before[:16]}... -> {after[:16]}...; changed: "
                          f"{', '.join(changed) or 'outside the partitions (the partition table)'})")
        print(f"OK the stranger's stick is byte for byte unchanged after two boots and an update (sha256 {after})",
              flush=True)
        print("BOOT DISK CHECK PASSED", flush=True)
    finally:
        for path in (STRANGER, STRANGER_KEY, STRANGER_KEY + ".pub"):
            if os.path.exists(path):
                os.remove(path)
