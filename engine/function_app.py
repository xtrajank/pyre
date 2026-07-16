"""PYRE streaming detection processor - Azure Functions entrypoint.

Deliberately thin: ONLY the Event Hubs batch trigger(s). All logic lives in the
reusable `pyre_engine` package, so the same code serves a future scheduled-query
or Container Apps host unchanged.

ONE TRIGGER PER HUB. A hub with no consumer accepts logs at full rate and
evaluates none, with no error anywhere, so the trigger set is DERIVED from the
same config Terraform builds the hubs from (HUBS_CONFIG) rather than written out
by hand. Each hub carries its own namespace CONNECTION and its own SHAPE (which
field names the log type, which the time, and any envelope), so one worker can
consume a Cribl namespace and an Azure-native namespace - different shapes - at
once. Routing to detections is by the event's log-type field alone; the hub an
event arrived on only buys isolation and its own parallelism ceiling.
"""
import json
import logging
import os

import azure.functions as func

from pyre_engine.config import load_runtime_config, Shape
from pyre_engine.processor import Processor

log = logging.getLogger("pyre.host")

app = func.FunctionApp()

# Built once per worker (cold start), reused across every invocation of every
# trigger below. One Processor holds one Registry and one Redis pool, so all
# hubs share a bundle rather than each paying for their own.
_config = load_runtime_config()
_processor = Processor(_config)


def _hub_specs() -> list[dict]:
    """One dict per hub to consume: {hub, connection, log_type_field,
    event_time_field, envelope}.

    HUBS_CONFIG (JSON, set by Terraform from config/sources.yaml) is the
    multi-namespace form. Absent it, fall back to the single-namespace settings
    so an app deployed before this change keeps working until its next apply.
    """
    raw = os.environ.get("HUBS_CONFIG")
    if raw:
        return json.loads(raw)
    names = os.environ.get("EVENTHUB_NAMES") or os.environ.get("EVENTHUB_NAME", "")
    return [{"hub": h.strip(), "connection": "EVENTHUB_CONNECTION",
             "log_type_field": _config.log_type_field,
             "event_time_field": _config.event_time_field,
             "envelope": _config.envelope}
            for h in names.split(",") if h.strip()]


def _register(spec: dict) -> None:
    """Register one batch trigger for one hub.

    A closure per hub, via this factory: defining the function inside the loop
    would let every registration capture the SAME loop variable and bind to the
    last hub - silence on every other hub, not an error. `hub` and `shape` are
    captured per call here.

    The function name must be unique and match [A-Za-z][A-Za-z0-9_]*, so dashes
    in the hub name are translated.
    """
    hub = spec["hub"]
    shape = Shape(
        spec.get("log_type_field") or _config.log_type_field,
        spec.get("event_time_field") or _config.event_time_field,
        spec.get("envelope", ""),
    )
    fn_name = "detect_" + hub.replace("-", "_")

    @app.function_name(name=fn_name)
    @app.event_hub_message_trigger(
        arg_name="events",
        event_hub_name=hub,
        connection=spec.get("connection", "EVENTHUB_CONNECTION"),  # per-namespace MI connection
        cardinality=func.Cardinality.MANY,                          # deliver a BATCH
    )
    def _detect(events: list[func.EventHubEvent]) -> None:
        """One invocation handles a whole batch. Batch size: engine/host.json."""
        raw = [e.get_body().decode("utf-8") for e in events]
        # partition_key + sequence_number is stable across an Event Hubs
        # redelivery, so the idempotency check keys on it (content-hash fallback
        # inside process_batch). Scoped per hub: sequence numbers restart per
        # partition per hub, so two hubs can legitimately share a pair.
        event_ids = [f"{hub}:{e.partition_key}:{e.sequence_number}" for e in events]
        _processor.process_batch(raw, event_ids=event_ids, shape=shape)


_SPECS = _hub_specs()
if not _SPECS:
    # Nothing to bind to means this worker evaluates nothing, forever. Say so at
    # cold start rather than presenting as a healthy app with no traffic.
    log.error("no hubs configured (HUBS_CONFIG/EVENTHUB_NAMES empty): this worker "
              "will consume NOTHING. Terraform sets these from config/sources.yaml.")
for _spec in _SPECS:
    _register(_spec)
log.info("consuming %d hub(s): %s", len(_SPECS),
         ", ".join(s["hub"] for s in _SPECS) or "-")
