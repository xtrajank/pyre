"""Signals + alerts write-back to the Cribl lake.

Panther writes a SIGNAL on every match (regardless of config) and an ALERT
when one fires. We reproduce that, writing to Cribl datasets via an HTTP
source. Model the new signals-centric layout (Panther retires the legacy
rule_matches tables on 2026-07-20).

Signals are batched: matches are far rarer than events, so this is cheap.

CONCURRENCY: function_app.py builds ONE Processor per worker and the Python
worker runs sync invocations on a thread pool, so one instance owning several
Event Hub partitions calls process_batch - and therefore add_signal/flush -
from several threads at once against this one object. The buffer is guarded
accordingly; see flush().
"""
import logging
import threading

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("pyre.signals")

# One pooled session per worker instead of a fresh connection per POST. A bare
# requests.post() builds a throwaway Session, so every flush paid a new TCP
# connect + TLS handshake to Cribl; at batch rates that handshake cost is per
# batch-with-matches, forever. Keep-alive amortises it to ~zero.
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))
_SESSION.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=32, max_retries=0))


class SignalWriter:
    def __init__(self, sink_url: str):
        self._url = sink_url  # Cribl HTTP source endpoint
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        if not self._url:
            # SIGNALS_SINK_URL is optional in Terraform (var.signals_sink_url
            # defaults to ""), and flush() no-ops without it - so a deployment
            # that simply forgot to set it discards every signal and every alert
            # record, i.e. the whole audit trail, and looks perfectly healthy
            # doing it. Say so once per worker at cold start: this is a
            # deployment mistake, not a per-batch event, so it must not be
            # logged per flush.
            log.warning(
                "SIGNALS_SINK_URL is not set: signals and alert records will be "
                "DISCARDED, not written. Alerts still dispatch, but nothing will "
                "record that these matches happened. Set signals_sink_url "
                "(Terraform) to the Cribl HTTP source."
            )

    # add_* take the lock so an append can never land on a list flush() has
    # already swapped out and is serialising - that signal would never be sent.
    # Both are called only on a MATCH, which is orders of magnitude rarer than an
    # event, so this lock is off the per-event path entirely.
    def add_signal(self, signal) -> None:
        rec = {
            "_dataset": "pyre_signals",
            "detection_id": signal.detection_id, "dataset": signal.log_type,
            "dedup": signal.dedup_string, "_time": signal.event_time,
            "p_fields": signal.p_fields, "event": signal.event_ref,
        }
        with self._lock:
            self._buf.append(rec)

    def add_alert(self, alert) -> None:
        rec = {
            "_dataset": "pyre_alerts",
            "alert_id": alert.alert_id, "detection_id": alert.detection_id,
            "severity": alert.severity, "title": alert.title, "dedup": alert.dedup_string,
            "event_count": alert.event_count, "first_event_time": alert.first_event_time,
        }
        with self._lock:
            self._buf.append(rec)

    def flush(self) -> None:
        """Post everything buffered so far and hand ownership of that list off.

        The swap has to be atomic. The old read-post-then-clear lost signals
        outright: this thread's POST releases the GIL, a concurrent batch appends
        matches during it, and the clear() then deleted those never-sent records.
        A signal is Panther-parity audit of a rule match, so dropping one is
        silent evidence loss - not just a hiccup.

        Taking another batch's signals along with our own is fine: every buffered
        signal is still sent exactly once, just possibly under a neighbour's POST.
        """
        with self._lock:
            buf, self._buf = self._buf, []
        if not buf or not self._url:
            return
        # Cribl HTTP source accepts newline-delimited JSON or an array.
        resp = _SESSION.post(self._url, json=buf, timeout=10)
        # raise_for_status is the point of this call. requests does NOT raise on
        # 5xx - it returns a Response - so a Cribl outage used to look exactly
        # like success: the signals were dropped, the batch checkpointed, and
        # nothing anywhere recorded that the matches ever happened. Raising fails
        # the batch, which releases the idempotency claims and lets Event Hubs
        # redeliver it (see Processor.process_batch).
        resp.raise_for_status()
