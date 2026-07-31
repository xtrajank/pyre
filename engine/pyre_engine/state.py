"""Stateful dedup / threshold / unique() / storm-limit / idempotency.

This is the piece Panther gives you for free and the thing most likely to be
under-built. Azure Functions are stateless and run many instances concurrently,
so every stateful behaviour needs an ATOMIC, low-latency store.

This module owns the SEMANTICS - the key names, the TTL rules, what is atomic -
and nothing else. The store underneath is injected (see backends/): Redis in
production, an in-process client for the POC. Because both go through this one
class, the two backends cannot drift in behaviour; the only real difference is
whether the state is shared between workers.

Design for scale/cost:
  * All ops for a whole batch are pipelined -> a 256-event batch is a few
    round-trips, not 256.
  * The dedup WINDOW is just the key's TTL. No sweeper process needed.
  * unique() uses HyperLogLog (PFADD/PFCOUNT) - matches Panther's "estimated
    count of unique values" and is memory-cheap at millions of members.
  * Idempotency: a short-TTL "seen" key per event id makes Event Hubs
    at-least-once redelivery safe (no double counting after a retry).
"""


class StateStore:
    def __init__(self, client):
        # `client` is any object speaking the small Redis subset used below -
        # a real redis.Redis, the POC's MemoryClient, or fakeredis in tests.
        self._r = client

    # ---- idempotency -------------------------------------------------------
    def is_new_event(self, pipe, event_id: str) -> None:
        # SET NX with TTL; result read after execute()
        pipe.set(f"seen:{event_id}", "1", nx=True, ex=3600)

    # ---- dedup + threshold -------------------------------------------------
    def bump_dedup(self, pipe, det_id: str, dedup_str: str, ttl: int) -> None:
        key = f"dd:{det_id}:{dedup_str}"
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)  # set TTL only on first write (window start)

    def alert_exists(self, det_id: str, dedup_str: str) -> str | None:
        """The id of the alert already open for this dedup string, or None.
        Returning the ID (not just a bool) is what lets a grouped match record
        which alert it belongs to."""
        return self._r.get(f"alert:{det_id}:{dedup_str}")

    def register_alert(self, det_id: str, dedup_str: str, alert_id: str, ttl: int) -> bool:
        # atomic: create the alert marker only if it does not exist yet
        return bool(self._r.set(f"alert:{det_id}:{dedup_str}", alert_id, nx=True, ex=ttl))

    # ---- unique() ----------------------------------------------------------
    def add_unique(self, pipe, det_id: str, dedup_str: str, value: str, ttl: int) -> None:
        key = f"uniq:{det_id}:{dedup_str}"
        pipe.pfadd(key, value)
        pipe.expire(key, ttl, nx=True)

    def bump_unique(self, pipe, det_id: str, dedup_str: str, value: str, ttl: int) -> None:
        """Like add_unique, but also pipelines a pfcount so the processor can read
        the updated distinct-value count back in the SAME round-trip (the shape
        the batch loop needs to decide unique-mode thresholds without an extra
        per-match call)."""
        key = f"uniq:{det_id}:{dedup_str}"
        pipe.pfadd(key, value)
        pipe.expire(key, ttl, nx=True)
        pipe.pfcount(key)

    def unique_count(self, det_id: str, dedup_str: str) -> int:
        return int(self._r.pfcount(f"uniq:{det_id}:{dedup_str}"))

    # ---- storm limiter -----------------------------------------------------
    def storm_ok(self, det_id: str, hour_bucket: str, limit: int) -> bool:
        key = f"storm:{det_id}:{hour_bucket}"
        n = self._r.incr(key)
        if n == 1:
            self._r.expire(key, 3600)
        return n <= limit

    def pipeline(self):
        return self._r.pipeline(transaction=False)
