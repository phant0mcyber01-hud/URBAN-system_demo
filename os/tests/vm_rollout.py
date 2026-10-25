"""Step 12, G4: releases by architecture and a staged rollout that halts by itself, in QEMU.

    python3 vm.py rollout        (dispatched by vm.py; this module is not run on its own)

Three machines: the centre (x86, the pilot's district,center as in the other scenarios), an x86 light unit edge-J-01 and
an arm64 light unit edge-J-02 (QEMU aarch64, emulated: every wait is vm.ARM_PACE times longer), both enrolled directly
to the centre (no district). The centre holds release 2 for x86 and release 2 for arm64, added with `urban-center
release add`, which reads each package's compatible from the signed package. Each unit reports its own compatible in
its health, is offered only its own platform's package (the centre's audit of every first offer: update-offered with
the package's compatible; neither unit's helper ever sees a package it refuses), installs it and boots version 2.

Then release 3, a broken system (a firewall that does not load), goes out for both platforms in a first wave of the x86
unit only. The x86 unit installs it, fails its self-check and rolls back to version 2 by itself; the centre sees it
come back on 2 and halts release 3 by itself (audited as rollout-halted with the device and the reason; GET
/api/releases and `release list` show it). The admin then widens the wave to every device: the arm64 unit, now in the
wave and connected anew, is still never offered its release 3 while the rollout is halted. The control: once the
admin resumes the release, the arm64 unit is offered it at once (so the halt was what held it back); the release is
then withdrawn before anything else happens.

vm.py's names are taken from the running vm.py (`__main__`), as in vm_arm.py.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

vm = sys.modules["__main__"]

X86, ARM = "urban-edge-x86_64", "urban-edge-aarch64"
UNITS = {"edge-J-01": {"junction": "J-01", "serial": "URB-1", "compatible": X86},
         "edge-J-02": {"junction": "J-02", "serial": "URB-2", "compatible": ARM}}
PLATFORM = "10.0.2.2:28443"  # the centre's device listener as a VM reaches it (the container's forwarded port)
WINDOW = 120  # seconds the arm64 unit is watched, connected anew, while release 3 is halted (it reports every second)


class Api:
    """The centre's API as the interface reads it: signed in as the first admin (the password is in the platform's
    journal), a new session after a 401."""

    def __init__(self, centre, ctx, port=2443):
        self.ctx, self.port, self.token = ctx, port, None
        self.password = vm.admin_password(centre, "/tmp/rollout-admin.txt")
        if not self.password:
            raise vm.Fail("the centre's first admin password is not in its journal")

    def _call(self, path, body=None):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})}
        req = urllib.request.Request(f"https://127.0.0.1:{self.port}{path}", headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
            return json.loads(r.read())

    def get(self, path):
        for _ in range(2):
            if not self.token:
                try:
                    self.token = self._call("/api/login", {"name": "admin", "password": self.password})["token"]
                except urllib.error.HTTPError as err:
                    raise vm.Fail(f"signing in to the centre failed: {err.code} {err.read().decode('utf-8', 'replace')[:200]}")
            try:
                return self._call(path)
            except urllib.error.HTTPError as err:
                if err.code != 401:
                    raise vm.Fail(f"GET {path}: {err.code} {err.read().decode('utf-8', 'replace')[:200]}")
                self.token = None
        raise vm.Fail(f"GET {path}: the centre keeps refusing the session")

    def devices(self):
        return {d["id"]: d for d in self.get("/api/devices")}

    def releases(self):
        return {(r["version"], r["compatible"]): r for r in self.get("/api/releases")}

    def audit(self, event):
        """The audit records of this event the centre keeps (its store: newest first, the last 500)."""
        return [r for r in self.get("/api/history/audit?limit=500") if r.get("event") == event]


def until(what, check, timeout, step=10):
    """check() -> (done, what it saw) until done; returns what it saw, which also goes into the failure."""
    end, seen = time.time() + timeout, None
    while time.time() < end:
        done, seen = check()
        if done:
            return seen
        time.sleep(step)
    raise vm.Fail(f"{what} within {timeout} s (last seen: {json.dumps(seen, sort_keys=True, default=str)[:600]})")


def enrol(centre, unit, uid):
    """What the administrator does, by the centre's CLI: the unit at its junction, straight to the centre; its kit onto
    it; the kit's platform pointed at the centre's forwarded device port (the certificate is still checked for
    urban-center, the name the kit carries)."""
    info = UNITS[uid]
    code = unit.ssh("urban-unit code").stdout.strip()
    if not code.startswith(f"URBAN1:{uid}:"):
        raise vm.Fail(f"{uid} printed no enrolment code: {code!r}")
    centre.ssh(f"{vm.PROVISION} enroll {uid} --junction {info['junction']} --public {code.split(':')[3]} --serial {info['serial']}")
    kit = centre.ssh(f"{vm.PROVISION} kit {uid}").stdout
    if '"urban_kit"' not in kit:
        raise vm.Fail(f"no kit for {uid}: {kit[-300:]}")
    with open(f"/tmp/kit-{uid}.json", "w") as f:
        f.write(kit)
    unit.put(f"/tmp/kit-{uid}.json", f"{vm.STAGE}/kit.json")
    r = unit.ssh(f"urban-unit setup {vm.STAGE}/kit.json", check=False)
    if r.returncode != 0:
        raise vm.Fail(f"urban-unit setup on {uid}: {(r.stderr or r.stdout).strip()[-300:]}")
    unit.ssh(f"sed -i 's|^PLATFORM=.*|PLATFORM={PLATFORM}|' /etc/urban/edge-{uid}.env && systemctl restart urban-edge@{uid}")


def online_count(unit, uid):
    log = unit.ssh(f"journalctl -u urban-edge@{uid} --no-pager -o cat", check=False).stdout
    return len(re.findall(r"edge: \S+ online at", log))


def wait_online(unit, uid, before=0):
    """The unit's service says it is online at the centre for the (before + 1)-th time."""
    def check():
        if online_count(unit, uid) > before:
            return True, None
        return False, unit.ssh(f"journalctl -u urban-edge@{uid} --no-pager -n 5 -o cat", check=False).stdout.strip()[-300:]
    until(f"urban-edge@{uid} did not come online at the centre", check, 240 * unit.pace, 5)


def add_release(centre, local, version, *wave):
    """One release on the centre, as the admin adds it: the package copied to the centre, `urban-center release add`."""
    name = local.rsplit("/", 1)[-1]
    centre.put(local, f"{vm.STAGE}/{name}")
    r = centre.ssh(f"urban-center release add {vm.STAGE}/{name} --version {version} {' '.join(wave)}", check=False,
                   timeout=2400)
    centre.ssh(f"rm -f {vm.STAGE}/{name}")
    if r.returncode != 0:
        raise vm.Fail(f"urban-center release add {name}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def updates(unit, since=0):
    """The update helper's words on the unit's console since line `since`."""
    return [l[l.index("URBAN-UPDATE"):] for l in unit.lines[since:] if "URBAN-UPDATE" in l]


def offered(api):
    """Which packages the centre offered to which device (each first offer is audited): {device: [notes]}."""
    out = {}
    for r in reversed(api.audit("update-offered")):
        out.setdefault(r["target"], []).append(r["note"])
    return out


def rollout():
    centre = vm.VM("rollout-center", unit="center", serial="PILOT-1", role="district,center", host="urban-center",
                   grow_mb=4096, forward=[(2443, 443), (28443, 8443)], ssh_port=2222)
    x86 = vm.VM("rollout-x86", unit="edge-J-01", serial="URB-1", vision="off", ssh_port=2223, mem=1024)
    arm = vm.VM("rollout-arm", arch="arm64", unit="edge-J-02", serial="URB-2", vision="off", ssh_port=2224, mem=1024)
    units = {"edge-J-01": x86, "edge-J-02": arm}
    try:
        for m in (centre, x86, arm):
            m.start()
        centre.booted("1", "A")
        x86.booted("1", "A")
        arm.booted("1", "A")
        vm.push_city(centre)
        ctx = vm.centre_ca(centre)
        vm.health(ctx)
        for uid, unit in units.items():
            enrol(centre, unit, uid)
        for uid, unit in units.items():
            wait_online(unit, uid)
        api = Api(centre, ctx)

        # every unit reports its own system in its health; the centre's Devices shows it
        def reported():
            rows = api.devices()
            ok = all(rows.get(u, {}).get("online") and (rows[u].get("health") or {}).get("compatible") == i["compatible"]
                     and rows[u].get("compatible") == i["compatible"] and (rows[u].get("health") or {}).get("os") == 1
                     for u, i in UNITS.items())
            return ok, (rows if ok else {u: {k: rows.get(u, {}).get(k) for k in ("online", "health", "compatible")}
                                         for u in UNITS})
        rows = until("the centre does not show both units online on os 1 with their own compatible", reported, 600)
        print("OK both units online at the centre, each with its own system in its health: " + "; ".join(
            f"{u} os {rows[u]['health']['os']}, compatible {rows[u]['health']['compatible']}" for u in UNITS), flush=True)

        # ---- release 2 for each platform: the compatible comes from the signed package
        said = [add_release(centre, f"{vm.OUT}/urban-edge-2.raucb", 2),
                add_release(centre, f"{vm.OUT}/urban-edge-arm64-2.raucb", 2)]
        if not said[0].startswith(f"added release 2 for {X86}:") or not said[1].startswith(f"added release 2 for {ARM}:"):
            raise vm.Fail(f"release add did not read the packages' compatibles: {said}")
        listing = centre.ssh("urban-center release list").stdout
        for compatible in (X86, ARM):
            if not re.search(rf"^\s*2 offered\s+{compatible} ", listing, re.M):
                raise vm.Fail(f"release 2 for {compatible} is not listed as offered:\n{listing}")
        print(f"OK centre: {said[0][:110]}... | {said[1][:110]}...", flush=True)
        print("   list:\n" + "\n".join(f"   {l}" for l in listing.strip().splitlines()), flush=True)

        # each unit installs its own platform's package by itself and boots version 2
        x86_mark, arm_mark = len(x86.lines), len(arm.lines)
        for uid, unit in units.items():
            vm.wait_helper(unit, uid, 2)
        for uid, unit in units.items():
            line = unit.booted("2", "B")
            booted, by_slot = vm.slots(unit)
            if booted != "B" or by_slot["B"].get("boot_status") != "good":
                raise vm.Fail(f"{uid} after the update: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
            print(f"OK {uid}: installed version 2 by itself, rebooted into slot B, {line[line.index('URBAN-OS-CHECK'):][:70]}",
                  flush=True)
        for uid, unit, mark in (("edge-J-01", x86, x86_mark), ("edge-J-02", arm, arm_mark)):
            words = updates(unit, mark)
            if any(" refused " in w for w in words) or not any(w.startswith("URBAN-UPDATE installed version=2") for w in words):
                raise vm.Fail(f"{uid}: its update helper said {words}: it was handed a package it refused")

        def on_two():
            rows = api.devices()
            done = all(rows.get(u, {}).get("online") and (rows[u].get("health") or {}).get("os") == 2 for u in UNITS)
            return done, {u: {k: rows.get(u, {}).get(k) for k in ("online", "health")} for u in UNITS}
        until("the centre does not show both units online on os 2", on_two, 900)
        offers = offered(api)
        want = {"edge-J-01": [f"release 2 for {X86}"], "edge-J-02": [f"release 2 for {ARM}"]}
        if {u: offers.get(u) for u in UNITS} != want:
            raise vm.Fail(f"the centre's offers were not each unit's own platform's package: {offers}")
        print(f"OK the centre offered each unit only its own platform's package (audit update-offered): "
              f"{json.dumps({u: offers[u] for u in UNITS})}; neither helper refused anything", flush=True)

        # ---- release 3, broken, for both platforms, in a first wave of the x86 unit only
        x86_mark, arm_mark = len(x86.lines), len(arm.lines)
        said = [add_release(centre, f"{vm.OUT}/urban-edge-3.raucb", 3, "--devices", "edge-J-01"),
                add_release(centre, f"{vm.OUT}/urban-edge-arm64-3.raucb", 3, "--devices", "edge-J-01")]
        for s, compatible in zip(said, (X86, ARM)):
            if not s.startswith(f"added release 3 for {compatible}:") or "to a first wave: edge-J-01;" not in s:
                raise vm.Fail(f"release 3 was not added for {compatible} in a first wave of edge-J-01: {s}")
        print(f"OK centre: release 3 (broken) for both platforms, first wave edge-J-01: {said[0][:100]}... | "
              f"{said[1][:100]}...", flush=True)
        vm.wait_helper(x86, "edge-J-01", 3)
        m = x86.wait(r"URBAN-OS-CHECK (OK|FAIL) version=(\S+) slot=(\S+)")
        failed = x86.lines[x86.mark - 1]
        if m.group(1) != "FAIL" or m.group(2) != "3" or m.group(3).rstrip(":") != "A":
            raise vm.Fail(f"the broken version 3 did not fail its self-check in slot A: {failed}")
        x86.wait(r"URBAN-HEALTH bad slot=A", 300)
        line = x86.booted("2", "B")
        print(f"OK edge-J-01: installed version 3, {failed[failed.index('URBAN-OS-CHECK'):][:90]}, rolled back by "
              f"itself: {line[line.index('URBAN-OS-CHECK'):][:60]}", flush=True)

        # the rollout halts by itself; Devices (GET /api/releases) shows it with the device and the reason
        reason = "rolled back: installed 3, runs 2"

        def halted():
            rel = api.releases()
            marks = [rel.get((3, c), {}).get("halted") or {} for c in (X86, ARM)]
            return all(h.get("device") == "edge-J-01" and h.get("reason") == reason for h in marks), marks
        halt = until("the centre did not halt release 3 after the rollback", halted, 900)[0]
        events = api.audit("rollout-halted")
        if [(e["target"], e["note"]) for e in events] != [("release-3", f"edge-J-01 ({X86}): {reason}")]:
            raise vm.Fail(f"the halt was not audited once with the device and the reason: {events}")
        listing = centre.ssh("urban-center release list").stdout
        for compatible in (X86, ARM):
            if not re.search(rf"^\s*3 HALTED\s+{compatible} ", listing, re.M):
                raise vm.Fail(f"release list does not show release 3 for {compatible} halted:\n{listing}")
        if f"halted by edge-J-01 ({X86}): {reason}" not in listing:
            raise vm.Fail(f"release list does not say who halted release 3 and why:\n{listing}")
        print(f"OK the rollout halted by itself: GET /api/releases release 3 (x86 and arm64) halted by {halt['device']}: "
              f"{halt['reason']} at {halt['at']}; audit rollout-halted {events[0]['target']}: {events[0]['note']}", flush=True)
        print("   list:\n" + "\n".join(f"   {l}" for l in listing.strip().splitlines()), flush=True)

        # ---- while halted: the admin widens the wave to every device; the arm64 unit, connected anew, gets nothing
        r = centre.ssh("urban-center release promote 3 --all", check=False)
        if r.returncode != 0 or f"promoted release 3 for {ARM}: now to every unit and district" not in r.stdout \
                or "nothing of it is offered until `release resume 3`" not in r.stdout:
            raise vm.Fail(f"release promote 3 --all: {(r.stderr or r.stdout).strip()[-400:]}")
        print("OK centre: " + " | ".join(r.stdout.strip().splitlines()), flush=True)
        if api.releases()[(3, ARM)]["in_wave"] != ["edge-J-02"]:
            raise vm.Fail(f"after promote --all the arm64 unit is not in release 3's wave: {api.releases()[(3, ARM)]}")
        arm_watch = len(arm.lines)
        before = online_count(arm, "edge-J-02")
        arm.ssh("systemctl restart urban-edge@edge-J-02")  # a new session: the hub's offer at session start too
        wait_online(arm, "edge-J-02", before)
        time.sleep(WINDOW)
        offers = offered(api)
        inbox = arm.ssh(f"ls -A {vm.INBOX}").stdout.split()
        row = api.devices()["edge-J-02"]
        if any(n.startswith("release 3") for n in offers.get("edge-J-02", [])) or updates(arm, arm_watch) or inbox \
                or (row.get("health") or {}).get("os") != 2 or (row.get("health") or {}).get("update"):
            raise vm.Fail(f"the arm64 unit was offered release 3 while it was halted: offers {offers.get('edge-J-02')}, "
                          f"helper {updates(arm, arm_watch)}, inbox {inbox}, health {row.get('health')}")
        installs = [w for w in updates(x86, x86_mark) if w.startswith("URBAN-UPDATE installing")]
        if len(installs) != 1 or x86.ssh(f"ls -A {vm.INBOX}").stdout.split():
            raise vm.Fail(f"edge-J-01 was offered release 3 again after it rolled back: {updates(x86, x86_mark)}")
        print(f"OK while halted, in the wave and connected anew, edge-J-02 was not offered release 3 for {WINDOW} s "
              f"(audit offers {offers.get('edge-J-02')}, its inbox empty, os {row['health']['os']}); edge-J-01 was not "
              f"offered it again (one install of version 3 in all, its inbox empty)", flush=True)

        # the control: resumed, the arm64 unit is offered its release 3 at once, so the halt was what held it back
        r = centre.ssh("urban-center release resume 3", check=False)
        if r.returncode != 0 or not r.stdout.startswith("resumed release 3: offered again to its wave"):
            raise vm.Fail(f"release resume 3: {(r.stderr or r.stdout).strip()[-300:]}")
        until("the control failed: after resume the arm64 unit was not offered its release 3",
              lambda: (f"release 3 for {ARM}" in (seen := offered(api).get("edge-J-02", [])), seen), 120 * arm.pace, 5)
        r = centre.ssh("urban-center release withdraw 3", check=False)
        if r.returncode != 0 or f"withdrawn release 3 for {ARM}" not in r.stdout:
            raise vm.Fail(f"release withdraw 3: {(r.stderr or r.stdout).strip()[-300:]}")
        print(f"OK control: resumed, edge-J-02 was offered release 3 for {ARM} at once (audit update-offered); then "
              f"withdrawn: {' | '.join(r.stdout.strip().splitlines())}", flush=True)
        print("ROLLOUT CHECK PASSED", flush=True)
    finally:
        for m in (centre, x86, arm):
            vm.drop(m)
