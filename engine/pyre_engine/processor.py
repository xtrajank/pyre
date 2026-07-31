"""The core batch processing loop - the equivalent of Panther's streaming
detection processor. Reused unchanged by the future scheduled-query module
(which just feeds it query-result rows instead of Event Hub events).

Order per event mirrors Panther exactly:
  idempotency check -> enrich -> select detections for log_type -> rule() ->
  on match: ALWAYS write signal -> dedup/threshold/unique -> storm-limit ->
  alert+dispatch.

Everything is batched and state ops are pipelined for cost at scale.

Nothing in here knows which state backend or record sink is in use - both are
chosen once in backends/ from config. That is what makes the POC and production
run identical detection logic.
"""
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from .backends import build_record_sink, build_state_store
from .config import RuntimeConfig
from .registry import BundleLoader
from .dac import source_from_config
from .enrichment import Enricher
from .dispatch import Dispatcher
from .event import Event
from .signals import RecordWriter
from .state import StateStore
from .models import Signal, Alert

log = logging.getLogger("pyre.processor")

def _load_enabled_ids() -> set[str] | None:
    # In prod, read the enabled-set from Azure App Configuration (short cache).
    # Here: optional env override; None means "trust the YAML Enabled flag".
    raw = os.environ.get("ENABLED_DETECTION_IDS")
    return set(raw.split(",")) if raw else None


def _content_hash(raw: str) -> str:
    # Fallback idempotency key when the caller has no transport-level event id
    # (e.g. local/testlab runs). Event Hubs at-least-once redelivery resends the
    # exact same bytes, so a content hash catches that case too; the production
    # path (function_app.py) passes the real partition+sequence_number instead,
    # which also distinguishes two genuinely different events with identical bodies.
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Processor:
    def __init__(self, cfg: RuntimeConfig, state: StateStore | None = None, sink=None):
        self.cfg = cfg
        # State and sink are the ONLY environment-dependent pieces, and both are
        # chosen in backends/ from config alone. They're injectable here purely so
        # a test or local run can substitute a fake; nothing below this line
        # behaves differently depending on which backend it got.
        self.state = state or build_state_store(cfg)
        # Detections come from the external DaC repo via a hot-reloading bundle,
        # not a static local dir. `loader.get()` returns a fresh Registry within
        # refresh_interval_seconds of a push, without per-event cost.
        self.loader = BundleLoader(
            source_from_config(cfg.dac),
            refresh_interval_seconds=cfg.dac.refresh_interval_seconds,
            enabled_provider=_load_enabled_ids,
        )
        self.enricher = Enricher(self.state)
        self.records = RecordWriter(sink or build_record_sink(cfg))
        self.dispatcher = Dispatcher(cfg.destinations_path)

    def process_batch(self, raw_events: list[str], event_ids: list[str] | None = None) -> None:
        registry = self.loader.get()          # hot-reloads on a DaC push / enable flip
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        # No universal metadata-field prefix (Cribl doesn't use Panther's `p_`
        # convention, and this may not even be Cribl - see cfg.log_type_field/
        # event_time_field), so these two configured routing/time fields are
        # captured explicitly on every signal; anything still prefixed `p_`
        # (e.g. p_enrichment) is swept up alongside them below.
        log_metadata_keys = (self.cfg.log_type_field, self.cfg.event_time_field)

        # Phase 0: parse, and expand any envelope. One transport message can carry
        # many log records (see cfg.event_envelope_field), so this is where "a
        # message" becomes "the events in it" - everything downstream deals only
        # in individual records.
        candidates = []  # (event_id, record dict)
        for idx, raw in enumerate(raw_events):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("skipping message %d: not valid JSON", idx)
                continue
            base = None
            if event_ids is not None and idx < len(event_ids):
                base = event_ids[idx]
            base = base or _content_hash(raw)
            for n, record in enumerate(self._unwrap(parsed)):
                if not isinstance(record, dict):
                    continue
                # The id is per RECORD, not per message, so a redelivered message
                # suppresses each of its records individually. The suffix is
                # positional and therefore stable across a redelivery.
                candidates.append((f"{base}#{n}", record))

        # Phase 1: idempotency. Event Hubs is at-least-once, so a checkpoint retry
        # can redeliver a batch we already processed. One extra pipelined round-trip
        # per BATCH (not per event) marks each event id "seen"; anything already
        # seen within the window is skipped below so signals/dedup counters can't
        # be double-written by a redelivery.
        id_pipe = self.state.pipeline()
        for eid, _record in candidates:
            self.state.is_new_event(id_pipe, eid)
        seen_results = id_pipe.execute()

        pipe = self.state.pipeline()
        pending = []  # matches needing an alert decision after the pipeline runs
        # Per-batch routing tally. Cheap (one dict, not one Redis call) and it is
        # the difference between "no alerts, no idea why" and a log line naming the
        # exact log-type values that arrived with no detections behind them.
        no_log_type = 0
        unrouted: dict[str, int] = {}

        for (eid, record), is_new in zip(candidates, seen_results):
            if not is_new:
                log.info("skipping already-processed event %s (redelivery)", eid)
                continue
            event = Event(record)
            log_type = event.get(self.cfg.log_type_field)
            if not log_type:
                no_log_type += 1
                continue
            if not registry.for_log_type(log_type):
                unrouted[log_type] = unrouted.get(log_type, 0) + 1
                continue
            event = self.enricher.enrich(event)

            for det in registry.for_log_type(log_type):
                try:
                    if not det.rule(event):
                        continue
                except Exception:
                    log.exception("detection %s raised while evaluating a %s event; skipping it for this event",
                                  det.id, log_type)
                    continue

                dedup_str = (det.dedup(event) or det.title(event))[:1000]
                # ALWAYS a signal on match. Keep the record: whether this match
                # ends up inside an alert isn't known until the thresholds below
                # are evaluated, and it hasn't been flushed yet, so p_alert_id is
                # filled in retrospectively.
                signal_rec = self.records.add_signal(Signal(
                    detection_id=det.id, log_type=log_type, dedup_string=dedup_str,
                    event_time=event.get(self.cfg.event_time_field, ""), event_ref=event,
                    p_fields={
                        **{k: event[k] for k in log_metadata_keys if k in event},
                        **{k: v for k, v in event.items() if k.startswith("p_")},
                    },
                ))
                if not det.create_alert:
                    continue

                # A detection may define unique(event) (Panther's "N distinct
                # values" thresholding, e.g. 5+ different source IPs) instead of
                # counting every match. Its presence picks the counting mode.
                unique_val = det.unique(event)
                if unique_val is not None:
                    self.state.bump_unique(pipe, det.id, dedup_str, str(unique_val), det.dedup_period_seconds)
                    pending.append((det, event, dedup_str, "unique", signal_rec))
                else:
                    self.state.bump_dedup(pipe, det.id, dedup_str, det.dedup_period_seconds)
                    pending.append((det, event, dedup_str, "count", signal_rec))

        if no_log_type:
            log.warning("%d event(s) in this batch had no value in the configured "
                        "log-type field '%s' - check LOG_TYPE_FIELD against your data",
                        no_log_type, self.cfg.log_type_field)
        if unrouted:
            log.warning("no detections are registered for these log-type values: %s. "
                        "A detection's YAML LogTypes must contain the value exactly.",
                        ", ".join(f"{lt} ({n} event(s))" for lt, n in sorted(unrouted.items())))

        results = pipe.execute()  # one round-trip for all dedup/unique bumps

        # Interpret pipeline results and decide alerts. "count" mode pipelines
        # [incr, expire] (want the incr result); "unique" mode pipelines
        # [pfadd, expire, pfcount] (want the pfcount result).
        i = 0
        for det, event, dedup_str, mode, signal_rec in pending:
            if mode == "unique":
                count = results[i + 2]; i += 3
            else:
                count = results[i]; i += 2
            if count < det.threshold:
                continue                # below threshold: signal stands alone
            existing = self.state.alert_exists(det.id, dedup_str)
            if existing:
                # Within the window -> this match is grouped into the alert that
                # is already open. Recording that id is what makes the signals
                # stream answer "which matches made up this alert?".
                signal_rec["p_alert_id"] = existing
                continue
            alert = Alert(
                alert_id=str(uuid.uuid4()), detection_id=det.id,
                title=det.title(event), severity=det.severity(event),
                dedup_string=dedup_str, context=det.alert_context(event),
                first_event_time=event.get(self.cfg.event_time_field, ""),
            )
            # atomic claim of the alert marker (first-event-wins)
            if not self.state.register_alert(det.id, dedup_str, alert.alert_id, det.dedup_period_seconds):
                # Another worker won the race; this match belongs to their alert.
                signal_rec["p_alert_id"] = self.state.alert_exists(det.id, dedup_str)
                continue
            if not self.state.storm_ok(det.id, hour, self.cfg.storm_limit_per_hour):
                # storm limit hit: keep the signal, skip the alert, surface it loudly
                log.error("storm limit hit for detection %s (>%s alerts in hour %s); alert dropped, signal retained",
                          det.id, self.cfg.storm_limit_per_hour, hour)
                continue
            routes = det.destinations(event) or self.cfg.default_routes
            alert.destinations = routes
            signal_rec["p_alert_id"] = alert.alert_id
            self.records.add_alert(alert)
            self.dispatcher.send(alert, routes)

        self.records.flush()  # one batched write-back per invocation

    def _unwrap(self, parsed) -> list:
        """"A message" -> "the log records inside it".

        Three shapes, all common:
          [ {...}, {...} ]                a JSON array of records
          {"records": [ {...}, ... ]}     an envelope - what Azure diagnostic
                                          settings emit to Event Hubs
          {...}                           one record, the simple case
        """
        if isinstance(parsed, list):
            return parsed
        field = self.cfg.event_envelope_field
        if field and isinstance(parsed, dict):
            inner = parsed.get(field)
            if isinstance(inner, list):
                return inner
        return [parsed]
