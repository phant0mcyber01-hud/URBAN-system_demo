"""In-process publish/subscribe.

This is the seam between data sources and the platform. In the demo everything
runs in one process; in a deployment the same topics would live on a broker
(NATS, Kafka) and sources would sit outside the platform's network zone.
"""
import inspect
from collections import defaultdict

SOURCE_FRAME = "source.frame"      # raw observations from a city data source
PLATFORM_FRAME = "platform.frame"  # analysed, client-ready state update


class Bus:
    def __init__(self):
        self._subs = defaultdict(list)

    def subscribe(self, topic, handler):
        self._subs[topic].append(handler)

    async def publish(self, topic, message):
        for handler in self._subs[topic]:
            result = handler(message)
            if inspect.isawaitable(result):
                await result
