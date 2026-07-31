"""Signals and alerts write-back.

Panther writes a SIGNAL on every match (regardless of config) and an ALERT when
one fires. We reproduce that. This module builds those records and buffers them;
where they physically go is the sink's problem (see backends/).

Every record is self-identifying, so the two streams stay distinguishable no
matter what they're written into:

    p_record_type   "signal" | "alert"
    p_signal_id     unique per match          (signals only)
    p_alert_id      unique per alert          (alerts; also set on the signals
                                               that belong to that alert)

That last one is the useful link: a signal carries the id of the alert it rolled
into, or null if it never reached one (below threshold, or CreateAlert: false).
So "which matches caused this alert?" and "which matches went nowhere?" are both
answerable from the signals stream alone.

Signals are batched: matches are far rarer than events, so this is cheap.
"""
import uuid


class RecordWriter:
    def __init__(self, sink):
        self._sink = sink
        self._buf: list[dict] = []

    def add_signal(self, signal) -> dict:
        """Buffer a signal and return the record.

        The caller keeps that reference so it can fill in `p_alert_id` later -
        whether this match becomes an alert isn't known until after the whole
        batch's thresholds are evaluated, and the record hasn't been flushed yet.
        """
        record = {
            "_dataset": "pyre_signals",
            "p_record_type": "signal",
            "p_signal_id": str(uuid.uuid4()),
            "p_alert_id": None,                 # filled in if this match alerts
            "detection_id": signal.detection_id,
            "dataset": signal.log_type,
            "dedup": signal.dedup_string,
            "_time": signal.event_time,
            "p_fields": signal.p_fields,
            "event": signal.event_ref,
        }
        self._buf.append(record)
        return record

    def add_alert(self, alert) -> dict:
        record = {
            "_dataset": "pyre_alerts",
            "p_record_type": "alert",
            "p_alert_id": alert.alert_id,
            "detection_id": alert.detection_id,
            "severity": alert.severity,
            "title": alert.title,
            "dedup": alert.dedup_string,
            "destinations": alert.destinations,
            "context": alert.context,
            "event_count": alert.event_count,
            "first_event_time": alert.first_event_time,
        }
        self._buf.append(record)
        return record

    def flush(self) -> None:
        if not self._buf:
            return
        try:
            self._sink.write(self._buf)
        finally:
            self._buf.clear()


# Kept so existing imports/tests keep working; RecordWriter is the name that
# describes what it does now that it writes both streams.
SignalWriter = RecordWriter
