"""The two things this system emits, and their shape.

    SIGNAL   written on EVERY `rule()` that returns True. The complete audit of
             what matched. Never deduplicated - repeats are real.
    ALERT    written when a match also clears the detection's Threshold and is
             not already covered by an open alert. Deduplicated by definition.

Expect far more signals than alerts. That gap is thresholds and dedup doing
their job, not events going missing.

Every record identifies itself, so the two streams stay readable even when
written into the same place:

    p_record_type   "signal" | "alert"
    p_signal_id     unique per match           (signals)
    p_alert_id      unique per alert           (alerts; on a signal, the alert
                                                this match RAISED or JOINED)

On a signal `p_alert_id` is null until a match actually reaches an alert - so the
matches below a threshold, and everything from a `CreateAlert: false` detection,
stay null. Filtering the signals stream on it therefore answers both "which
matches belong to this alert?" and "what matched but was held back?".
"""
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Signal:
    detection_id: str
    log_type: str
    dedup_string: str
    event_time: str
    event: dict[str, Any]


@dataclass
class Alert:
    alert_id: str
    detection_id: str
    title: str
    severity: str
    dedup_string: str
    context: dict[str, Any] = field(default_factory=dict)
    first_event_time: str = ""


class RecordWriter:
    """Buffers a batch's records and writes them out in one go at the end of the
    invocation. Matches are rare relative to events, so this is cheap."""

    def __init__(self, sink):
        self._sink = sink
        self._buf: list[dict] = []

    def add_signal(self, signal: Signal) -> dict:
        """Buffer a signal and hand back the record.

        The caller keeps that reference so it can fill in `p_alert_id` later:
        whether a match becomes an alert isn't known until the whole batch's
        thresholds are evaluated, and nothing has been flushed yet.
        """
        record = {
            "p_record_type": "signal",
            "p_signal_id": str(uuid.uuid4()),
            "p_alert_id": None,                  # filled in if this match alerts
            "detection_id": signal.detection_id,
            "log_type": signal.log_type,
            "dedup": signal.dedup_string,
            "event_time": signal.event_time,
            "event": signal.event,
        }
        self._buf.append(record)
        return record

    def add_alert(self, alert: Alert) -> dict:
        record = {
            "p_record_type": "alert",
            "p_alert_id": alert.alert_id,
            "detection_id": alert.detection_id,
            "severity": alert.severity,
            "title": alert.title,
            "dedup": alert.dedup_string,
            "context": alert.context,
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
