"""Scenarios for URBAN Edge OS in QEMU, run inside the builder container (see os/tests/lib.mjs).

    python3 vm.py image                 the disk image's layout, without booting it
    python3 vm.py boot                  boots; read-only root, firewall, watchdog, SSH keys only
    python3 vm.py firstboot             unit key and enrolment code on first boot, the same key after a reboot
    python3 vm.py update good|foreign|broken
                                        signed A/B update; a foreign signature; a broken system rolled back
    python3 vm.py hardware              the same image from NVMe, SATA and a USB stick, with an Intel network card
    python3 vm.py roles                 the pilot's district server and control centre on a bigger disk
    python3 vm.py encrypted             with a TPM the data partition becomes LUKS2 (key in the TPM); without, plain
    python3 vm.py light                 a light unit (VISION=off) in 1 GB of memory
    python3 vm.py chain                 unit -> district -> centre: three machines, the relay, the district's reports
    python3 vm.py demo [pilot]          the three machines (or the pilot's one) kept running, screens over VNC
    python3 vm.py demo-enrol            enrol the demo's district and unit at its centre, by the centre's CLI
    python3 vm.py demo-kit FILE DEVICE  put a kit downloaded from the demo's centre onto that machine
    python3 vm.py unitimage IMG.xz SUMS KEY UNIT SERIAL
                                        a unit image as shipped: identity and maintenance key from its ESP only

Each scenario starts from a fresh copy of the built image, with a software TPM (swtpm) unless told otherwise,
as a real mini-PC has one. The serial console is kept in /out/logs/;
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
                 add_key=True, bus="virtio", nic="virtio-net-pci", role=None, host=None, grow_mb=0, forward=(),
                 listen="127.0.0.1", vnc=None, ssh_port=2222, mem=2048, tpm=True, center=None, vision=None):
        os.makedirs(LOGS, exist_ok=True)
        self.name, self.disk, self.key, self.bus, self.nic = name, f"/tmp/{name}.img", key, bus, nic
        self.forward = forward  # more (host port, guest port) pairs than SSH
        self.listen, self.vnc = listen, vnc  # where those ports listen; VNC display number for the screen, or none
        self.ssh_port, self.mem, self.tpm = ssh_port, mem, tpm
        self.swtpm = None
        if image:  # a finished unit image: used as it is, nothing added
            sh(["mv", image, self.disk])
            unit, add_key = None, False
        else:
            sh(["cp", "--sparse=always", f"{OUT}/urban-edge-{version}.img", self.disk])
        parts = partitions(self.disk)
        esp, data = parts["esp"]["start"], parts["urban-data"]["start"]
        if unit:  # the installer's step: the unit id and serial on the ESP
            with open("/tmp/urban-unit.txt", "w", newline="\r\n") as f:  # written on a Windows laptop: CRLF
                f.write(f"UNIT={unit}\nSERIAL={serial}\n" + (f"ROLE={role}\n" if role else "") + (f"HOST={host}\n" if host else "")
                        + (f"CENTER={center}\n" if center else "") + (f"VISION={vision}\n" if vision else ""))
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

    def _tpm(self) -> list[str]:
        """A software TPM 2.0 of this machine's own (its state survives reboots of the VM, as a chip's would)."""
        d = f"/tmp/{self.name}-tpm"
        os.makedirs(d, exist_ok=True)
        self.swtpm = subprocess.Popen(["swtpm", "socket", "--tpmstate", f"dir={d}", "--ctrl", f"type=unixio,path={d}/sock",
                                       "--tpm2", "--log", "level=0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(f"{d}/sock"):
                break
            time.sleep(0.1)
        return ["-chardev", f"socket,id=chrtpm,path={d}/sock", "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
                "-device", "tpm-tis,tpmdev=tpm0"]

    def start(self):
        accel = ["-enable-kvm", "-cpu", "host"] if os.environ.get("URBAN_KVM") == "1" else ["-cpu", "max"]
        vars_ = f"/tmp/{self.name}-vars.fd"  # firmware variables of this machine, like a real board's NVRAM
        if not os.path.exists(vars_):
            shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", vars_)
        self.proc = subprocess.Popen(
            ["qemu-system-x86_64", "-machine", "q35", "-m", str(self.mem), "-smp", "2", *accel,
             "-drive", "if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
             "-drive", f"if=pflash,format=raw,unit=1,file={vars_}",
             *DISK_BUS[self.bus](self.disk),
             *(self._tpm() if self.tpm else []),
             "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22"
             + "".join(f",hostfwd=tcp:{self.listen}:{h}-:{g}" for h, g in self.forward), "-device", f"{self.nic},netdev=n0",
             "-device", "i6300esb", "-watchdog-action", "reset",
             *(["-vnc", f"127.0.0.1:{self.vnc}"] if self.vnc is not None else ["-display", "none"]),
             "-serial", "stdio", "-monitor", f"unix:/tmp/{self.name}.mon,server,nowait",
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
        return remote_ssh(self.ssh_port, self.key, cmd, check)

    def put(self, src, dst):
        remote_put(self.ssh_port, self.key, src, dst)

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

    def power_cycle(self):
        """Switched off and on again, as a cabinet after a power cut: a clean poweroff, then a new start of the same
        machine (its disk, firmware variables and TPM state are kept)."""
        self.ssh("systemctl poweroff", check=False)
        try:
            self.proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.swtpm and self.swtpm.poll() is None:
            self.swtpm.kill()
            self.swtpm.wait()
        self.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.swtpm and self.swtpm.poll() is None:
            self.swtpm.kill()
            self.swtpm.wait()
        self.log.close()


def remote_ssh(port, key, cmd, check=True):
    return sh(["ssh", "-i", key, "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "root@127.0.0.1", cmd], check=check, timeout=600)


def remote_put(port, key, src, dst):
    sh(["scp", "-i", key, "-P", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", src, f"root@127.0.0.1:{dst}"], timeout=900)


class Shell:
    """A machine that is already running (the demo's), reached by its SSH port only."""

    def __init__(self, port, key=KEY):
        self.ssh_port, self.key = port, key

    def ssh(self, cmd, check=True):
        return remote_ssh(self.ssh_port, self.key, cmd, check)

    def put(self, src, dst):
        remote_put(self.ssh_port, self.key, src, dst)


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
        "the district relay (kernel forwarding rules)": "usr/local/bin/urban-relay",
        "the kernel hardening": "etc/sysctl.d/90-urban-hardening.conf",
        "the district relay service": "etc/systemd/system/urban-relay.service",
        "the district's link to the centre": "etc/systemd/system/urban-district@.service",
        "cryptsetup (the data partition is LUKS2)": "usr/sbin/cryptsetup",
        "systemd-cryptsetup": "usr/lib/systemd/systemd-cryptsetup",
        "systemd-cryptenroll": "usr/bin/systemd-cryptenroll",
        "the TPM2 library systemd loads (esys)": "usr/lib/x86_64-linux-gnu/libtss2-esys.so.0",
        "the TPM2 library systemd loads (device tcti)": "usr/lib/x86_64-linux-gnu/libtss2-tcti-device.so.0",
    }
    for what, path in need.items():
        if not re.search(r"(^|/)" + re.escape(path) + r"$", listing, re.M):
            raise Fail(f"the image lacks {what} ({path})")
    links = sh(["unsquashfs", "-lls", slot, "opt/urban/data"]).stdout
    if "-> /var/lib/urban/platform" not in links:
        raise Fail("the control centre's data does not live on the data partition")
    shutil.rmtree("/tmp/sq2", ignore_errors=True)
    sh(["unsquashfs", "-q", "-n", "-d", "/tmp/sq2", slot, "etc/nftables.conf", "usr/local/bin/urban-prepare",
        "opt/urban/web/dist/index.html", "etc/modules", "etc/fstab", "usr/local/bin/urban-grow"])
    if "dm_crypt" not in open("/tmp/sq2/etc/modules").read():
        raise Fail("dm-crypt is not loaded at boot: the encrypted data partition could not be opened")
    if "LABEL=urban-data" not in open("/tmp/sq2/etc/fstab").read():
        raise Fail("the data partition is not mounted by label (the opened LUKS volume would not mount)")
    grow = open("/tmp/sq2/usr/local/bin/urban-grow").read()
    for need in ("luksFormat --type luks2", "systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=", "--wipe-slot=password",
                 "systemd-cryptsetup", "attach urban-data"):
        if need not in grow:
            raise Fail(f"urban-grow does not {need}")
    if 'include "/run/urban/nft/*.nft"' not in open("/tmp/sq2/etc/nftables.conf").read():
        raise Fail("the firewall does not take the role's openings")
    prepare = open("/tmp/sq2/usr/local/bin/urban-prepare").read()
    for role in ("unit)", "district)", "center|district,center"):
        if role not in prepare:
            raise Fail(f"urban-prepare does not know the role {role}")
    for need in ("urban-relay.service", "urban-district@$UNIT.service", "VISION"):
        if need not in prepare:
            raise Fail(f"urban-prepare does not start or open {need}")
    if "<html" not in open("/tmp/sq2/opt/urban/web/dist/index.html").read().lower():
        raise Fail("the interface in the image is not a page")
    print("   roles: unit, district, center in one image; power manager, control centre, relay and district link; "
          "cryptsetup with the TPM2 libraries; dm-crypt at boot", flush=True)


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


def push_city(vm):
    """The real city (data/city) onto a centre, so its map and junctions are there."""
    if not os.path.exists("/work/data/city/meta.json"):
        return
    sh("tar -C /work/data/city -czf /tmp/city.tgz .")
    vm.put("/tmp/city.tgz", "/var/lib/urban/update/city.tgz")
    vm.ssh("tar -C /var/lib/urban/platform/city -xzf /var/lib/urban/update/city.tgz && rm /var/lib/urban/update/city.tgz"
           " && chown -R urban:urban /var/lib/urban/platform/city && systemctl restart urban-platform")


def admin_password(vm, path):
    """The centre's first admin password, from its journal, written next to the logs for the person trying it."""
    end, password = time.time() + 600, ""
    while time.time() < end and not password:
        log = vm.ssh("journalctl -u urban-platform --no-pager -o cat", check=False).stdout
        m = re.search(r"(?i)password\S*\s*[:=]\s*(\S+)", log)
        password = m.group(1) if m else ""
        if not password:
            time.sleep(5)
    with open(path, "w") as f:
        note = f"password {password}" if password else "password: not in the journal - journalctl -u urban-platform"
        f.write("URBAN control centre: user admin" + chr(10) + note + chr(10))


def centre_ca(vm, path="/tmp/center-ca.pem", timeout=900):
    """The centre's own CA, once its platform has made it; a TLS context trusting it."""
    import ssl
    end = time.time() + timeout
    while time.time() < end:
        ca = vm.ssh("cat /var/lib/urban/platform/pki/ca.pem", check=False).stdout
        if "BEGIN CERTIFICATE" in ca:
            with open(path, "w") as f:
                f.write(ca)
            ctx = ssl.create_default_context(cafile=path)
            ctx.check_hostname = False  # the forwarded port is 127.0.0.1; the name is checked elsewhere
            return ctx
        time.sleep(5)
    raise Fail("the centre never made its CA")


def health(ctx, port=2443, timeout=600):
    import urllib.request
    end, last = time.time() + timeout, ""
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:{port}/api/health", context=ctx, timeout=20) as r:
                body = json.loads(r.read())
                if body.get("ok"):
                    return body
        except Exception as err:  # still starting
            last = str(err)
        time.sleep(5)
    raise Fail(f"the control centre did not answer over HTTPS: {last}")


def raw_partition_has(disk, label, needle: bytes) -> bool:
    """Is this byte string anywhere in the raw partition (no file system, just the bytes on the disk)?"""
    part = partitions(disk)[label]
    with open(disk, "rb") as f:
        f.seek(part["start"] * 512)
        left, tail = part["size"] * 512, b""
        while left > 0:
            chunk = f.read(min(left, 64 << 20))
            if not chunk:
                break
            left -= len(chunk)
            if needle in tail + chunk:
                return True
            tail = chunk[-len(needle):]
    return False


def encrypted():
    """With a TPM the data partition becomes LUKS2 at first boot, its key sealed in the TPM; what the installer
    put there is kept; a reboot unlocks it again. Without a TPM it stays plain and says so. The proof is the
    raw partition: the maintenance key is in clear on the plain one and nowhere on the encrypted one."""
    vm = VM("crypt", unit="edge-J-05", serial="URB-5")
    try:
        vm.start()
        vm.wait(r"URBAN-DATA the partition is now LUKS2 with its key sealed in this machine's TPM")
        m = vm.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+)")
        fp = m.group(3)
        line = vm.booted("1", "A")
        if "data=rw,luks2-tpm2" not in line:
            raise Fail(f"the self-check does not say the data is encrypted: {line}")
        status = vm.ssh("cryptsetup status urban-data; cryptsetup luksDump /dev/disk/by-partlabel/urban-data").stdout
        if not re.search(r"type:\s+LUKS2", status) or "systemd-tpm2" not in status:
            raise Fail(f"not a LUKS2 volume with a TPM2 token:\n{status[:600]}")
        if "Keyslots:" in status and status.count("luks2") > 1:
            raise Fail("more than one key slot: a passphrase was left behind")
        src = vm.ssh("findmnt -n -o SOURCE /var/lib/urban").stdout.strip()
        if src != "/dev/mapper/urban-data":
            raise Fail(f"the data is mounted from {src}, not the opened volume")
        screen = vm.ssh("cat /run/urban/console.issue").stdout
        if "encrypted - key sealed in this machine's TPM" not in screen:
            raise Fail("the screen does not say the data is encrypted")
        vm.reboot()
        vm.wait(r"URBAN-DATA encrypted \(LUKS2\), unlocked by this machine's TPM")
        m2 = vm.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+) code=(\S+)")
        if m2.group(3) != fp:
            raise Fail(f"the unit key changed across the reboot: {fp} -> {m2.group(3)}")
        vm.booted("1", "A")
        print(f"   LUKS2 with a TPM2 token, mounted from /dev/mapper; unlocked again after a reboot, key {fp} kept", flush=True)
    finally:
        vm.stop()
    if raw_partition_has(vm.disk, "urban-data", b"ssh-ed25519"):
        raise Fail("the maintenance key is readable in the raw encrypted partition")
    os.remove(vm.disk)

    plain = VM("plain", unit="edge-J-06", serial="URB-6", tpm=False)  # the control: no TPM, so no encryption
    try:
        plain.start()
        plain.wait(r"URBAN-DATA plain: no TPM on this machine")
        line = plain.booted("1", "A")
        if "data=rw,plain" not in line:
            raise Fail(f"without a TPM the self-check should say plain: {line}")
        screen = plain.ssh("cat /run/urban/console.issue").stdout
        if "NOT encrypted - this machine has no TPM" not in screen:
            raise Fail("the screen does not warn that the data is not encrypted")
    finally:
        plain.stop()
    if not raw_partition_has(plain.disk, "urban-data", b"ssh-ed25519"):
        raise Fail("control failed: the plain partition should hold the maintenance key in clear")
    os.remove(plain.disk)
    print("   without a TPM: plain, the screen says so, and the key is readable on the raw disk (control)", flush=True)
    print("ENCRYPTION CHECK PASSED", flush=True)


def monitor(vm, command):
    """A QEMU monitor command (e.g. set_link n0 off: the network cable pulled)."""
    import socket as _socket
    for _ in range(60):  # a machine just switched on: its monitor socket appears a moment later
        try:
            with _socket.socket(_socket.AF_UNIX) as mon:
                mon.connect(f"/tmp/{vm.name}.mon")
                mon.sendall((command + chr(10)).encode())
                time.sleep(1)
                return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(1)
    raise Fail(f"no QEMU monitor for {vm.name}")


MEASURE = r"""
unit=$(sed -n 's/^UNIT=//p' /var/lib/urban/etc/unit.conf)
mono() { systemctl show -p "$2" --value "$1"; }
echo "boot $(systemd-analyze time 2>/dev/null | head -n 1)"
echo "edge_active_us $(mono urban-edge@$unit.service ActiveEnterTimestampMonotonic)"
echo "prepare_done_us $(mono urban-prepare.service ActiveEnterTimestampMonotonic)"
echo "check_done_us $(mono urban-os-check.service ActiveEnterTimestampMonotonic)"
echo "health_done_us $(mono urban-health.service ActiveEnterTimestampMonotonic)"
for u in urban-grow urban-prepare urban-os-check urban-health urban-power urban-edge@$unit; do
  echo "cpu_ns $u $(mono $u.service CPUUsageNSec)"
  echo "mem $u $(mono $u.service MemoryCurrent)"
done
echo "blame"; systemd-analyze blame 2>/dev/null | head -n 12
echo "chain"; systemd-analyze critical-chain urban-edge@$unit.service 2>/dev/null | tail -n +3 | head -n 15
"""


def busy_pct(vm, seconds=30):
    """CPU in use while the unit just runs (no cameras): from /proc/stat over `seconds`."""
    a = vm.ssh("head -n 1 /proc/stat").stdout.split()[1:]
    time.sleep(seconds)
    b = vm.ssh("head -n 1 /proc/stat").stdout.split()[1:]
    d = [int(y) - int(x) for x, y in zip(a, b)]
    idle = d[3] + d[4]
    return round(100.0 * (sum(d) - idle) / max(1, sum(d)), 1)


def measure(label="now"):
    """How fast a junction unit is, as numbers: boot to its control service running (a normal boot, not the
    first), the same with the network cable pulled, CPU used by each URBAN service at boot, CPU in use while it
    just runs, memory. Written to /out/logs/measure-<label>.json; the same VM, the same emulation, so two labels
    compare like with like."""
    unit = "edge-J-07"
    vm = VM(f"measure-{label}", unit=unit, serial="URB-M")
    out = {"label": label, "kvm": os.environ.get("URBAN_KVM") == "1"}
    try:
        vm.start()
        vm.booted("1", "A")
        vm.wait_ssh()
        vm.ssh(STAND_KIT.replace("UNIT", unit))
        r = vm.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
        if r.returncode != 0:
            raise Fail(f"urban-unit setup: {(r.stderr or r.stdout).strip()[-300:]}")
        for run in ("cabled", "no-network"):
            vm.power_cycle()
            if run == "no-network":  # the cable pulled as the kernel starts: control must not wait for it
                vm.wait(r"Kernel command line", 600)
                monitor(vm, "set_link n0 off")
            vm.wait(rf"URBAN-UNIT starting urban-edge@{unit}")
            vm.wait(r"URBAN-OS-CHECK (OK|FAIL)", 600)
            vm.wait(r"URBAN-HEALTH", 600)
            time.sleep(20)  # let it settle
            if run == "no-network":
                monitor(vm, "set_link n0 on")
            vm.wait_ssh()
            text = vm.ssh(MEASURE, check=False).stdout
            m = {"boot": "", "cpu_s": {}, "mem_mb": {}, "blame": [], "chain": []}
            section = None
            for line in text.splitlines():
                if line in ("blame", "chain"):
                    section = line
                    continue
                if section:
                    m[section].append(line.strip())
                    continue
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "boot":
                    m["boot"] = line[5:]
                elif parts[0].endswith("_us") and len(parts) > 1 and parts[1].isdigit():
                    m[parts[0][:-3] + "_s"] = round(int(parts[1]) / 1e6, 1)
                elif parts[0] == "cpu_ns" and len(parts) > 2 and parts[2].isdigit():
                    m["cpu_s"][parts[1]] = round(int(parts[2]) / 1e9, 2)
                elif parts[0] == "mem" and len(parts) > 2 and parts[2].isdigit():
                    m["mem_mb"][parts[1]] = round(int(parts[2]) / 2**20, 1)
            if run == "cabled":
                m["busy_pct_idle"] = busy_pct(vm)
                m["mem_used_mb"] = int(vm.ssh("free -m | awk '/Mem:/{print $3}'").stdout.strip())
            out[run] = m
            print(f"   {run}: unit control running at {m.get('edge_active_s')} s after the kernel started "
                  f"(prepare done {m.get('prepare_done_s')} s, self-check {m.get('check_done_s')} s); "
                  f"boot CPU {round(sum(m['cpu_s'].values()), 1)} s; {m['boot'][:90]}", flush=True)
        c = out["cabled"]
        print(f"   running: CPU {c['busy_pct_idle']} % busy, {c['mem_used_mb']} MB in use; "
              f"by service: {json.dumps(c['cpu_s'])}", flush=True)
        with open(f"{LOGS}/measure-{label}.json", "w") as f:
            json.dump(out, f, indent=1)
        print("MEASURED", flush=True)
        return out
    finally:
        vm.stop()
        if os.path.exists(vm.disk):
            os.remove(vm.disk)


# what an intruder would try, each next to a control that must succeed (so a broken probe cannot pass)
ATTACK = r"""
set -u
unit=$(sed -n 's/^UNIT=//p' /var/lib/urban/etc/unit.conf)
pid=$(systemctl show -p MainPID --value urban-edge@$unit.service)
say() { echo "$1 $2"; }
try() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
say lockdown "$(cat /sys/kernel/security/lockdown)"
say root_dev_mem "$(try head -c 16 /dev/mem)"
say root_kcore "$(try head -c 16 /proc/kcore)"
say root_kmem_control "$(try head -c 16 /proc/version)"
memread() { python3 - "$1" <<'PY'
import sys
pid = sys.argv[1]
try:
    for line in open(f"/proc/{pid}/maps"):
        addr, perms = line.split()[:2]
        if perms.startswith("r"):
            start = int(addr.split("-")[0], 16)
            with open(f"/proc/{pid}/mem", "rb") as m:
                m.seek(start)
                m.read(16)
            print("yes")
            break
except OSError:
    print("no")
PY
}
say root_reads_unit_memory "$(memread $pid)"
say root_reads_own_memory_control "$(memread self)"
for k in kernel.yama.ptrace_scope kernel.kptr_restrict kernel.dmesg_restrict kernel.kexec_load_disabled \
         kernel.unprivileged_bpf_disabled kernel.core_pattern fs.suid_dumpable kernel.sysrq net.ipv4.ip_forward; do
  say "sysctl:$k" "$(sysctl -n $k)"
done
# inside the unit service's sandbox, as its user, with none of root's powers: what a hijacked unit service has
inside() { nsenter -t "$pid" -m -- setpriv --reuid=urban --regid=urban --clear-groups --inh-caps=-all --bounding-set=-all -- "$@"; }
say jail_reads_ssh_keys "$(try inside cat /var/lib/urban/ssh/authorized_keys)"
say root_reads_ssh_keys_control "$(try cat /var/lib/urban/ssh/authorized_keys)"
say jail_reads_boot "$(try inside ls /boot/efi)"
say jail_writes_system "$(try inside touch /opt/urban/intruder)"
say jail_writes_etc "$(try inside touch /var/lib/urban/etc/intruder)"
say jail_writes_own_keys_control "$(try inside touch /var/lib/urban/etc/pki/edge/.probe)"
rm -f /var/lib/urban/etc/pki/edge/.probe
say jail_sees_pid1 "$(try inside test -e /proc/1/status)"
say jail_sees_itself_control "$(try inside test -e /proc/$pid/status)"
say listening "$(ss -Hltn | awk '{print $4}' | grep -v '^127\.' | sort -u | tr '\n' ' ')"
echo exposure
for u in urban-edge@$unit urban-vision@$unit urban-district@district-x urban-platform urban-power urban-relay ssh; do
  systemd-analyze security --no-pager "$u.service" 2>/dev/null | sed -n "s/.*Overall exposure level for \(\S*\): \([0-9.]*\).*/\1 \2/p"
done
"""

EXPOSURE_MAX = 3.0  # systemd's scale 0-10; 3.0 or less is what it calls "OK" and better


def hardened():
    """An intruder on a junction unit. As root: no kernel memory, no other process's memory (the unit's key lives
    in its service), no loading another kernel, no core dumps. As the hijacked unit service: no maintenance keys,
    no boot files, nothing writable but its own key folder, no other process. Every probe has a control that must
    succeed. Every network-facing URBAN service sandboxed to systemd's 'OK' or better. SSH: modern cryptography
    only. Nothing listens but SSH."""
    unit = "edge-J-09"
    vm = VM("hardened", unit=unit, serial="URB-9")
    try:
        vm.start()
        vm.booted("1", "A")
        vm.wait_ssh()
        vm.ssh(STAND_KIT.replace("UNIT", unit))
        r = vm.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
        if r.returncode != 0:
            raise Fail(f"urban-unit setup: {(r.stderr or r.stdout).strip()[-300:]}")
        time.sleep(5)
        if not running(vm, unit):
            raise Fail(f"urban-edge@{unit} does not run in its sandbox: "
                       + vm.ssh(f"journalctl -u urban-edge@{unit} --no-pager -n 15 -o cat", check=False).stdout)
        out = vm.ssh(ATTACK, check=False).stdout
        seen, exposure, section = {}, {}, None
        for line in out.splitlines():
            if line == "exposure":
                section = "exposure"
                continue
            k, _, v = line.partition(" ")
            (exposure if section else seen)[k] = v.strip()
        want = {
            "root_dev_mem": "no", "root_kcore": "no", "root_kmem_control": "yes",
            "root_reads_unit_memory": "no", "root_reads_own_memory_control": "yes",
            "sysctl:kernel.yama.ptrace_scope": "3", "sysctl:kernel.kptr_restrict": "2", "sysctl:kernel.dmesg_restrict": "1",
            "sysctl:kernel.kexec_load_disabled": "1", "sysctl:kernel.core_pattern": "|/bin/false",
            "sysctl:fs.suid_dumpable": "0", "sysctl:kernel.sysrq": "0", "sysctl:net.ipv4.ip_forward": "0",
            "jail_reads_ssh_keys": "no", "root_reads_ssh_keys_control": "yes", "jail_reads_boot": "no",
            "jail_writes_system": "no", "jail_writes_etc": "no", "jail_writes_own_keys_control": "yes",
            "jail_sees_pid1": "no", "jail_sees_itself_control": "yes",
        }
        wrong = {k: (seen.get(k), v) for k, v in want.items() if seen.get(k) != v}
        if "[confidentiality]" not in seen.get("lockdown", ""):
            wrong["lockdown"] = (seen.get("lockdown"), "[confidentiality]")
        if seen.get("sysctl:kernel.unprivileged_bpf_disabled") not in ("1", "2"):
            wrong["unprivileged_bpf_disabled"] = (seen.get("sysctl:kernel.unprivileged_bpf_disabled"), "1 or 2")
        if seen.get("listening", "").split() not in (["0.0.0.0:22", "[::]:22"], ["0.0.0.0:22"], ["[::]:22"], ["*:22"]):
            wrong["listening"] = (seen.get("listening"), "SSH only")
        if wrong:
            raise Fail("an intruder gets more than nothing: " + "; ".join(f"{k}: got {g!r}, want {w!r}" for k, (g, w) in wrong.items()))
        urban = {k: float(v) for k, v in exposure.items() if k.startswith("urban-")}
        if len(urban) < 6:
            raise Fail(f"systemd did not rate every URBAN service: {exposure}")
        loose = {k: v for k, v in urban.items() if v > EXPOSURE_MAX}
        if loose:
            raise Fail(f"services exposed above {EXPOSURE_MAX}: {loose}")
        # the detector runs inside vision's own sandbox (no executable memory, its system call filter, no devices):
        # the hardening must not break the work it protects
        props = vm.ssh("systemctl show urban-vision@x.service -p MemoryDenyWriteExecute -p NoNewPrivileges "
                       "-p PrivateDevices -p RestrictNamespaces -p LockPersonality --value").stdout.split()
        if props[:3] != ["yes", "yes", "yes"]:
            raise Fail(f"vision's sandbox is not what the unit file says: {props}")
        infer = vm.ssh(
            "systemd-run --wait --pipe --collect --quiet --uid=urban --gid=urban -p WorkingDirectory=/opt/urban "
            "-p MemoryDenyWriteExecute=yes -p NoNewPrivileges=yes -p PrivateDevices=yes -p ProtectSystem=strict "
            "-p LockPersonality=yes -p RestrictNamespaces=yes -p SystemCallArchitectures=native "
            "-p SystemCallFilter=@system-service -p SystemCallFilter=~@privileged -p SystemCallErrorNumber=EPERM "
            "/opt/urban/.venv/bin/python -c \"import numpy as np, time; from pathlib import Path; "
            "from urban.vision.detector import YoloxDetector; d = YoloxDetector(Path('/opt/urban/models/yolox_nano.onnx'), threads=1); "
            "f = np.zeros((480, 640, 3), np.uint8); d.detect(f); t = time.perf_counter(); d.detect(f); "
            "print('INFER-OK', round((time.perf_counter() - t) * 1000))\"", check=False)
        if "INFER-OK" not in infer.stdout:
            raise Fail(f"the detector does not run in vision's sandbox: {(infer.stdout + infer.stderr)[-400:]}")
        infer_ms = infer.stdout.split("INFER-OK")[1].split()[0]
        # SSH: an old cipher or key exchange is refused, the modern default works (the control)
        base = ["ssh", "-i", KEY, "-p", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        weak = {"cipher aes128-cbc": ["-o", "Ciphers=aes128-cbc"],
                "cipher aes256-ctr (no AEAD)": ["-o", "Ciphers=aes256-ctr"],
                "kex diffie-hellman-group14-sha1": ["-o", "KexAlgorithms=diffie-hellman-group14-sha1"],
                "host key ssh-rsa": ["-o", "HostKeyAlgorithms=ssh-rsa"]}
        # only AEAD ciphers are offered, so no separate MAC is ever used; the configured list is checked as well
        conf = vm.ssh("sshd -T | grep -E '^(ciphers|macs|kexalgorithms|hostkeyalgorithms) '").stdout
        if re.search(r"sha1|cbc|group1[46]|group-exchange|-ctr", conf, re.I):
            raise Fail("SSH still configures weak algorithms: " + conf.replace(chr(10), "; "))
        for what, opt in weak.items():
            if sh([*base, *opt, "root@127.0.0.1", "true"], check=False).returncode == 0:
                raise Fail(f"SSH accepted {what}")
        if sh([*base, "root@127.0.0.1", "true"], check=False).returncode != 0:
            raise Fail("control failed: SSH with the default modern algorithms was refused")
        print("   root: no /dev/mem, no /proc/kcore, no memory of the unit's process (its own: yes); lockdown "
              "confidentiality; ptrace off for all", flush=True)
        print("   hijacked unit service: no SSH keys, no boot files, no writes outside its key folder (inside: yes), "
              "no other process", flush=True)
        print("   exposure (systemd, 0 best - 10 worst): " + ", ".join(f"{k.split('@')[0]} {v}" for k, v in sorted(urban.items()))
              + f"; ssh {exposure.get('ssh.service', '?')}", flush=True)
        print(f"   the detector runs inside vision's sandbox ({infer_ms} ms a frame, emulated CPU)", flush=True)
        print("   SSH refuses CBC and non-AEAD ciphers, SHA-1 key exchange and RSA host keys; no SHA-1 MAC configured; "
              "only SSH listens", flush=True)
        print("HARDENING CHECK PASSED", flush=True)
    finally:
        vm.stop()
        os.remove(vm.disk)


def reboots(tpm="on"):
    """Diagnosis: does a unit come back from three boots in a row (two warm reboots), with or without a TPM?"""
    vm = VM(f"reboots-{tpm}", unit="edge-J-03", serial="URB-3", tpm=(tpm == "on"))
    try:
        vm.start()
        for n in range(3):
            if n:
                vm.reboot()
            t = time.time()
            vm.wait(r"URBAN-OS-CHECK (OK|FAIL)", 900)
            print(f"   tpm {tpm}: boot {n + 1} reached the self-check in {time.time() - t:.0f} s", flush=True)
            vm.wait(r"URBAN-HEALTH", 600)
            vm.wait_ssh()
        print(f"REBOOTS OK tpm={tpm}", flush=True)
    finally:
        vm.stop()
        os.remove(vm.disk)


def light():
    """A light unit: VISION=off, 1 GB of memory. No vision process, a plan without vision, the unit service runs."""
    unit = "edge-J-08"
    vm = VM("light", unit=unit, serial="URB-8", vision="off", mem=1024)
    try:
        vm.start()
        vm.wait(rf"URBAN-ROLE role=unit name={unit} vision=off")
        line = vm.booted("1", "A")
        plan = json.loads(vm.ssh("cat /run/urban/power.json").stdout)
        if not plan["vision"].get("off") or plan["vision"]["fps"] != 0 or plan["vision"]["threads"] != 0:
            raise Fail(f"a light unit's plan still has vision work: {plan['vision']}")
        screen = vm.ssh("cat /run/urban/console.issue").stdout
        if "light (no cameras)" not in screen or "no vision (light unit)" not in screen:
            raise Fail("the screen does not say this is a light unit")
        vm.ssh(STAND_KIT.replace("UNIT", unit))
        vm.ssh(f"echo '[{{\"camera\": \"rtsp://nowhere/1\"}}]' > /var/lib/urban/etc/cameras.json")  # even configured: ignored
        r = vm.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
        if r.returncode != 0:
            raise Fail(f"urban-unit setup: {(r.stderr or r.stdout).strip()[-300:]}")
        time.sleep(5)
        if not running(vm, unit):
            raise Fail(f"urban-edge@{unit} is not running after setup")
        procs = vm.ssh("pgrep -fc '[u]rban.vision' || true").stdout.strip()
        if procs not in ("", "0"):
            raise Fail(f"a vision process is running on a light unit ({procs})")
        used = int(vm.ssh("free -m | awk '/Mem:/{print $3}'").stdout.strip())
        total = int(vm.ssh("free -m | awk '/Mem:/{print $2}'").stdout.strip())
        if total > 1100 or used >= 400:
            raise Fail(f"memory: {used} MB used of {total} MB (want under 400 of ~1024)")
        vm.reboot()
        vm.wait(rf"URBAN-UNIT starting urban-edge@{unit} \(light: no vision\)")
        vm.booted("1", "A")
        time.sleep(3)
        vm.screendump(f"{LOGS}/light-screen.ppm")
        print(f"   {unit}: {used} MB of {total} MB in use, no vision process, urban-edge@ runs; "
              f"{line[line.index('URBAN-OS-CHECK'):][:60]}", flush=True)
        print("LIGHT UNIT CHECK PASSED", flush=True)
    finally:
        vm.stop()
        os.remove(vm.disk)


PROVISION = ("cd /opt/urban && runuser -u urban -- env $(grep -v '^#' /var/lib/urban/etc/platform.env | xargs) "
             "/opt/urban/.venv/bin/python -m urban.provision")


def chain_vms(prefix, listen="127.0.0.1", vnc=False):
    """The three machines of the chain: the centre (the pilot's district,center), a district with its relay to the
    centre's forwarded port, a light unit. Each has its own SSH port; the district's 8443 is the container's 8443,
    which is where the unit's kit sends it (10.0.2.2:8443 from inside a VM)."""
    centre = VM(f"{prefix}-center", unit="center", serial="PILOT-1", role="district,center", host="urban-center",
                grow_mb=2048, forward=[(2443, 443), (28443, 8443)], ssh_port=2222, listen=listen, vnc=0 if vnc else None)
    district = VM(f"{prefix}-district", unit="district-1", serial="SRV-1", role="district", center="10.0.2.2:28443",
                  forward=[(8443, 8443)], ssh_port=2223, mem=1024, listen=listen, vnc=1 if vnc else None)
    unit = VM(f"{prefix}-unit", unit="edge-J-01", serial="URB-1", vision="off", ssh_port=2224, mem=1024, vnc=2 if vnc else None)
    return centre, district, unit


def enrol_chain(centre, district, unit, junction="J-01"):
    """What the administrator does in the UI, done by the centre's CLI: the district with the address its units
    use, the unit at its junction through the district; then each kit onto its machine."""
    codes = {}
    for dev, vm in (("district-1", district), (f"edge-{junction}", unit)):
        code = vm.ssh("urban-unit code").stdout.strip()
        if not code.startswith(f"URBAN1:{dev}:"):
            raise Fail(f"{dev} printed no enrolment code: {code!r}")
        codes[dev] = code
    centre.ssh(f"{PROVISION} enroll-district district-1 --address 10.0.2.2 --public {codes['district-1'].split(':')[3]} --serial SRV-1")
    centre.ssh(f"{PROVISION} enroll edge-{junction} --junction {junction} --public {codes[f'edge-{junction}'].split(':')[3]} --serial URB-1")
    centre.ssh(f"{PROVISION} assign edge-{junction} --district district-1")
    for dev, vm in (("district-1", district), (f"edge-{junction}", unit)):
        kit = centre.ssh(f"{PROVISION} kit {dev}").stdout
        if '"urban_kit"' not in kit:
            raise Fail(f"no kit for {dev}: {kit[-300:]}")
        path = f"/tmp/kit-{dev}.json"
        with open(path, "w") as f:
            f.write(kit)
        vm.put(path, f"{STAGE}/kit.json")
        r = vm.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
        if r.returncode != 0:
            raise Fail(f"urban-unit setup on {dev}: {(r.stderr or r.stdout).strip()[-300:]}")
    return codes


def wait_online(vm, service, who, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        log = vm.ssh(f"journalctl -u {service} --no-pager -o cat", check=False).stdout
        if re.search(rf"{who}: \S+ online at", log):
            return log
        time.sleep(5)
    raise Fail(f"{service} never came online:\n{vm.ssh(f'journalctl -u {service} --no-pager -n 15 -o cat', check=False).stdout}")


def chain():
    """unit -> district -> centre, three machines. The unit's TLS session reaches the centre through the district's
    relay (the certificate seen through the relay is the centre's); the district is a device of the centre with its
    address and health; its queues for its junction arrive; through a 60 s outage of the centre it keeps its reports
    and the centre's history has them afterwards; the district's firewall opens 8443 from private networks only."""
    centre, district, unit = chain_vms("chain")
    try:
        for vm in (centre, district, unit):
            vm.start()
        centre.booted("1", "A")
        district.wait(r"URBAN-RELAY units -> 10\.0\.2\.2:28443 in the kernel")
        district.booted("1", "A")
        unit.booted("1", "A")
        push_city(centre)
        ctx = centre_ca(centre)
        health(ctx)
        enrol_chain(centre, district, unit)
        wait_online(district, "urban-district@district-1", "district")
        wait_online(unit, "urban-edge@edge-J-01", "edge")
        body = health(ctx)
        if body.get("devices_online") != 2:
            raise Fail(f"the centre sees {body.get('devices_online')} device(s) online, not the district and the unit")
        seen = sh(["bash", "-c", "openssl s_client -connect 127.0.0.1:8443 -servername urban-center -CAfile /tmp/center-ca.pem "
                   "</dev/null 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName"]).stdout
        if "urban-center" not in seen or "URBAN device gateway" not in seen:
            raise Fail(f"through the relay a client does not see the centre's certificate: {seen}")
        relay = district.ssh("nft list table ip urban_relay").stdout
        if not re.search(r"ip saddr \{[^}]*10\.0\.0\.0/8[^}]*\} tcp dport 8443 dnat (ip )?to 10\.0\.2\.2:28443", relay):
            raise Fail(f"the district does not forward 8443 from private networks only to the centre:\n{relay}")
        if "policy drop" not in district.ssh("nft list chain inet urban input").stdout:
            raise Fail("the district's input chain does not drop by default")
        listening = district.ssh("ss -Hltn 'sport = :8443'").stdout.strip()
        if listening:
            raise Fail(f"a program on the district listens on 8443 (the relay should be the kernel's): {listening}")
        stats = json.loads(district.ssh("cat /run/urban/relay.json").stdout)
        if stats["units"] < 1 or stats["bytes_up"] <= 0 or not stats.get("kernel"):
            raise Fail(f"the kernel relay carries nothing: {stats}")
        print(f"   unit edge-J-01 online through district-1 (relay: {stats['units']} unit, {stats['bytes_up']} B up); "
              f"the centre sees both; the relay shows the centre's certificate", flush=True)
        # the district's cameras: a queue for the unit's junction, reported by the district, kept by the centre
        district.ssh("echo '{\"J-01-E\": 42.0}' > /run/urban/queues.json")
        db = "/var/lib/urban/platform/urban.db"
        count = ("/opt/urban/.venv/bin/python -c \"import sqlite3; c = sqlite3.connect('%s'); "
                 "print(c.execute(\\\"select count(*) from queues where junction = 'J-01' and approach = 'J-01-E' %s\\\").fetchone()[0])\"")
        end = time.time() + 150
        while time.time() < end:
            if int(centre.ssh(count % (db, "and metres >= 1")).stdout.strip()) > 0:
                break
            time.sleep(10)
        else:
            raise Fail("the district's queue reading never reached the centre's history")
        print("   the district's queue for J-01-E is in the centre's history", flush=True)
        # an outage of the centre: the district keeps its reports and the history gets them afterwards
        t_stop = int(centre.ssh("date +%s; systemctl stop urban-platform").stdout.split()[0])
        time.sleep(60)
        t_start = int(centre.ssh("systemctl start urban-platform; date +%s").stdout.split()[-1])
        end = time.time() + 240
        while time.time() < end:
            log = district.ssh("journalctl -u urban-district@district-1 --no-pager -o cat", check=False).stdout
            if log.count("online at the centre") >= 2:
                break
            time.sleep(5)
        else:
            raise Fail("the district did not reconnect after the outage")
        time.sleep(15)
        kept = int(centre.ssh(count % (db, f"and metres = 42 and at between {t_stop + 8} and {t_start - 1}")).stdout.strip())
        if kept < 20:
            raise Fail(f"only {kept} of the district's readings from inside the outage are in the centre's history")
        print(f"   centre down {t_start - t_stop} s: {kept} readings from inside the outage are in its history", flush=True)
        for vm, name in ((centre, "center"), (district, "district"), (unit, "unit")):
            vm.screendump(f"{LOGS}/chain-{name}-screen.ppm")
        print("CHAIN CHECK PASSED", flush=True)
    finally:
        for vm in (centre, district, unit):
            vm.stop()
            if os.path.exists(vm.disk):
                os.remove(vm.disk)


def demo(pilot=False):
    """The machines kept running for a person: screens on VNC :0 :1 :2 (noVNC in front of them), the centre's
    interface on 2443, the admin password in /out/logs/demo-admin.txt."""
    if pilot:
        vms = [VM("demo-center", unit="center", serial="PILOT-1", role="district,center", host="urban-center", grow_mb=6144,
                  forward=[(2443, 443)], listen="0.0.0.0", vnc=0)]
    else:
        vms = list(chain_vms("demo", listen="0.0.0.0", vnc=True))
    centre = vms[0]
    try:
        for vm in vms:
            vm.start()
        print("   booting URBAN OS ...", flush=True)
        for vm in vms:
            vm.booted("1", "A")
            vm.wait_ssh()
        push_city(centre)
        admin_password(centre, f"{LOGS}/demo-admin.txt")
        for vm in vms[1:]:
            print(f"   {vm.name}: {vm.ssh('urban-unit code').stdout.strip()}", flush=True)
        print("DEMO READY", flush=True)
        centre.proc.wait()  # until the container is stopped
    finally:
        for vm in vms:
            vm.stop()


def demo_shells():
    return Shell(2222), Shell(2223), Shell(2224)


def demo_enrol():
    centre, district, unit = demo_shells()
    codes = enrol_chain(centre, district, unit)
    wait_online(district, "urban-district@district-1", "district")
    wait_online(unit, "urban-edge@edge-J-01", "edge")
    print(f"   district-1 and edge-J-01 enrolled by the centre's CLI and online ({', '.join(codes)})")
    print("DEMO ENROLLED", flush=True)


def demo_kit(path, device):
    machine = Shell(2223) if device.startswith("district-") else Shell(2224)
    machine.put(path, f"{STAGE}/kit.json")
    r = machine.ssh(f"urban-unit setup {STAGE}/kit.json", check=False)
    if r.returncode != 0:
        raise Fail(f"urban-unit setup on {device}: {(r.stderr or r.stdout).strip()[-300:]}")
    print(f"   kit of {device} in place: {r.stdout.strip().splitlines()[-1]}")
    print("DEMO KIT INSTALLED", flush=True)


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
        elif what == "encrypted":
            encrypted()
        elif what == "light":
            light()
        elif what == "reboots":
            reboots(sys.argv[2] if len(sys.argv) > 2 else "on")
        elif what == "hardened":
            hardened()
        elif what == "measure":
            measure(sys.argv[2] if len(sys.argv) > 2 else "now")
        elif what == "chain":
            chain()
        elif what == "demo":
            demo(pilot=len(sys.argv) > 2 and sys.argv[2] == "pilot")
        elif what == "demo-enrol":
            demo_enrol()
        elif what == "demo-kit":
            demo_kit(sys.argv[2], sys.argv[3])
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
