"""Camera vision on the unit: detector output decoding, video in, queue metres out, the calibration page."""
import csv
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from urban.vision import cameras as cams  # noqa: E402
from urban.vision.__main__ import check  # noqa: E402
from urban.vision.detector import decode, letterbox, vehicles  # noqa: E402
from urban.vision.draw import overlay  # noqa: E402
from urban.vision.setup import Setup, handler  # noqa: E402
from urban.vision.source import VideoSource, expand, redact  # noqa: E402

# a synthetic camera: stop line at v=440, a line 100 m up the approach at v=40, two lanes of 3.5 m on u 220..420
CAMERA = {"camera": "cam-1", "approach": "J-07-N", "lanes": 2, "lane_width": 3.5, "zone": 100,
          "points": [{"px": [220, 440], "m": [0, 0]}, {"px": [420, 440], "m": [0, 7]},
                     {"px": [420, 40], "m": [100, 7]}, {"px": [220, 40], "m": [100, 0]}]}
PX_PER_M = 4.0  # along the approach


def test_yolox_output_is_decoded_to_vehicle_boxes():
    size = (64, 64)  # grids 8x8 + 4x4 + 2x2 = 84 anchors
    raw = np.zeros((84, 85), dtype=np.float32)
    # objectness and class scores are probabilities in YOLOX exports: 0 everywhere means nothing found
    a = 3 * 8 + 2  # stride 8, column 2, row 3: centre (2 + 0.5) * 8 = 20, (3 + 0.5) * 8 = 28
    raw[a, :4] = [0.5, 0.5, np.log(2.0), np.log(1.0)]  # 16 x 8 pixels
    raw[a, 4], raw[a, 5 + 2] = 1.0, 0.9          # a car
    b = a + 1                                       # the same car again, weaker: suppressed
    raw[b, :4] = [-0.4, 0.5, np.log(2.0), np.log(1.0)]
    raw[b, 4], raw[b, 5 + 2] = 1.0, 0.6
    p = 64 + 4 * 2 + 1                              # stride 16: a person, not a vehicle
    raw[p, 4], raw[p, 5 + 0] = 1.0, 0.95
    out = vehicles(decode(raw, size), ratio=0.5)
    assert len(out) == 1
    u, v, w, h, score, cls = out[0]
    assert cls == 2 and score == pytest.approx(0.9)
    assert (u, v, w, h) == pytest.approx((24.0, 48.0, 32.0, 16.0))  # (20-8)/0.5, (28-4)/0.5, 16/0.5, 8/0.5
    with pytest.raises(ValueError):
        decode(raw[:10], size)  # not a YOLOX head of this size


def test_letterbox_keeps_proportions():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    image, ratio = letterbox(frame, (416, 416))
    assert image.shape == (3, 416, 416) and ratio == pytest.approx(0.65)
    assert image[0, 400, 10] == 114  # padding below the picture


def test_camera_passwords_stay_out_of_logs(monkeypatch):
    monkeypatch.setenv("CAM1_PASS", "s3cret")
    assert expand("rtsp://admin:${CAM1_PASS}@10.0.0.11/s1") == "rtsp://admin:s3cret@10.0.0.11/s1"
    assert redact("rtsp://admin:s3cret@10.0.0.11/s1") == "rtsp://***@10.0.0.11/s1"
    assert redact("/data/approach.mp4") == "/data/approach.mp4"


def red_boxes(frame):
    """A stand-in detector for synthetic video: red rectangles are vehicles."""
    mask = cv2.inRange(frame, (0, 0, 200), (60, 60, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return [[*cv2.boundingRect(c), 1.0, 2] for c in contours]


def street_video(path, seconds=8, fps=10):
    """Four cars standing at the stop line of lane 0, 7.5 m apart; one car driving up lane 1."""
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 480))
    for i in range(seconds * fps):
        frame = np.full((480, 640, 3), 60, dtype=np.uint8)
        for k in range(4):
            front = 1 + 7.5 * k                                   # metres from the stop line
            v1, v0 = 440 - front * PX_PER_M, 440 - (front + 5) * PX_PER_M
            cv2.rectangle(frame, (235, int(v0)), (290, int(v1)), (0, 0, 255), -1)
        moving = 90 - 10 * i / fps                                 # 10 m/s towards the stop line
        cv2.rectangle(frame, (340, int(440 - (moving + 5) * PX_PER_M)), (395, int(440 - moving * PX_PER_M)), (0, 0, 255), -1)
        out.write(frame)
    out.release()


def test_a_video_becomes_queue_metres(tmp_path):
    video = tmp_path / "approach.avi"
    street_video(video)
    frames = list(VideoSource(str(video), fps=4, realtime=False).frames())
    assert 25 <= len(frames) <= 45 and frames[-1][0] > frames[0][0]

    (tmp_path / "cameras.json").write_text(json.dumps([CAMERA]))
    args = SimpleNamespace(cameras=str(tmp_path / "cameras.json"), camera="cam-1", video=str(video), fps=4.0,
                           out_video=str(tmp_path / "checked.mp4"), log=str(tmp_path / "queues.csv"), t0=1000.0, seconds=None)
    detector = SimpleNamespace(detect=red_boxes)
    check(args, detector=detector)
    rows = list(csv.DictReader(open(tmp_path / "queues.csv", encoding="utf-8")))
    assert rows[0].keys() == {"time", "approach", "metres"} and float(rows[0]["time"]) == pytest.approx(1000.0)
    late = [float(r["metres"]) for r in rows[3:]]
    # four standing cars: the last front is 23.5 m from the stop line, plus its length (at least 4 m)
    assert late and all(26 <= q <= 32 for q in late), late
    assert (tmp_path / "checked.mp4").stat().st_size > 10_000


def test_overlay_draws_lanes_and_queue():
    frame = np.full((480, 640, 3), 60, dtype=np.uint8)
    drawn = overlay(frame, CAMERA, [[235, 300, 55, 20, 0.9, 2]], queue=28.0)
    assert drawn.shape == frame.shape and not np.array_equal(drawn, frame)
    assert np.array_equal(frame, np.full((480, 640, 3), 60, dtype=np.uint8))  # the original is untouched


def test_calibration_page_needs_its_token_and_keeps_secrets(tmp_path, monkeypatch):
    video = tmp_path / "approach.avi"
    street_video(video, seconds=1)
    junction = tmp_path / "edge-J-07.junction.json"
    junction.write_text(json.dumps({"junction": "J-07", "name": "x", "approaches": [{"id": "J-07-N", "name": "Alpha"}]}))
    setup = Setup(tmp_path / "cameras.json", junction)
    with pytest.raises(ValueError):
        setup.upsert({"camera": "cam 1", "source": "x"})                   # not an id
    with pytest.raises(ValueError):
        setup.upsert({"camera": "cam-1", "source": "x", "approach": "J-99-S"})  # not in the passport
    setup.upsert({"camera": "cam-1", "source": "rtsp://u:${CAM1_PASS}@10.0.0.11/s", "approach": "J-07-N", "lanes": 2})
    assert setup.state()["cameras"][0]["source"] == "rtsp://***@10.0.0.11/s"
    with pytest.raises(ValueError):
        setup.calibrate({"camera": "cam-1", "points": [{"px": [0, 0], "m": [0, 0]}] * 4})  # degenerate
    setup.calibrate({"camera": "cam-1", "points": CAMERA["points"]})
    assert cams.calibrated(cams.load(tmp_path / "cameras.json")[0])
    setup.upsert({"camera": "cam-2", "source": str(video), "approach": "J-07-N"})
    assert setup.snapshot("cam-2", overlay=False)[:2] == b"\xff\xd8"  # a JPEG, made on request, kept nowhere

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(setup, "tok3n"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as no:
            urllib.request.urlopen(base + "/api/state")
        assert no.value.code == 403
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/state?token=wrong")
        with urllib.request.urlopen(base + "/?token=tok3n") as r:
            assert "urban_setup=tok3n" in r.headers["Set-Cookie"] and "HttpOnly" in r.headers["Set-Cookie"]
        req = urllib.request.Request(base + "/api/state", headers={"Cookie": "urban_setup=tok3n"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["junction"] == "J-07"
    finally:
        server.shutdown()
