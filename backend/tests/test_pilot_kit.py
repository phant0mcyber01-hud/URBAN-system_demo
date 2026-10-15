"""Survey sheets and summary; bench tests A, V, G without hardware."""
import csv

from urban import bench, survey
from urban.equipment import snmp

JUNCTIONS = [
    {"id": "J-01", "name": "Oriyat / Yuksalish", "lat": 40.38, "lon": 71.78, "camera": False,
     "approaches": [{"id": "a1", "name": "Oriyat koʻchasi", "zone": 100.0}],
     "phases": [{"index": 0, "edges": ["a1"], "downstream": [{"junction": "CAM-02", "approach": "b1", "phase": 0}]}]},
    {"id": "CAM-02", "name": "Mustaqillik <&>", "lat": 40.39, "lon": 71.79, "camera": True,
     "approaches": [{"id": "b1", "name": "", "zone": 80.0}], "phases": [{"index": 0, "edges": ["b1"]}]},
]


def test_sheets_and_empty_table(tmp_path):
    page, table = survey.sheets(JUNCTIONS, tmp_path)
    text = page.read_text(encoding="utf-8")
    assert "J-01 · Oriyat / Yuksalish" in text and "&lt;&amp;&gt;" in text and "openstreetmap.org" in text
    assert "CAM-02 (Mustaqillik &lt;&amp;&gt;)" in text and "есть окно камеры" in text
    assert text.count("<tr>") == 2 * len(survey.FIELDS)
    raw = table.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel reads Cyrillic only with the BOM
    rows = list(csv.DictReader(table.open(encoding="utf-8-sig")))
    assert [r["junction"] for r in rows] == ["J-01", "CAM-02"] and rows[0]["protocol"] == ""


def test_summary_tells_what_to_build_and_who_is_ready():
    blank = {k: "" for k, _ in survey.FIELDS}
    rows = [
        {**blank, "junction": "J-01", "controller_vendor": "Swarco", "controller_model": "ITC-3", "protocol": "NTCIP",
         "cabinet_ok": "да", "power": "да", "space": "да", "poles": "2"},
        {**blank, "junction": "J-02", "controller_vendor": "Dahua", "controller_model": "ITRTSC", "protocol": "gbt20999",
         "cabinet_ok": "да", "power": "да", "space": "да", "poles": "1"},
        {**blank, "junction": "J-03", "protocol": "relay", "cabinet_ok": "нет", "power": "да", "space": "да", "poles": "1"},
        {**blank, "junction": "J-04"},
    ]
    text = survey.summary(rows)
    assert "3 из 4" in text and "Ещё не обследованы: J-04" in text
    assert "| ntcip | 1 | NTCIP по SNMPv3: адаптер есть" in text and "| gbt20999 | 1 | GB/T 20999: адаптера нет" in text
    assert "Готовы к этапу 1 (камеры и блок, контроллер не трогаем): 2" in text and "J-01, J-02" in text
    assert "Готовы к этапу 2 (управление): 1" in text
    assert "- J-03:" in text  # gaps listed


def test_queue_accuracy_against_an_observer():
    logged = [{"time": str(t), "approach": "a1", "metres": m} for t, m in ((100, 30), (110, 60), (120, 90), (130, 20))]
    observed = [{"time": "101", "approach": "a1", "metres": "33", "cars": ""},     # 3 m off: fine
                {"time": "111", "approach": "a1", "metres": "", "cars": "6"},      # 45 m observed, 60 logged: 15 = two cars
                {"time": "121", "approach": "a1", "metres": "60", "cars": ""},     # 30 m off on 60: fails
                {"time": "200", "approach": "a1", "metres": "10", "cars": ""}]     # nothing logged near: unmatched
    r = bench.accuracy(logged, observed)
    assert (r["n"], r["ok"], r["unmatched"]) == (3, 2, 1)
    assert [p["ok"] for p in r["pairs"]] == [True, True, False] and r["bias"] > 0


def test_log_queues_appends_rows(tmp_path):
    sensors = tmp_path / "queues.json"
    sensors.write_text('{"a1": 12.5, "a2": 0}', encoding="utf-8")
    out = tmp_path / "log.csv"
    bench.log_queues(sensors, out, clock=lambda: 1000.0, sleep=lambda s: None, rounds=2)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 4 and rows[0] == {"time": "1000.0", "approach": "a1", "metres": "12.5"}


def test_rollback_timing_logic():
    t = [0.0]
    readings = iter([3, 3, None, 3, 0])

    def read():
        v = next(readings)
        if v is None:
            raise OSError("controller silent")
        return v
    value, took = bench.wait_for(read, lambda v: v == 0, timeout=60, clock=lambda: t[0],
                                 sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert value == 0 and took == 4.0
    _, never = bench.wait_for(lambda: 3, lambda v: v == 0, timeout=3, clock=lambda: t[0],
                              sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert never is None
    assert bench.limit_for("link", 120) == 180 and bench.limit_for("unit", 120) == 120


def test_v2c_get_round_trip():
    packet = snmp.encode_get("public", "1.3.6.1.2.1.1.3.0", 5)
    assert packet[0] == snmp.SEQUENCE and bytes([0xA0]) in packet
    bind = snmp._tlv(snmp.SEQUENCE, snmp._oid("1.3.6.1.2.1.1.3.0") + snmp._tlv(0x43, (123456).to_bytes(3, "big")))
    pdu = snmp._tlv(snmp.GET_RESPONSE, snmp._int(5) + snmp._int(0) + snmp._int(0) + snmp._tlv(snmp.SEQUENCE, bind))
    reply = snmp._tlv(snmp.SEQUENCE, snmp._int(1) + snmp._tlv(snmp.OCTET_STRING, b"public") + pdu)
    assert snmp.decode_response(reply) == (5, 0) and snmp.decode_value(reply) == 123456
