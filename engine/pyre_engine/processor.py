"""The core batch processing loop - the equivalent of Panther's streaming
detection processor. Reused unchanged by the future scheduled-query module
(which just feeds it query-result rows instead of Event Hub events).

Order per event mirrors Panther exactly:
  idempotency check -> enrich -> select detections for log_type -> rule() ->
  on match: ALWAYS write signal -> dedup/threshold/unique -> storm-limit ->
  alert+dispatch.

Everything is batched and Redis ops are pipelined for cost at scale.
"""
import collections
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from .config import RuntimeConfig, Shape
from .registry import BundleLoader
from .dac import source_from_config
from .dedup import StateStore
from .enrichment import Enricher
from .dispatch import Dispatcher
from .event import Event
from .signals import SignalWriter
from .models import Signal, Alert

log = logging.getLogger("pyre.processor")


def _p_fields(event, metadata_keys) -> dict:
    # The configured routing/time fields plus anything already prefixed `p_`
    # (e.g. p_enrichment). Shared by the signal and the alert so both carry the
    # same context.
    return {
        **{k: event[k] for k in metadata_keys if k in event},
        **{k: v for k, v in event.items() if k.startswith("p_")},
    }


def _content_hash(raw: str) -> str:
    # Fallback idempotency key when the caller has no transport-level event id
    # (e.g. local/testlab runs). Event Hubs at-least-once redelivery resends the
    # exact same bytes, so a content hash catches that case too; the production
    # path (function_app.py) passes the real partition+sequence_number instead,
    # which also distinguishes two genuinely different events with identical bodies.
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Processor:
    def __init__(self, cfg: RuntimeConfig, state: StateStore | None = None):
        self.cfg = cfg
        # `state` is injectable so a local run/test can pass a fakeredis-backed
        # StateStore; in Azure it defaults to the real (Entra/TLS) Redis.
        self.state = state or StateStore(cfg.redis_host, cfg.redis_port, cfg.redis_use_entra)
        # Detections come from the external DaC repo via a hot-reloading bundle,
        # not a static local dir. `loader.get()` returns a fresh Registry within
        # refresh_interval_seconds of a push, without per-event cost.
        # A detection's DaC `Enabled: false` flag is the sole disable mechanism:
        # from_bundle drops it, so it is never indexed, imported, or evaluated.
        # A republish changes the bundle version and reloads within the interval.
        self.loader = BundleLoader(
            source_from_config(cfg.dac),
            refresh_interval_seconds=cfg.dac.refresh_interval_seconds,
        )
        self.enricher = Enricher(self.state)
        self.dispatcher = Dispatcher(cfg.destinations_path)
        self.signals = SignalWriter(cfg.signals_sink_url)
        if not cfg.default_routes:
            # Only detections that name their own destinations() would be
            # deliverable. Every other alert would fail dispatch - loudly, but
            # once per alert forever. Say it once per worker instead, at the
            # point where it is still a config fix. Terraform sets this from
            # var.default_routes.
            log.warning(
                "DEFAULT_ROUTES is empty: any alert whose detection does not name its "
                "own destinations() will NOT be delivered. Set default_routes "
                "(Terraform) to destination name(s) from %s.", cfg.destinations_path)

    def process_batch(self, raw_events: list[str], event_ids: list[str] | None = None,
                      shape: Shape | None = None) -> None:
        """Run one Event Hubs batch end to end.

        `shape` is how to read THIS batch's hub (log-type/time fields, envelope).
        The host passes the hub's shape so one worker can serve feeds of different
        shapes; None uses the instance default (cfg.default_shape).

        Failure model. Event Hubs delivers at-least-once and neither Redis nor
        Cribl can enlist in a transaction with it, so there is no exactly-once to
        be had: the only real choice is which way to fail. This fails toward
        DUPLICATES, never toward loss, because a duplicate signal is noise while a
        lost one is a match that provably happened and that no one can ever see.

        Concretely: phase 0 CLAIMS every event id; if any later phase raises, the
        claims are released so the redelivery does real work instead of reading
        as "already processed" and skipping the batch clean.

        What a redelivery then costs, honestly:
          * signals may duplicate (same match written twice);
          * dedup/threshold counters get bumped again, so a threshold can fire
            slightly early;
          * alerts do NOT duplicate - register_alert's SET NX marker survives the
            retry and still says first-event-wins.
        See StateStore.release_events.
        """
        claimed: list[str] = []
        try:
            self._process_batch(raw_events, event_ids, claimed, shape or self.cfg.default_shape)
        except Exception:
            # Best-effort: if this cleanup itself fails we still re-raise the
            # original error, and the events stay claimed until their (short) TTL
            # lapses. A worker killed outright can't run this at all - which is
            # why the idempotency TTL is minutes, not an hour.
            try:
                self.state.release_events(claimed)
            except Exception:
                log.exception("could not release %d idempotency claims after a failed batch; "
                              "those events will be skipped if redelivered within their TTL",
                              len(claimed))
            raise

    def _process_batch(self, raw_events: list[str], event_ids: list[str] | None,
                       claimed: list[str], shape: Shape) -> None:
        registry = self.loader.get()          # hot-reloads on a DaC push
        lt_field = shape.log_type_field
        et_field = shape.event_time_field
        envelope = shape.envelope
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        # Per-batch tallies instead of per-event log lines. At 4-5TB/day a single
        # log.info per event is ~50k App Insights records/sec - a bill and an
        # ingestion bottleneck that scale with traffic rather than with problems.
        # Counting and emitting once per batch keeps observability O(batches).
        stats = collections.Counter()
        det_errors: dict[str, int] = {}
        # No universal metadata-field prefix (Cribl doesn't use Panther's `p_`
        # convention, and this may not even be Cribl - see cfg.log_type_field/
        # event_time_field), so these two configured routing/time fields are
        # captured explicitly on every signal; anything still prefixed `p_`
        # (e.g. p_enrichment) is swept up alongside them below.
        log_metadata_keys = (lt_field, et_field)

        # Phase 0: idempotency. Event Hubs is at-least-once, so a checkpoint retry
        # can redeliver a batch we already processed. One extra pipelined round-trip
        # per BATCH (not per event) claims each event id; anything already claimed
        # within the window is skipped below so signals/dedup counters can't be
        # double-written by a redelivery.
        id_pipe = self.state.pipeline()
        candidates = []  # (event_id, raw:str|None, record:dict|None)
        for idx, raw in enumerate(raw_events):
            eid = None
            if event_ids is not None and idx < len(event_ids):
                eid = event_ids[idx]
            eid = eid or _content_hash(raw)

            if not envelope:
                # The common path. Parsing stays LAZY (below, after the claim) so
                # a redelivered event is never parsed at all.
                candidates.append((eid, raw, None))
                self.state.is_new_event(id_pipe, eid, self.cfg.idempotency_ttl_seconds)
                continue

            # One transport message carrying MANY records (Azure Monitor's
            # {"records":[...]}). Expand before claiming, so each record gets its
            # own idempotency key and its own signal: claiming the envelope as
            # one id would make a redelivery skip every record inside it.
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                stats["malformed_json"] += 1
                continue
            records = payload.get(envelope) if isinstance(payload, dict) else None
            if not isinstance(records, list):
                # The envelope key is configured but absent/not an array: the feed
                # is not the shape it was declared to be, so every event in it is
                # being dropped. Count it rather than let a whole source vanish.
                stats["bad_envelope"] += 1
                continue
            for i, rec in enumerate(records):
                # "#i" scopes the id WITHIN the message, so redelivery of the same
                # message re-derives exactly the same per-record ids.
                candidates.append((f"{eid}#{i}", None, rec))
                self.state.is_new_event(id_pipe, f"{eid}#{i}", self.cfg.idempotency_ttl_seconds)
        seen_results = id_pipe.execute()

        pipe = self.state.pipeline()
        pending = []  # matches needing an alert decision after the pipeline runs

        for (eid, raw, rec), is_new in zip(candidates, seen_results):
            if not is_new:
                stats["redelivered_skipped"] += 1
                continue
            # Only ids WE claimed are ours to release if this batch fails.
            claimed.append(eid)
            if rec is None:
                try:
                    event = Event(json.loads(raw))
                except json.JSONDecodeError:
                    stats["malformed_json"] += 1
                    continue
            else:
                event = Event(rec)          # already parsed out of the envelope
            log_type = event.get(lt_field)
            if not log_type:
                stats["no_log_type"] += 1
                continue
            # Resolved ONCE. This used to be called twice per event - once to
            # test emptiness for the counter, once to iterate - so every event
            # in every batch paid the lookup twice for one answer.
            detections = registry.for_log_type(log_type)
            if not detections:
                # Nothing can match, so skip the rest of the per-event work
                # instead of enriching an event no rule will ever read. Today
                # enrich() is a stub and this is just tidy; once it does real
                # LUT lookups (its docstring's plan is Redis), this is the
                # difference between an uncovered feed costing a counter bump
                # and it costing a Redis round-trip per event.
                stats["no_detections_for_log_type"] += 1
                continue
            event = self.enricher.enrich(event)

            for det in detections:
                try:
                    if not det.rule(event):
                        continue
                except Exception:
                    # Tallied, not logged per event: a detection that raises on
                    # every event would otherwise emit one stack trace per event
                    # per batch - a self-inflicted log storm precisely when the
                    # system is already unhealthy. One line per detection per
                    # batch below, with the count.
                    det_errors[det.id] = det_errors.get(det.id, 0) + 1
                    continue

                try:
                    # str(): dedup/title are detection-authored and may return a
                    # non-string (an int id, say). Slicing that raises TypeError,
                    # which - before this - escaped the batch and killed it.
                    dedup_str = str(det.dedup(event) or det.title(event))[:1000]
                    # A detection may define unique(event) (Panther's "N distinct
                    # values" thresholding, e.g. 5+ different source IPs) instead
                    # of counting every match. Its presence picks the counting mode.
                    unique_val = det.unique(event)
                except Exception:
                    det_errors[det.id] = det_errors.get(det.id, 0) + 1
                    continue

                # ALWAYS a signal on match
                self.signals.add_signal(Signal(
                    detection_id=det.id, log_type=log_type, dedup_string=dedup_str,
                    event_time=event.get(et_field, ""), event_ref=event,
                    p_fields=_p_fields(event, log_metadata_keys),
                ))
                stats["signals"] += 1
                if not det.create_alert:
                    continue

                if unique_val is not None:
                    self.state.bump_unique(pipe, det.id, dedup_str, str(unique_val), det.dedup_period_seconds)
                else:
                    self.state.bump_dedup(pipe, det.id, dedup_str, det.dedup_period_seconds)
                pending.append((det, event, dedup_str))

        results = pipe.execute()  # one round-trip for all dedup/unique bumps

        # Both counting modes are a single scripted command that RETURNS the
        # count, so results line up 1:1 with `pending`. This used to be index
        # arithmetic that hard-coded how many commands each bump queued (2 for
        # count, 3 for unique) - the processor silently depended on dedup.py's
        # internals, and adding a command there would have misread every count
        # from that point on with no error.
        for (det, event, dedup_str), count in zip(pending, results):
            if count < det.threshold:
                continue
            try:
                alert = Alert(
                    alert_id=str(uuid.uuid4()), detection_id=det.id,
                    title=det.title(event), severity=det.severity(event),
                    dedup_string=dedup_str, context=det.alert_context(event),
                    first_event_time=event.get(et_field, ""), event_count=count,
                    event=dict(event), p_fields=_p_fields(event, log_metadata_keys),
                )
                routes = det.destinations(event) or self.cfg.default_routes
            except Exception:
                # Same containment as the rule() path: title/severity/
                # alert_context/destinations are all detection-authored, and an
                # exception here would take down a batch that other detections
                # matched in too.
                det_errors[det.id] = det_errors.get(det.id, 0) + 1
                continue
            # Atomic claim of the alert marker (first-event-wins). This SET NX is
            # itself the "already alerted in this window?" test, so there's no
            # preceding existence check: that read was a second round-trip per
            # grouped match - the COMMON case under dedup, where many matches
            # share one alert - and being a check-then-act, it couldn't be relied
            # on for correctness anyway. Building the Alert we may discard costs
            # microseconds; the round-trip it replaced cost ~1ms.
            if not self.state.register_alert(det.id, dedup_str, alert.alert_id, det.dedup_period_seconds):
                continue  # within window -> event grouped, no new alert
            if not self.state.storm_ok(det.id, hour, self.cfg.storm_limit_per_hour):
                # storm limit hit: keep the signal, skip dispatch, surface it loudly
                log.error("storm limit hit for detection %s (>%s alerts in hour %s); alert dropped, signal retained",
                          det.id, self.cfg.storm_limit_per_hour, hour)
                stats["storm_suppressed"] += 1
                continue
            alert.destinations = routes
            self.signals.add_alert(alert)
            try:
                self.dispatcher.send(alert, routes)
            except Exception:
                # Delivery failed. Re-open the alert window so the next matching
                # event fires it again, instead of the marker suppressing every
                # future match under an alert nobody ever received.
                #
                # Deliberately NOT re-raised: failing the batch here would throw
                # away the SIGNALS too - the durable audit that these matches
                # happened - to retry a destination that is probably still down.
                # The signal is the record of record; delivery is best-effort and
                # self-healing. dispatcher.send has already logged the detail.
                stats["dispatch_failed"] += 1
                try:
                    self.state.release_alert(det.id, dedup_str)
                except Exception:
                    log.exception("alert %s undelivered AND its window could not be re-opened; "
                                  "this detection will not re-alert for %ss",
                                  alert.alert_id, det.dedup_period_seconds)
                continue
            stats["alerts"] += 1

        self.signals.flush()  # batched write-back to Cribl
        _report(stats, det_errors, len(raw_events), envelope)




def _report(stats, det_errors: dict[str, int], batch_size: int, envelope: str = "") -> None:
    """One log record per batch, not per event.

    Everything here was previously either a per-event log line (a ~50k/sec App
    Insights firehose at target volume, costing most when the system is least
    healthy) or - worse - nothing at all. Malformed JSON, an event with no log
    type, and an event whose log type no detection covers were all a bare
    `continue`: at 4-5TB/day you could silently drop a whole feed and the only
    evidence would be an alert that never fired. These are the numbers that say
    "we are dropping 5% of this source", so they must be countable.
    """
    dropped = stats["malformed_json"] + stats["no_log_type"] + stats["bad_envelope"]
    if dropped:
        # WARNING, not INFO: a non-zero value here means events entered the
        # pipeline and produced nothing - always worth someone's attention.
        log.warning("batch dropped %d/%d event(s): %d malformed json, %d missing the "
                    "'log type' field, %d with no '%s' envelope array", dropped, batch_size,
                    stats["malformed_json"], stats["no_log_type"], stats["bad_envelope"],
                    envelope or "-")
    if stats["bad_envelope"]:
        # ENVELOPE is configured but these messages aren't that shape, so EVERY
        # record inside them was dropped. Almost always a wrong-instance mistake:
        # a Cribl-shaped feed pointed at an Azure-native instance, or vice versa.
        log.error("batch had %d message(s) with no '%s' array: this instance is "
                  "configured to unwrap an envelope these events do not have. Check "
                  "ENVELOPE matches the feed this instance reads.",
                  stats["bad_envelope"], envelope)
    if stats["redelivered_skipped"]:
        log.info("batch skipped %d/%d already-processed event(s) (Event Hubs redelivery)",
                 stats["redelivered_skipped"], batch_size)
    if stats["no_detections_for_log_type"]:
        # Not necessarily wrong (a source can legitimately have no detections
        # yet), but it's how a log-type rename silently disables coverage.
        log.info("batch had %d event(s) whose log type matches no enabled detection",
                 stats["no_detections_for_log_type"])
    for det_id, n in det_errors.items():
        log.error("detection %s raised on %d event(s) in this batch and was skipped for them",
                  det_id, n)
    if stats["dispatch_failed"]:
        log.error("batch failed to deliver %d alert(s); their windows were re-opened so a "
                  "later match re-fires them", stats["dispatch_failed"])
    if stats["signals"] or stats["alerts"]:
        log.info("batch produced %d signal(s), %d alert(s), %d storm-suppressed, %d undelivered",
                 stats["signals"], stats["alerts"], stats["storm_suppressed"],
                 stats["dispatch_failed"])
