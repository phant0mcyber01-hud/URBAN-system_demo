"""The control centre updates itself (step 13, G1): `release self-update N` refuses a halted, withdrawn, not newer or
other-platform release with the reason and stages nothing; an accepted one is handed to the root update helper (the
package and request.json in the update inbox, exactly what the helper reads) and nothing is installed by the platform;
the platform audits centre-update-started once, and in the boot after the restart, once its self-check has spoken,
centre-updated or centre-rolled-back (a release the centre rolled back from is halted); Devices shows the centre's own
system and its last self-update (GET /api/centre-os)."""
import json
import os
import re
import subprocess

import pytest

from urban import release, self_update
from urban.release import ReleaseError, ReleaseStore

X86, ARM = "urban-edge-x86_64", "urban-edge-aarch64"
REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen  # before nothing_is_run: one test runs the command line itself
HELPER_FILE = re.compile(r"urban-[0-9]+\.raucb")  # the root helper's own rule for the file name (urban-unit apply_update)


class Machine:
    """The centre's machine as the code sees it: its OS release, RAUC's system.conf, the update inbox, the helper's
    status, the self-check's verdict and the boot id, all in a temporary folder."""

    def __init__(self, root, monkeypatch, version=1, compatible=X86):
        self.root = root
        self.inbox = root / "update-inbox"
        self.inbox.mkdir(parents=True)
        self.run = root / "run"
        self.run.mkdir()
        for env, path in (("URBAN_OS_RELEASE", root / "urban-os-release"), ("URBAN_RAUC_CONF", root / "system.conf"),
                          ("URBAN_UPDATE_INBOX", self.inbox), ("URBAN_UPDATE_STATUS", self.run / "update.json"),
                          ("URBAN_HEALTH", self.run / "health"), ("URBAN_BOOT_ID", self.run / "boot-id"),
                          ("URBAN_UPDATE_REBOOT", self.run / "update-reboot")):
            monkeypatch.setenv(env, str(path))
        monkeypatch.setattr(self_update, "KERNEL_BOOT_ID", root / "no-kernel-boot-id")
        (root / "system.conf").write_text(f"[system]\ncompatible={compatible}\nbootloader=custom\n")
        self.boot("boot-1", version)

    def boot(self, ident, version, health=None):
        """A (re)start: /run is empty again, the slot's own version runs."""
        for f in self.run.iterdir():
            f.unlink()
        (self.run / "boot-id").write_text(ident + "\n")
        (self.root / "urban-os-release").write_text(f"VERSION={version}\nBUILD=test\n")
        if health:
            self.verdict(health)

    def verdict(self, word):
        (self.run / "health").write_text({"good": "good - this system is marked working",
                                          "bad": "BAD - going back to the previous system (slot A)"}[word] + "\n")

    def helper_says(self, state, version, reason=""):
        """As the helper's say(): a new file moved over the old one (mv -f), so each answer is a new file."""
        tmp = self.run / "update.json.tmp"
        tmp.write_text(json.dumps({"state": state, "version": version, "reason": reason}))
        os.replace(tmp, self.run / "update.json")

    def staged(self):
        return sorted(p.name for p in self.inbox.iterdir())


@pytest.fixture
def machine(tmp_path, monkeypatch):
    return Machine(tmp_path / "machine", monkeypatch)


@pytest.fixture
def store(tmp_path):
    return ReleaseStore(tmp_path / "releases")


def add(store, tmp_path, version, compatible=X86, devices=None):
    src = tmp_path / f"pkg-{compatible}-{version}.raucb"
    src.write_bytes(f"URBAN {compatible} {version}".encode() * 1000)
    return store.add(src, version, devices, compatible=compatible)


class Audit:
    def __init__(self):
        self.records = []

    def __call__(self, event, target, note="", actor="platform"):
        self.records.append((event, target, note, actor))

    def events(self):
        return [r[0] for r in self.records]


@pytest.fixture(autouse=True)
def nothing_is_run(monkeypatch):
    """The platform's side never runs RAUC or anything else: installing is the root helper's."""
    def refuse(*a, **kw):
        raise AssertionError(f"self-update ran a program: {a}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_refused_halted_withdrawn_not_newer_and_other_platform_releases_stage_nothing(machine, store, tmp_path):
    machine.boot("boot-1", 2)
    add(store, tmp_path, 2)
    add(store, tmp_path, 3)
    add(store, tmp_path, 4)
    add(store, tmp_path, 5, ARM)
    store.halt(3, "edge-J-01", X86, "rolled back: installed 3, runs 2")
    store.withdraw(4)
    cases = {3: "release 3 is halted (by edge-J-01 (urban-edge-x86_64): rolled back: installed 3, runs 2",
             4: "release 4 for urban-edge-x86_64 is withdrawn",
             2: "release 2 is not newer than the running version 2",
             5: "no release 5 for this machine's compatible (urban-edge-x86_64): only for urban-edge-aarch64",
             9: "no release 9 for this machine's compatible (urban-edge-x86_64)"}
    for version, words in cases.items():
        with pytest.raises(ReleaseError) as err:
            self_update.start(store, version)
        assert str(err.value).startswith(words), str(err.value)
        assert machine.staged() == [] and self_update.read_record(store) is None
    # the command line says it the way every release command refuses
    for version, words in cases.items():
        with pytest.raises(SystemExit) as out:
            release.main(["--dir", str(store.dir), "self-update", str(version), "--wait", "0"])
        assert str(out.value).startswith(f"refused: {words}")
    assert machine.staged() == [] and not (store.dir / self_update.RECORD).exists()


def test_the_command_line_run_as_a_module_says_refused_not_a_traceback(machine, store, monkeypatch):
    """`python -m urban.release` (what urban-center runs) is the module __main__, and its `except ReleaseError` must
    still catch the urban.release.ReleaseError that self_update raises: the QEMU check found it let through."""
    import sys
    from pathlib import Path
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)  # this test runs the command itself, as urban-center does
    done = REAL_RUN([sys.executable, "-m", "urban.release", "--dir", str(store.dir), "self-update", "9", "--wait", "0"],
                    capture_output=True, text=True, cwd=Path(release.__file__).parents[1], timeout=120)
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == 1
    assert done.stderr.strip() == "refused: no release 9 for this machine's compatible (urban-edge-x86_64)"
    assert machine.staged() == []


def test_refused_on_a_machine_that_is_not_urban_os_or_has_no_inbox(machine, store, tmp_path, monkeypatch):
    add(store, tmp_path, 2)
    monkeypatch.setenv("URBAN_RAUC_CONF", str(tmp_path / "missing.conf"))
    with pytest.raises(ReleaseError, match="this machine's system is not known"):
        self_update.start(store, 2)
    monkeypatch.setenv("URBAN_RAUC_CONF", str(machine.root / "system.conf"))
    monkeypatch.setenv("URBAN_UPDATE_INBOX", str(tmp_path / "no-inbox"))
    with pytest.raises(ReleaseError, match="no update inbox"):
        self_update.start(store, 2)
    assert self_update.read_record(store) is None


def test_an_accepted_release_is_handed_to_the_root_helper_not_installed(machine, store, tmp_path, capsys):
    rel = add(store, tmp_path, 2)
    release.main(["--dir", str(store.dir), "self-update", "2", "--wait", "0"])
    out = capsys.readouterr().out
    assert out.startswith("self-update to release 2 for urban-edge-x86_64 (from 1) handed to the root update helper")
    assert "centre-update-started" in out and "centre-updated or centre-rolled-back" in out
    # exactly what a unit's client leaves for the helper: the package under the helper's name, and request.json
    assert machine.staged() == ["request.json", "urban-2.raucb"]
    request = json.loads((machine.inbox / "request.json").read_text())
    assert request == {"version": 2, "file": "urban-2.raucb", "sha256": rel["sha256"], "size": rel["size"]}
    assert HELPER_FILE.fullmatch(request["file"]) and request["file"] == f"urban-{request['version']}.raucb"
    assert (machine.inbox / "urban-2.raucb").read_bytes() == store.path(rel).read_bytes()
    assert store.path(rel).is_file()  # the release stays in the store
    rec = self_update.read_record(store)
    assert {k: rec[k] for k in ("state", "from", "to", "compatible", "boot")} == \
        {"state": "started", "from": 1, "to": 2, "compatible": X86, "boot": "boot-1"}
    # a second one is refused while this one is under way
    add(store, tmp_path, 3)
    with pytest.raises(ReleaseError, match="the self-update to release 2 started at .* has not ended yet"):
        self_update.start(store, 3)
    assert self_update.read_record(store)["id"] == rec["id"]
    # and none beside a request still waiting for the helper, or while an installed one waits for the restart
    (store.dir / self_update.RECORD).unlink()
    with pytest.raises(ReleaseError, match="an update is already waiting for the root update helper"):
        self_update.start(store, 3)
    (machine.inbox / "request.json").unlink()
    (machine.run / "update-reboot").write_text("")
    with pytest.raises(ReleaseError, match="an update is installed already"):
        self_update.start(store, 3)
    assert machine.staged() == ["urban-2.raucb"] and self_update.read_record(store) is None


def test_the_command_waits_for_the_helper_and_says_its_refusal(machine, store, tmp_path, capsys, monkeypatch):
    add(store, tmp_path, 2)
    real = self_update.wait_answer

    def helper_answers(version, before, timeout, **kw):
        machine.helper_says("refused", version, "signature")
        return real(version, before, timeout, pause=lambda s: None)
    monkeypatch.setattr(self_update, "wait_answer", helper_answers)
    with pytest.raises(SystemExit) as out:
        release.main(["--dir", str(store.dir), "self-update", "2", "--wait", "5"])
    assert str(out.value) == "refused: the root update helper refused release 2: signature"
    # and an answer that it is installing
    (store.dir / self_update.RECORD).unlink()
    for f in machine.inbox.iterdir():
        f.unlink()

    def installing(version, before, timeout, **kw):
        machine.helper_says("installing", version, "installing into the other slot")
        return real(version, before, timeout, pause=lambda s: None)
    monkeypatch.setattr(self_update, "wait_answer", installing)
    release.main(["--dir", str(store.dir), "self-update", "2", "--wait", "5"])
    assert "the root update helper is installing release 2 into the other slot" in capsys.readouterr().out


def test_wait_answer_ignores_an_old_status_and_gives_up_after_the_timeout(machine):
    machine.helper_says("refused", 2, "not newer")
    before = self_update._status_seen()
    ticks = iter(range(0, 1000, 10))
    assert self_update.wait_answer(2, before, 30, clock=lambda: next(ticks), pause=lambda s: None) is None
    machine.helper_says("installing", 2)
    assert self_update.wait_answer(2, before, 30, pause=lambda s: None)["state"] == "installing"


def test_the_platform_audits_the_start_once_and_centre_updated_once_after_the_restart(machine, store, tmp_path):
    add(store, tmp_path, 2)
    self_update.start(store, 2, by="cli:urban")
    audit = Audit()
    watch = self_update.Watch(store, audit)
    for _ in range(3):
        watch.step()
    assert audit.records == [("centre-update-started", "centre", "release 2 for urban-edge-x86_64, from 1", "cli:urban")]
    machine.helper_says("installing", 2)
    watch.step()
    assert audit.events() == ["centre-update-started"]  # the same boot: under way
    # the restart into slot B: the self-check has not spoken yet, so nothing is decided
    machine.boot("boot-2", 2)
    watch = self_update.Watch(store, audit)  # a new platform process
    watch.step()
    assert audit.events() == ["centre-update-started"]
    machine.verdict("good")
    for _ in range(3):
        watch.step()
    assert audit.records[1:] == [("centre-updated", "centre", "now 2", "platform")]
    rec = self_update.read_record(store)
    assert rec["state"] == "updated" and rec["running"] == 2
    view = self_update.view(store)
    assert view["os"] == 2 and view["compatible"] == X86
    assert {k: view["self_update"][k] for k in ("state", "from", "to", "running")} == \
        {"state": "updated", "from": 1, "to": 2, "running": 2}
    assert not store.get(2)["halted"]


def test_a_start_not_audited_before_the_restart_is_audited_first_then_the_outcome(machine, store, tmp_path):
    add(store, tmp_path, 2)
    self_update.start(store, 2)
    machine.boot("boot-2", 2, health="good")
    audit = Audit()
    self_update.Watch(store, audit).step()
    assert audit.events() == ["centre-update-started", "centre-updated"]


def test_a_bad_version_rolls_back_audited_once_and_the_release_is_halted(machine, store, tmp_path):
    machine.boot("boot-1", 2)
    add(store, tmp_path, 2)
    add(store, tmp_path, 3)
    self_update.start(store, 3)
    audit = Audit()
    self_update.Watch(store, audit).step()
    # the broken version boots: its self-check fails and it is on its way back; nothing is decided there
    machine.boot("boot-2", 3, health="bad")
    self_update.Watch(store, audit).step()
    assert audit.events() == ["centre-update-started"]
    # back on version 2 by itself
    machine.boot("boot-3", 2, health="good")
    watch = self_update.Watch(store, audit)
    watch.step()
    watch.step()
    assert audit.records[1:] == [
        ("centre-rolled-back", "centre", "started 3, back on 2", "platform"),
        ("rollout-halted", "release-3", "centre (urban-edge-x86_64): rolled back: installed 3, runs 2", "platform")]
    halted = store.get(3)["halted"]
    assert halted["device"] == "centre" and halted["reason"] == "rolled back: installed 3, runs 2"
    assert store.newest_for("edge-J-01")["version"] == 2  # no unit is offered it
    rec = self_update.read_record(store)
    assert rec["state"] == "rolled-back" and rec["running"] == 2
    assert self_update.view(store)["self_update"]["state"] == "rolled-back"
    # the same command again: refused, the release is halted
    for f in machine.inbox.iterdir():  # the helper took the package of the first try
        f.unlink()
    with pytest.raises(ReleaseError, match="release 3 is halted"):
        self_update.start(store, 3)
    assert machine.staged() == [] and self_update.read_record(store)["state"] == "rolled-back"


def test_a_refusal_by_the_root_helper_is_audited_and_ends_the_self_update(machine, store, tmp_path):
    add(store, tmp_path, 2)
    self_update.start(store, 2)
    audit = Audit()
    watch = self_update.Watch(store, audit)
    watch.step()
    machine.helper_says("refused", 2, "signature")
    watch.step()
    watch.step()
    assert audit.events() == ["centre-update-started", "centre-update-refused"]
    assert audit.records[1][2] == "release 2: signature"
    assert self_update.read_record(store)["state"] == "refused"
    # the admin may try again once the helper refused it
    (machine.inbox / "request.json").unlink(missing_ok=True)
    (machine.inbox / "urban-2.raucb").unlink(missing_ok=True)
    self_update.start(store, 2)
    assert self_update.read_record(store)["state"] == "started"


def test_nothing_to_audit_without_a_record_and_devices_shows_the_centre(machine, store):
    audit = Audit()
    assert self_update.Watch(store, audit).step() is None and audit.records == []
    assert self_update.view(store) == {"os": 1, "compatible": X86, "self_update": None}


def test_the_platform_serves_the_centre_and_audits_from_its_event_loop():
    """GET /api/centre-os and the watcher are wired into the platform (api/app.py)."""
    from pathlib import Path
    app = (Path(release.__file__).parent / "api" / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/centre-os")' in app and "self_update.view(hub.releases)" in app
    assert "self_update.Watch(hub.releases, core_audit)" in app and "centre_update_watch()" in app


def test_a_retry_after_a_refusal_waits_for_the_helpers_new_answer_not_the_old_one(machine, store, tmp_path):
    """The helper refused release 2 once; the admin tries again in the same boot. Its old refusal is still in the
    status file while it checks the package again: that is not the answer to the retry."""
    add(store, tmp_path, 2)
    self_update.start(store, 2)
    audit = Audit()
    watch = self_update.Watch(store, audit)
    machine.helper_says("refused", 2, "install failed")
    watch.step()
    assert audit.events() == ["centre-update-started", "centre-update-refused"]
    for f in machine.inbox.iterdir():
        f.unlink()
    retry = self_update.start(store, 2)[0]
    for _ in range(3):
        watch.step()  # the old refusal is still there: under way, not refused
    assert audit.events()[2:] == ["centre-update-started"]
    assert self_update.read_record(store)["state"] == "started"
    with pytest.raises(ReleaseError, match="has not ended yet"):  # nor may a third one start on the old refusal
        self_update.start(store, 2)
    machine.helper_says("installing", 2)
    watch.step()
    assert self_update.read_record(store)["reached"] is True
    machine.boot("boot-2", 2, health="good")
    self_update.Watch(store, audit).step()
    assert audit.events()[2:] == ["centre-update-started", "centre-updated"]
    assert self_update.read_record(store)["id"] == retry["id"]


def test_a_refusal_the_command_saw_is_not_taken_for_a_rollback_after_a_restart(machine, store, tmp_path, monkeypatch):
    """The command saw the helper refuse; the machine restarted before the platform looked: refused, nothing halted."""
    add(store, tmp_path, 2)
    real = self_update.wait_answer

    def helper_refuses(version, before, timeout, **kw):
        machine.helper_says("refused", version, "signature")
        return real(version, before, timeout, pause=lambda s: None)
    monkeypatch.setattr(self_update, "wait_answer", helper_refuses)
    with pytest.raises(SystemExit):
        release.main(["--dir", str(store.dir), "self-update", "2", "--wait", "5"])
    assert self_update.read_record(store)["refused"] == "signature"
    machine.boot("boot-2", 1, health="good")
    audit = Audit()
    self_update.Watch(store, audit).step()
    assert audit.events() == ["centre-update-started", "centre-update-refused"]
    assert audit.records[1][2] == "release 2: signature"
    assert self_update.read_record(store)["state"] == "refused" and not store.get(2)["halted"]


def test_a_restart_before_the_helper_installed_it_is_not_a_rollback(machine, store, tmp_path):
    """A power cut while the helper still checked the package: the centre comes back on its old version, but nothing
    was installed, so nothing rolled back and no release is halted for the fleet."""
    add(store, tmp_path, 2)
    self_update.start(store, 2)
    audit = Audit()
    self_update.Watch(store, audit).step()
    machine.boot("boot-2", 1, health="good")
    self_update.Watch(store, audit).step()
    assert audit.events() == ["centre-update-started", "centre-update-refused"]
    assert audit.records[1][2] == ("release 2: not installed: the centre restarted before the root update helper "
                                   "installed it")
    assert not store.get(2)["halted"] and self_update.read_record(store)["state"] == "refused"


def test_installing_seen_then_back_on_the_old_version_is_a_rollback(machine, store, tmp_path):
    """The helper said it was installing (seen by the platform); the bad slot's own boot was never seen: back on the old
    version is a rollback and the release is halted."""
    add(store, tmp_path, 2)
    self_update.start(store, 2)
    audit = Audit()
    watch = self_update.Watch(store, audit)
    machine.helper_says("installing", 2)
    watch.step()
    machine.boot("boot-3", 1, health="good")
    self_update.Watch(store, audit).step()
    assert audit.events() == ["centre-update-started", "centre-rolled-back", "rollout-halted"]
    assert store.get(2)["halted"]["device"] == "centre"
