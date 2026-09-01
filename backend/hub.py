"""WebSocket fan-out.

ONE MECHANISM, THREE CASES
    live    a client subscribes and receives events as they are parsed
    resume  a dropped client reconnects with ?from_seq=N; the gap is served from the
            durable events.jsonl, then it rejoins the live tail
    replay  a finished run is pushed from events.jsonl through this same socket

    Because every event is persisted with a monotonic `seq` BEFORE it is broadcast, all
    three are the same operation with a different starting point. That is what makes P4
    (replay + history) nearly free, and it means a dropped socket mid-demo is invisible to
    the audience.

BACKPRESSURE
    Each client has its own bounded queue and the producer never blocks on it. If a client
    cannot keep up it is closed (1013) and told to reconnect with `from_seq`; it then
    catches up from disk. Blocking the producer instead would back up the stdout pipe and
    stall ns-3 itself — an unacceptable coupling during a live demo.
"""

import asyncio
import contextlib

QUEUE_MAX = 20000          # a whole Full-evidence run is ~50k events; Standard is ~3-5k


class Client:
    def __init__(self, ws, run_id, from_seq=1):
        self.ws = ws
        self.run_id = run_id
        self.from_seq = from_seq
        self.queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.lagged = False

    def offer(self, ev):
        """Never blocks. A full queue means this client is too slow to keep up."""
        try:
            self.queue.put_nowait(ev)
            return True
        except asyncio.QueueFull:
            self.lagged = True
            return False


class Hub:
    def __init__(self):
        self.clients = {}          # run_id -> set[Client]
        self._lock = asyncio.Lock()

    async def add(self, client):
        async with self._lock:
            self.clients.setdefault(client.run_id, set()).add(client)

    async def remove(self, client):
        async with self._lock:
            peers = self.clients.get(client.run_id)
            if peers:
                peers.discard(client)
                if not peers:
                    self.clients.pop(client.run_id, None)

    async def broadcast(self, run_id, ev):
        for c in list(self.clients.get(run_id, ())):
            if not c.offer(ev):
                # drop the slow client rather than stalling the parser
                with contextlib.suppress(Exception):
                    await c.ws.close(code=1013, reason="client too slow; resume with from_seq")
                await self.remove(c)

    async def finish(self, run_id):
        """Signal end-of-stream to everyone still attached."""
        for c in list(self.clients.get(run_id, ())):
            c.offer({"type": "_eos"})


async def pump(client):
    """Drain one client's queue to its socket until end-of-stream."""
    while True:
        ev = await client.queue.get()
        if ev.get("type") == "_eos":
            return
        await client.ws.send_json(ev)
