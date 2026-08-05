"""Dedup windows, thresholds, `unique()` counts, the storm limiter and the
redelivery guard - everything the engine has to remember between events.

`StateStore` owns the SEMANTICS: the key names, the TTL rules, what has to be
atomic. The client underneath it is either Redis (shared across workers) or an
in-process dict (one worker). Because both go through this one class they cannot
drift in behaviour; the only real difference is whether the state is shared.

Design notes that matter at volume:
  * every op for a batch is pipelined, so a 256-event batch is a few round-trips
  * the dedup window IS the key's TTL - no sweeper process
  * `unique()` uses PFADD/PFCOUNT (HyperLogLog on Redis), which is memory-cheap
    at millions of members
  * idempotency is a short-TTL "seen" key per event id, which makes Event Hubs'
    at-least-once redelivery safe
"""
import hashlib
import logging
import os
import threading
import time

log = logging.getLogger("pyre.state")


def _scope(det_id: str, dedup_str: str) -> str:
    """The key suffix identifying one detection's one dedup group.

    The dedup string is HASHED rather than interpolated. A dedup string is
    detection-authored and derived from event data, so it can contain `:` (an
    IP:port, a URL, a DN) and would otherwise merge into the key namespace -
    two different detections could then collide on one counter. Hashing also
    bounds the key size, which the 1000-char dedup truncation upstream no longer
    has to do on Redis' behalf.
    """
    return f"{det_id}:{hashlib.sha256(dedup_str.encode('utf-8')).hexdigest()[:32]}"


class StateStore:
    def __init__(self, client):
        # Any object speaking the small Redis subset used below: a real
        # redis.Redis, the MemoryClient here, or fakeredis in tests.
        self._r = client

    # ---- idempotency -------------------------------------------------------
    def is_new_event(self, pipe, event_id: str) -> None:
        """SET NX with a TTL. The result is read back after execute()."""
        pipe.set(f"seen:{event_id}", "1", nx=True, ex=3600)

    # ---- dedup + threshold -------------------------------------------------
    def bump_dedup(self, pipe, det_id: str, dedup_str: str, ttl: int) -> None:
        key = f"dd:{_scope(det_id, dedup_str)}"
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)   # TTL on first write only: the window must not slide

    def bump_unique(self, pipe, det_id: str, dedup_str: str, value: str, ttl: int) -> None:
        """Add a value to the distinct set and read the new count back in the
        SAME round-trip, which is the shape the batch loop needs to decide
        unique-mode thresholds without a per-match call."""
        key = f"uniq:{_scope(det_id, dedup_str)}"
        pipe.pfadd(key, value)
        pipe.expire(key, ttl, nx=True)
        pipe.pfcount(key)

    # ---- alert claim -------------------------------------------------------
    def alert_exists(self, det_id: str, dedup_str: str) -> str | None:
        """The id of the alert already open for this dedup string, or None.
        Returning the id rather than a bool is what lets a grouped match record
        which alert it belongs to."""
        return self._r.get(f"alert:{_scope(det_id, dedup_str)}")

    def register_alert(self, det_id: str, dedup_str: str, alert_id: str, ttl: int) -> bool:
        """Atomic first-event-wins claim: only one worker can create the marker."""
        return bool(self._r.set(f"alert:{_scope(det_id, dedup_str)}", alert_id, nx=True, ex=ttl))

    # ---- storm limiter -----------------------------------------------------
    def storm_ok(self, det_id: str, hour_bucket: str, limit: int) -> bool:
        key = f"storm:{det_id}:{hour_bucket}"
        n = self._r.incr(key)
        if n == 1:
            self._r.expire(key, 3600)
        return n <= limit

    def pipeline(self):
        return self._r.pipeline(transaction=False)


def build_state_store(cfg) -> StateStore:
    """STATE_BACKEND names which. `redis` with no REDIS_HOST is reported by
    RuntimeConfig.problems() and falls back here rather than failing the app -
    detection with per-worker state beats no detection at all."""
    if cfg.state_backend == "redis" and cfg.redis_host:
        log.info("state: redis at %s (shared across workers)", cfg.redis_host)
        return StateStore(_redis_client(cfg))
    if cfg.state_backend == "redis":
        log.error("STATE_BACKEND=redis but REDIS_HOST is not set; falling back to "
                  "in-process state. Thresholds and dedup will NOT be shared across workers.")
    else:
        log.info("state: in-process (per worker, resets on restart)")
    return StateStore(MemoryClient())


def _redis_client(cfg):
    """Azure Cache for Redis over TLS, authenticated with Entra - no password
    anywhere. Imported lazily so an environment without Redis never loads it."""
    import redis
    from azure.identity import DefaultAzureCredential
    from redis.credentials import CredentialProvider

    class _Entra(CredentialProvider):
        """Fetches a fresh token whenever redis-py opens a NEW connection (pool
        growth, or a reconnect after a blip). Grabbing one token at cold start
        instead would silently start failing every call once it expired - which
        is exactly what a long-warm worker under sustained load hits."""

        def __init__(self):
            self._cred = DefaultAzureCredential(
                managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None)
            self._token = None

        def get_credentials(self):
            if self._token is None or self._token.expires_on - time.time() < 300:
                self._token = self._cred.get_token("https://redis.azure.com/.default")
            return "", self._token.token

    return redis.Redis(host=cfg.redis_host, port=cfg.redis_port, ssl=True,
                       decode_responses=True, credential_provider=_Entra())


class MemoryClient:
    """The in-process stand-in: two dicts and a lock, implementing only the
    commands StateStore issues so an unsupported call fails loudly rather than
    silently doing nothing.

    The honest trade: this lives and dies with the worker process. Dedup windows,
    thresholds, the storm limit and the redelivery guard are all per-instance and
    reset on a restart. On one instance that behaves identically to Redis; across
    scale-out it does not, because two workers would count independently and both
    could alert. Setting REDIS_HOST is the entire fix.
    """

    def __init__(self):
        self._v: dict[str, object] = {}
        self._exp: dict[str, float] = {}
        # Functions runs several invocations per worker on a thread pool, so the
        # read-modify-write in incr()/pfadd() needs a lock to be as atomic as the
        # real Redis commands. RLock because the public methods call _live().
        self._lock = threading.RLock()

    def _live(self, key: str) -> bool:
        """True if the key exists and hasn't expired. Expiry is lazy - no
        sweeper, exactly like Redis' own TTL model."""
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
        # An exact set, not a HyperLogLog. At one-worker volumes exactness is
        # easier to explain than an estimate, and pfcount reads back identically,
        # so a unique() threshold behaves the same as it will on Redis.
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
    queued command. Keeping redis-py's pipeline shape means the batch loop runs
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
