"""Runs the camera pipeline on an URBAN Edge unit.

    python -m urban.sensors --calibration /etc/urban/cameras.json --out /run/urban/queues.json < detections.jsonl

Reads detections as JSON lines from stdin (or --input), one line per camera frame:
    {"t": 1712.5, "approach": "140393120#29", "boxes": [[u, v, w, h], ...]}
where t is the unit's wall clock in seconds and boxes are pixel rectangles from whatever detector runs
on the unit (a neural network on its accelerator, or a camera with analytics on board). Writes the
queue length per approach to --out once a second; the unit passes that file as --sensors.
No frames, no identities: only boxes come in, only metres go out.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .pipeline import QueuePipeline, load_views


def main():
    ap = argparse.ArgumentParser(description="URBAN camera pipeline: detections -> queue metres")
    ap.add_argument("--calibration", required=True, help="JSON with the four reference points per camera view")
    ap.add_argument("--out", required=True, help="queues.json for the Edge unit (--sensors)")
    ap.add_argument("--input", help="JSON lines of detections; stdin by default")
    args = ap.parse_args()

    pipeline = QueuePipeline(load_views(Path(args.calibration)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0.0
    with (open(args.input, encoding="utf-8") if args.input else sys.stdin) as stream:
        for line in stream:
            try:
                frame = json.loads(line)
                pipeline.feed(str(frame["approach"]), float(frame["t"]), frame.get("boxes") or [])
            except (ValueError, KeyError, TypeError):
                continue  # a bad frame is skipped, the next one counts
            now = time.time()
            if now - written >= 1.0:
                pipeline.write(out, now)
                written = now
    pipeline.write(out)


if __name__ == "__main__":
    main()
