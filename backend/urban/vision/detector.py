"""Vehicles in a frame: a neural network detector run with ONNX Runtime on the unit.

The default model is YOLOX (Apache-2.0) trained on COCO; only its vehicle classes are kept. Any
YOLOX export works (nano for a small computer, s or m with an accelerator): the input size is read
from the model. Output is boxes in pixels, nothing else - no crops, no identities.
"""
from pathlib import Path

import numpy as np

VEHICLES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}  # COCO class ids
STRIDES = (8, 16, 32)


def letterbox(frame: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float]:
    """Resize keeping proportions, pad with grey to the model's input (as YOLOX was trained). CHW float32."""
    import cv2
    h, w = size
    ratio = min(h / frame.shape[0], w / frame.shape[1])
    resized = cv2.resize(frame, (int(frame.shape[1] * ratio), int(frame.shape[0] * ratio)), interpolation=cv2.INTER_LINEAR)
    padded = np.full((h, w, 3), 114, dtype=np.uint8)
    padded[:resized.shape[0], :resized.shape[1]] = resized
    return np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32), ratio


def decode(raw: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """YOLOX head output (N, 5 + classes) on the grid -> boxes as centre x, centre y, w, h in input pixels."""
    h, w = size
    grids, strides = [], []
    for s in STRIDES:
        gh, gw = h // s, w // s
        xv, yv = np.meshgrid(np.arange(gw), np.arange(gh))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        strides.append(np.full((gh * gw, 1), s))
    grid, stride = np.concatenate(grids), np.concatenate(strides)
    if raw.shape[0] != grid.shape[0]:
        raise ValueError(f"model output has {raw.shape[0]} anchors, a YOLOX head of {w}x{h} has {grid.shape[0]}")
    out = raw.astype(np.float32).copy()
    out[:, :2] = (out[:, :2] + grid) * stride
    out[:, 2:4] = np.exp(np.clip(out[:, 2:4], -20, 20)) * stride
    return out


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    """Greedy non-maximum suppression on (x1, y1, x2, y2) boxes."""
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        iou = inter / (area[i] + area[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= threshold]
    return keep


def vehicles(decoded: np.ndarray, ratio: float, score: float = 0.35, overlap: float = 0.45) -> list[list[float]]:
    """Decoded YOLOX rows -> [[u, v, w, h, score, class], ...] for vehicles only, in frame pixels."""
    if decoded.shape[1] < 5 + max(VEHICLES) + 1:
        raise ValueError("the model does not have the COCO classes")
    cls_scores = decoded[:, 5:] * decoded[:, 4:5]
    classes = cls_scores.argmax(1)
    best = cls_scores[np.arange(len(classes)), classes]
    mask = (best >= score) & np.isin(classes, list(VEHICLES))
    if not mask.any():
        return []
    rows, best, classes = decoded[mask], best[mask], classes[mask]
    xyxy = np.stack([rows[:, 0] - rows[:, 2] / 2, rows[:, 1] - rows[:, 3] / 2,
                     rows[:, 0] + rows[:, 2] / 2, rows[:, 1] + rows[:, 3] / 2], 1) / ratio
    keep = nms(xyxy, best, overlap)  # across classes: a truck is not also a car
    return [[float(xyxy[i, 0]), float(xyxy[i, 1]), float(xyxy[i, 2] - xyxy[i, 0]), float(xyxy[i, 3] - xyxy[i, 1]),
             round(float(best[i]), 3), int(classes[i])] for i in keep]


class YoloxDetector:
    def __init__(self, model: Path, score: float = 0.35, threads: int = 2):
        import onnxruntime as ort
        if not Path(model).exists():
            raise FileNotFoundError(f"no model at {model} (see docs/vision.md: which model and where to get it)")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model), opts, providers=ort.get_available_providers())
        inp = self.session.get_inputs()[0]
        self.input = inp.name
        self.size = (int(inp.shape[2]), int(inp.shape[3]))
        self.score = score

    def detect(self, frame: np.ndarray) -> list[list[float]]:
        """BGR frame -> vehicle boxes [[u, v, w, h, score, class], ...]."""
        image, ratio = letterbox(frame, self.size)
        raw = self.session.run(None, {self.input: image[None]})[0][0]
        return vehicles(decode(raw, self.size), ratio, self.score)
