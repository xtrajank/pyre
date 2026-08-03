"""The batch loop. Everything the engine does happens here, in this order:

    unwrap envelope -> skip redeliveries -> read the log-type field -> select
    that log type's detections -> rule() -> on match ALWAYS write a signal ->
    threshold + dedup -> storm limit -> alert

One `Processor` serves every source. The per-source differences (which field
routes, which field is the timestamp, whether records are wrapped) arrive as the
`Source` passed to `process_batch`, so twenty sources with twenty different log
shapes still run one copy of this code.

All state operations for a batch are pipelined: a 256-event batch costs a few
round-trips, not 256.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from .bundle import source_from_config
from .config import RuntimeConfig, Source
from .event import Event
from .records import Alert, RecordWriter, Signal
from .registry import BundleLoader
from .sinks import build_sink
from .state import build_state_store

log = logging.getLogger("pyre.processor")


class Processor:
    def __init__(self, cfg: RuntimeConfig, state=None, sink=None):
        self.cfg = cfg
        # State and sink are the only environment-dependent pieces, and both are
        # chosen from config alone. They're injectable purely so a test or local
        # run can substitute a fake; nothing below behaves differently for it.
        self.state = state or build_state_store(cfg)
        self.records = RecordWriter(sink or build_sink(cfg))
        self.loader = BundleLoader(source_from_config(cfg), cfg.dac_refresh_seconds)

    def process_batch(self, messages: list[str], source: Source,
                      event_ids: list[str] | None = None) -> None:
        """One Event Hub invocation's worth of messages, from one source.

        `event_ids` are the transport ids (partition + sequence number), which
        stay stable across an Event Hubs redelivery and are what the redelivery
        guard keys on. Omitted (the `ingest` endpoint, local runs), the message
        body is hashed instead.
        """
        registry = self.loader.get()          # hot-reloads on a detection publish
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        # --- phase 0: messages -> records ----------------------------------
        # One transport message can carry many log records. This is where "a
        # message" becomes "the events in it"; everything below deals only in
        # individual records.
        candidates = []                        # (event_id, record)
        for idx, raw in enumerate(messages):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("skipping message %d: not valid JSON", idx)
                continue
            base = (event_ids[idx] if event_ids and idx < len(event_ids) else None) \
                or hashlib.sha1(raw.encode("utf-8")).hexdigest()
            for n, record in enumerate(_unwrap(parsed, source.envelope_field)):
                if isinstance(record, dict):
                    # The id is per RECORD, not per message, so a redelivered
                    # message suppresses each of its records individually. The
                    # suffix is positional and so stable across a redelivery.
                    candidates.append((f"{base}#{n}", record))

        # --- phase 1: drop redeliveries ------------------------------------
        # Event Hubs is at-least-once: a checkpoint retry can redeliver a batch
        # already processed. One extra pipelined round-trip per BATCH marks each
        # id seen, so signals and dedup counters can't be double-written.
        id_pipe = self.state.pipeline()
        for eid, _ in candidates:
            self.state.is_new_event(id_pipe, eid)
        first_sighting = id_pipe.execute()

        # --- phase 2: route and evaluate ------------------------------------
        pipe = self.state.pipeline()
        pending = []                           # matches awaiting an alert decision
        # Per-batch routing tally. One dict, no extra calls, and it's the
        # difference between "no alerts and no idea why" and a log line naming
        # the exact values that arrived with no detection behind them.
        no_log_type = 0
        unrouted: dict[str, int] = {}

        for (eid, record), is_new in zip(candidates, first_sighting):
            if not is_new:
                log.info("skipping already-processed event %s (redelivery)", eid)
                continue
            event = Event(record)
            log_type = event.get(source.log_type_field)
            if not log_type:
                no_log_type += 1
                continue
            detections = registry.for_log_type(log_type)
            if not detections:
                unrouted[log_type] = unrouted.get(log_type, 0) + 1
                continue

            for det in detections:
                try:
                    if not det.rule(event):
                        continue
                except Exception:
                    log.exception("detection %s raised on a %s event; skipping it for this event",
                                  det.id, log_type)
                    continue

                dedup_str = (det.dedup(event) or det.title(event))[:1000]
                # ALWAYS a signal on match. Whether it ends up inside an alert
                # isn't known until the thresholds below are evaluated, and
                # nothing is flushed yet, so p_alert_id is filled in later.
                signal = self.records.add_signal(Signal(
                    detection_id=det.id, log_type=log_type, dedup_string=dedup_str,
                    event_time=event.get(source.event_time_field, ""), event=event,
                ))
                if not det.create_alert:
                    continue

                # A detection defining unique() counts DISTINCT values (5 different
                # source IPs) instead of every match. Its presence picks the mode.
                unique_val = det.unique(event)
                if unique_val is not None:
                    self.state.bump_unique(pipe, det.id, dedup_str, str(unique_val),
                                           det.dedup_period_seconds)
                    pending.append((det, event, dedup_str, "unique", signal))
                else:
                    self.state.bump_dedup(pipe, det.id, dedup_str, det.dedup_period_seconds)
                    pending.append((det, event, dedup_str, "count", signal))

        if no_log_type:
            log.warning("%d event(s) from hub '%s' had no value in the log-type field '%s' - "
                        "check log_type_field in config/sources.yaml against your data",
                        no_log_type, source.hub, source.log_type_field)
        if unrouted:
            log.warning("no detections are registered for these log-type values: %s. "
                        "A detection's YAML LogTypes must contain the value exactly.",
                        ", ".join(f"{lt} ({n} event(s))" for lt, n in sorted(unrouted.items())))

        results = pipe.execute()               # one round-trip for every dedup/unique bump

        # --- phase 3: threshold, dedup, alert -------------------------------
        # "count" mode pipelined [incr, expire]; "unique" pipelined
        # [pfadd, expire, pfcount]. Read the counts back by position.
        i = 0
        for det, event, dedup_str, mode, signal in pending:
            if mode == "unique":
                count = results[i + 2]; i += 3
            else:
                count = results[i]; i += 2
            if count < det.threshold:
                continue                       # below threshold: the signal stands alone
            existing = self.state.alert_exists(det.id, dedup_str)
            if existing:
                # Inside the window: this match joins the alert already open.
                # Recording that id is what makes the signals stream answer
                # "which matches made up this alert?".
                signal["p_alert_id"] = existing
                continue
            alert = Alert(
                alert_id=str(uuid.uuid4()), detection_id=det.id,
                title=det.title(event), severity=det.severity(event),
                dedup_string=dedup_str, context=det.alert_context(event),
                first_event_time=event.get(source.event_time_field, ""),
            )
            if not self.state.register_alert(det.id, dedup_str, alert.alert_id,
                                             det.dedup_period_seconds):
                # Another worker won the atomic claim; this match is theirs.
                signal["p_alert_id"] = self.state.alert_exists(det.id, dedup_str)
                continue
            if not self.state.storm_ok(det.id, hour, self.cfg.storm_limit_per_hour):
                log.error("storm limit hit for %s (>%s alerts in hour %s); alert dropped, "
                          "signal retained", det.id, self.cfg.storm_limit_per_hour, hour)
                continue
            signal["p_alert_id"] = alert.alert_id
            self.records.add_alert(alert)

        self.records.flush()                   # one write-back per invocation


def _unwrap(parsed, envelope_field: str) -> list:
    """"A message" -> "the log records inside it". Three shapes, all common:

        {"records": [ {...}, ... ]}     an envelope - what Azure diagnostic
                                        settings emit to Event Hubs
        [ {...}, {...} ]                a JSON array of records
        {...}                           one record, the simple case
    """
    if isinstance(parsed, list):
        return parsed
    if envelope_field and isinstance(parsed, dict):
        inner = parsed.get(envelope_field)
        if isinstance(inner, list):
            return inner
    return [parsed]
