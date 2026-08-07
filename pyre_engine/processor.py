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

LOGGING is one INFO line per invocation, whatever the volume:

    batch platform/applog msgs=3 events=7 new=7 signals=4 alerts=1 12ms

Anything that would otherwise be per-event - a redelivery, a detection raising,
a log type with no detections behind it - is counted during the batch and
reported once, with the values you would need to fix it. That is deliberate: the
per-event version of these lines is loudest exactly when something is wrong and
the logs are least readable. See docs/operations.md.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from .bundle import source_from_config
from .config import RuntimeConfig, Source
from .event import Event
from .records import RecordWriter, build_alert, build_signal, normalize_indicators, now_iso
from .registry import BundleLoader
from .sinks import build_router
from .state import build_state_store

log = logging.getLogger("pyre.processor")


class Processor:
    def __init__(self, cfg: RuntimeConfig, state=None, sink=None):
        self.cfg = cfg
        # State and sink are the only environment-dependent pieces, and both are
        # chosen from config alone. They're injectable purely so a test or local
        # run can substitute a fake; nothing below behaves differently for it.
        self.state = state or build_state_store(cfg)
        self.sink = sink or build_router(cfg)
        self.loader = BundleLoader(source_from_config(cfg), cfg.detections_refresh_seconds)

    def destinations(self) -> dict:
        """Where each stream actually resolved, redacted, for /health. An
        injected test sink has no such notion, hence the fallback."""
        describe = getattr(self.sink, "describe", None)
        return describe() if callable(describe) else {"signal": "injected", "alert": "injected"}

    def process_batch(self, messages: list[str], source: Source,
                      event_ids: list[str] | None = None) -> None:
        """One Event Hub invocation's worth of messages, from one source.

        `event_ids` are the transport ids (partition + sequence number), which
        stay stable across an Event Hubs redelivery and are what the redelivery
        guard keys on. Omitted (the `ingest` endpoint, local runs), the message
        body is hashed instead.
        """
        started = time.monotonic()
        # Local to this call, not a Processor attribute: the Functions host runs
        # several invocations concurrently on one worker (different partitions,
        # same process), and a buffer shared across them would let one
        # invocation's flush() clear records another one just added but hadn't
        # written yet - silent loss, worse the more concurrency there is.
        records = RecordWriter(self.sink)
        registry = self.loader.get()          # hot-reloads on a detection publish
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        processed_time = now_iso()

        # Per-batch tallies. Every one of these would otherwise be a per-event
        # log line; counted here, they cost nothing and become one readable
        # summary at the end.
        unparseable = 0
        redelivered = 0
        no_log_type = 0
        unrouted: dict[str, int] = {}
        det_errors: dict[str, int] = {}
        storm_dropped: dict[str, int] = {}
        n_signals = n_alerts = 0

        # --- phase 0: messages -> records ----------------------------------
        # One transport message can carry many log records. This is where "a
        # message" becomes "the events in it"; everything below deals only in
        # individual records.
        candidates = []                        # (event_id, record)
        for idx, raw in enumerate(messages):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                unparseable += 1
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

        for (eid, record), is_new in zip(candidates, first_sighting):
            if not is_new:
                redelivered += 1
                continue
            event = Event(record)
            log_type = event.get(source.log_type_field) if source.log_type_field else source.hub
            if not log_type:
                no_log_type += 1
                continue
            detections = registry.for_log_type(log_type)
            if not detections:
                unrouted[log_type] = unrouted.get(log_type, 0) + 1
                continue

            for det in detections:
                if not det.enabled:
                    continue
                try:
                    if not det.rule(event):
                        continue
                except Exception:
                    # One traceback per detection per batch, then a count. A
                    # detection that raises on every event would otherwise emit
                    # a full stack trace per event, unthrottled.
                    if det.id not in det_errors:
                        log.exception("detection %s raised on a %s event; skipping it for "
                                      "this event", det.id, log_type)
                    det_errors[det.id] = det_errors.get(det.id, 0) + 1
                    continue

                dedup_str = (det.dedup(event) or det.title(event))[:1000]
                indicators = normalize_indicators(det.indicators(event))
                # ALWAYS a signal on match. Whether it ends up inside an alert
                # isn't known until the thresholds below are evaluated, and
                # nothing is flushed yet, so p_alert_id is filled in later.
                signal = records.add(build_signal(
                    det, source, event, log_type, dedup_str, indicators, processed_time))
                n_signals += 1
                if not det.create_alert:
                    continue

                # A detection defining unique() counts DISTINCT values (5 different
                # source IPs) instead of every match. Its presence picks the mode.
                unique_val = det.unique(event)
                if unique_val is not None:
                    self.state.bump_unique(pipe, det.id, dedup_str, str(unique_val),
                                           det.dedup_period_seconds)
                    mode = "unique"
                else:
                    self.state.bump_dedup(pipe, det.id, dedup_str, det.dedup_period_seconds)
                    mode = "count"
                pending.append((det, event, log_type, dedup_str, mode, signal, indicators))

        results = pipe.execute()               # one round-trip for every dedup/unique bump

        # --- phase 3: threshold, dedup, alert -------------------------------
        # "count" mode pipelined [incr, expire]; "unique" pipelined
        # [pfadd, expire, pfcount]. Read the counts back by position.
        i = 0
        for det, event, log_type, dedup_str, mode, signal, indicators in pending:
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
            # The storm check comes BEFORE the claim. Claiming first would leave
            # an `alert:` marker behind for a storm-dropped alert, and every
            # later match in the window would then be stamped with an alert id
            # that was never written anywhere.
            if not self.state.storm_ok(det.id, hour, self.cfg.alert_storm_limit_per_hour):
                storm_dropped[det.id] = storm_dropped.get(det.id, 0) + 1
                continue
            alert = build_alert(det, source, event, log_type, dedup_str, indicators,
                                signal["p_signal_id"], count, processed_time)
            if not self.state.register_alert(det.id, dedup_str, alert["p_alert_id"],
                                             det.dedup_period_seconds):
                # Another worker won the atomic claim; this match is theirs.
                signal["p_alert_id"] = self.state.alert_exists(det.id, dedup_str)
                continue
            signal["p_alert_id"] = alert["p_alert_id"]
            records.add(alert)
            n_alerts += 1

        records.flush()                        # one write-back per invocation

        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("batch %s msgs=%d events=%d new=%d signals=%d alerts=%d %dms",
                 source.id, len(messages), len(candidates),
                 len(candidates) - redelivered, n_signals, n_alerts, elapsed_ms)
        self._log_anomalies(source, unparseable, redelivered, no_log_type, unrouted,
                            det_errors, storm_dropped)

    def _log_anomalies(self, source, unparseable, redelivered, no_log_type, unrouted,
                       det_errors, storm_dropped) -> None:
        """One line per KIND of problem in this batch, naming the values needed
        to fix it. Silent when the batch was clean, which is the normal case."""
        if unparseable:
            log.warning("%s: %d message(s) were not valid JSON and were skipped",
                        source.id, unparseable)
        if redelivered:
            log.info("%s: %d event(s) already processed (Event Hubs redelivery); skipped",
                     source.id, redelivered)
        if no_log_type:
            log.warning("%s: %d event(s) had no value in the log-type field %r - check "
                        "log_type_field in config/sources.yaml against your data",
                        source.id, no_log_type, source.log_type_field)
        if unrouted:
            # Informational, not a warning: an instance normally ingests more
            # log-type values than it has detections written for, especially
            # early on, and that is not itself a problem to flag loudly.
            log.info("%s: no detections are registered for these log-type values: %s. "
                     "A detection's YAML LogTypes must contain the value exactly.",
                     source.id,
                     ", ".join(f"{lt} ({n} event(s))" for lt, n in sorted(unrouted.items())))
        if det_errors:
            log.warning("%s: detection(s) raised and were skipped for those events: %s",
                        source.id,
                        ", ".join(f"{d} ({n}x)" for d, n in sorted(det_errors.items())))
        if storm_dropped:
            log.error("%s: alert storm limit (%d/hour) reached; alert(s) dropped, signals "
                      "retained: %s", source.id, self.cfg.alert_storm_limit_per_hour,
                      ", ".join(f"{d} ({n}x)" for d, n in sorted(storm_dropped.items())))


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
