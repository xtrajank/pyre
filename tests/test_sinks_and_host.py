"""Where records go, and whether the Function App itself imports.

The host test is the cheap one that matters most: if `function_app.py` raises at
import, Azure deploys it happily and then shows an EMPTY function list, with the
reason buried in the log stream.
"""
import json

from pyre_engine.config import RuntimeConfig
from pyre_engine.sinks import AlertWebhook, BlobSink, HttpSink, build_sink

SIGNAL = {"p_record_type": "signal", "p_signal_id": "s1"}
ALERT = {"p_record_type": "alert", "p_alert_id": "a1"}


# ---- picking a sink ---------------------------------------------------------

def test_build_sink_follows_config():
    # HTTP wins: that's the production endpoint, and blob is its stand-in.
    assert isinstance(build_sink(RuntimeConfig(output_http_url="https://x/y",
                                               output_blob_account_url="https://a.blob.core.windows.net")),
                      HttpSink)
    assert isinstance(build_sink(RuntimeConfig(output_http_url="",
                                               output_blob_account_url="https://a.blob.core.windows.net")),
                      BlobSink)
    # Neither configured: detection still runs, records go nowhere, nothing raises.
    build_sink(RuntimeConfig(output_http_url="", output_blob_account_url="")).write([SIGNAL])


def test_alert_webhook_wraps_whatever_sink_was_chosen():
    sink = build_sink(RuntimeConfig(output_http_url="https://x/y",
                                    alert_webhook_url="https://case-tool/hook"))
    assert isinstance(sink, AlertWebhook)


def test_the_webhook_posts_alerts_only_and_never_swallows_the_record(monkeypatch):
    """A webhook that is down must lose the page, never the record."""
    posted = []
    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda url, **kw: posted.append(kw["json"]))

    class Inner:
        def __init__(self):
            self.got = []

        def write(self, records):
            self.got.extend(records)

    inner = Inner()
    AlertWebhook(inner, "https://case-tool/hook").write([SIGNAL, ALERT])
    assert inner.got == [SIGNAL, ALERT]                # everything still recorded
    assert posted == [ALERT]                           # only the alert paged

    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    AlertWebhook(inner, "https://case-tool/hook").write([ALERT])   # must not raise


# ---- the blob sink ----------------------------------------------------------

class _FakeBlob:
    def __init__(self):
        self.blocks = []

    def create_append_blob(self):
        pass

    def append_block(self, data):
        self.blocks.append(data)


def _fake_blob_sink(monkeypatch):
    sink = BlobSink("https://acct.blob.core.windows.net", "pyre-output")
    written = {}
    monkeypatch.setattr(sink, "_blob_client", lambda prefix: written.setdefault(prefix, _FakeBlob()))
    return sink, written


def _lines(fake):
    return [json.loads(l) for l in b"".join(fake.blocks).decode().splitlines()]


def test_blob_sink_splits_signals_and_alerts_into_separate_streams(monkeypatch):
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([SIGNAL, ALERT, {"p_record_type": "signal", "p_signal_id": "s2"}])
    assert sorted(written) == ["alerts", "signals"]
    assert [r["p_signal_id"] for r in _lines(written["signals"])] == ["s1", "s2"]
    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1"]


def test_blob_sink_dedups_alerts_but_never_signals(monkeypatch):
    """An alert should appear once. Signals are an audit trail, so repeats there
    are real and must survive."""
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([ALERT, SIGNAL])
    sink.write([ALERT,                                          # a repeat
                {"p_record_type": "alert", "p_alert_id": "a2"},  # new
                {"p_record_type": "signal", "p_signal_id": "s2"}])
    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1", "a2"]
    assert len(_lines(written["signals"])) == 2


def test_a_sink_never_raises_into_the_batch(monkeypatch):
    """Losing a write must not fail the batch: Event Hubs would redeliver it and
    the alert would fire twice."""
    sink = BlobSink("https://acct.blob.core.windows.net", "pyre-output")
    monkeypatch.setattr(sink, "_blob_client",
                        lambda prefix: (_ for _ in ()).throw(RuntimeError("403")))
    sink.write([ALERT])

    http = HttpSink("https://lake.example/in")
    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    http.write([SIGNAL])

    BlobSink("", "pyre-output").write([SIGNAL])     # inert without an account url


# ---- the Function App itself ------------------------------------------------

_FUNCTIONS = None


def _registered_functions():
    """`FunctionApp.get_functions()` records every name it has ever validated and
    raises "not unique" the second time it's called, so call it once and share
    the result. The Azure worker calls it once too."""
    global _FUNCTIONS
    if _FUNCTIONS is None:
        import function_app
        _FUNCTIONS = {f.get_function_name(): f for f in function_app.app.get_functions()}
    return _FUNCTIONS


def test_function_app_imports_and_registers_a_trigger_per_source():
    """Imports function_app.py the way the Azure worker does. A failure here is
    the "deployed fine, function list is empty" bug, caught before deploying."""
    import function_app

    names = _registered_functions()
    assert {"health", "ingest"} <= set(names)
    for source in function_app._config.sources:
        assert source.function_name in names


def _request(method, url, body=b"", params=None):
    import azure.functions as func
    return func.HttpRequest(method=method, url=url, body=body, params=params or {})


def test_health_reports_the_routing_config_and_what_loaded():
    """The endpoint the guides send you to first. It has to answer "which field
    am I routing on" and "did the bundle load" without any other tooling."""
    import function_app

    body = json.loads(function_app.health(_request("GET", "/api/health")).get_body())
    assert body["sources"][0]["log_type_field"]     # the value to compare against your data
    assert body["status"] in ("ok", "no-detections-loaded", "bundle-load-failed")
    # Whichever it is, the field that explains it is present.
    assert "detections" in body or "error" in body


def test_ingest_rejects_an_unknown_source_by_name():
    import function_app

    resp = function_app.ingest(_request("POST", "/api/ingest", b'{"a": 1}',
                                        params={"source": "not-a-hub"}))
    assert resp.status_code == 400
    body = json.loads(resp.get_body())
    assert body["known"] == sorted(function_app._sources)


def test_ingest_accepts_an_event_hub_message_shape(monkeypatch):
    """`ingest` runs the identical path as the Event Hub trigger from
    process_batch onward, so what it accepts must be exactly what Event Hubs
    carries - envelope and all."""
    import function_app

    seen = {}
    monkeypatch.setattr(function_app._processor, "process_batch",
                        lambda messages, source, **kw: seen.update(n=len(messages), src=source.hub))

    envelope = b'{"records": [{"category": "X"}, {"category": "Y"}]}'
    resp = function_app.ingest(_request("POST", "/api/ingest", envelope))
    assert resp.status_code == 202
    assert seen == {"n": 1, "src": function_app._config.sources[0].hub}   # one MESSAGE

    # Newline-delimited JSON is several messages.
    function_app.ingest(_request("POST", "/api/ingest", b'{"a": 1}\n{"a": 2}\n'))
    assert seen["n"] == 2

    assert function_app.ingest(_request("POST", "/api/ingest", b"")).status_code == 400
