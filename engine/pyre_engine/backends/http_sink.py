"""Production record sink: POST signals and alerts to an HTTP endpoint (the Cribl
lake's HTTP source).

Deliberately thinner than the blob sink. Everything the blob sink has to do by
hand, the lake already does:

  * routing     - one POST carries both streams; Cribl splits them on `_dataset`
  * dedup       - the processor's Redis-backed alert claim guarantees an alert is
                  emitted once, so there is nothing to compensate for here
  * batching    - matches are rare relative to ingest, so a batch per invocation
                  is already cheap

Signals are written on every match and alerts when one fires; both are the same
records the blob sink receives, unchanged.
"""
import logging

import requests

log = logging.getLogger("pyre.sink.http")


class HttpSink:
    def __init__(self, url: str, timeout: int = 10):
        self._url = url
        self._timeout = timeout

    def write(self, records: list[dict]) -> None:
        """Never raises, for the same reason the blob sink doesn't: a failed
        write-back must not fail the batch and cause an at-least-once redelivery
        to re-alert."""
        if not records or not self._url:
            return
        try:
            # The Cribl HTTP source accepts a JSON array or newline-delimited JSON.
            requests.post(self._url, json=records, timeout=self._timeout)
        except Exception:
            log.exception("record write-back failed (%d record(s) dropped)", len(records))
