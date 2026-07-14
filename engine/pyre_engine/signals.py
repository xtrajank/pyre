"""Signals + alerts write-back to the Cribl lake.

Panther writes a SIGNAL on every match (regardless of config) and an ALERT
when one fires. We reproduce that, writing to Cribl datasets via an HTTP
source. Model the new signals-centric layout (Panther retires the legacy
rule_matches tables on 2026-07-20).

Signals are batched: matches are far rarer than events, so this is cheap.
"""
import requests


class SignalWriter:
    def __init__(self, sink_url: str):
        self._url = sink_url  # Cribl HTTP source endpoint
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
        if not self._buf or not self._url:
            self._buf.clear()
            return
        # Cribl HTTP source accepts newline-delimited JSON or an array.
        try:
            requests.post(self._url, json=self._buf, timeout=10)
        finally:
            self._buf.clear()
