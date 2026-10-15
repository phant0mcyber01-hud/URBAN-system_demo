"""Camera vision of an URBAN Edge unit: video in, vehicles found by a neural network, queue metres out.

source.py    frames from an RTSP camera or a file, in memory only
detector.py  YOLOX (ONNX Runtime): vehicle boxes in pixels
cameras.py   which camera watches which approach, and its calibration
setup.py     the calibration page on the unit (frames are shown there and nowhere else)
draw.py      lanes, zone, vehicles and queue drawn over a frame, for people at the unit
The queue itself is measured by urban.sensors, the same rule the demo twin uses.
"""
