"""Camera pipeline of an URBAN Edge unit: detections in, queue length in metres per approach out.

Lives on the unit, next to the cameras; nothing but numbers leaves it (see docs/hardware.md, 6.5).
The object detector itself (a neural network on the unit's accelerator, or a vendor camera) is
outside: it feeds boxes to `urban.sensors` as JSON lines. The queue rule is the same one the
simulation uses (sources/measure.lane_queue), so the demo and the street measure alike.
"""
