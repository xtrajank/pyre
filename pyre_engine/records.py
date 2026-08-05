"""The two things this system emits, and their exact shape.

    SIGNAL   written on EVERY `rule()` that returns True. The complete audit of
             what matched. Never deduplicated - repeats are real.
    ALERT    written when a match also clears the detection's Threshold and is
             not already covered by an open alert. Deduplicated by definition.

Expect far more signals than alerts. That gap is thresholds and dedup doing
their job, not events going missing.

This module is the ONLY place either schema is written down, so a field cannot
exist in one half of the code and not the other. docs/signals-and-alerts.md
documents what is built here.

Every engine-added field is `p_`-prefixed. The raw log record lives under
`p_event` untouched, so an event carrying its own `severity` or `title` can
never collide with the engine's.

Three fields tie the streams together:

    p_record_type   "signal" | "alert"
    p_signal_id     unique per match           (signals)
    p_alert_id      unique per alert           (alerts; on a signal, the alert
                                                this match RAISED or JOINED)

On a signal `p_alert_id` is null until a match actually reaches an alert - so
matches below a threshold, and everything from a `CreateAlert: false` detection,
stay null. Filtering the signals stream on it therefore answers both "which
matches belong to this alert?" and "what matched but was held back?".

An ALERT is deliberately self-sufficient: log type, severity, runbook,
timestamps, the threshold that fired it and one example event all travel with
it, so a case tool can open a ticket without joining back to the signals stream.
"""
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger("pyre.records")

SIGNAL = "signal"
ALERT = "alert"


def now_iso() -> str:
    """The engine's own clock, ISO 8601 UTC. Distinct from the event's
    timestamp, which comes from the producer and may be wrong, missing, or in
    any format at all."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_signal(det, source, event, log_type: str, dedup_string: str,
                 indicators: dict, processed_time: str) -> dict:
    """One match. `det` is a Detection, `source` a Source - both duck-typed, so
    this module stays independent of how either is loaded."""
    return {
        "p_record_type": SIGNAL,
        "p_signal_id": str(uuid.uuid4()),
        # Filled in later if this match reaches an alert - see the module
        # docstring. The processor keeps this dict by reference to do it.
        "p_alert_id": None,

        "p_detection_id": det.id,
        "p_detection_name": det.display_name,
        "p_severity": det.severity(event),
        "p_tags": det.tags,
        "p_reports": det.reports,

        "p_log_type": log_type,
        "p_source_namespace": source.namespace,
        "p_source_hub": source.hub,

        "p_dedup": dedup_string,
        "p_event_time": event.get(source.event_time_field, ""),
        "p_processed_time": processed_time,

        **indicators,
        "p_event": dict(event),
    }


def build_alert(det, source, event, log_type: str, dedup_string: str, indicators: dict,
                signal_id: str, signal_count: int, created_time: str) -> dict:
    """One alert, self-sufficient for case creation.

    `signal_count` is the threshold counter's value at the moment the alert was
    raised. It is a POINT-IN-TIME number: alerts are written once and never
    rewritten, so later matches joining this alert's dedup window do not update
    it. Count `p_signal_id`s in the signals stream filtered on `p_alert_id` for
    the live total.
    """
    return {
        "p_record_type": ALERT,
        "p_alert_id": str(uuid.uuid4()),

        "p_detection_id": det.id,
        "p_detection_name": det.display_name,
        "p_severity": det.severity(event),
        "p_title": det.title(event),
        "p_description": det.description,
        "p_runbook": det.runbook,
        "p_reference": det.reference,
        "p_tags": det.tags,
        "p_reports": det.reports,

        "p_log_type": log_type,
        "p_source_namespace": source.namespace,
        "p_source_hub": source.hub,

        "p_dedup": dedup_string,
        "p_threshold": det.threshold,
        "p_dedup_period_minutes": det.dedup_period_seconds // 60,
        "p_signal_count": signal_count,

        # The match that raised this alert: its signal, its timestamp, and the
        # event itself. Enough to triage without querying the signals stream.
        "p_first_signal_id": signal_id,
        "p_first_event_time": event.get(source.event_time_field, ""),
        "p_created_time": created_time,

        "p_context": det.alert_context(event),
        **indicators,
        "p_event": dict(event),
    }


def normalize_indicators(raw) -> dict:
    """A detection's optional `indicators(event)` output, made safe to merge.

    Returned keys become `p_any_*` pivot fields. `{"ip_addresses": [...]}` and
    `{"p_any_ip_addresses": [...]}` are both accepted so a detection can be
    written the short way. Values are coerced to a sorted list of strings so the
    field is queryable regardless of what the detection handed back, and a
    detection returning nonsense costs its own indicators, not the record.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        name = str(key) if str(key).startswith("p_any_") else f"p_any_{key}"
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        cleaned = sorted({str(v) for v in items if v is not None and str(v) != ""})
        if cleaned:
            out[name] = cleaned
    return out


class RecordWriter:
    """Buffers a batch's records and writes them out in one go at the end of the
    invocation. Matches are rare relative to events, so this is cheap."""

    def __init__(self, sink):
        self._sink = sink
        self._buf: list[dict] = []

    def add(self, record: dict) -> dict:
        """Buffer a record and hand back the same dict.

        The caller keeps that reference so it can fill in `p_alert_id` on a
        signal later: whether a match becomes an alert isn't known until the
        whole batch's thresholds are evaluated, and nothing has been flushed yet.
        """
        self._buf.append(record)
        return record

    def flush(self) -> None:
        if not self._buf:
            return
        try:
            self._sink.write(self._buf)
        finally:
            self._buf.clear()
