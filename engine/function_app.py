"""PYRE streaming detection processor - Azure Functions entrypoint.

This file is deliberately thin. It is ONLY the Event Hubs batch trigger.
All real logic lives in the reusable `pyre_engine` package so the same
code can be reused by the (future) scheduled-query module and by a
Container Apps host without modification.
"""
import azure.functions as func

from pyre_engine.config import load_runtime_config
from pyre_engine.processor import Processor

app = func.FunctionApp()

# Built once per worker process (cold start), reused across invocations.
_config = load_runtime_config()
_processor = Processor(_config)


@app.function_name(name="detect")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENTHUB_NAME%",          # from app settings, e.g. "logs-in"
    connection="EVENTHUB_CONNECTION",           # Managed-Identity connection (see host settings)
    cardinality=func.Cardinality.MANY,          # deliver a BATCH, not one event
)
def detect(events: list[func.EventHubEvent]) -> None:
    """One invocation handles a whole batch. Cost lever: batch size in host.json."""
    raw = [e.get_body().decode("utf-8") for e in events]
    _processor.process_batch(raw)
