"""Step 13, G3: the control centre updates itself with one admin command, in QEMU.

    python3 vm.py center-update        (dispatched by vm.py; this module is not run on its own)

Two machines: the centre (x86, the pilot's district,center as in the other scenarios) and an x86 light unit edge-J-01
enrolled straight to it. The centre keeps release 2 and a broken release 3 (a firewall that does not load), added with
`urban-center release add` in a first wave of the centre's own name only, so that no unit is offered them. Then:

  * `urban-center release self-update 2` on the centre: the command hands the package to the root update helper (the
    same `urban-unit apply-update` behind urban-update.path that installs a unit's update; its console words
    URBAN-UPDATE installing / installed), the centre restarts into version 2 in slot B; its database (the audit chain,
    every record before the update unchanged and the chain intact), the unit's enrolment (Devices) and the platform's
    files (device registry, accounts, keys, releases) are the same as before; the unit comes back online by itself; the
    audit says centre-update-started and centre-updated, once each; GET /api/centre-os shows version 2 and the update;
  * `urban-center release self-update 3` (broken): installed by the same helper into slot A, its self-check fails, the
    centre goes back to version 2 in slot B by itself, with the same data, the unit online again, the audit says
    centre-rolled-back (`started 3, back on 2`) once, and release 3 is halted as a unit's rollback halts a release;
  * the same command for the halted release 3 is refused with the reason, and nothing is staged or audited.

vm.py's names are taken from the running vm.py (`__main__`), as in vm_rollout.py, whose helpers this reuses.
"""
import sys
import time

import vm_rollout as ro

vm = sys.modules["__main__"]

X86 = "urban-edge-x86_64"
UID = "edge-J-01"
PLATFORM_FILES = ("pki/devices.json", "pki/gateway.key", "pki/gateway.cert.json", "pki/ca.pem", "pki/server.pem",
                  "pki/audit.key", "pki/root.pub", "users.json")
RELEASES_JSON = "releases/releases.json"


def safe(call):
    """What the centre answers, or None while it is restarting (no connection, a reset, a timeout)."""
    try:
        return call()
    except (OSError, vm.Fail):
        return None


def platform_files(centre, names):
    """sha256 of the centre's platform files on its data partition."""
    out = centre.ssh("cd /var/lib/urban/platform && sha256sum " + " ".join(names), check=False).stdout
    got = {line.split()[1].lstrip("*"): line.split()[0] for line in out.splitlines() if len(line.split()) == 2}
    if set(got) != set(names):
        raise vm.Fail(f"the centre's platform files are not all there: {sorted(set(names) - set(got))}\n{out}")
    return got


def snapshot(centre, api, names=PLATFORM_FILES + (RELEASES_JSON,)):
    """What must be the same after the centre restarted into another system: the enrolled devices as Devices shows
    them, every audit record (sequence -> hash), the platform's files."""
    devices = {d: {k: row.get(k) for k in ("kind", "status", "fingerprint", "enrolled", "junction", "serial",
                                           "district")} for d, row in api.devices().items()}
    audit = {r["seq"]: r["hash"] for r in api.get("/api/history/audit?limit=500")}
    return {"devices": devices, "audit": audit, "files": platform_files(centre, names)}


def same_data(centre, api, before, what, names=PLATFORM_FILES + (RELEASES_JSON,)):
    """The centre's data after the restart against `before`; the audit only grew, and its chain is intact."""
    now = snapshot(centre, api, names)
    if now["devices"] != before["devices"]:
        raise vm.Fail(f"{what}: the enrolled devices changed: {before['devices']} -> {now['devices']}")
    lost = {s: h for s, h in before["audit"].items() if now["audit"].get(s) != h}
    if lost:
        raise vm.Fail(f"{what}: audit records before the update are gone or changed: {sorted(lost)[:10]}")
    if now["files"] != before["files"]:
        changed = sorted(n for n in before["files"] if now["files"].get(n) != before["files"][n])
        raise vm.Fail(f"{what}: the centre's platform files changed: {changed}")
    chain = api.get("/api/history")["chain"]
    if not chain.get("intact") or chain.get("records", 0) < max(before["audit"]):
        raise vm.Fail(f"{what}: the audit chain is not intact: {chain}")
    return now, chain


def events(api, event):
    return [(r["target"], r["note"]) for r in reversed(api.audit(event))]


def self_update(centre, version):
    """The admin's one command on the centre."""
    return centre.ssh(f"urban-center release self-update {version}", check=False, timeout=2400)


def wait_settled(api, event, what, timeout=900):
    """The centre answers again and its audit has `event` (the platform records the outcome at its start)."""
    def check():
        seen = safe(lambda: events(api, event))
        return bool(seen), seen
    return ro.until(what, check, timeout, 10)


def unit_back(api, unit, before):
    """The unit's own service says it is online at the centre again (a new session), and Devices shows it online."""
    ro.wait_online(unit, UID, before)
    row = ro.until(f"{UID} is not shown online at the centre again",
                   lambda: (bool((r := safe(api.devices) or {}).get(UID, {}).get("online")), r.get(UID)), 300, 5)
    return row


def center_update():
    centre = vm.VM("cupd-center", unit="center", serial="PILOT-1", role="district,center", host="urban-center",
                   grow_mb=6144, forward=[(2443, 443), (28443, 8443)], ssh_port=2222)
    unit = vm.VM("cupd-unit", unit=UID, serial="URB-1", vision="off", ssh_port=2223, mem=1024)
    try:
        for m in (centre, unit):
            m.start()
        centre.booted("1", "A")
        unit.booted("1", "A")
        vm.push_city(centre)
        ctx = vm.centre_ca(centre)
        vm.health(ctx)
        ro.enrol(centre, unit, UID)
        ro.wait_online(unit, UID)
        api = ro.Api(centre, ctx)
        ro.until(f"the centre does not show {UID} online", lambda: (bool(api.devices().get(UID, {}).get("online")), None), 600)
        print(f"OK centre (district,center) on version 1 in slot A, {UID} enrolled and online at it", flush=True)

        # no release yet: refused with the reason, nothing staged
        r = self_update(centre, 2)
        if r.returncode == 0 or not r.stderr.strip().startswith(
                f"refused: no release 2 for this machine's compatible ({X86})"):
            raise vm.Fail(f"self-update 2 without a release 2 was not refused: {(r.stderr or r.stdout).strip()[-300:]}")
        if centre.ssh(f"ls -A {vm.INBOX}").stdout.split():
            raise vm.Fail("a refused self-update left something in the update inbox")
        print(f"OK refused before any release: {r.stderr.strip()}", flush=True)

        # releases 2 and 3 (broken), in a first wave of the centre's own name: no unit is offered them
        said = [ro.add_release(centre, f"{vm.OUT}/urban-edge-2.raucb", 2, "--devices", "center"),
                ro.add_release(centre, f"{vm.OUT}/urban-edge-3.raucb", 3, "--devices", "center")]
        for s, v in zip(said, (2, 3)):
            if not s.startswith(f"added release {v} for {X86}:") or "to a first wave: center;" not in s:
                raise vm.Fail(f"release {v} was not added for {X86} in a first wave of the centre's name: {s}")
        print(f"OK centre: {said[0][:100]}... | {said[1][:100]}...", flush=True)

        # ---- release 2 by the one command
        before = snapshot(centre, api)
        online_before = ro.online_count(unit, UID)
        centre.mark = len(centre.lines)
        r = self_update(centre, 2)
        out = r.stdout.strip()
        if r.returncode != 0 or not out.startswith(f"self-update to release 2 for {X86} (from 1) handed to the root "
                                                   f"update helper") \
                or not ("the root update helper is installing release 2 into the other slot" in out
                        or "the root update helper has not answered within" in out):  # its console words are checked below
            raise vm.Fail(f"urban-center release self-update 2: {(r.stderr or r.stdout).strip()[-500:]}")
        print("OK centre: " + " | ".join(out.splitlines()), flush=True)
        since = centre.mark
        vm.wait_helper(centre, "the centre", 2)
        helper = ro.updates(centre, since)
        if not helper or not helper[0].startswith("URBAN-UPDATE installing version=2") or any(" refused " in w for w in helper):
            raise vm.Fail(f"the root update helper did not install release 2: {helper}")
        line = centre.booted("2", "B")
        booted, by_slot = vm.slots(centre)
        if booted != "B" or by_slot["B"].get("boot_status") != "good":
            raise vm.Fail(f"the centre after its self-update: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
        print(f"OK the root update helper installed it ({' | '.join(helper)[:160]}); the centre restarted into slot B: "
              f"{line[line.index('URBAN-OS-CHECK'):][:70]}", flush=True)
        vm.health(ctx)
        wait_settled(api, "centre-updated", "the centre's audit does not say centre-updated after the restart")
        started, updated = events(api, "centre-update-started"), events(api, "centre-updated")
        if started != [("centre", f"release 2 for {X86}, from 1")] or updated != [("centre", "now 2")]:
            raise vm.Fail(f"the self-update was not audited once each: started {started}, updated {updated}")
        now, chain = same_data(centre, api, before, "after the self-update to 2")
        row = unit_back(api, unit, online_before)
        view = api.get("/api/centre-os")
        su = view.get("self_update") or {}
        if view.get("os") != 2 or view.get("compatible") != X86 or (su.get("state"), su.get("from"), su.get("to")) != ("updated", 1, 2):
            raise vm.Fail(f"GET /api/centre-os does not show the centre on 2 after its self-update: {view}")
        if ro.offered(api).get(UID) or (row.get("health") or {}).get("os") != 1:
            raise vm.Fail(f"{UID} was offered the centre's release: {ro.offered(api).get(UID)}, health {row.get('health')}")
        print(f"OK same data after the restart: {len(now['devices'])} enrolled device(s) as before, {len(before['audit'])} "
              f"audit records unchanged (chain intact, {chain['records']} records), platform files identical "
              f"({', '.join(sorted(now['files']))}); {UID} back online by itself (still os 1, offered nothing); audit "
              f"centre-update-started {started[0][1]!r}, centre-updated {updated[0][1]!r}; GET /api/centre-os os "
              f"{view['os']}, last self-update {su['from']} -> {su['to']} {su['state']}", flush=True)

        # ---- the broken release 3 by the same command: back on 2 by itself
        files3 = PLATFORM_FILES  # releases.json changes on purpose: release 3 is halted after the rollback
        before = snapshot(centre, api, files3)
        online_before = ro.online_count(unit, UID)
        centre.mark = len(centre.lines)
        r = self_update(centre, 3)
        out = r.stdout.strip()
        if r.returncode != 0 or not out.startswith(f"self-update to release 3 for {X86} (from 2) handed to the root "
                                                   f"update helper"):
            raise vm.Fail(f"urban-center release self-update 3: {(r.stderr or r.stdout).strip()[-500:]}")
        print("OK centre: " + " | ".join(out.splitlines()), flush=True)
        vm.wait_helper(centre, "the centre", 3)
        m = centre.wait(r"URBAN-OS-CHECK (OK|FAIL) version=(\S+) slot=(\S+)")
        failed = centre.lines[centre.mark - 1]
        if m.group(1) != "FAIL" or m.group(2) != "3" or m.group(3).rstrip(":") != "A":
            raise vm.Fail(f"the broken version 3 did not fail its self-check in slot A: {failed}")
        centre.wait(r"URBAN-HEALTH bad slot=A", 300)
        line = centre.booted("2", "B")
        booted, by_slot = vm.slots(centre)
        if booted != "B" or by_slot["B"].get("boot_status") != "good":
            raise vm.Fail(f"the centre after the rollback: booted {booted}, slot B {by_slot['B'].get('boot_status')}")
        print(f"OK the helper installed version 3, {failed[failed.index('URBAN-OS-CHECK'):][:90]}; the centre went back "
              f"by itself: {line[line.index('URBAN-OS-CHECK'):][:60]}", flush=True)
        vm.health(ctx)
        wait_settled(api, "centre-rolled-back", "the centre's audit does not say centre-rolled-back after the rollback")
        started, back = events(api, "centre-update-started"), events(api, "centre-rolled-back")
        if started != [("centre", f"release 2 for {X86}, from 1"), ("centre", f"release 3 for {X86}, from 2")] \
                or back != [("centre", "started 3, back on 2")] or events(api, "centre-updated") != [("centre", "now 2")]:
            raise vm.Fail(f"the rollback was not audited once: started {started}, rolled back {back}, "
                          f"updated {events(api, 'centre-updated')}")
        now, chain = same_data(centre, api, before, "after the rollback", files3)
        unit_back(api, unit, online_before)
        reason = "rolled back: installed 3, runs 2"
        halted = (api.releases().get((3, X86)) or {}).get("halted") or {}
        if (halted.get("device"), halted.get("reason")) != ("centre", reason) \
                or events(api, "rollout-halted") != [("release-3", f"centre ({X86}): {reason}")]:
            raise vm.Fail(f"release 3 was not halted after the centre's rollback: {halted}, "
                          f"audit {events(api, 'rollout-halted')}")
        view = api.get("/api/centre-os")
        su = view.get("self_update") or {}
        if view.get("os") != 2 or (su.get("state"), su.get("to"), su.get("running")) != ("rolled-back", 3, 2):
            raise vm.Fail(f"GET /api/centre-os does not show the rollback: {view}")
        print(f"OK same data after the rollback ({len(before['audit'])} audit records unchanged, chain intact with "
              f"{chain['records']}, devices and platform files identical); {UID} back online by itself; audit "
              f"centre-rolled-back {back[0][1]!r}, rollout-halted release-3 by the centre; GET /api/centre-os os "
              f"{view['os']}, last self-update {su['from']} -> {su['to']} {su['state']}", flush=True)

        # ---- the halted release: refused by the command, nothing staged, nothing audited
        count = len(api.audit("centre-update-started"))
        r = self_update(centre, 3)
        words = f"refused: release 3 is halted (by centre ({X86}): {reason}"
        if r.returncode == 0 or not r.stderr.strip().startswith(words):
            raise vm.Fail(f"self-update of the halted release 3 was not refused: {(r.stderr or r.stdout).strip()[-400:]}")
        time.sleep(10)  # the platform looks every 5 s: a record written by mistake would be audited by now
        inbox = centre.ssh(f"ls -A {vm.INBOX}").stdout.split()
        su_now = (api.get("/api/centre-os").get("self_update") or {})
        if inbox or len(api.audit("centre-update-started")) != count or su_now.get("state") != "rolled-back":
            raise vm.Fail(f"the refused self-update left traces: inbox {inbox}, record {su_now}")
        print(f"OK the halted release refused: {r.stderr.strip()} (inbox empty, nothing audited, the last self-update "
              f"still the rollback)", flush=True)
        print("CENTER UPDATE CHECK PASSED", flush=True)
    finally:
        for m in (centre, unit):
            vm.drop(m)
