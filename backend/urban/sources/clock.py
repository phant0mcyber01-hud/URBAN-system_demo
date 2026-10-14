"""The data source of the platform when it runs on real equipment: nothing but a clock.

In this mode queues come from the reports of URBAN Edge units (platform.on_report), there is
no traffic to draw and no scripted city events. One frame per wall-clock second keeps the
analysis, the leases and the history ticking at the same pace as the devices, whose clocks
are wall clocks too.
"""
import threading
import time


def frame(now: float) -> dict:
    # observed=False: there is no traffic source, so counts and speeds are unknown, not zero
    return {"t": int(now), "vehicles": [], "events": [], "bus_loss": [], "observed": False}


def run(publish, stop: threading.Event, clock=time.time, sleep=None):
    """Call publish(frame) once a second until `stop` is set. sleep(seconds) is `stop.wait` by default."""
    sleep = sleep or stop.wait
    last = None
    while not stop.is_set():
        now = clock()
        if last is None or int(now) != last:
            last = int(now)
            publish(frame(now))
        sleep(max(0.0, 1.0 - (now - int(now))))
