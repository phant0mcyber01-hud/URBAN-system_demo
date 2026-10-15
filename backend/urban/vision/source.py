"""Frames from a camera: an RTSP stream from an IP camera in the cabinet's network, or a video file.

Frames live in memory only, a few per second, and are dropped as soon as the detector is done with
them. Nothing is recorded. A stream that breaks is reopened with a growing pause; while it is down
the approach simply has no fresh frames and reports no data, which the platform shows as such.
"""
import os
import threading
import time
from pathlib import Path


def expand(source: str) -> str:
    """Camera passwords stay in the unit's env file: rtsp://${CAM1_USER}:${CAM1_PASS}@10.0.0.11/stream1."""
    return os.path.expandvars(source)


def redact(source: str) -> str:
    """For logs: the address without credentials."""
    if "@" in source and "://" in source:
        scheme, rest = source.split("://", 1)
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    return source


def is_file(source: str) -> bool:
    return "://" not in source and Path(source).exists()


class VideoSource:
    def __init__(self, source: str, fps: float = 4.0, realtime: bool = True):
        """fps: frames per second handed on (the detector does not need 25);
        realtime: a file is played at its own pace and stamped with the wall clock, like a camera.
        With realtime=False a file is read as fast as possible and stamped with its own time (tests, checks)."""
        self.source, self.fps, self.realtime = source, fps, realtime
        self.error: str | None = None

    def frames(self, stop: threading.Event | None = None):
        """Yields (t, BGR frame). Ends when a file ends; a stream is reopened until `stop` is set."""
        import cv2
        stop = stop or threading.Event()
        url = expand(self.source)
        pause = 1.0
        while not stop.is_set():
            cap = cv2.VideoCapture(url)
            if not cap.isOpened():
                self.error = f"cannot open {redact(self.source)}"
                if is_file(url) or not self.realtime and "://" not in url:
                    return
                stop.wait(pause)
                pause = min(pause * 2, 30.0)
                continue
            self.error, pause = None, 1.0
            native = cap.get(cv2.CAP_PROP_FPS) or 25.0
            step = max(1, round(native / self.fps))
            started, index = time.time(), 0
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                index += 1
                if (index - 1) % step:
                    continue
                video_t = (index - 1) / native
                if self.realtime and is_file(url):
                    ahead = started + video_t - time.time()
                    if ahead > 0:
                        stop.wait(ahead)
                yield (time.time() if self.realtime else video_t), frame
            cap.release()
            if is_file(url):
                return
            self.error = f"stream {redact(self.source)} broke, reopening"
            stop.wait(pause)
