"""Stateful dedup / threshold / unique / storm-limit, backed by Redis.

This is the piece Panther gives you for free and the thing most likely to be
under-built. Azure Functions are stateless and run many instances concurrently,
so every stateful behavior needs an ATOMIC, low-latency, external store.

Design for scale/cost:
  * All ops for a whole batch are pipelined -> a 256-event batch is a few
    round-trips, not 256.
  * The dedup WINDOW is just the key's TTL. No sweeper process needed.
  * unique() uses HyperLogLog (PFADD/PFCOUNT) - matches Panther's "estimated
    count of unique values" and is memory-cheap at millions of members.
  * Idempotency: a short-TTL "seen" set on event id makes Event Hubs
    at-least-once redelivery safe (no double counting after a retry).

Read-modify-write is done in LUA, not in Python, for three reasons that all
matter at volume:

  1. ATOMICITY. "INCR, then EXPIRE if it's new" as two client commands is not
     atomic: crash or fail between them and the key is left with NO TTL - it
     then never expires and leaks for the lifetime of the cache. Unbounded
     state from a partial failure is exactly the trap DDIA warns about; the
     script makes the pair indivisible.
  2. PORTABILITY. The obvious one-command fix, `EXPIRE key ttl NX`, is Redis
     7.0+. Azure Cache for Redis Basic/Standard/Premium is 6.0, so that syntax
     raises there - while fakeredis accepts it, so no local test would ever
     catch it. Lua works on both.
  3. ROUND TRIPS. One script call replaces two commands.

The scripts re-arm a TTL whenever they find a key without one (TTL < 0), so a
window key left over from an older, non-atomic write heals itself instead of
leaking forever.
"""
import hashlib
import logging

import redis
from azure.identity import DefaultAzureCredential
from redis.credentials import CredentialProvider

log = logging.getLogger("pyre.dedup")

# INCR + arm the TTL, atomically. Returns the post-increment count.
_COUNT_WINDOW_LUA = """
local n = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return n
"""

# PFADD + arm the TTL + read the estimate back, atomically. Returns the distinct
# count so the batch loop can decide a unique() threshold without another call.
_UNIQUE_WINDOW_LUA = """
redis.call('PFADD', KEYS[1], ARGV[2])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return redis.call('PFCOUNT', KEYS[1])
"""

# Bound every Redis op. redis-py defaults to NO socket timeout, so a silently
# dropped connection (an Azure Redis failover, a VNet blip) parks the worker
# thread forever instead of failing the batch and letting Event Hubs redeliver.
_SOCKET_TIMEOUT = 5
_CONNECT_TIMEOUT = 5


class _EntraCredentialProvider(CredentialProvider):
    """Fetches a fresh Entra token whenever redis-py opens a NEW connection
    (pool growth, or a reconnect after a network blip), instead of the old
    fixed-password approach where a token grabbed once at cold start would
    silently start failing every Redis call once it expired - exactly the
    failure mode a long-warm worker under sustained load would hit."""

    def __init__(self):
        self._cred = DefaultAzureCredential()
        self._token = None

    def get_credentials(self):
        import time
        if self._token is None or self._token.expires_on - time.time() < 300:
            self._token = self._cred.get_token("https://redis.azure.com/.default")
        return "", self._token.token


def _window_key(prefix: str, det_id: str, dedup_str: str) -> str:
    """A fixed-size key for a (detection, dedup string) window.

    The dedup string is detection-authored and only capped at 1000 chars, so
    embedding it raw put up to ~1KB in EVERY dd:/uniq:/alert: key - and the key
    is stored per distinct dedup value, per detection, for the whole window.
    Hashing pins it at 32 hex chars. blake2b-128 makes an accidental collision
    (two dedup strings grouping into one alert) far less likely than the cache
    losing the key anyway. The detection id stays readable so keys are still
    greppable per detection; the full dedup string is carried on the alert and
    signal records, which is where anyone actually reads it.
    """
    h = hashlib.blake2b(dedup_str.encode("utf-8", "replace"), digest_size=16).hexdigest()
    return f"{prefix}:{det_id}:{h}"


class StateStore:
    def __init__(self, host: str, port: int, use_entra: bool = True, client=None,
                 max_connections: int = 64):
        # `client` lets a caller inject a redis-compatible client (e.g. fakeredis
        # for local runs/tests). When set, no Azure credential is ever requested.
        if client is not None:
            self._r = client
        else:
            kwargs = dict(
                host=host, port=port, ssl=True, decode_responses=True,
                socket_timeout=_SOCKET_TIMEOUT,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_keepalive=True,
                # Detect a half-open connection before a batch blocks on it.
                health_check_interval=30,
                # Bound the pool. redis-py's default is effectively unlimited, so
                # a thread-pool worker under load can open connections without
                # limit and exhaust the cache's client budget.
                max_connections=max_connections,
                # NOT retry_on_timeout: INCR/PFADD are not idempotent, so a
                # transparent retry after a timeout could double-count a
                # threshold. Fail the batch instead and let Event Hubs redeliver
                # it - process_batch releases its idempotency claims on failure,
                # so the redelivery is real work, not a silent skip.
            )
            if use_entra:
                kwargs["credential_provider"] = _EntraCredentialProvider()
            self._r = redis.Redis(**kwargs)

        self._count_window = self._r.register_script(_COUNT_WINDOW_LUA)
        self._unique_window = self._r.register_script(_UNIQUE_WINDOW_LUA)

    # ---- idempotency -------------------------------------------------------
    def is_new_event(self, pipe, event_id: str, ttl: int = 900) -> None:
        """Claim an event id; result read after execute(). SET NX + TTL.

        `ttl` is THE dominant Redis cost in this system, because it's the only key
        written for EVERY event rather than per match. Resident key count is
        events/sec x ttl, so at ~50k events/sec an hour holds ~180M keys
        (~15-20GB) purely to catch redeliveries. It only has to outlive an Event
        Hubs checkpoint retry, which is seconds-to-minutes - hence the 15-minute
        default. Tunable via IDEMPOTENCY_TTL_SECONDS; see config.py.
        """
        pipe.set(f"seen:{event_id}", "1", nx=True, ex=ttl)

    def release_events(self, event_ids) -> None:
        """Drop idempotency claims for events whose batch did NOT finish.

        Without this the claim is a "mark, then do" - the mark is committed
        before the work, so a batch that dies after phase 0 (a Cribl 5xx, a
        dispatch failure) is redelivered by Event Hubs, finds every event
        already claimed, skips them all, and the matches are lost with no error.
        Releasing on failure turns the claim back into "do, then mark": a
        redelivery re-does real work, so the system loses nothing and may at
        worst duplicate - the right side of that trade for security telemetry.
        """
        ids = list(event_ids)
        if not ids:
            return
        pipe = self._r.pipeline(transaction=False)
        for eid in ids:
            pipe.delete(f"seen:{eid}")
        pipe.execute()

    # ---- dedup + threshold -------------------------------------------------
    def bump_dedup(self, pipe, det_id: str, dedup_str: str, ttl: int) -> None:
        """Count this match in its window; count read after execute()."""
        self._count_window(keys=[_window_key("dd", det_id, dedup_str)], args=[ttl], client=pipe)

    def register_alert(self, det_id: str, dedup_str: str, alert_id: str, ttl: int) -> bool:
        """Claim the alert marker for this dedup window; True if WE created it.

        Atomic (SET NX), and deliberately the only alert-window test there is:
        False already means "someone alerted for this dedup string inside the
        window", so a separate exists() read would just be an extra round-trip on
        the common grouped-match path - and a check-then-act race besides.
        """
        return bool(self._r.set(_window_key("alert", det_id, dedup_str), alert_id, nx=True, ex=ttl))

    def release_alert(self, det_id: str, dedup_str: str) -> None:
        """Re-open the alert window after a delivery failure.

        register_alert claims the window BEFORE dispatch, so if dispatch fails the
        marker is a lie: it says "already alerted" for the rest of the window, and
        every later match is grouped under an alert that nobody ever received.
        Dropping the marker lets the next match re-fire and re-deliver, so a
        transient Torq/webhook outage costs a delayed alert instead of a lost one.
        """
        self._r.delete(_window_key("alert", det_id, dedup_str))

    # ---- unique() ----------------------------------------------------------
    def bump_unique(self, pipe, det_id: str, dedup_str: str, value: str, ttl: int) -> None:
        """Add a value to this window's distinct set; the updated estimate is read
        back after execute(), so unique-mode thresholds cost no extra call."""
        self._unique_window(keys=[_window_key("uniq", det_id, dedup_str)],
                            args=[ttl, value], client=pipe)

    # ---- storm limiter -----------------------------------------------------
    def storm_ok(self, det_id: str, hour_bucket: str, limit: int) -> bool:
        """Has this detection stayed under its alerts-per-hour budget?

        Same atomic count-with-TTL as dedup. The old two-command form could leave
        the hour bucket with no TTL, so a detection that stormed once would stay
        counted forever and be suppressed for the life of the cache.
        """
        n = self._count_window(keys=[f"storm:{det_id}:{hour_bucket}"], args=[3600])
        return n <= limit

    def pipeline(self):
        return self._r.pipeline(transaction=False)
