"""A frame with what the unit understood: lanes, the measured zone, vehicles and the queue.

Only for people on the unit: the setup page and the check of a recorded video. These pictures never
leave the unit; the platform gets metres.
"""
import numpy as np

from .cameras import back
from .detector import VEHICLES

ZONE = (90, 90, 90)
LANE = (150, 150, 150)
QUEUE = (40, 160, 230)     # amber-ish in BGR
VEHICLE = (230, 200, 60)
STOP = (60, 60, 230)


def _px(h, x, y):
    u, v = h.road(x, y)
    return int(round(u)), int(round(v))


def overlay(frame: np.ndarray, cam: dict, boxes: list | None = None, queue: float | None = None) -> np.ndarray:
    import cv2
    out = frame.copy()
    if len(cam.get("points") or []) == 4:
        h = back(cam)
        lanes, width, zone = int(cam.get("lanes", 2)), float(cam.get("lane_width", 3.5)), float(cam.get("zone", 100))
        if queue:
            shade = out.copy()
            poly = np.array([_px(h, 0, 0), _px(h, queue, 0), _px(h, queue, lanes * width), _px(h, 0, lanes * width)])
            cv2.fillPoly(shade, [poly], QUEUE)
            out = cv2.addWeighted(shade, 0.35, out, 0.65, 0)
        for k in range(lanes + 1):  # lane borders along the approach
            pts = np.array([_px(h, x, k * width) for x in np.linspace(0, zone, 12)])
            cv2.polylines(out, [pts], False, LANE if 0 < k < lanes else ZONE, 1, cv2.LINE_AA)
        cv2.line(out, _px(h, 0, 0), _px(h, 0, lanes * width), STOP, 2, cv2.LINE_AA)
        cv2.line(out, _px(h, zone, 0), _px(h, zone, lanes * width), ZONE, 1, cv2.LINE_AA)
        for p in cam["points"]:
            cv2.circle(out, (int(p["px"][0]), int(p["px"][1])), 5, (255, 255, 255), 1, cv2.LINE_AA)
    for b in boxes or []:
        u, v, w, hh = (int(round(x)) for x in b[:4])
        cv2.rectangle(out, (u, v), (u + w, v + hh), VEHICLE, 2, cv2.LINE_AA)
        if len(b) >= 6:
            text = f"{VEHICLES.get(int(b[5]), '?')} {b[4]:.2f}"
            cv2.rectangle(out, (u, max(0, v - 16)), (u + 8 * len(text), v), VEHICLE, -1)
            cv2.putText(out, text, (u + 2, max(12, v - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (20, 20, 20), 1, cv2.LINE_AA)
    label = f"{cam.get('camera', '')} {cam.get('approach', '')}  queue {'-' if queue is None else f'{queue:.0f} m'}"
    cv2.rectangle(out, (0, 0), (12 + 9 * len(label), 26), (20, 20, 20), -1)
    cv2.putText(out, label, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (235, 235, 235), 1, cv2.LINE_AA)
    return out
