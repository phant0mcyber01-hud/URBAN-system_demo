"""Camera vision of an URBAN Edge unit: video in, queue metres per approach out.

    python -m urban.vision run --cameras /etc/urban/cameras.json --model /opt/urban/models/yolox_nano.onnx \\
                               --out /run/urban/queues.json
        on the unit, as a service next to urban-edge: every calibrated camera, a few frames a second,
        queues.json rewritten once a second (the unit reads it with --sensors)
    python -m urban.vision check --video approach.mp4 --camera cam-1 --cameras cameras.json --model yolox_nano.onnx \\
                                 --out-video checked.mp4 --log queues.csv
        a recorded video through the same pipeline: an annotated copy to look at, and the queue every
        second in the format of `python -m urban.bench accuracy` (test A against an observer)
    python -m urban.vision setup --cameras /etc/urban/cameras.json --unit edge-J-07
        the calibration page on the unit (see setup.py)

Frames stay in memory on the unit and are never stored or sent; only metres go to the platform.
`run` follows the machine's power plan (/run/urban/power.json, see power.py): fewer frames when it is hot.
"""
import argparse
import csv
import sys
import threading
import time
from pathlib import Path

from ..sensors.pipeline import QueuePipeline
from . import cameras as cams
from .power import PowerPlan, Stats, Throttle
from .source import VideoSource, redact


def make_detector(args, threads: int | None = None):
    from .detector import YoloxDetector
    try:
        return YoloxDetector(Path(args.model), score=args.score, threads=threads or args.threads or 2)
    except FileNotFoundError as err:
        raise SystemExit(str(err))


class Latest:
    """The newest frame of one camera: the detector takes whatever is newest, older frames are dropped."""

    def __init__(self, cam: dict, fps: float, stop: threading.Event):
        self.cam, self.frame, self.t, self.taken = cam, None, 0.0, True
        self.source = VideoSource(cam["source"], fps=fps, realtime=True)
        self.lock = threading.Lock()
        threading.Thread(target=self._read, args=(stop,), daemon=True).start()

    def _read(self, stop):
        for t, frame in self.source.frames(stop):
            with self.lock:
                self.t, self.frame, self.taken = t, frame, False

    def take(self):
        with self.lock:
            if self.taken or self.frame is None:
                return None
            self.taken = True
            return self.t, self.frame


def run(args):
    config = cams.load(Path(args.cameras))
    ready = [c for c in config if cams.calibrated(c) and c.get("source")]
    for c in config:
        if c not in ready:
            print(f"vision: {c.get('camera')} is not calibrated yet, skipped (python -m urban.vision setup)", flush=True)
    if not ready:
        raise SystemExit("vision: no calibrated camera; nothing to measure")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    plan = PowerPlan(Path(args.power) if args.power else None, args.fps)
    plan.refresh()
    threads = plan.threads(args.threads)
    detector = make_detector(args, threads)
    pipeline = QueuePipeline([cams.view(c) for c in ready])
    stop = threading.Event()
    feeds = [Latest(c, args.fps, stop) for c in ready]
    throttle, stats = Throttle(), Stats(out.parent / "vision.json")
    print(f"vision: {len(ready)} camera(s), {threads} thread(s): "
          + ", ".join(f"{c['camera']}->{c['approach']} ({redact(c['source'])})" for c in ready), flush=True)
    written, errors, rate = 0.0, {}, plan.fps
    try:
        while True:
            busy = False
            for feed in feeds:
                if not throttle.due(feed.cam["camera"], time.time(), rate):
                    continue
                got = feed.take()
                if got is None:
                    continue
                throttle.mark(feed.cam["camera"], time.time())
                busy = True
                t, frame = got
                started = time.perf_counter()
                boxes = detector.detect(frame)
                stats.add((time.perf_counter() - started) * 1000)
                pipeline.feed(feed.cam["approach"], t, [b[:4] for b in boxes])
            now = time.time()
            if now - written >= 1.0:
                pipeline.write(out, now)
                written = now
                plan.refresh()
                if plan.fps != rate:
                    print(f"vision: power plan {rate} -> {plan.fps} frames/s per camera"
                          + (" (paused)" if plan.fps == 0 else ""), flush=True)
                    rate = plan.fps
                stats.write(len(feeds), rate, now)
                for feed in feeds:  # say it once when a camera breaks or comes back
                    if errors.get(feed.cam["camera"]) != feed.source.error:
                        errors[feed.cam["camera"]] = feed.source.error
                        print(f"vision: {feed.cam['camera']}: {feed.source.error or 'stream ok'}", flush=True)
            if not busy:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


def check(args, detector=None):
    """A recorded video through the pipeline, at the video's own clock."""
    import cv2
    config = cams.load(Path(args.cameras))
    cam = next((c for c in config if c.get("camera") == args.camera), None)
    if cam is None or not cams.calibrated(cam):
        raise SystemExit(f"camera {args.camera} is not in {args.cameras} or not calibrated")
    from .draw import overlay
    detector = detector or make_detector(args)
    pipeline = QueuePipeline([cams.view(cam)])
    writer, log_file, log, rows, last, said = None, None, None, 0, -1, -1
    if args.log:
        log_file = open(args.log, "w", encoding="utf-8", newline="")
        log = csv.writer(log_file)
        log.writerow(["time", "approach", "metres"])
    start = time.time()
    for t, frame in VideoSource(args.video, fps=args.fps, realtime=False).frames():
        if args.seconds and t > args.seconds:
            break
        boxes = detector.detect(frame)
        queue = pipeline.feed(cam["approach"], t, [b[:4] for b in boxes])
        if args.out_video:
            if writer is None:
                writer = cv2.VideoWriter(args.out_video, cv2.VideoWriter_fourcc(*"mp4v"), args.fps,
                                         (frame.shape[1], frame.shape[0]))
            writer.write(overlay(frame, cam, boxes, queue))
        if log is not None and int(t) != last:
            last = int(t)
            log.writerow([f"{args.t0 + t:.1f}", cam["approach"], queue])
            rows += 1
        if int(t) % 10 == 0 and int(t) != said:
            said = int(t)
            print(f"  {t:6.1f} s  {len(boxes):2} vehicles  queue {queue} m", flush=True)
    if writer is not None:
        writer.release()
    if log_file is not None:
        log_file.close()
    print(f"done in {time.time() - start:.0f} s" + (f"; {rows} rows in {args.log}" if log else "")
          + (f"; annotated video {args.out_video}" if args.out_video else ""))


def main():
    ap = argparse.ArgumentParser(prog="python -m urban.vision", description="URBAN Edge camera vision")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def detector_args(p):
        p.add_argument("--model", required=True, help="YOLOX ONNX model, e.g. /opt/urban/models/yolox_nano.onnx")
        p.add_argument("--score", type=float, default=0.35, help="minimum detection confidence")
        p.add_argument("--threads", type=int, help="CPU threads for the network (default: the power plan's, else 2)")
        p.add_argument("--fps", type=float, default=4.0, help="frames per second per camera (the most; the power plan may lower it)")

    r = sub.add_parser("run", help="measure queues from the unit's cameras")
    r.add_argument("--cameras", required=True)
    r.add_argument("--out", required=True, help="queues.json the unit reads (--sensors)")
    r.add_argument("--power", default="/run/urban/power.json", help="the machine's power plan ('' to ignore it)")
    detector_args(r)
    ch = sub.add_parser("check", help="run a recorded video through the pipeline")
    ch.add_argument("--video", required=True)
    ch.add_argument("--camera", required=True, help="camera id in --cameras whose calibration fits the video")
    ch.add_argument("--cameras", required=True)
    ch.add_argument("--out-video", help="annotated copy (mp4)")
    ch.add_argument("--log", help="queue every second as CSV for `python -m urban.bench accuracy`")
    ch.add_argument("--t0", type=float, default=0.0, help="wall-clock time of the video's start, for the log")
    ch.add_argument("--seconds", type=float, help="stop after this much video")
    detector_args(ch)
    s = sub.add_parser("setup", help="calibration page on the unit")
    s.add_argument("--cameras", required=True)
    s.add_argument("--unit", help="this unit's id: its junction's approaches come from the kit")
    s.add_argument("--junction", help="the junction file written by `urban.edge --setup` (default: next to the env file)")
    s.add_argument("--host", default="127.0.0.1", help="listen address; keep it local and use an SSH tunnel")
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--model", help="optional: show what the detector finds on the snapshot")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args)
    elif args.cmd == "check":
        check(args)
    else:
        from .setup import serve
        serve(args)


if __name__ == "__main__":
    sys.exit(main())
