"""Step 7 (G4, and the escrow part of G3): the data partition's key sealed in the TPM to PCR 7, the recovery key escrowed.

    python3 vm.py pcr7      one machine with a TPM and Secure Boot, four facts:
        a) first boot: LUKS2 with a TPM2 token bound to PCR 7 and a recovery key slot, urban-recovery.enc on the ESP;
           switched off and on again exactly as in b), Secure Boot unchanged: unlocked by itself (the control, which
           shows that the TPM's state survives that power path, so that what locks b) is PCR 7)
        b) the same machine (disk and TPM state kept) started with Secure Boot off: DATA LOCKED on its consoles, with the
           advice to turn Secure Boot back on before typing the key; the data not mounted, none of its services and no
           SSH while it waits, the raw partition still the same LUKS2 volume; a wrong key, typed only once the password
           agent's prompt is on the console, is said and asked for again
        c) urban-recovery.enc from its ESP decrypted with the escrow private key (os/recovery.sh, what node os/recovery.mjs
           runs) and typed at the prompt on the serial console: unlocked, re-sealed to the new state, the unit boots with
           its data (the same device key), nothing of the unit had started before the key was accepted, no key typed
           shows on the console or in the journal, and the screen says the seal protects nothing while Secure Boot is off
        d) a reboot with the same firmware (Secure Boot still off): unlocked by the TPM by itself, the screen still saying so
    escrow_in_image()       called by vm.py image(): the image carries the escrow public key and no trace of its private key

Run from vm.py (the script): its VM, Fail and helpers are taken from it as __main__. Importing "vm" again would make a
second module with its own Fail, which vm.py's `except Fail` would not catch.
"""
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time

vm = sys.modules["__main__"]

ESCROW_DIR = "/out/keys/escrow"
RECOVERY_TOOL = "/work/os/recovery.sh"  # the decryption os/recovery.mjs runs in the builder
MODHEX = "cbdefghijklnrtuv"
RECOVERY_KEY = re.compile(rf"^recovery key: ([{MODHEX}]{{8}}(?:-[{MODHEX}]{{8}}){{7}})$", re.M)
KVM = os.environ.get("URBAN_KVM") == "1"
LOCK_WINDOW = 90 if KVM else 240  # seconds the locked machine is watched for anything of the unit starting
PROMPT = "URBAN data partition - recovery key:"  # systemd's password agent; its line ends only when the key is typed
PROMPT_WAIT = 120 if KVM else 300  # seconds for the prompt to show after urban-grow says it waits for the key
LOCK_BLOCK = re.compile(r"^\s*DATA LOCKED: the TPM no longer matches this machine's Secure Boot state\s*$")
LOCK_LINE = ("URBAN-DATA LOCKED: the TPM no longer matches this machine's Secure Boot state (Secure Boot turned off, its "
             "keys changed, or a firmware update). The data stays closed. Recovery: decrypt urban-recovery.enc from the "
             "ESP with the offline escrow key (node os/recovery.mjs decrypt urban-recovery.enc) and type the key here.")
# Secure Boot off while locked: the screen's block and the journal advise turning it back on before the key is typed
LOCK_SB_OFF_BLOCK = re.compile(r"^\s*Secure Boot is OFF on this machine\. Turn it on in the firmware settings and\s*$")
LOCK_SB_OFF_LINE = ("URBAN-DATA Secure Boot is OFF on this machine. Turn it on in the firmware settings and restart before "
                    "typing the key, unless it is meant to stay off: the key is re-sealed to the state of this boot.")
# sealed (or re-sealed) to PCR 7 while Secure Boot is off: said in the journal, and on the screen's data line
SB_OFF_LINE = ("URBAN-DATA sealed to PCR 7 while Secure Boot is OFF: protects nothing on this machine until it is on (the "
               "recovery key will be asked once after turning it on)")
SB_OFF_SCREEN = "while Secure Boot is OFF: protects nothing on this machine"
STARTED = re.compile(r"URBAN-(?!DATA)[A-Z]|login:")  # a line of the unit's own system (prepare, self-check, health), getty
UNLOCKED = r"URBAN-DATA unlocked with the recovery key"
NOT_THE_KEY = r"URBAN-DATA (the key typed is not the recovery key|could not open the data partition)"
RESEALED = "URBAN-DATA re-sealed to this machine's current Secure Boot state"
TPM_UNLOCK = r"URBAN-DATA encrypted \(LUKS2\), unlocked by this machine's TPM"
# what must not have begun before the recovery key was accepted: the data mount and everything that needs it
WAITED = ("var-lib-urban.mount", "urban-prepare.service", "systemd-networkd.service", "ssh.service", "urban-power.service",
          "urban-os-check.service", "urban-health.service")


def _section(dump, name):
    m = re.search(rf"^{name}:\n(.*?)(?=^\S|\Z)", dump, re.M | re.S)
    return m.group(1) if m else ""


def luks_state(m):
    """The data volume as the unit itself reads it: the device it was opened from, its UUID, key slots and tokens (from
    each token's own JSON, so the PCRs do not depend on how this systemd version prints them)."""
    dev = m.ssh("cryptsetup status urban-data | sed -n 's/^ *device: *//p'").stdout.strip()
    if not dev.startswith("/dev/"):
        raise vm.Fail(f"the data volume urban-data is not open (cryptsetup status says device {dev!r})")
    dump = m.ssh(f"cryptsetup luksDump {dev}").stdout
    uuid = re.search(r"^UUID:\s*(\S+)", dump, re.M)
    tokens = []
    for tid, _ in re.findall(r"^\s+(\d+): (\S+)\s*$", _section(dump, "Tokens"), re.M):
        js = json.loads(m.ssh(f"cryptsetup token export --token-id {tid} {dev}").stdout)
        tokens.append({"id": int(tid), "type": js.get("type"), "keyslots": sorted(int(k) for k in js.get("keyslots", [])),
                       "pcrs": js.get("tpm2-pcrs", []), "policy": js.get("tpm2-policy-hash", ""),
                       "blob": js.get("tpm2-blob", "")})
    return {"dev": dev, "uuid": uuid.group(1) if uuid else "", "dump": dump, "tokens": tokens,
            "keyslots": sorted(int(k) for k in re.findall(r"^\s+(\d+): luks2\s*$", _section(dump, "Keyslots"), re.M))}


def sealed_to_pcr7(st, what):
    """Exactly two key slots, the TPM's (bound to PCR 7 and nothing else) and the recovery key's; no passphrase left."""
    tpm = [t for t in st["tokens"] if t["type"] == "systemd-tpm2"]
    rec = [t for t in st["tokens"] if t["type"] == "systemd-recovery"]
    if len(tpm) != 1 or tpm[0]["pcrs"] != [7]:
        raise vm.Fail(f"{what}: want one TPM2 token bound to PCR 7, got {[(t['type'], t['pcrs']) for t in st['tokens']]}")
    if len(rec) != 1:
        raise vm.Fail(f"{what}: want one recovery key token, got {[t['type'] for t in st['tokens']]}")
    tokened = sorted(tpm[0]["keyslots"] + rec[0]["keyslots"])
    if st["keyslots"] != tokened or len(tokened) != 2:
        raise vm.Fail(f"{what}: key slots {st['keyslots']}, want exactly the TPM's and the recovery key's {tokened} "
                      "(a passphrase left behind, or a key missing)")
    return tpm[0], rec[0]


def escrow_fpr(pem):
    der = subprocess.run(["openssl", "pkey", "-pubin", "-in", pem, "-outform", "DER"], capture_output=True, check=True).stdout
    return "sha256:" + hashlib.sha256(der).hexdigest()


def recovery_header(text):
    fields = dict(re.findall(r"^(unit|volume|escrow-key|made): *(.*?)\s*$", text, re.M))
    if not re.search(r"^-----BEGIN URBAN RECOVERY KEY-----$(.|\n)+^-----END URBAN RECOVERY KEY-----$", text, re.M):
        raise vm.Fail(f"urban-recovery.enc has no BEGIN/END URBAN RECOVERY KEY block:\n{text[:600]}")
    return fields


def power_off(m):
    """A clean poweroff; the machine stays off (disk, firmware variables and TPM state kept) until power_on()."""
    m._cycling = True
    m.ssh("systemctl poweroff", check=False)
    try:
        m.proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        pass
    m.cut_power()  # whatever is left of QEMU and the software TPM ends; nothing restarts the machine


def raw_luks_header(disk, work="/tmp/pcr7-hdr.img"):
    """The data partition read from the disk file itself (the unit cannot be asked while it is locked): its LUKS2 magic,
    UUID, and its key slots and tokens as cryptsetup in this container reads the 16 MiB header area."""
    part = vm.partitions(disk)["urban-data"]
    hdr = vm.read_at(disk, part["start"], 16 << 20)
    with open(work, "wb") as f:
        f.write(hdr)
    dump = vm.sh(["cryptsetup", "luksDump", work], check=False)
    text = dump.stdout
    return {"magic": hdr[:6] == b"LUKS\xba\xbe", "uuid": hdr[168:208].split(b"\0")[0].decode(errors="replace"),
            "keyslots": sorted(int(k) for k in re.findall(r"^\s+(\d+): luks2\s*$", _section(text, "Keyslots"), re.M)),
            "tokens": sorted(t for _, t in re.findall(r"^\s+(\d+): (\S+)\s*$", _section(text, "Tokens"), re.M)),
            "ok": dump.returncode == 0, "said": (dump.stderr or dump.stdout).strip()[:300]}


def wait_prompt(m, timeout=PROMPT_WAIT):
    """Until the password agent's prompt is on the serial console, from the line the mark was set past ("waiting for the
    recovery key", which the prompt may share when the two reach the console together) on. Only then is the console's
    echo off: keys typed before it are echoed by the terminal in clear. The prompt's line has no newline until the key is
    typed, so it is looked for in what the console wrote last (m.partial) as well as in whole lines (a later message ends
    it)."""
    end = time.time() + timeout
    while time.time() < end:
        if PROMPT in m.partial or any(PROMPT in l for l in m.lines[max(m.mark - 1, 0):]):
            time.sleep(2)  # the agent turns the echo off right after it writes the prompt
            return
        time.sleep(0.5)
    raise vm.Fail(f"no '{PROMPT}' on the serial console within {timeout} s (log: {vm.LOGS}/{m.name}.log)")


def type_key(m, key, typed, timeout=600):
    """Types `key` at the prompt (once it is there) and waits for the unit's verdict: True (opened) or False (refused,
    asked again). Every key typed is kept in `typed`, to be looked for on the console afterwards."""
    wait_prompt(m)
    m.mark = len(m.lines)
    typed.append(key)
    m.type_console(key + "\n")
    got = m.wait(rf"{UNLOCKED}|{NOT_THE_KEY}", timeout)
    return got.group(0).startswith("URBAN-DATA unlocked")


def not_echoed(m, typed, what):
    """No key typed at the prompt shows on the serial console (the agent shows a star per key, or nothing after TAB)."""
    console = "\n".join(m.lines) + "\n" + m.partial
    shown = [k for k in typed if k in console]
    if shown:
        raise vm.Fail(f"{what}: a key typed at the prompt shows in clear on the serial console ({shown[0][:8]}-...): "
                      "it was echoed, not masked by the password agent")


def pcr7():
    # a disk bigger than the image, as every real one is: the data partition is grown before it is encrypted
    m = vm.VM("pcr7", unit="edge-J-17", serial="URB-17", console_input=True, grow_mb=1024)
    rk = ""
    try:
        # ---- (a) first boot with Secure Boot as enrolled
        m.start()
        m.wait(r"URBAN-DATA the partition is now LUKS2 with its key sealed in this machine's TPM, bound to PCR 7")
        e = m.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+)")
        fp = e.group(3)
        line = m.booted("1", "A")
        for need in ("data=rw,luks2-tpm2", "secureboot=on"):
            if need not in line:
                raise vm.Fail(f"a) the self-check line lacks {need}: {line}")
        st_a = luks_state(m)
        tpm_a, rec_a = sealed_to_pcr7(st_a, "a) after the first boot")
        # the disk is bigger than the image: the data partition was grown first, and the kernel kept its GPT name (the
        # data volume's link, without which the boot would wait for ever, needs it: urban-bootdisk crypt)
        if not any("URBAN-DATA grown by" in l for l in m.lines):
            raise vm.Fail("a) the data partition was not grown to the end of a disk bigger than the image")
        if m.ssh("grep -x PARTNAME=urban-data /sys/class/block/$(basename $(readlink -f /dev/disk/urban/urban-data))/uevent",
                 check=False).returncode != 0:
            raise vm.Fail("a) after growing, the kernel lost the data partition's GPT name (PARTNAME)")
        # the mark of a finished volume (urban-data-new until then): only a volume still marked new is ever redone
        label = re.search(r"^Label:\s*(.*?)\s*$", st_a["dump"], re.M)
        if not label or label.group(1) != "urban-data-ready":
            raise vm.Fail(f"a) the finished volume is not marked urban-data-ready in its LUKS2 header (Label: "
                          f"{label.group(1) if label else 'none'!r})")
        enc = m.ssh("cat /boot/efi/urban-recovery.enc").stdout
        head = recovery_header(enc)
        want_fpr = escrow_fpr(f"{ESCROW_DIR}/escrow.pem")
        if head.get("volume") != st_a["uuid"] or head.get("escrow-key") != want_fpr or head.get("unit") != "edge-J-17":
            raise vm.Fail(f"a) urban-recovery.enc names unit {head.get('unit')}, volume {head.get('volume')}, escrow key "
                          f"{head.get('escrow-key')}; want edge-J-17, {st_a['uuid']}, {want_fpr}")
        seal = m.ssh("cat /run/urban/data-seal").stdout.strip()
        screen = m.ssh("cat /run/urban/console.issue").stdout
        if seal != "pcr7" or "bound to its Secure Boot state (PCR 7)" not in screen or SB_OFF_SCREEN in screen:
            raise vm.Fail(f"a) the unit does not say its data is sealed to PCR 7 (data-seal {seal!r}); its screen:\n{screen[-900:]}")
        if any(SB_OFF_LINE in l for l in m.lines):
            raise vm.Fail("a) the unit says the seal protects nothing, with Secure Boot on")
        print(f"   OK a) first boot with Secure Boot: LUKS2 {st_a['uuid']} from {st_a['dev']}, key slots {st_a['keyslots']}: "
              f"systemd-tpm2 (slot {tpm_a['keyslots']}, PCRs {tpm_a['pcrs']}) and systemd-recovery (slot {rec_a['keyslots']}), "
              f"no passphrase; urban-recovery.enc on the ESP for this volume, encrypted to escrow key {want_fpr[:23]}...",
              flush=True)

        # The control: switched off and on exactly as in (b) - a clean poweroff, QEMU and swtpm ended, the TPM's state
        # directory kept, a new swtpm on it at power-on - with Secure Boot as enrolled. It must unlock by itself. Then the
        # TPM's state survives this power path, and when (b) locks, what changed is the firmware's Secure Boot setting,
        # which reaches the TPM through PCR 7, the only PCR the token is bound to (sealed_to_pcr7). Chosen over matching the
        # wording of the lock reason: urban-grow can only pass on systemd-cryptsetup's words (with headless=true its last
        # line is "Password querying disabled via 'headless' option." whatever the TPM said), and how systemd 252 words a
        # PCR policy mismatch cannot be pinned down here before a run; whether a boot unlocks is not a matter of wording.
        # It costs no extra boot: the control restarts the machine anyway.
        since = len(m.lines)
        m.mark = since
        power_off(m)
        m.power_on()
        m.wait(TPM_UNLOCK)
        line = m.booted("1", "A")
        locked = [l for l in m.lines[since:] if "LOCKED" in l]
        if locked or "secureboot=on" not in line:
            raise vm.Fail(f"a) control: switched off and on with the same firmware, the data did not simply unlock: {locked[:1] or line}")
        st_a2 = luks_state(m)
        if st_a2["keyslots"] != st_a["keyslots"] or [t["policy"] for t in st_a2["tokens"]] != [t["policy"] for t in st_a["tokens"]]:
            raise vm.Fail("a) control: the key slots changed across a power cycle with the same firmware")
        print("   OK a) control: switched off and on as in b), Secure Boot unchanged: the TPM (its state kept, swtpm "
              "restarted) opened the data by itself (no LOCKED line, the same key slots)", flush=True)

        # ---- (b) the same machine with Secure Boot turned off in its firmware
        power_off(m)
        m.set_secure_boot(False)
        since = lock_mark = len(m.lines)
        m.mark = since
        m.power_on()
        got = m.wait(rf"URBAN-DATA waiting for the recovery key on the console|{TPM_UNLOCK}")
        if "unlocked by this machine's TPM" in got.group(0):
            raise vm.Fail("b) the TPM unlocked the data although Secure Boot was turned off (PCR 7 did not change?)")
        lock_at = time.time()
        tail = m.lines[since:]
        if not any(LOCK_BLOCK.search(l) for l in tail):
            raise vm.Fail(f"b) the DATA LOCKED block is not on the serial console (log: {vm.LOGS}/{m.name}.log)")
        if not any(LOCK_LINE in l for l in tail):
            raise vm.Fail("b) the URBAN-DATA LOCKED line with the recovery instructions is not on the console")
        shown = next((l for l in tail if "URBAN-DATA the lock notice is on the screens:" in l), "")
        if not re.search(r"\btty\d", shown) or "ttyS0" not in shown:
            raise vm.Fail(f"b) the lock notice did not reach both the monitor's console and the serial line: {shown!r}")
        if any("unlocked by this machine's TPM" in l for l in tail):
            raise vm.Fail("b) the TPM unlocked the data although Secure Boot was turned off")
        if not any(LOCK_SB_OFF_BLOCK.search(l) for l in tail) or not any(LOCK_SB_OFF_LINE in l for l in tail):
            raise vm.Fail("b) the lock notice does not say that Secure Boot is off and should be turned on before the key "
                          "is typed (block and journal line)")
        said = next((l for l in tail if "URBAN-DATA the TPM did not release the key" in l), "").strip()
        time.sleep(5)
        m.screendump(f"{vm.LOGS}/pcr7-locked.ppm")  # the unit's own monitor while it waits, for people to look at
        while time.time() < lock_at + LOCK_WINDOW:
            after = m.lines[since:]
            first = next((i for i, l in enumerate(after) if LOCK_LINE in l), 0)
            up = [l for l in after[first:] if STARTED.search(l)]
            if up:
                raise vm.Fail(f"b) something of the unit started while its data was locked: {up[0].strip()[:200]}")
            time.sleep(5)
        r = m.ssh("true", check=False)
        if r.returncode == 0:
            raise vm.Fail("b) SSH answers on a unit whose data is locked")
        raw = raw_luks_header(m.disk)
        if not raw["magic"] or raw["uuid"] != st_a["uuid"] or not raw["ok"] or raw["keyslots"] != st_a2["keyslots"] \
                or raw["tokens"] != sorted(t["type"] for t in st_a2["tokens"]):
            raise vm.Fail(f"b) the raw data partition is not the same LUKS2 volume any more: {raw}")
        print(f"   OK b) Secure Boot turned off on the same machine: '{said[:110]}'; the DATA LOCKED block, with the advice "
              f"to turn Secure Boot on before typing the key, on {shown.split(':', 1)[1].strip()}; "
              f"watched {LOCK_WINDOW} s: nothing of the unit started, SSH does not answer; the raw partition is still LUKS2 "
              f"{raw['uuid']} with key slots {raw['keyslots']} and tokens {raw['tokens']} (nothing wiped); "
              f"screen saved as {vm.LOGS}/pcr7-locked.ppm", flush=True)

        typed = []  # every key typed at the prompt: none may show on the console
        wrong = "-".join("".join(secrets.choice(MODHEX) for _ in range(8)) for _ in range(8))
        if type_key(m, wrong, typed):
            raise vm.Fail("b) a wrong recovery key opened the data")
        m.wait(r"URBAN-DATA waiting for the recovery key on the console", 300)
        if m.ssh("true", check=False).returncode == 0:
            raise vm.Fail("b) SSH answers after a wrong key")
        not_echoed(m, typed, "b)")
        print(f"   OK b) a wrong key typed at '{PROMPT}' on the console: 'not the recovery key of this data partition', "
              "asked again, still locked; the key typed did not show on the console", flush=True)

        # ---- (c) the recovery key from the ESP, decrypted with the offline escrow key, typed on the console
        vm.esp_get(m.disk, "/urban-recovery.enc", "/tmp/pcr7-recovery.enc")
        if recovery_header(open("/tmp/pcr7-recovery.enc").read()).get("volume") != st_a["uuid"]:
            raise vm.Fail("c) the urban-recovery.enc on the ESP is not the one of this volume")
        out = vm.sh(["bash", RECOVERY_TOOL, "decrypt", "/tmp/pcr7-recovery.enc", f"{ESCROW_DIR}/escrow.key"]).stdout
        found = RECOVERY_KEY.search(out)
        if not found:
            raise vm.Fail(f"c) the recovery tool printed no recovery key:\n{out[:600]}")
        rk = found.group(1)
        if not type_key(m, rk, typed):
            m.wait(r"URBAN-DATA waiting for the recovery key on the console", 300)
            if not type_key(m, rk.upper().replace("-", " "), typed):  # also: capitals and blanks
                raise vm.Fail("c) the recovery key decrypted from urban-recovery.enc does not open the data")
        waited_s = int(time.time() - lock_at)
        m.wait(re.escape(RESEALED), 600)
        e = m.wait(r"URBAN-ENROLMENT unit=(\S+) serial=(\S+) fingerprint=(\S+)")
        if e.group(3) != fp:
            raise vm.Fail(f"c) the unit's device key changed (the data was not the same): {fp} -> {e.group(3)}")
        line = m.booted("1", "A")
        for need in ("data=rw,luks2-tpm2", "secureboot=off"):
            if need not in line:
                raise vm.Fail(f"c) the self-check line lacks {need}: {line}")
        times = m.ssh(
            "journalctl -b -o json -u urban-grow.service | python3 -c '\n"
            "import json, sys\n"
            "for l in sys.stdin:\n"
            "    e = json.loads(l)\n"
            "    if str(e.get(\"MESSAGE\", \"\")).startswith(\"URBAN-DATA unlocked with the recovery key\"):\n"
            "        print(\"unlocked\", e[\"__MONOTONIC_TIMESTAMP\"]); break\n"
            "'; for u in " + " ".join(WAITED) + "; do echo \"$u $(systemctl show -p InactiveExitTimestampMonotonic --value $u)\"; done"
        ).stdout
        mono = dict(l.split()[:2] for l in times.splitlines() if len(l.split()) >= 2)
        unlocked_us = int(mono.get("unlocked", "0") or 0)
        if not unlocked_us:
            raise vm.Fail(f"c) the journal has no 'unlocked with the recovery key' line of urban-grow:\n{times}")
        early = [u for u in WAITED if not mono.get(u, "").isdigit() or int(mono[u]) == 0 or int(mono[u]) < unlocked_us]
        if early:
            raise vm.Fail(f"c) began before the recovery key was accepted (or never): {early}\n{times}")
        st_c = luks_state(m)
        tpm_c, rec_c = sealed_to_pcr7(st_c, "c) after the recovery")
        if tpm_c["policy"] == tpm_a["policy"] or tpm_c["blob"] == tpm_a["blob"]:
            raise vm.Fail("c) the TPM key slot was not sealed again (same policy and blob as before)")
        if rec_c["keyslots"] != rec_a["keyslots"] or st_c["uuid"] != st_a["uuid"]:
            raise vm.Fail(f"c) the recovery slot or the volume changed: {rec_a['keyslots']} -> {rec_c['keyslots']}")
        not_echoed(m, typed, "c)")
        for k in typed:
            if m.ssh(f"journalctl -b --no-pager | grep -cF {shlex.quote(k)}", check=False).stdout.strip() not in ("", "0"):
                raise vm.Fail("c) a key typed at the prompt was written to the unit's journal")
        seal = m.ssh("cat /run/urban/data-seal").stdout.strip()
        screen = m.ssh("cat /run/urban/console.issue").stdout
        if seal != "pcr7-resealed-sb-off":
            raise vm.Fail(f"c) the unit does not say it re-sealed while Secure Boot is off (data-seal {seal!r})")
        if SB_OFF_SCREEN not in screen or not any(SB_OFF_LINE in l for l in m.lines[lock_mark:]):
            raise vm.Fail(f"c) re-sealed with Secure Boot off, the unit does not say the seal protects nothing on this "
                          f"machine until it is on (journal line, screen):\n{screen[-900:]}")
        print(f"   OK c) urban-recovery.enc from the ESP decrypted with the escrow private key ({RECOVERY_TOOL}): key "
              f"{rk[:8]}-...; typed at the prompt on the serial console: '{UNLOCKED}', '{RESEALED}'; the same device key "
              f"{fp}; the data mount and {len(WAITED) - 1} services began only after the key was accepted (locked for about "
              f"{waited_s} s); the TPM slot sealed anew to PCR 7 (new policy hash), the recovery slot "
              f"{rec_c['keyslots']} kept; no key typed shows on the console or in the journal; data-seal {seal}, the "
              f"screen: '{SB_OFF_SCREEN}'; {line[line.index('URBAN-OS-CHECK'):][:80]}", flush=True)

        # ---- (d) reboot with the same firmware (Secure Boot off): sealed to this state now
        since = len(m.lines)
        m.mark = since
        m.reboot()
        m.wait(TPM_UNLOCK)
        line = m.booted("1", "A")
        tail = m.lines[since:]
        if any("LOCKED" in l for l in tail) or any(RESEALED in l for l in tail) or "secureboot=off" not in line:
            raise vm.Fail(f"d) the reboot did not simply unlock with the TPM: {[l for l in tail if 'URBAN-DATA' in l][:4]}")
        st_d = luks_state(m)
        if st_d["keyslots"] != st_c["keyslots"]:
            raise vm.Fail("d) the key slots changed across the reboot")
        seal = m.ssh("cat /run/urban/data-seal").stdout.strip()
        screen = m.ssh("cat /run/urban/console.issue").stdout
        if seal != "pcr7-sb-off" or SB_OFF_SCREEN not in screen or not any(SB_OFF_LINE in l for l in tail):
            raise vm.Fail(f"d) sealed with Secure Boot off, the unit does not say the seal protects nothing on this machine "
                          f"until it is on (data-seal {seal!r}, journal line, screen):\n{screen[-900:]}")
        not_echoed(m, typed, "d)")
        print(f"   OK d) rebooted with Secure Boot still off: the TPM opened the data by itself, no prompt; data-seal "
              f"{seal}, the screen: '{SB_OFF_SCREEN}' ({line[line.index('URBAN-OS-CHECK'):][:80]})", flush=True)
    finally:
        m.stop()
    if rk:
        for label in ("esp", "urban-data"):
            if vm.raw_partition_has(m.disk, label, rk.encode()):
                raise vm.Fail(f"the recovery key lies in clear on the raw {label} partition")
        print("   OK the recovery key itself is nowhere in clear on the disk (raw ESP and data partition)", flush=True)
    os.remove(m.disk)
    print("PCR7 CHECK PASSED", flush=True)


# ---------------------------------------------------------------- the escrow in the image (G3)

def _openssl(*args, text=False):
    return subprocess.run(["openssl", *args], capture_output=True, check=True, text=text).stdout


def _needles(key):
    """Byte strings that any copy of the escrow private key would carry: its prime p (raw, in any DER of it, and in hex) and
    a line from the middle of its PEM, PKCS#8 and PKCS#1."""
    text = _openssl("rsa", "-in", key, "-noout", "-text", text=True)
    m = re.search(r"^prime1:\s*\n((?:[ \t]+[0-9a-f:]+\n)+)", text, re.M)
    if not m:
        raise vm.Fail(f"{key}: no prime1 in its text form: not an RSA private key?")
    p = bytes.fromhex(re.sub(r"[^0-9a-f]", "", m.group(1))).lstrip(b"\0")

    def middle(pem):
        body = [l for l in pem.splitlines() if l.strip() and not l.startswith("-----")]
        return body[len(body) // 2].encode()

    return {"its prime p (raw bytes, in any DER copy)": p, "its prime p in hex": p.hex().encode(),
            "a line of its PEM (PKCS#8)": middle(open(key).read()),
            "a line of its PEM (PKCS#1)": middle(_openssl("rsa", "-in", key, "-traditional", text=True))}


def _scan(f, needles, limit=None):
    keep, tail, found, left = max(len(n) for n in needles.values()) - 1, b"", set(), limit
    while left is None or left > 0:
        chunk = f.read(8 << 20 if left is None else min(8 << 20, left))
        if not chunk:
            break
        if left is not None:
            left -= len(chunk)
        buf = tail + chunk
        found |= {name for name, n in needles.items() if n in buf}
        tail = buf[-keep:]
    return found


def escrow_in_image(disk, parts, esp):
    """The image carries the recovery escrow's public key (the one in data/os/keys/escrow) and the tools urban-grow needs
    for it; the escrow private key is nowhere in it: not as a file, not as bytes in any file of the system, not in the raw
    ESP or data partition. The scan has its control: it finds the key in the key's own PEM and DER."""
    pub, key = f"{ESCROW_DIR}/escrow.pem", f"{ESCROW_DIR}/escrow.key"
    if not os.path.exists(pub):
        raise vm.Fail(f"{pub} is missing: keys.sh makes the recovery escrow key")
    slot = f"{vm.OUT}/build/rootfs-1.img"
    listing = vm.sh(["unsquashfs", "-l", slot]).stdout
    for what, path in (("the escrow public key", "usr/share/urban/recovery-escrow.pem"), ("openssl", "usr/bin/openssl"),
                       ("systemd-cryptenroll", "usr/bin/systemd-cryptenroll"),
                       ("systemd-ask-password", "usr/bin/systemd-ask-password")):
        if not re.search(r"(^|/)" + re.escape(path) + r"$", listing, re.M):
            raise vm.Fail(f"the image lacks {what} ({path})")
    named = [l for l in listing.splitlines() if re.search(r"escrow", l, re.I) and not l.endswith("usr/share/urban/recovery-escrow.pem")]
    if named:
        raise vm.Fail(f"the image has other escrow files: {named[:5]}")
    work = "/tmp/escrow-scan"
    shutil.rmtree(work, ignore_errors=True)
    r = vm.sh(["unsquashfs", "-q", "-n", "-no-xattrs", "-d", work, slot], check=False)
    if not os.path.isfile(f"{work}/etc/fstab"):
        raise vm.Fail(f"the system could not be unpacked for the scan: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        inside = f"{work}/usr/share/urban/recovery-escrow.pem"
        fpr = escrow_fpr(pub)
        if escrow_fpr(inside) != fpr:
            raise vm.Fail(f"the image's escrow key ({escrow_fpr(inside)}) is not data/os/keys/escrow/escrow.pem ({fpr})")
        if not os.path.exists(key):
            print(f"   escrow: the image carries the escrow public key {fpr[:23]}... (usr/share/urban/recovery-escrow.pem); "
                  "escrow.key is not on this machine (kept offline): its bytes were not scanned for", flush=True)
            return
        needles = _needles(key)
        der = "/tmp/escrow-scan.der"
        with open(der, "wb") as f:
            f.write(_openssl("pkey", "-in", key, "-outform", "DER"))
        with open(key, "rb") as f:
            pem_hits = _scan(f, needles)
        with open(der, "rb") as f:
            der_hits = _scan(f, needles)
        os.remove(der)
        if "a line of its PEM (PKCS#8)" not in pem_hits or "its prime p (raw bytes, in any DER copy)" not in der_hits:
            raise vm.Fail(f"control: the scan does not find the private key in its own files (PEM: {pem_hits}, DER: {der_hits})")
        files = size = 0
        for root, _, names in os.walk(work):
            for n in names:
                path = os.path.join(root, n)
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue
                files, size = files + 1, size + st.st_size
                with open(path, "rb") as f:
                    hit = _scan(f, needles)
                if hit:
                    raise vm.Fail(f"the escrow private key is in the image: {path[len(work):]} holds {sorted(hit)}")
        for label in ("esp", "urban-data"):
            with open(disk, "rb") as f:
                f.seek(parts[label]["start"] * 512)
                hit = _scan(f, needles, parts[label]["size"] * 512)
            if hit:
                raise vm.Fail(f"the escrow private key is on the image's raw {label} partition: {sorted(hit)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"   escrow: the image carries the escrow public key {fpr[:23]}... (usr/share/urban/recovery-escrow.pem, the same as "
          f"data/os/keys/escrow/escrow.pem) and openssl, systemd-cryptenroll, systemd-ask-password; no trace of the private "
          f"key in {files} files ({size >> 20} MiB) of the system, nor in the raw ESP and data partition "
          f"(control: the scan finds it in its own PEM and DER)", flush=True)
