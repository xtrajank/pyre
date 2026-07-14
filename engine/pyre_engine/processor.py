"""The core batch processing loop - the equivalent of Panther's streaming
detection processor. Reused unchanged by the future scheduled-query module
(which just feeds it query-result rows instead of Event Hub events).

Order per event mirrors Panther exactly:
  enrich -> select detections for log_type -> rule() -> on match:
  ALWAYS write signal -> dedup/threshold/unique -> storm-limit -> alert+dispatch.

Everything is batched and Redis ops are pipelined for cost at scale.
"""
import json
import os
import uuid
from datetime import datetime, timezone

from .config import RuntimeConfig
from .registry import BundleLoader
from .dac import source_from_config
from .dedup import StateStore
from .enrichment import Enricher
from .dispatch import Dispatcher
from .signals import SignalWriter
from .models import Signal, Alert


def _load_enabled_ids() -> set[str] | None:
    # In prod, read the enabled-set from Azure App Configuration (short cache).
    # Here: optional env override; None means "trust the YAML Enabled flag".
    raw = os.environ.get("ENABLED_DETECTION_IDS")
    return set(raw.split(",")) if raw else None


class Processor:
    def __init__(self, cfg: RuntimeConfig, state: StateStore | None = None):
        self.cfg = cfg
        # `state` is injectable so a local run/test can pass a fakeredis-backed
        # StateStore; in Azure it defaults to the real (Entra/TLS) Redis.
        self.state = state or StateStore(cfg.redis_host, cfg.redis_port, cfg.redis_use_entra)
        # Detections come from the external DaC repo via a hot-reloading bundle,
        # not a static local dir. `loader.get()` returns a fresh Registry within
        # refresh_interval_seconds of a push, without per-event cost.
        self.loader = BundleLoader(
            source_from_config(cfg.dac),
            refresh_interval_seconds=cfg.dac.refresh_interval_seconds,
            enabled_provider=_load_enabled_ids,
        )
        self.enricher = Enricher(self.state)
        self.dispatcher = Dispatcher(cfg.destinations_path)
        self.signals = SignalWriter(cfg.signals_sink_url)

    def process_batch(self, raw_events: list[str]) -> None:
        registry = self.loader.get()          # hot-reloads on a DaC push / enable flip
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        pipe = self.state.pipeline()
        pending = []  # matches needing an alert decision after the pipeline runs

        for raw in raw_events:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            log_type = event.get("p_log_type")
            if not log_type:
                continue
            event = self.enricher.enrich(event)

            for det in registry.for_log_type(log_type):
                try:
                    if not det.rule(event):
                        continue
                except Exception:
                    # detection error: log to Log Analytics; keep processing others
                    continue

                dedup_str = (det.dedup(event) or det.title(event))[:1000]
                # ALWAYS a signal on match
                self.signals.add_signal(Signal(
                    detection_id=det.id, log_type=log_type, dedup_string=dedup_str,
                    event_time=event.get("p_event_time", ""), event_ref=event,
                    p_fields={k: v for k, v in event.items() if k.startswith("p_")},
                ))
                if not det.create_alert:
                    continue

                self.state.bump_dedup(pipe, det.id, dedup_str, det.dedup_period_seconds)
                pending.append((det, event, dedup_str))

        results = pipe.execute()  # one round-trip for all dedup bumps

        # Interpret pipeline results (incr, expire pairs) and decide alerts.
        i = 0
        for det, event, dedup_str in pending:
            count = results[i]; i += 2  # skip the paired expire result
            if count < det.threshold:
                continue
            if self.state.alert_exists(det.id, dedup_str):
                continue  # within window -> event grouped, no new alert
            alert = Alert(
                alert_id=str(uuid.uuid4()), detection_id=det.id,
                title=det.title(event), severity=det.severity(event),
                dedup_string=dedup_str, context=det.alert_context(event),
                first_event_time=event.get("p_event_time", ""),
            )
            # atomic claim of the alert marker (first-event-wins)
            if not self.state.register_alert(det.id, dedup_str, alert.alert_id, det.dedup_period_seconds):
                continue
            if not self.state.storm_ok(det.id, hour, self.cfg.storm_limit_per_hour):
                # storm limit hit: keep the signal, skip dispatch, raise a system error
                continue
            routes = det.destinations(event) or _default_routes(self.cfg.env)
            alert.destinations = routes
            self.signals.add_alert(alert)
            self.dispatcher.send(alert, routes)

        self.signals.flush()  # batched write-back to Cribl


def _default_routes(env: str) -> list[str]:
    return ["mock"] if env == "dev" else ["torq_prod"]
