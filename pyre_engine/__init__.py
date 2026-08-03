"""pyre_engine - the detection processor.

Nothing in here imports `azure.functions`, so the same code runs under the Azure
Functions host (function_app.py), on a laptop (tools/run_local.py) and under
pytest, unchanged. The Azure SDK is imported lazily, and only by the two modules
that talk to Blob or Redis.

    config.py      app settings + config/sources.yaml
    processor.py   the batch loop: route -> rule() -> signal -> threshold -> alert
    registry.py    detections, loaded from a bundle and indexed by log type
    bundle.py      where that bundle comes from (Blob, or a local folder)
    state.py       dedup / thresholds / redelivery guard (Redis or in-process)
    sinks.py       where signals and alerts go (HTTP, Blob, alert webhook)
    records.py     what a signal and an alert look like
    event.py       the object rule() receives
"""
