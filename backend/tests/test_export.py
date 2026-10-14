"""External audit copy: file and syslog sinks, catching up, signed checkpoints, detecting a rebuilt chain."""
import json
import socket
import threading

from urban.devices import crypto
from urban.export import Exporter, FileSink, SyslogSink, checkpoint, checkpoint_ok, verify_copy
from urban.gateway.audit import GENESIS, AuditLog, _digest
from urban.store import Store


def fill(store, n, start=0):
    log = AuditLog(store=store)
    for i in range(n):
        log.append(start + i, "proposed", f"cmd-{start + i}", "tls.select_plan", "J")
    return log


def read(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_file_copy_catches_up_and_carries_signed_checkpoints(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path / "h.db", clock=lambda: clock[0])
    key = crypto.new_key()
    fill(store, 3)
    sink = FileSink(tmp_path / "copy" / "audit.jsonl")
    ex = Exporter(store, key, [sink], clock=lambda: clock[0], every=300)
    ex.run_once()
    lines = read(sink.path)
    assert [x.get("seq") for x in lines] == [1, 2, 3, 3] and lines[-1]["type"] == "checkpoint"
    assert checkpoint_ok(key.public_key(), lines[-1]) and lines[-1]["head"] == lines[2]["hash"]
    fill(store, 2, start=3)
    ex.run_once()                                   # no checkpoint yet: not due
    assert [x.get("seq") for x in read(sink.path)][-2:] == [4, 5]
    # a new process starts from what the file already holds
    ex2 = Exporter(store, key, [FileSink(sink.path)], clock=lambda: clock[0])
    clock[0] += 10
    ex2.run_once()
    assert [x["seq"] for x in read(sink.path) if "hash" in x] == [1, 2, 3, 4, 5]
    assert ex2.status()["checkpoint"]["seq"] == 5 and ex2.status()["sinks"][0]["error"] is None


def test_a_failing_sink_is_reported_and_caught_up(tmp_path):
    store = Store(tmp_path / "h.db")
    fill(store, 2)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory should be")
    sink = FileSink(blocked / "audit.jsonl")
    ex = Exporter(store, crypto.new_key(), [sink])
    ex.run_once()
    assert ex.status()["sinks"][0]["error"] and ex.last_checkpoint is None
    blocked.unlink()
    ex.run_once()
    assert [x["seq"] for x in read(sink.path) if "hash" in x] == [1, 2] and ex.status()["sinks"][0]["error"] is None


def test_verify_detects_a_rebuilt_chain_in_the_database(tmp_path):
    key = crypto.new_key()
    store = Store(tmp_path / "h.db")
    fill(store, 4)
    sink = FileSink(tmp_path / "audit.jsonl")
    Exporter(store, key, [sink]).run_once()
    copy = read(sink.path)
    db = {r["seq"]: r["hash"] for r in store.audit_page(limit=100)}
    assert verify_copy(copy, key.public_key(), db)["ok"]

    # someone with the server rewrites record 2 and recomputes every hash after it: the chain alone looks fine
    prev, rebuilt = GENESIS, {}
    for r in sorted(store.audit_page(limit=100), key=lambda r: r["seq"]):
        r = {k: v for k, v in r.items() if k != "run"}
        if r["seq"] == 2:
            r["target"] = "something else"
        r["prev"] = prev
        r["hash"] = _digest(r)
        prev, rebuilt[r["seq"]] = r["hash"], r["hash"]
    result = verify_copy(copy, key.public_key(), rebuilt)
    assert not result["ok"] and any("differs" in p for p in result["problems"])
    assert any("checkpoint" in p for p in result["problems"])

    forged = dict(copy[-1], head="0" * 64)
    assert not verify_copy(copy[:-1] + [forged], key.public_key())["ok"]
    assert not verify_copy(copy, crypto.new_key().public_key())["ok"]   # signed by another key


def test_syslog_over_udp_and_tcp(tmp_path):
    store = Store(tmp_path / "h.db")
    fill(store, 2)
    key = crypto.new_key()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(2)
    sink = SyslogSink(f"udp://127.0.0.1:{udp.getsockname()[1]}", tmp_path / "pos", host="urban-test")
    Exporter(store, key, [sink]).run_once()
    got = [udp.recvfrom(65535)[0].decode() for _ in range(3)]
    udp.close()
    assert got[0].startswith("<110>1 ") and " urban-test urban - audit - {" in got[0]
    assert json.loads(got[0].split(" - audit - ", 1)[1])["seq"] == 1 and " checkpoint - " in got[2]
    assert sink.position() == 2

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = []

    def serve():
        conn, _ = srv.accept()
        data = b""
        while chunk := conn.recv(65535):
            data += chunk
        conn.close()
        while data:  # octet counting: "<len> <message>"
            n, _, rest = data.partition(b" ")
            received.append(rest[:int(n)].decode())
            data = rest[int(n):]
    t = threading.Thread(target=serve)
    t.start()
    tcp = SyslogSink(f"tcp://127.0.0.1:{srv.getsockname()[1]}", tmp_path / "pos-tcp")
    Exporter(store, key, [tcp]).run_once()
    t.join(3)
    srv.close()
    assert len(received) == 3 and all(m.startswith("<110>1 ") for m in received)


def test_checkpoint_signature_covers_seq_head_and_time():
    key = crypto.new_key()
    cp = checkpoint(key, 7, "ab" * 32, 1000.0)
    assert checkpoint_ok(key.public_key(), cp)
    for field, value in (("seq", 8), ("head", "cd" * 32), ("at", 1001.0)):
        assert not checkpoint_ok(key.public_key(), dict(cp, **{field: value}))


def test_syslog_over_tls_checks_the_collector(tmp_path):
    import ssl

    from urban.devices.registry import _write_tls
    _write_tls(tmp_path)  # a throwaway CA and a certificate for localhost
    store = Store(tmp_path / "h.db")
    fill(store, 1)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    received = []

    def serve():
        for _ in range(2):
            conn, _ = srv.accept()
            try:
                with ctx.wrap_socket(conn, server_side=True) as s:
                    data = b""
                    while chunk := s.recv(65535):
                        data += chunk
                    received.append(data)
            except (ssl.SSLError, OSError):
                received.append(None)  # the client refused our certificate
    t = threading.Thread(target=serve)
    t.start()
    port = srv.getsockname()[1]
    good = SyslogSink(f"tls://localhost:{port}", tmp_path / "pos", cafile=str(tmp_path / "ca.pem"))
    Exporter(store, crypto.new_key(), [good]).run_once()
    bad = SyslogSink(f"tls://localhost:{port}", tmp_path / "pos2")  # system CAs only: our private CA is not trusted
    ex = Exporter(store, crypto.new_key(), [bad])
    ex.run_once()
    t.join(5)
    srv.close()
    assert received[0] and b"<110>1 " in received[0] and good.position() == 1
    assert ex.status()["sinks"][0]["error"] and bad.position() == 0
