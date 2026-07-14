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
"""
import redis
from azure.identity import DefaultAzureCredential


class StateStore:
    def __init__(self, host: str, port: int, use_entra: bool = True, client=None):
        # `client` lets a caller inject a redis-compatible client (e.g. fakeredis
        # for local runs/tests). When set, no Azure credential is ever requested.
        if client is not None:
            self._r = client
        elif use_entra:
            token = DefaultAzureCredential().get_token("https://redis.azure.com/.default")
            self._r = redis.Redis(host=host, port=port, ssl=True,
                                  username="", password=token.token, decode_responses=True)
        else:
            self._r = redis.Redis(host=host, port=port, ssl=True, decode_responses=True)

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
        return self._r.get(f"alert:{det_id}:{dedup_str}")

    def register_alert(self, det_id: str, dedup_str: str, alert_id: str, ttl: int) -> bool:
        # atomic: create the alert marker only if it does not exist yet
        return bool(self._r.set(f"alert:{det_id}:{dedup_str}", alert_id, nx=True, ex=ttl))

    # ---- unique() ----------------------------------------------------------
    def add_unique(self, pipe, det_id: str, dedup_str: str, value: str, ttl: int) -> None:
        key = f"uniq:{det_id}:{dedup_str}"
        pipe.pfadd(key, value)
        pipe.expire(key, ttl, nx=True)

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
