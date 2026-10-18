"""The district server's own client: reports kept through an outage of the link to the centre and replayed
in order, commands refused, the relay's counts carried in its health. (The relay itself is the kernel's:
os/rootfs/usr/local/bin/urban-relay, checked on three machines by os/tests/check-chain.mjs.)"""
from urban.devices import crypto
from urban.node.district import DistrictDevice


def test_the_district_client_buffers_offline_and_replays_in_order():
    key = crypto.new_key()
    dev = DistrictDevice("district-1", key, gateway_public=crypto.load_public(crypto.public_hex(crypto.new_key())), buffer=5)
    hello = dev.hello("nonce")
    assert hello["device"] == "district-1"
    assert crypto.verify(crypto.load_public(crypto.public_hex(key)), {"device": "district-1", "nonce": "nonce"}, hello["sig"])
    t = 1000
    assert dev.tick(t, {"a1": 1.0}, online=False) == []
    for i in range(1, 8):
        dev.tick(t + i, {"a1": float(i)}, online=False)
    assert len(dev.buffer) == 5 and dev.health()["buffered"] == 5  # bounded: the oldest are gone
    out = dev.tick(t + 8, {"a1": 8.0}, online=True)
    assert [r["t"] for r in out] == [t + 3, t + 4, t + 5, t + 6, t + 7, t + 8]
    assert all(r["type"] == "report" for r in out) and out[-1]["queues"] == {"a1": 8.0} and len(dev.buffer) == 0
    dev.on_command({"type": "cmd", "body": {"op": "lease"}})  # refused, and said in the next report
    rep = dev.tick(t + 9, {}, online=True)[0]
    assert rep["events"] == [{"type": "edge", "status": "refused", "device": "district-1",
                              "reason": "a district server takes no commands"}]
    dev.relay_stats = lambda: {"units": 3, "sessions": 5, "bytes_up": 10, "bytes_down": 20, "refused": 0}
    assert dev.health()["relay"]["units"] == 3
