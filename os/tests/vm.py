"""Scenarios for URBAN Edge OS in QEMU, run inside the builder container (see os/tests/lib.mjs).

    python3 vm.py image                 the disk image's layout, without booting it
    python3 vm.py boot                  boots; read-only root, firewall, watchdog, SSH keys only
    python3 vm.py firstboot             unit key and enrolment code on first boot, the same key after a reboot
    python3 vm.py update good|foreign|broken
                                        signed A/B update; a foreign signature; a broken system rolled back

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


class VM:
    def __init__(self, name, version="1", unit="edge-J-07", serial="URB-TEST"):
        os.makedirs(LOGS, exist_ok=True)
        self.name, self.disk = name, f"/tmp/{name}.img"
        sh(["cp", "--sparse=always", f"{OUT}/urban-edge-{version}.img", self.disk])
        parts = partitions(self.disk)
        esp, data = parts["esp"]["start"], parts["urban-data"]["start"]
        if unit:  # the installer's step: the unit id and serial on the ESP
            with open("/tmp/urban-unit.txt", "w", newline="\r\n") as f:  # written on a Windows laptop: CRLF
                f.write(f"UNIT={unit}\nSERIAL={serial}\n")
            sh(f"mcopy -o -i {self.disk}@@{esp * 512} /tmp/urban-unit.txt ::/urban-unit.txt")
        if not os.path.exists(KEY):  # a maintenance key on the data partition, for the scenarios only
            sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", KEY])
        size = parts["urban-data"]["size"]
        sh(f"dd if={self.disk} of=/tmp/data.part bs=512 skip={data} count={size} status=none")
        sh(["debugfs", "-w", "-R", "mkdir ssh", "/tmp/data.part"])
        sh(["debugfs", "-w", "-R", f"write {KEY}.pub ssh/authorized_keys", "/tmp/data.part"])
        sh(f"dd if=/tmp/data.part of={self.disk} bs=512 seek={data} conv=notrunc status=none")
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
             "-drive", f"file={self.disk},format=raw,if=virtio",
             "-netdev", "user,id=n0,hostfwd=tcp:127.0.0.1:2222-:22", "-device", "virtio-net-pci,netdev=n0",
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
        return sh(["ssh", "-i", KEY, "-p", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "root@127.0.0.1", cmd], check=check, timeout=600)

    def put(self, src, dst):
        sh(["scp", "-i", KEY, "-P", "2222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
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
    print("IMAGE LAYOUT VERIFIED", flush=True)


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


def check_code(code, fingerprint):
    parts = code.split(":")
    if len(parts) != 5 or parts[0] != "URBAN1" or parts[1] != "edge-J-07" or parts[2] != "URB-TEST":
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
        elif what == "update":
            if sys.argv[2] == "foreign":
                foreign_bundle()
            update(sys.argv[2])
        else:
            raise Fail(f"unknown scenario {what}")
    except Fail as err:
        print(f"FAILED: {err}", flush=True)
        sys.exit(1)
