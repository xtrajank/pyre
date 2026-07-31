"""The ONE place that knows which environment we're in.

Everything above this package - the processor, the registry, the detections -
is environment-agnostic. It routes a log, runs `rule()`, writes a signal on every
match, applies threshold/dedup, and emits an alert. That behaviour is identical
in the POC and in production, and nothing in this package can change it.

What DOES differ is only ever two things, and both are selected here:

    concern          POC (no Redis, no lake)     production
    ---------------  --------------------------  ----------------------------
    state            memory_state.MemoryClient   redis_state (Entra + TLS)
                     in-process, per worker      shared, atomic, survives restart
    records out      blob_sink.BlobSink          http_sink.HttpSink
                     append blobs you can read   POST to the Cribl lake

Selected by two app settings, `STATE_BACKEND` and the presence of
`SIGNALS_SINK_URL` / `OUTPUT_BLOB_ACCOUNT_URL`. Going to production is therefore
a settings change, not a code change - see docs/poc/to-production.md.

Adding a third backend (a queue sink, a Postgres state store) means adding one
module here and one branch in the matching build_* function. Nothing else moves.
"""
import logging

from ..state import StateStore

log = logging.getLogger("pyre.backends")


def build_state_store(cfg) -> StateStore:
    """Where dedup counters, thresholds, unique() sets and the redelivery guard
    live. `StateStore` owns the key naming and TTL semantics for both backends;
    only the client underneath it changes, so the two can never drift."""
    backend = (cfg.state_backend or "redis").lower()
    if backend == "memory":
        from .memory_state import MemoryClient
        log.info("state backend: memory (in-process, per-worker - POC only)")
        return StateStore(MemoryClient())
    if backend == "redis":
        from .redis_state import build_client
        log.info("state backend: redis (%s)", cfg.redis_host)
        return StateStore(build_client(cfg))
    raise ValueError(f"unknown STATE_BACKEND {backend!r} (expected 'memory' or 'redis')")


def build_record_sink(cfg):
    """Where signals and alerts are written. Both sinks take the same records and
    are free to lay them out however that medium wants - Cribl routes on
    `_dataset`, a blob container has no routing so the sink splits the streams
    into two files itself. Same records either way."""
    if cfg.signals_sink_url:
        from .http_sink import HttpSink
        log.info("record sink: http (%s)", cfg.signals_sink_url)
        return HttpSink(cfg.signals_sink_url)
    if cfg.output_blob_account_url:
        from .blob_sink import BlobSink
        log.info("record sink: blob (%s/%s)", cfg.output_blob_account_url, cfg.output_blob_container)
        return BlobSink(cfg.output_blob_account_url, cfg.output_blob_container)
    log.warning("no record sink configured (set SIGNALS_SINK_URL or "
                "OUTPUT_BLOB_ACCOUNT_URL); signals and alerts will be dropped")
    return _NullSink()


class _NullSink:
    """Used when nothing is configured. Detection still runs and still logs; the
    records just go nowhere. Better than failing a batch over a missing setting."""

    def write(self, records: list[dict]) -> None:
        pass
