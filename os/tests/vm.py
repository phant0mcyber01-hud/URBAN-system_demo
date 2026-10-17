"""Scenarios for URBAN Edge OS in QEMU, run inside the builder container (see os/tests/lib.mjs).

    python3 vm.py image                 the disk image's layout, without booting it
    python3 vm.py boot                  boots; read-only root, firewall, watchdog, SSH keys only
    python3 vm.py firstboot             unit key and enrolment code on first boot, the same key after a reboot
    python3 vm.py update good|foreign|broken
                                        signed A/B update; a foreign signature; a broken system rolled back
    python3 vm.py hardware              the same image from NVMe, SATA and a USB stick, with an Intel network card
    python3 vm.py roles                 the pilot's district server and control centre on a bigger disk
    python3 vm.py unitimage IMG.xz SUMS KEY UNIT SERIAL
                                        a unit image as shipped: identity and maintenance key from its ESP only

Each scenario starts from a fresh copy of the built image. The serial console is kept in /out/logs/;
only the lines that matter are printed. The last line is the verdict the gate looks for.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

OUT = "/out"
LOGS = f"{OUT}/logs"
KEY = "/tmp/vm-key"
STAGE = "/var/lib/urban/update"  # bundles are staged on the data partition: /tmp is a small tmpfs
BOOT_TIMEOUT = 1500 if os.environ.get("URBAN_KVM") != "1" else 300  # seconds; emulation without KVM is slow
SHOW = re.compile(r"URBAN-|Kernel panic|watchdog|Emergency|Failed to start|rauc|end Kernel")


class Fail(Exception):
    pass


def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise Fail(f"{cmd if isinstance(cmd, str) else ' '.join(cmd)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def partitions(disk):
    table = json.loads(sh(["sfdisk", "-J", disk]).stdout)["partitiontable"]["partitions"]
    return {p["name"]: p for p in table}


def read_at(disk, sector, n):
    with open(disk, "rb") as f:
        f.seek(sector * 512)
        return f.read(n)


# how the unit's disk is attached: the virtual one of the other scenarios, and what mini-PCs really have
DISK_BUS = {
    "virtio": lambda d: ["-drive", f"file={d},format=raw,if=virtio"],
    "nvme": lambda d: ["-drive", f"file={d},format=raw,if=none,id=d0", "-device", "nvme,drive=d0,serial=URBAN0001"],
    "sata": lambda d: ["-drive", f"file={d},format=raw,if=none,id=d0", "-device", "ide-hd,drive=d0,bus=ide.0"],
    "usb": lambda d: ["-drive", f"file={d},format=raw,if=none,id=d0", "-device", "qemu-xhci,id=xhci",
                      "-device", "usb-storage,drive=d0,bus=xhci.0,removable=on"],
}


class VM:
    def __init__(self, name, version="1", unit="edge-J-07", serial="URB-TEST", image=None, key=KEY,
                 add_key=True, bus="virtio", nic="virtio-net-pci", role=None, host=None, grow_mb=0, forward=()):
        os.makedirs(LOGS, exist_ok=True)
        self.name, self.disk, self.key, self.bus, self.nic = name, f"/tmp/{name}.img", key, bus, nic
        self.forward = forward  # more (host port, guest port) pairs than SSH
        if image:  # a finished unit image: used as it is, nothing added
            sh(["mv", image, self.disk])
            unit, add_key = None, False
        else:
            sh(["cp", "--sparse=always", f"{OUT}/urban-edge-{version}.img", self.disk])
        parts = partitions(self.disk)
        esp, data = parts["esp"]["start"], parts["urban-data"]["start"]
        if unit:  # the installer's step: the unit id and serial on the ESP
            with open("/tmp/urban-unit.txt", "w", newline="\r\n") as f:  # written on a Windows laptop: CRLF
                f.write(f"UNIT={unit}\nSERIAL={serial}\n" + (f"ROLE={role}\n" if role else "") + (f"HOST={host}\n" if host else ""))
            sh(f"mcopy -o -i {self.disk}@@{esp * 512} /tmp/urban-unit.txt ::/urban-unit.txt")
        if not os.path.exists(KEY):  # a maintenance key on the data partition, for the scenarios only
            sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", KEY])
        if add_key:
            size = parts["urban-data"]["size"]
            sh(f"dd if={self.disk} of=/tmp/data.part bs=512 skip={data} count={size} status=none")
            sh(["debugfs", "-w", "-R", "mkdir ssh", "/tmp/data.part"])
            sh(["debugfs", "-w", "-R", f"write {KEY}.pub ssh/authorized_keys", "/tmp/data.part"])
            sh(f"dd if=/tmp/data.part of={self.disk} bs=512 seek={data} conv=notrunc status=none")
        if grow_mb:  # a disk bigger than the image, as every real one is
            sh(["truncate", "-s", f"+{grow_mb}M", self.disk])
        self.lines, self.mark, self.proc = [], 0, None
        self.log = open(f"{LOGS}/{name}.log", "w", encoding="utf-8", errors="replace")

    def start(self):
        accel = ["-enable-kvm", "-cpu", "host"] if os.environ.get("URBAN_KVM") == "1" else ["-cpu", "max"]
        vars_ = f"/tmp/{self.name}-vars.fd"  # firmware variables of this machine, like a real board's NVRAM
        if not os.path.exists(vars_):
            shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", vars_)
        self.proc = subprocess.Popen(
            ["qemu-system-x86_64", "-machine", "q35", "-m", "2048", "-smp", "2", *accel,
             "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
             "-drive", f"if=pflash,format=raw,unit=1,file={vars_}",
             *DISK_BUS[self.bus](self.disk),
             "-netdev", "user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22"
             + "".join(f",hostfwd=tcp:127.0.0.1:{h}-:{g}" for h, g in self.forward), "-device", f"{self.nic},netdev=n0",
             "-device", "i6300esb", "-watchdog-action", "reset",
             "-display", "none", "-serial", "stdio", "-monitor", f"unix:/tmp/{self.name}.mon,server,nowait",
             "-name", self.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.proc.stdout.readline, b""):
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", "replace")).rstrip()
            self.lines.append(line)
            self.log.write(line + "\n")
            self.log.flush()
            if SHOW.search(line):
                print(f"   vm| {line[:200]}", flush=True)

    def wait(self, pattern, timeout=BOOT_TIMEOUT):
        """The first line matching `pattern` after the mark; the mark moves past it."""
        rx, end = re.compile(pattern), time.time() + timeout
        while time.time() < end:
            for i in range(self.mark, len(self.lines)):
                m = rx.search(self.lines[i])
                if m:
                    self.mark = i + 1
                    return m
            if self.proc.poll() is not None:
                raise Fail(f"the VM stopped while waiting for {pattern!r}")
            time.sleep(1)
        raise Fail(f"no {pattern!r} within {timeout} s (log: {LOGS}/{self.name}.log)")

    def booted(self, version, slot):
        m = self.wait(r"URBAN-OS-CHECK (OK|FAIL) version=(\S+) slot=(\S+)")
        line = self.lines[self.mark - 1]
        if m.group(1) != "OK" or m.group(2) != version or m.group(3).rstrip(":") != slot:
            raise Fail(f"expected a healthy version {version} in slot {slot}, got: {line}")
        self.wait(rf"URBAN-HEALTH good slot={slot}", 300)
        self.wait_ssh()
        return line

    def wait_ssh(self, timeout=300):
        end, last = time.time() + timeout, ""
        while time.time() < end:
            r = self.ssh("true", check=False)
            if r.returncode == 0:
                return
            last = (r.stderr or r.stdout).strip()
            time.sleep(3)
        raise Fail(f"SSH did not come up: {last}")

    def ssh(self, cmd, check=True):
        return sh(["ssh", "-i", self.key, "-p", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "root@127.0.0.1", cmd], check=check, timeout=600)

    def put(self, src, dst):
        sh(["scp", "-i", self.key, "-P", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", src, f"root@127.0.0.1:{dst}"], timeout=900)

    def install(self, bundle):
        """rauc install on the unit; on failure the reason from RAUC's own log."""
        r = self.ssh(f"rauc install {bundle}", check=False)
        if r.returncode != 0:
            log = self.ssh("journalctl -u rauc --no-pager -n 25 -o cat", check=False).stdout
            raise Fail("rauc install failed: " + r.stderr.strip() + "\n" + log)
        return r

    def screendump(self, path):
        """What the unit's own monitor shows (the VGA console), as a PPM picture."""
        import socket as _socket
        with _socket.socket(_socket.AF_UNIX) as mon:
            mon.connect(f"/tmp/{self.name}.mon")
            mon.sendall(("screendump " + path + chr(10)).encode())
            time.sleep(2)

    def reboot(self):
        self.ssh("systemctl reboot", check=False)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()


# ---------------------------------------------------------------- scenarios

def image():
    disk = f"{OUT}/urban-edge-1.img"
    parts = partitions(disk)
    names = ["esp", "rootfs-a", "rootfs-b", "urban-data"]
    if list(parts) != names:
        raise Fail(f"partitions {list(parts)}, expected {names}")
    if parts["esp"]["type"] != "C12A7328-F81F-11D2-BA4B-00A0C93EC93B":
        raise Fail("the first partition is not an EFI system partition")
    if read_at(disk, parts["rootfs-a"]["start"], 4) != b"hsqs":
        raise Fail("slot A does not hold a squashfs root")
    if read_at(disk, parts["rootfs-b"]["start"], 4096).strip(b"\0"):
        raise Fail("slot B is not empty in a fresh image")
    esp = parts["esp"]["start"] * 512
    files = sh(f"mdir -/ -b -i {disk}@@{esp} ::/").stdout
    for need in ("::/EFI/BOOT/BOOTX64.EFI", "::/EFI/BOOT/grub.cfg", "::/EFI/BOOT/grubenv", "::/urban-unit.txt"):
        if need not in files:
            raise Fail(f"ESP lacks {need}")
    env = sh(f"mtype -i {disk}@@{esp} ::/EFI/BOOT/grubenv").stdout
    if "A_OK=1" not in env or "B_OK=0" not in env:
        raise Fail(f"boot state is not 'slot A good, slot B empty': {env!r}")
    data = parts["urban-data"]
    sh(f"dd if={disk} of=/tmp/data.part bs=512 skip={data['start']} count={data['size']} status=none")
    label = sh(["dumpe2fs", "-h", "/tmp/data.part"]).stdout
    if "urban-data" not in label:
        raise Fail("the data partition is not the ext4 file system urban-data")
    with open("/tmp/verify.conf", "w") as f:  # the unit's own trust: the URBAN update CA, code-signing purpose
        f.write("[system]\ncompatible=urban-edge-x86_64\nbootloader=noop\n"
                f"[keyring]\npath={OUT}/keys/ca.pem\ncheck-purpose=codesign\n")
    info = sh(["rauc", "--conf", "/tmp/verify.conf", "info", "--output-format=json", f"{OUT}/urban-edge-1.raucb"]).stdout
    if json.loads(info).get("compatible") != "urban-edge-x86_64":
        raise Fail("the update bundle is not for urban-edge-x86_64")
    contents()
    print("IMAGE LAYOUT VERIFIED", flush=True)


def contents():
    """What a real mini-PC needs from the system itself: network firmware, microcode, watchdog drivers."""
    slot = f"{OUT}/build/rootfs-1.img"
    listing = sh(["unsquashfs", "-l", slot]).stdout
    need = {
        "Realtek network firmware (RTL8125/8168)": r"lib/firmware/rtl_nic/rtl8125",
        "Intel CPU microcode": r"lib/firmware/intel-ucode/",
        "AMD CPU microcode": r"lib/firmware/amd-ucode/",
        "Intel board watchdog driver": r"/iTCO_wdt\.ko",
        "AMD board watchdog driver": r"/sp5100_tco\.ko",
        "Intel network driver": r"/e1000e\.ko",
        "NVMe driver": r"/nvme\.ko",
        "USB storage driver": r"/usb-storage\.ko",
    }
    for what, rx in need.items():
        if not re.search(rx, listing):
            raise Fail(f"the system lacks {what}")
    shutil.rmtree("/tmp/sq", ignore_errors=True)
    sh(["unsquashfs", "-q", "-n", "-d", "/tmp/sq", slot, "etc/initramfs-tools/modules", "etc/modprobe.d/urban-console.conf",
        "boot/initrd.img"])
    early = open("/tmp/sq/etc/initramfs-tools/modules").read().split()
    for m in ("iTCO_wdt", "sp5100_tco", "i6300esb"):
        if m not in early:
            raise Fail(f"the watchdog driver {m} is not loaded before systemd starts")
    if "blacklist amdgpu" not in open("/tmp/sq/etc/modprobe.d/urban-console.conf").read():
        raise Fail("AMD graphics would take the console over and leave it black")
    initrd = open("/tmp/sq/boot/initrd.img", "rb").read()
    for cpu in (b"GenuineIntel.bin", b"AuthenticAMD.bin"):
        if b"kernel/x86/microcode/" + cpu not in initrd:
            raise Fail(f"no early microcode for {cpu.decode()} in the initramfs")
    print("   firmware: Realtek network, Intel and AMD microcode (early), watchdogs iTCO/sp5100/i6300esb", flush=True)
    roles_in(slot, listing)


def roles_in(slot, listing):
    """One image for the three roles: the power manager everywhere, the unit's and vision's services, the
    control centre with its built interface and its data on the data partition, the role's firewall openings."""
    need = {
        "the power manager": "opt/urban/backend/urban/node/power.py",
        "the power service, started at every boot": "etc/systemd/system/multi-user.target.wants/urban-power.service",
        "the unit's service": "etc/systemd/system/urban-edge@.service",
        "the vision service": "etc/systemd/system/urban-vision@.service",
        "the control centre's service": "etc/systemd/system/urban-platform.service",
        "the control centre's interface": "opt/urban/web/dist/index.html",
    }
    for what, path in need.items():
        if not re.search(r"(^|/)" + re.escape(path) + r"$", listing, re.M):
            raise Fail(f"the image lacks {what} ({path})")
    links = sh(["unsquashfs", "-lls", slot, "opt/urban/data"]).stdout
    if "-> /var/lib/urban/platform" not in links:
        raise Fail("the control centre's data does not live on the data partition")
    shutil.rmtree("/tmp/sq2", ignore_errors=True)
    sh(["unsquashfs", "-q", "-n", "-d", "/tmp/sq2", slot, "etc/nftables.conf", "usr/local/bin/urban-prepare",
        "opt/urban/web/dist/index.html"])
    if 'include "/run/urban/nft/*.nft"' not in open("/tmp/sq2/etc/nftables.conf").read():
        raise Fail("the firewall does not take the role's openings")
    prepare = open("/tmp/sq2/usr/local/bin/urban-prepare").read()
    for role in ("unit)", "district)", "center|district,center"):
        if role not in prepare:
            raise Fail(f"urban-prepare does not know the role {role}")
    if "<html" not in open("/tmp/sq2/opt/urban/web/dist/index.html").read().lower():
        raise Fail("the interface in the image is not a page")
    print("   roles: unit, district, center in one image; power manager, control centre and its interface", flush=True)


def boot():
    vm = VM("boot")
    try:
        vm.start()
        line = vm.booted("1", "A")
        for need in ("root=ro", "data=rw", "firewall=on"):
            if need not in line:
                raise Fail(f"self-check line lacks {need}: {line}")
        # from outside: the root refuses writes, passwords are refused, the firewall drops by default
        r = vm.ssh("touch /etc/urban-write-test", check=False)
        if r.returncode == 0 or "Read-only" not in r.stderr:
            raise Fail("the root file system accepted a write")
        r = sh(["ssh", "-p", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "root@127.0.0.1", "true"], check=False)
        if r.returncode == 0 or "publickey" not in r.stderr:
            raise Fail(f"SSH offered something other than keys: {r.stderr.strip()}")
        rules = vm.ssh("nft list chain inet urban input").stdout
        if "policy drop" not in rules:
            raise Fail("the input chain does not drop by default")
        # armed means systemd (PID 1) holds the watchdog device open and keeps feeding it
        held = vm.ssh("ls -l /proc/1/fd 2>/dev/null | grep -c watchdog; systemctl show -p RuntimeWatchdogUSec --value").stdout.split()
        if not held or held[0] == "0" or len(held) < 2 or held[1] in ("0", "infinity"):
            raise Fail(f"the hardware watchdog is not held by systemd: {held}")
        print(f"   {line}")
        print("BOOT CHECK PASSED", flush=True)
    finally:
        vm.stop()


def check_code(code, fingerprint, unit="edge-J-07", serial="URB-TEST"):
    parts = code.split(":")
    if len(parts) != 5 or parts[0] != "URBAN1" or parts[1] != unit or parts[2] != serial:
        raise Fail(f"enrolment code has the wrong shape: {code}")
    if hashlib.sha256(":".join(parts[:4]).encode()).hexdigest()[:6].upper() != parts[4]:
        raise Fail("enrolment code check sum does not match")
    fp = hashlib.sha256(bytes.fromhex(parts[3])).hexdigest()[:16].upper()
    if "-".join(fp[i:i + 4] for i in range(0, 16, 4)) != fingerprint:
        raise Fail("the printed fingerprint is not the fingerprint of the key in the code")


def firstboot():
    bare = VM("firstboot-noid", unit=None)  # control: without an identity there is no key and the unit says why
    try:
        bare.start()
        bare.wait(r"URBAN-ENROLMENT none")
        line = bare.wait(r"URBAN-OS-CHECK (OK|FAIL)").group(0)
        if "OK" not in line:
            raise Fail(f"an unprovisioned unit should still boot healthy: {line}")
        if bare.ssh("ls /var/lib/urban/etc/pki/edge/", check=False).stdout.strip():
            raise Fail("a key appeared without an identity")
    finally:
        bare.stop()

    vm = VM("firstboot")
    try:
        vm.start()
        m = vm.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+)")
        unit, fp, code = m.group(1), m.group(3), m.group(4)
        check_code(code, fp)
        vm.booted("1", "A")
        mode = vm.ssh(f"stat -c '%a %U' /var/lib/urban/etc/pki/edge/{unit}.key").stdout.split()
        if mode != ["600", "urban"]:
            raise Fail(f"the unit key is not private to the unit's service: {mode}")
        vm.reboot()
        m2 = vm.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+)")
        if m2.group(3) != fp:
            raise Fail(f"the key changed across a reboot: {fp} -> {m2.group(3)}")
        vm.booted("1", "A")
        print(f"   unit {unit}, key {fp}, code {code[:28]}...")
        print("FIRSTBOOT CHECK PASSED", flush=True)
    finally:
        vm.stop()


def disk_and_nic(vm):
    """How the running unit sees its own hardware: the bus of its disk and the drivers of its network cards."""
    tran = vm.ssh("lsblk -ndo TRAN /dev/$(lsblk -ndo PKNAME /dev/disk/by-partlabel/urban-data)").stdout.strip()
    nics = vm.ssh("for d in /sys/class/net/*/device/driver; do basename $(readlink $d); done").stdout.split()
    return tran, nics


def hardware():
    for bus in ("nvme", "sata", "usb"):
        vm = VM(f"hw-{bus}", bus=bus, nic="e1000e")
        try:
            vm.start()
            line = vm.booted("1", "A")
            tran, nics = disk_and_nic(vm)
            if tran != bus or "e1000e" not in nics:
                raise Fail(f"{bus}: the unit sees disk bus {tran!r} and network drivers {nics}")
            print(f"   {bus}: disk on {tran}, network {','.join(nics)}; {line[line.index('URBAN-OS-CHECK'):][:90]}", flush=True)
        finally:
            vm.stop()
            os.remove(vm.disk)
    print("HARDWARE CHECK PASSED", flush=True)


# a stand-in for the kit the platform hands out (Устройства -> Комплект): a made-up platform with its own CA,
# made on the unit for the unit's own key, by the same code the platform uses
STAND_KIT = r"""set -e
mkdir -p /tmp/pki
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout /tmp/pki/ca.key -out /tmp/pki/ca.pem   -days 30 -subj /CN=stand-platform 2>/dev/null
/opt/urban/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from urban.devices import crypto
from urban.devices.enrolment import make_kit
key = crypto.load_private(Path("/var/lib/urban/etc/pki/edge/UNIT.key").read_text())
Path("/tmp/pki/root.pub").write_text(crypto.public_hex(crypto.new_key()))
rec = {"id": "UNIT", "junction": "J-07", "name": "stand", "public_key": crypto.public_hex(key)}
kit = make_kit(rec, Path("/tmp/pki"), "platform.invalid", 8443)
Path("/var/lib/urban/update/kit.json").write_text(json.dumps(kit))
PY
"""


def running(vm, unit):
    return vm.ssh(f"systemctl show -p ActiveState --value urban-edge@{unit}").stdout.strip() in ("active", "activating")


def unitimage(xz, sums, key, unit, serial):
    """The file a person writes onto a mini-PC: unpacked, compared with its published checksums, booted from
    NVMe with an Intel network card. Its name and maintenance key come from its ESP, nothing is added here."""
    name = os.path.basename(xz)[:-3]
    img = f"/tmp/{name}"
    sh(["bash", "-c", f"set -o pipefail; xz -dc {xz} | cp --sparse=always /dev/stdin {img}"])
    want = dict(reversed(line.split(maxsplit=1)) for line in open(sums).read().splitlines() if line.strip())
    for path, label in ((xz, os.path.basename(xz)), (img, name)):
        got = sh(["sha256sum", path]).stdout.split()[0]
        if want.get(label) != got:
            raise Fail(f"{label}: sha256 {got} is not the published {want.get(label)}")
    parts = partitions(img)
    esp, data = parts["esp"]["start"] * 512, parts["urban-data"]
    said = sh(f"mtype -i {img}@@{esp} ::/urban-unit.txt").stdout
    if f"UNIT={unit}" not in said or f"SERIAL={serial}" not in said:
        raise Fail(f"the ESP does not name the unit: {said!r}")
    sh(f"mtype -i {img}@@{esp} ::/maintenance.pub")
    sh(f"dd if={img} of=/tmp/data.part bs=512 skip={data['start']} count={data['size']} status=none")
    if "authorized_keys" in sh(["debugfs", "-R", "ls -l ssh", "/tmp/data.part"], check=False).stdout:
        raise Fail("the data partition already holds keys: the image was not made from the ESP alone")
    sh(["install", "-m", "600", key, "/tmp/maintenance"])
    vm = VM("unit", image=img, key="/tmp/maintenance", bus="nvme", nic="e1000e")
    try:
        vm.start()
        m = vm.wait(r"URBAN-MAINTENANCE-KEYS added=(\d+) total=(\d+)")
        if m.group(1) != "1":
            raise Fail(f"maintenance keys from the ESP: {m.group(0)}")
        m = vm.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+)")
        check_code(m.group(4), m.group(3), unit, serial)
        line = vm.booted("1", "A")
        r = sh(["ssh", "-i", KEY, "-p", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "root@127.0.0.1", "true"], check=False)
        if r.returncode == 0:
            raise Fail("a key that was never put on the unit was let in")
        esp_now = vm.ssh("ls /boot/efi").stdout.split()
        if "maintenance.pub" in esp_now or "maintenance.pub.imported" not in esp_now:
            raise Fail(f"the key file on the ESP was not retired after import: {esp_now}")
        screen = vm.ssh("cat /run/urban/console.issue").stdout
        if m.group(4) not in screen or "1 key(s) allowed" not in screen:
            raise Fail("the unit's screen does not show its code and its maintenance key")
        time.sleep(3)
        vm.screendump(f"{LOGS}/unit-screen.ppm")
        print(f"   {unit} {serial}: key {m.group(3)}, other keys refused; {line[line.index('URBAN-OS-CHECK'):][:80]}")
        # the installer's last step: the kit goes in and the unit's service runs, now and after every reboot,
        # without writing into the read-only system
        vm.ssh(STAND_KIT.replace("UNIT", unit))
        r = vm.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
        if r.returncode != 0:
            raise Fail(f"urban-unit setup: {(r.stderr or r.stdout).strip()[-300:]}")
        if not running(vm, unit):
            raise Fail(f"urban-edge@{unit} is not running after setup")
        vm.reboot()
        vm.wait(rf"URBAN-UNIT starting urban-edge@{unit}")
        vm.booted("1", "A")
        if not running(vm, unit):
            raise Fail(f"urban-edge@{unit} did not come back after a reboot")
        print(f"   kit set up; urban-edge@{unit} runs, also after a reboot", flush=True)
        plan = json.loads(vm.ssh("cat /run/urban/power.json").stdout)
        if plan["roles"] != ["unit"] or plan["vision"]["threads"] > 2:
            raise Fail(f"a unit's power plan: {plan}")
        print(f"   power: {plan['level']}, clock {plan['clock_pct']}%, vision {plan['vision']['fps']} fps", flush=True)
        print("UNIT IMAGE CHECK PASSED", flush=True)
    finally:
        vm.stop()
        os.remove(vm.disk)


def roles():
    """The pilot's district server and control centre: one machine, ROLE=district,center, on a disk 6 GB bigger
    than the image. From outside: the interface over HTTPS, trusted through the centre's own CA."""
    import ssl
    import urllib.request
    vm = VM("roles", unit="center", serial="PILOT-1", role="district,center", host="urban-center", grow_mb=6144,
            forward=[(2443, 443)])
    try:
        vm.start()
        m = vm.wait(r"URBAN-DATA (\d+) MiB")
        if int(m.group(1)) < 7000:
            raise Fail(f"the data partition did not take the disk: {m.group(0)}")
        vm.wait(r"URBAN-ROLE role=district,center name=center")
        line = vm.booted("1", "A")
        power = vm.wait(r"URBAN-POWER roles=district,center level=\S+.*", 300).group(0)
        plan = json.loads(vm.ssh("cat /run/urban/power.json").stdout)
        if plan["roles"] != ["district", "center"] or plan["vision"]["threads"] != max(1, plan["cores"] - 2):
            raise Fail(f"power plan for the role: {plan}")
        rules = vm.ssh("nft list chain inet urban input").stdout
        if not re.search(r"tcp dport \{ 443, 8443 \} accept", rules) or "policy drop" not in rules:
            raise Fail(f"the firewall does not open exactly the centre's ports:\n{rules}")
        end, body, last = time.time() + 900, None, ""
        while time.time() < end and body is None:
            ca = vm.ssh("cat /var/lib/urban/platform/pki/ca.pem", check=False).stdout  # once the platform ran
            if "BEGIN CERTIFICATE" in ca:
                with open("/tmp/center-ca.pem", "w") as f:
                    f.write(ca)
                ctx = ssl.create_default_context(cafile="/tmp/center-ca.pem")
                ctx.check_hostname = False  # the forwarded port is 127.0.0.1; the name is checked below
                try:
                    with urllib.request.urlopen("https://127.0.0.1:2443/api/health", context=ctx, timeout=20) as r:
                        body = json.loads(r.read())
                except Exception as err:  # still starting
                    last = str(err)
            if body is None:
                time.sleep(10)
        if body is None or not body.get("ok"):
            log = vm.ssh("journalctl -u urban-platform --no-pager -n 30 -o cat", check=False).stdout
            raise Fail(f"the control centre did not answer over HTTPS: {last}\n{log}")
        names = sh(["openssl", "x509", "-noout", "-ext", "subjectAltName"],
                   input=vm.ssh("cat /var/lib/urban/platform/pki/server.pem").stdout).stdout
        if "urban-center" not in names:
            raise Fail(f"the certificate does not name the centre: {names}")
        page = None
        with urllib.request.urlopen("https://127.0.0.1:2443/", context=ctx, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
        if "<html" not in page.lower():
            raise Fail("the interface itself is not served")
        print(f"   {line[line.index('URBAN-OS-CHECK'):][:90]}")
        print(f"   {power[:160]}")
        print(f"   https://urban-center/ answers (mode {body.get('mode')}), interface served; data partition {m.group(1)} MiB",
              flush=True)
        time.sleep(3)
        vm.screendump(f"{LOGS}/center-screen.ppm")
        print("ROLES CHECK PASSED", flush=True)
    finally:
        vm.stop()
        os.remove(vm.disk)


def slots(vm):
    st = json.loads(vm.ssh("rauc status --output-format=json").stdout)
    return st["booted"], {s["bootname"]: s for slot in st["slots"] for s in slot.values()}


def update(kind):
    vm = VM(f"update-{kind}")
    try:
        vm.start()
        vm.booted("1", "A")
        if kind == "good":
            vm.put(f"{OUT}/urban-edge-2.raucb", f"{STAGE}/update.raucb")
            vm.install(f"{STAGE}/update.raucb")
            vm.reboot()
            line = vm.booted("2", "B")
            booted, by_slot = slots(vm)
            if booted != "B" or by_slot["B"].get("boot_status") != "good":
                raise Fail(f"after the update: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
            print(f"   {line}")
            print("UPDATE CHECK PASSED", flush=True)
        elif kind == "foreign":
            vm.put(f"{OUT}/urban-edge-2.raucb", f"{STAGE}/genuine.raucb")
            if vm.ssh(f"rauc info {STAGE}/genuine.raucb", check=False).returncode != 0:
                raise Fail("control failed: the unit does not accept its own release's signature")
            vm.put(f"{OUT}/foreign-2.raucb", f"{STAGE}/foreign.raucb")
            r = vm.ssh(f"rauc install {STAGE}/foreign.raucb", check=False)
            if r.returncode == 0:
                raise Fail("a bundle signed with a foreign key was installed")
            env = vm.ssh("grub-editenv /boot/efi/EFI/BOOT/grubenv list").stdout
            booted, _ = slots(vm)
            if "B_OK=0" not in env or booted != "A":
                raise Fail(f"the refused bundle still changed the boot state: {env.split()}")
            said = [l.strip() for l in (r.stderr + r.stdout).splitlines() if l.strip()]
            reason = next((l for l in reversed(said) if "fail" in l.lower() or "error" in l.lower()), said[-1] if said else "")
            print(f"   refused: {reason[:160]}")
            print("FOREIGN BUNDLE REFUSED", flush=True)
        elif kind == "broken":
            vm.put(f"{OUT}/urban-edge-3.raucb", f"{STAGE}/update.raucb")
            vm.install(f"{STAGE}/update.raucb")
            vm.reboot()
            m = vm.wait(r"URBAN-OS-CHECK (OK|FAIL) version=(\S+) slot=(\S+)")
            if m.group(1) != "FAIL" or m.group(2) != "3":
                raise Fail(f"the broken system did not fail its self-check: {vm.lines[vm.mark - 1]}")
            print(f"   {vm.lines[vm.mark - 1][:200]}")
            vm.wait(r"URBAN-HEALTH bad slot=B", 300)
            line = vm.booted("1", "A")  # back on the previous system without anyone touching the unit
            booted, by_slot = slots(vm)
            if booted != "A" or by_slot["B"].get("boot_status") != "bad":
                raise Fail(f"after the rollback: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
            print(f"   {line}")
            print("ROLLBACK CHECK PASSED", flush=True)
        else:
            raise Fail(f"unknown update scenario {kind}")
    finally:
        vm.stop()


def foreign_bundle():
    """The same system as version 2, signed by someone who is not URBAN's release key."""
    k = "/tmp/foreign"
    shutil.rmtree(k, ignore_errors=True)
    os.makedirs(f"{k}/bundle")
    sh(f"openssl req -x509 -newkey rsa:2048 -nodes -keyout {k}/key.pem -out {k}/cert.pem -days 30 -subj /CN=not-urban")
    shutil.copy(f"{OUT}/build/rootfs-2.img", f"{k}/bundle/rootfs.img")
    shutil.copy("/work/os/tests/foreign.raucm", f"{k}/bundle/manifest.raucm")
    if os.path.exists(f"{OUT}/foreign-2.raucb"):
        os.remove(f"{OUT}/foreign-2.raucb")
    sh(["rauc", "bundle", "--cert", f"{k}/cert.pem", "--key", f"{k}/key.pem", f"{k}/bundle", f"{OUT}/foreign-2.raucb"])


if __name__ == "__main__":
    try:
        what = sys.argv[1]
        if what == "image":
            image()
        elif what == "boot":
            boot()
        elif what == "firstboot":
            firstboot()
        elif what == "screen":  # the unit as a person sees it: its console screen and its maintenance command
            vm = VM("screen")
            try:
                vm.start()
                vm.wait(r"URBAN-OS-CHECK (OK|FAIL)")
                vm.wait(r"URBAN-HEALTH", 300)
                vm.wait_ssh()
                time.sleep(5)
                vm.screendump(f"{LOGS}/screen.ppm")
                print("--- urban-unit status ---")
                print(vm.ssh("urban-unit status", check=False).stdout)
            finally:
                vm.stop()
        elif what == "shell":  # debugging: boot version 1, run one command as root, print what it says
            vm = VM("shell")
            try:
                vm.start()
                vm.wait(r"URBAN-OS-CHECK (OK|FAIL)")
                vm.wait_ssh()
                r = vm.ssh(" ".join(sys.argv[2:]), check=False)
                print(r.stdout + r.stderr)
            finally:
                vm.stop()
        elif what == "roles":
            roles()
        elif what == "hardware":
            hardware()
        elif what == "unitimage":
            unitimage(*sys.argv[2:7])
        elif what == "update":
            if sys.argv[2] == "foreign":
                foreign_bundle()
            update(sys.argv[2])
        else:
            raise Fail(f"unknown scenario {what}")
    except Fail as err:
        print(f"FAILED: {err}", flush=True)
        sys.exit(1)
