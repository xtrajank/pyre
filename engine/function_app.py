"""PYRE streaming detection processor - Azure Functions entrypoint.

This file is deliberately thin. It holds ONLY trigger wiring; all real logic
lives in the reusable `pyre_engine` package so the same code can be reused by
the (future) scheduled-query module and by a Container Apps host without
modification.

Four functions, and no more than four:

  detect            Event Hub batch trigger. THE function - everything else is
                    scaffolding around it.
  health            GET. Which bundle is loaded, how many detections, which log
                    types they cover, which field routes. The first thing to
                    check when a demo produces no alerts.
  ingest            POST a log (or a list of logs) straight into the processor,
                    bypassing Event Hubs. Proves the detection half in isolation
                    when you're debugging which half is broken.
  bundle_published  Event Grid trigger on "blob created" in the detections
                    container: turns the bundle poll into a push so a publish is
                    live in seconds rather than one refresh interval.
"""
import json
import logging

import azure.functions as func

from pyre_engine.config import load_runtime_config
from pyre_engine.processor import Processor

log = logging.getLogger("pyre.host")

app = func.FunctionApp()

# Built once per worker process (cold start), reused across invocations.
_config = load_runtime_config()
_processor = Processor(_config)


@app.function_name(name="detect")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENTHUB_NAME%",          # from app settings, e.g. "logs-in"
    connection="EVENTHUB_CONNECTION",           # connection string or Managed-Identity settings
    cardinality=func.Cardinality.MANY,          # deliver a BATCH, not one event
)
def detect(events: list[func.EventHubEvent]) -> None:
    """One invocation handles a whole batch. Cost lever: batch size in host.json."""
    raw = [e.get_body().decode("utf-8") for e in events]
    # partition_key + sequence_number is stable across an Event Hubs redelivery
    # (a checkpoint retry redelivers the same messages), so it's what the
    # processor's idempotency check keys on. Falls back to a content hash
    # (inside process_batch) if a given event lacks one.
    event_ids = [f"{e.partition_key}:{e.sequence_number}" for e in events]
    _processor.process_batch(raw, event_ids=event_ids)


@app.function_name(name="health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Answers "is the engine actually loaded, and with what?" without sending
    an event. `log_types` is the list a detection's YAML `LogTypes:` must match
    for routing to reach it - compare it against the value your events carry in
    the `log_type_field` below."""
    body = {
        "env": _config.env,
        "state_backend": _config.state_backend,
        "log_type_field": _config.log_type_field,
        "event_time_field": _config.event_time_field,
        "bundle_mode": _config.dac.bundle_mode,
        "output_container": _config.output_blob_container if _config.output_blob_account_url else None,
        "default_routes": _config.default_routes,
    }
    try:
        registry = _processor.loader.get()
        body["bundle_version"] = _processor.loader.version
        body.update(registry.stats())
        body["status"] = "ok" if body["detections"] else "no-detections-loaded"
    except Exception as exc:
        # A cold start with no publishable bundle is the single most common POC
        # setup failure; say so here rather than only in the trigger's logs.
        body["status"] = "bundle-load-failed"
        body["error"] = f"{type(exc).__name__}: {exc}"
    code = 200 if body.get("status") == "ok" else 503
    return func.HttpResponse(json.dumps(body, indent=2), status_code=code,
                             mimetype="application/json")


@app.function_name(name="ingest")
@app.route(route="ingest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    """Feed logs to the processor directly. Body is one JSON log object, or a
    JSON array of them, or newline-delimited JSON. Same code path as `detect`
    from process_batch onward, so a match here writes the same signal/alert
    blobs - it only skips Event Hubs.

    No event ids are supplied, so the processor falls back to hashing the body
    for its redelivery guard: posting the SAME log twice is treated as a
    redelivery and the second copy is skipped. Vary a field to send a genuinely
    new event."""
    raw = req.get_body().decode("utf-8").strip()
    if not raw:
        return func.HttpResponse('{"error": "empty body"}', status_code=400,
                                 mimetype="application/json")
    try:
        parsed = json.loads(raw)
        events = [json.dumps(e) for e in parsed] if isinstance(parsed, list) else [raw]
    except json.JSONDecodeError:
        events = [ln for ln in (l.strip() for l in raw.splitlines()) if ln]  # NDJSON
    try:
        _processor.process_batch(events)
    except Exception as exc:
        log.exception("ingest failed")
        return func.HttpResponse(json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
                                 status_code=500, mimetype="application/json")
    return func.HttpResponse(json.dumps({"accepted": len(events)}), status_code=202,
                             mimetype="application/json")


@app.function_name(name="bundle_published")
@app.event_grid_trigger(arg_name="event")
def bundle_published(event: func.EventGridEvent) -> None:
    """Event Grid "blob created" on the detections container. Marks the bundle
    stale so the next batch re-probes the pointer immediately instead of waiting
    out refresh_interval_seconds.

    Reload happens on the batch path, not here, so a bad publish can never take
    detection down: the loader keeps serving the last-good Registry."""
    subject = event.subject or ""
    if _config.dac.blob_container and f"/{_config.dac.blob_container}/" not in subject:
        return  # a write to some other container; not our bundle
    _processor.loader.invalidate()
    log.info("bundle publish detected (%s); registry will reload on the next batch", subject)
