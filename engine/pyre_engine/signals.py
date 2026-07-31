"""Signals + alerts write-back to the Cribl lake.

Panther writes a SIGNAL on every match (regardless of config) and an ALERT
when one fires. We reproduce that, writing to Cribl datasets via an HTTP
source. Model the new signals-centric layout (Panther retires the legacy
rule_matches tables on 2026-07-20).

Signals are batched: matches are far rarer than events, so this is cheap.
"""
import requests


class SignalWriter:
    def __init__(self, sink_url: str, blob_sink=None):
        self._url = sink_url  # Cribl HTTP source endpoint
        # POC fallback when there is no Cribl to write back to: an append-blob
        # sink (see blobsink.py). Used only when no sink_url is configured.
        self._blob = blob_sink
        self._buf: list[dict] = []

    def add_signal(self, signal) -> None:
        self._buf.append({
            "_dataset": "pyre_signals",
            "detection_id": signal.detection_id, "dataset": signal.log_type,
            "dedup": signal.dedup_string, "_time": signal.event_time,
            "p_fields": signal.p_fields, "event": signal.event_ref,
        })

    def add_alert(self, alert) -> None:
        self._buf.append({
            "_dataset": "pyre_alerts",
            "alert_id": alert.alert_id, "detection_id": alert.detection_id,
            "severity": alert.severity, "title": alert.title, "dedup": alert.dedup_string,
            "event_count": alert.event_count, "first_event_time": alert.first_event_time,
        })

    def flush(self) -> None:
        if not self._buf:
            return
        try:
            if self._url:
                # Cribl HTTP source accepts newline-delimited JSON or an array.
                requests.post(self._url, json=self._buf, timeout=10)
            elif self._blob is not None:
                self._blob.append(self._buf)
        finally:
            self._buf.clear()
