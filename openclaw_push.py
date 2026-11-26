"""Fail-closed notification provenance for bridge-issued OpenClaw runs.

Gateway broadcasts and session names are not proof of a Pocket-origin turn.
This process-local ledger intentionally forgets grants after a bridge restart.
"""
from collections import OrderedDict
from dataclasses import dataclass
import time


@dataclass
class PushOrigin:
    session: str
    expires: float
    generation: int
    final_sent: bool = False


class PushOrigins:
    def __init__(self, limit=4096, ttl=86400, clock=time.monotonic):
        self.limit = limit
        self.ttl = ttl
        self.clock = clock
        self._runs = OrderedDict()
        self._generation = 0

    def clear(self):
        self._runs.clear()
        self._generation += 1

    def get(self, session, run_id):
        origin = self._runs.get((session, run_id))
        if origin is not None and origin.expires > self.clock():
            return origin
        self._runs.pop((session, run_id), None)
        return None

    def begin(self, session, run_id):
        previous = self.get(session, run_id)
        if previous is not None:
            return previous, False
        origin = PushOrigin(session, self.clock() + self.ttl, self._generation)
        self.bind(origin, run_id)
        return origin, True

    def bind(self, origin, run_id):
        if (not origin.session or not run_id or origin.expires <= self.clock()
                or origin.generation != self._generation):
            return
        self._runs[(origin.session, run_id)] = origin
        while len(self._runs) > self.limit:
            self._runs.popitem(last=False)

    def revoke(self, origin):
        for key, value in list(self._runs.items()):
            if value is origin:
                del self._runs[key]
