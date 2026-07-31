"""POC state backend: an in-process store speaking the slice of the Redis API
the engine uses.

The trade-off, stated plainly: this lives and dies with the worker process.
Dedup windows, thresholds, the storm limit and the redelivery guard are all
PER-INSTANCE and reset on a cold start. With one instance over a demo that
behaves identically to Redis. Across scale-out it does not - two workers would
count independently and could both alert.

That is the single behavioural difference between the POC and production, and
it is fixed by setting STATE_BACKEND=redis. No other code changes.
"""
import threading
import time


class MemoryClient:
    """Two dicts and a lock. Only the commands StateStore issues are implemented,
    so an unsupported call fails loudly instead of silently doing nothing."""

    def __init__(self):
        self._v: dict[str, object] = {}
        self._exp: dict[str, float] = {}
        # Functions runs several invocations per worker on a thread pool, so the
        # read-modify-write in incr()/pfadd() needs a lock to be atomic the way
        # the real Redis commands are. RLock because the public methods call
        # _live() beneath them.
        self._lock = threading.RLock()

    def _live(self, key: str) -> bool:
        """True if the key exists and hasn't expired. Expiry is lazy - there is
        no sweeper, exactly like the TTL model Redis gives us."""
        exp = self._exp.get(key)
        if exp is not None and exp <= time.time():
            self._v.pop(key, None)
            self._exp.pop(key, None)
        return key in self._v

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            if nx and self._live(key):
                return None            # redis-py returns None when NX doesn't apply
            self._live(key)            # drop a stale entry so this write starts a new window
            self._v[key] = value
            self._exp.pop(key, None)
            if ex is not None:
                self._exp[key] = time.time() + ex
            return True

    def get(self, key):
        with self._lock:
            return self._v.get(key) if self._live(key) else None

    def incr(self, key):
        with self._lock:
            n = (int(self._v[key]) + 1) if self._live(key) else 1
            self._v[key] = n
            return n

    def expire(self, key, ttl, nx=False):
        with self._lock:
            if not self._live(key):
                return False
            if nx and key in self._exp:
                return False           # window already started; don't slide it
            self._exp[key] = time.time() + ttl
            return True

    def pfadd(self, key, value):
        # An exact set, not a HyperLogLog. At POC volumes exactness is easier to
        # explain than an estimate, and pfcount reads back identically - so a
        # unique() threshold behaves the same as it will on Redis.
        with self._lock:
            if not self._live(key):
                self._v[key] = set()
            members = self._v[key]
            added = value not in members
            members.add(value)
            return int(added)

    def pfcount(self, key):
        with self._lock:
            return len(self._v[key]) if self._live(key) else 0

    def pipeline(self, transaction=False):
        return _MemoryPipeline(self)


class _MemoryPipeline:
    """Queues commands and replays them on execute(), returning one result per
    queued command. The processor batches all its state ops through a pipeline to
    save Redis round-trips; keeping that shape here means the batch loop runs
    unchanged - it just resolves locally instead of over TCP."""

    def __init__(self, client: MemoryClient):
        self._client = client
        self._queued: list[tuple] = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self                # redis-py pipelines are chainable
        return queue

    def execute(self):
        with self._client._lock:
            results = [getattr(self._client, n)(*a, **kw) for n, a, kw in self._queued]
        self._queued.clear()
        return results
