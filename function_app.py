"""pyre - the Azure Functions entrypoint. Trigger wiring only; all the logic
lives in `pyre_engine/`.

Three kinds of function:

  detect_<hub>   Event Hub batch trigger, ONE PER SOURCE listed in
                 config/sources.yaml. Adding a log source is an entry in that
                 file - there is no code to write, however many sources you have.
  health         GET. Which bundle is loaded, how many detections, which log
                 types they cover, which field each source routes on. The first
                 thing to check when nothing alerts.
  ingest         POST logs straight into the processor, bypassing Event Hubs.
                 Proves the detection half in isolation when you're working out
                 which half is broken.
"""
import json
import logging

import azure.functions as func

from pyre_engine.config import RuntimeConfig
from pyre_engine.processor import Processor

log = logging.getLogger("pyre.host")

app = func.FunctionApp()

# Built once per worker process (cold start) and reused across invocations.
_config = RuntimeConfig()
_processor = Processor(_config)
_sources = {s.hub: s for s in _config.sources}


def _register(source):
    """Attach one batch trigger to one source's hub.

    Registering in a loop rather than writing N copies of a decorated function
    is what makes "add a log source" a config change. Every trigger funnels into
    the same processor, carrying its own source so the routing fields, the
    timestamp field and the envelope shape are that source's own.
    """
    # Azure keys checkpoints and metrics on the function name, so it must be
    # stable across deploys and unique per hub.
    name = "detect_" + "".join(c if c.isalnum() else "_" for c in source.hub)

    @app.function_name(name=name)
    @app.event_hub_message_trigger(
        arg_name="events",
        event_hub_name=source.hub,
        connection=source.connection,
        consumer_group=source.consumer_group,
        cardinality=func.Cardinality.MANY,      # deliver a BATCH, not one event
    )
    def _trigger(events: list[func.EventHubEvent]) -> None:
        # partition_key + sequence_number is stable across an Event Hubs
        # redelivery, so it's what the redelivery guard keys on.
        _processor.process_batch(
            [e.get_body().decode("utf-8") for e in events],
            source,
            event_ids=[f"{e.partition_key}:{e.sequence_number}" for e in events],
        )

    return _trigger


for _source in _config.sources:
    _register(_source)

if not _config.sources:
    log.error("no log sources: config/sources.yaml is empty or missing. "
              "The HTTP functions still work, but nothing is being ingested.")


@app.function_name(name="health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Answers "is this thing actually loaded, and with what?" without sending an
    event.

    Two fields carry the answer to almost every "why no alerts?": `detections`
    (did the bundle load?) and `log_types` (do the values your data carries in
    each source's `log_type_field` appear in this list, exactly?).
    """
    body = {
        "env": _config.env,
        "state": _config.state_backend,
        "output": _config.output_http_url or
                  (f"{_config.output_blob_account_url}/{_config.output_blob_container}"
                   if _config.output_blob_account_url else None),
        "sources": [
            {"hub": s.hub, "log_type_field": s.log_type_field,
             "event_time_field": s.event_time_field, "envelope_field": s.envelope_field or None}
            for s in _config.sources
        ],
    }
    try:
        registry = _processor.loader.get()
        body["bundle_version"] = _processor.loader.version
        body.update(registry.stats())
        body["status"] = "ok" if body["detections"] else "no-detections-loaded"
    except Exception as exc:
        # A cold start with no published bundle is the most common setup failure.
        # Say so here rather than only in the trigger's logs.
        body["status"] = "bundle-load-failed"
        body["error"] = f"{type(exc).__name__}: {exc}"
    code = 200 if body.get("status") == "ok" else 503
    return func.HttpResponse(json.dumps(body, indent=2), status_code=code,
                             mimetype="application/json")


@app.function_name(name="ingest")
@app.route(route="ingest", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def ingest(req: func.HttpRequest) -> func.HttpResponse:
    """Feed logs to the processor directly, as if they had arrived on a hub.

    Body: one JSON object, a JSON array of them, or newline-delimited JSON -
    whatever you copied out of Event Hubs Data Explorer, envelope and all.

    `?source=<hub>` picks whose field config to interpret them with; the first
    source in config/sources.yaml is the default.

    No transport ids exist here, so the redelivery guard falls back to hashing
    the body: posting the SAME payload twice is treated as a redelivery and the
    second copy is skipped. Vary a field to send a genuinely new event.
    """
    if not _config.sources:
        return func.HttpResponse('{"error": "no sources configured"}', status_code=503,
                                 mimetype="application/json")
    hub = req.params.get("source")
    if hub and hub not in _sources:
        return func.HttpResponse(
            json.dumps({"error": f"unknown source {hub!r}", "known": sorted(_sources)}),
            status_code=400, mimetype="application/json")
    source = _sources[hub] if hub else _config.sources[0]

    raw = req.get_body().decode("utf-8").strip()
    if not raw:
        return func.HttpResponse('{"error": "empty body"}', status_code=400,
                                 mimetype="application/json")
    try:
        parsed = json.loads(raw)
        messages = [json.dumps(m) for m in parsed] if isinstance(parsed, list) else [raw]
    except json.JSONDecodeError:
        messages = [ln for ln in (l.strip() for l in raw.splitlines()) if ln]   # NDJSON
    try:
        _processor.process_batch(messages, source)
    except Exception as exc:
        log.exception("ingest failed")
        return func.HttpResponse(json.dumps({"error": f"{type(exc).__name__}: {exc}"}),
                                 status_code=500, mimetype="application/json")
    return func.HttpResponse(json.dumps({"accepted": len(messages), "source": source.hub}),
                             status_code=202, mimetype="application/json")
