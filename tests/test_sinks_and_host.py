"""Where records go, and whether the Function App itself imports.

The host test is the cheap one that matters most: if `function_app.py` raises at
import, Azure deploys it happily and then shows an EMPTY function list, with the
reason buried in the log stream.
"""
import json

from pyre_engine.config import RuntimeConfig
from pyre_engine.sinks import BlobSink, HttpSink, build_router, redact

SIGNAL = {"p_record_type": "signal", "p_signal_id": "s1"}
SIGNAL2 = {"p_record_type": "signal", "p_signal_id": "s2"}
ALERT = {"p_record_type": "alert", "p_alert_id": "a1"}
ALERT2 = {"p_record_type": "alert", "p_alert_id": "a2"}

BLOB = "https://acct.blob.core.windows.net"


def cfg(**kw):
    """A config with nothing inherited from the ambient environment, so these
    assertions are about the settings under test and nothing else."""
    base = dict(sources=[], detections_source="local", signal_destination="none",
                alert_destination="none", state_backend="memory", redis_host="")
    return RuntimeConfig(**{**base, **kw})


# ---- routing each stream to its own destination -----------------------------

def test_each_stream_resolves_its_own_destination():
    """The whole point of splitting them: signals into a blob you can read,
    alerts into a case tool, from one instance."""
    router = build_router(cfg(
        signal_destination="blob", signal_blob_account_url=BLOB,
        alert_destination="http", alert_http_url="https://case-tool/hook"))

    assert isinstance(router._by_stream["signal"], BlobSink)
    assert isinstance(router._by_stream["alert"], HttpSink)


def test_two_streams_pointed_at_one_target_share_a_single_sink():
    """Pointing both at the same place must cost exactly what one destination
    costs - one instance, one write per batch - or a shared HTTP endpoint gets
    double the requests for no reason."""
    router = build_router(cfg(
        signal_destination="blob", signal_blob_account_url=BLOB,
        alert_destination="blob", alert_blob_account_url=BLOB))
    assert router._by_stream["signal"] is router._by_stream["alert"]

    writes = []
    router._by_stream["signal"].write = writes.append
    router.write([SIGNAL, ALERT, SIGNAL2])
    assert writes == [[SIGNAL, ALERT, SIGNAL2]]            # one call, both streams


def test_different_containers_on_one_account_are_different_sinks():
    router = build_router(cfg(
        signal_destination="blob", signal_blob_account_url=BLOB,
        signal_blob_container="signals",
        alert_destination="blob", alert_blob_account_url=BLOB,
        alert_blob_container="alerts"))
    assert router._by_stream["signal"] is not router._by_stream["alert"]


def test_a_stream_set_to_none_is_discarded_and_the_other_still_writes():
    router = build_router(cfg(signal_destination="none",
                              alert_destination="http", alert_http_url="https://x/y"))
    assert router._by_stream["signal"] is None

    got = []
    router._by_stream["alert"].write = got.append
    router.write([SIGNAL, ALERT])
    assert got == [[ALERT]]


def test_a_destination_with_nowhere_to_send_is_a_named_problem_not_a_silent_drop():
    """Without this, "blob selected but no account URL" is indistinguishable
    from a healthy app that happens to produce no output."""
    problems = cfg(signal_destination="blob", signal_blob_account_url="").problems()
    assert any("SIGNAL_BLOB_ACCOUNT_URL is not set" in p for p in problems)

    problems = cfg(alert_destination="http", alert_http_url="").problems()
    assert any("ALERT_HTTP_URL is not set" in p for p in problems)

    # Both off is legal but worth saying out loud.
    assert any("nothing is written anywhere" in p for p in cfg().problems())
    # Fully configured says nothing about destinations at all.
    configured = cfg(signal_destination="blob", signal_blob_account_url=BLOB,
                     alert_destination="http", alert_http_url="https://x/y").problems()
    assert not [p for p in configured if "DESTINATION" in p]

    # And nothing raises when a misconfigured stream is written to.
    build_router(cfg(signal_destination="blob", signal_blob_account_url="")).write([SIGNAL])


# ---- the HTTP sink ----------------------------------------------------------

def test_http_batching_matches_what_each_consumer_wants(monkeypatch):
    """A lake ingesting signals wants the array; a case tool opening a ticket per
    alert wants one record per request."""
    posted = []
    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda url, **kw: posted.append(kw["json"]) or _ok())

    HttpSink("https://lake/in", batch=True).write([SIGNAL, SIGNAL2])
    assert posted == [[SIGNAL, SIGNAL2]]

    posted.clear()
    HttpSink("https://case-tool/hook", batch=False).write([ALERT, ALERT2])
    assert posted == [ALERT, ALERT2]


def test_http_auth_header_is_sent_whole(monkeypatch):
    """Supplied whole ("Bearer x", "SharedKey y") so any scheme works without a
    setting per scheme - and so it can be a Key Vault reference."""
    seen = {}
    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda url, **kw: seen.update(kw) or _ok())

    HttpSink("https://lake/in", auth_header="Bearer secret-token").write([SIGNAL])
    assert seen["headers"] == {"Authorization": "Bearer secret-token"}

    seen.clear()
    HttpSink("https://lake/in").write([SIGNAL])
    assert seen["headers"] == {}


def test_an_error_status_is_reported_not_treated_as_success(monkeypatch, caplog):
    """Swallowing a 401 is how a destination silently stops working for a week."""
    monkeypatch.setattr("pyre_engine.sinks.requests.post",
                        lambda url, **kw: _ok(401, "unauthorized"))
    HttpSink("https://lake/in").write([SIGNAL])
    assert "401" in caplog.text and "1 record(s) dropped" in caplog.text


def test_urls_are_redacted_wherever_they_are_logged():
    """Webhook URLs routinely carry a shared-access token in the query string,
    and both the startup lines and /health echo the configured destination."""
    assert redact("https://hook.example/services/T1?sig=SECRET") == "https://hook.example/services/T1?..."
    assert redact("https://acct.blob.core.windows.net") == "https://acct.blob.core.windows.net"
    assert redact("") == ""
    assert "SECRET" not in HttpSink("https://x/y?token=SECRET").describe()


def _ok(status=200, text=""):
    class _Resp:
        status_code = status

    _Resp.text = text
    return _Resp()


# ---- the blob sink ----------------------------------------------------------

class _FakeBlob:
    def __init__(self):
        self.blocks = []

    def create_append_blob(self):
        pass

    def append_block(self, data):
        self.blocks.append(data)


def _fake_blob_sink(monkeypatch):
    sink = BlobSink(BLOB, "pyre-output")
    written = {}
    monkeypatch.setattr(sink, "_blob_client", lambda prefix: written.setdefault(prefix, _FakeBlob()))
    return sink, written


def _lines(fake):
    return [json.loads(l) for l in b"".join(fake.blocks).decode().splitlines()]


def test_blob_sink_splits_signals_and_alerts_into_separate_streams(monkeypatch):
    """The stream prefix lives inside the container, so one shared container
    keeps both readable and two containers also work - with no extra setting."""
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([SIGNAL, ALERT, SIGNAL2])
    assert sorted(written) == ["alerts", "signals"]
    assert [r["p_signal_id"] for r in _lines(written["signals"])] == ["s1", "s2"]
    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1"]


def test_blob_sink_dedups_alerts_but_never_signals(monkeypatch):
    """An alert should appear once. Signals are an audit trail, so repeats there
    are real and must survive."""
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([ALERT, SIGNAL])
    sink.write([ALERT, ALERT2, SIGNAL2])            # a repeat, a new one, a signal
    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1", "a2"]
    assert len(_lines(written["signals"])) == 2


def test_blob_sink_never_recreates_an_existing_blob(monkeypatch):
    """create_append_blob() resets an existing blob to 0 bytes - Put Blob
    overwrites by default. A blob another worker (or an earlier batch this
    run) already started must never see that call again, or the day's file
    gets wiped on every new execution."""
    from azure.core.exceptions import ResourceExistsError

    created = []

    class _FakeClient:
        def __init__(self, exists):
            self.blocks = []
            self._exists = exists

        def create_append_blob(self, match_condition=None):
            created.append(1)
            if self._exists:
                raise ResourceExistsError("already exists")
            self._exists = True

        def append_block(self, data):
            self.blocks.append(data)

    class _FakeSvc:
        def __init__(self, client):
            self._client = client

        def create_container(self, name):
            pass

        def get_blob_client(self, container, name):
            return self._client

    # A blob that already has content on the service - the case that must
    # never be reset, whether from a prior batch this run or another worker.
    existing = _FakeClient(exists=True)
    sink = BlobSink(BLOB, "pyre-output")
    monkeypatch.setattr(sink, "_service", lambda: _FakeSvc(existing))

    sink.write([SIGNAL])
    sink.write([SIGNAL2])                  # a second batch, same worker, same day

    assert len(created) == 1               # creation attempted once, never again
    assert len(existing.blocks) == 2       # both batches landed - nothing reset


def test_blob_sink_authenticates_as_the_configured_identity(monkeypatch):
    """AZURE_CLIENT_ID pins every credential in the app to one user-assigned
    identity. Dropping it here fails every Azure call on a user-assigned setup,
    and the error names none of this."""
    seen = {}

    class _Cred:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setenv("AZURE_CLIENT_ID", "the-client-id")
    monkeypatch.setitem(__import__("sys").modules, "azure.identity",
                        type("m", (), {"DefaultAzureCredential": _Cred}))
    monkeypatch.setitem(__import__("sys").modules, "azure.storage.blob",
                        type("m", (), {"BlobServiceClient": lambda url, credential: ("svc", url)}))

    BlobSink(BLOB, "c")._service()
    assert seen == {"managed_identity_client_id": "the-client-id"}


def test_a_sink_never_raises_into_the_batch(monkeypatch):
    """Losing a write must not fail the batch: Event Hubs would redeliver it and
    the alert would fire twice."""
    sink = BlobSink(BLOB, "pyre-output")
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


def test_health_reports_the_routing_config_and_every_contradiction():
    """The endpoint the guides send you to first. It has to answer "which field
    am I routing on", "did the bundle load" and "what is misconfigured" without
    any other tooling."""
    import function_app

    resp = function_app.health(_request("GET", "/api/health"))
    body = json.loads(resp.get_body())

    assert isinstance(body["problems"], list)          # named, never implied
    assert set(body["destinations"]) == {"signal", "alert"}
    assert "endpoint" in body["identity"]
    assert body["status"] in ("ok", "no-sources-configured", "no-detections-loaded",
                              "bundle-load-failed", "configuration-problems")
    assert (resp.status_code == 200) == (body["status"] == "ok")
    if body["sources"]:
        assert body["sources"][0]["log_type_field"]    # compare this against your data


def test_ingest_rejects_an_unknown_source_by_name():
    import function_app

    resp = function_app.ingest(_request("POST", "/api/ingest", b'{"a": 1}',
                                        params={"source": "not-a-hub"}))
    assert resp.status_code in (400, 503)
    if resp.status_code == 400:
        assert json.loads(resp.get_body())["known"] == sorted(function_app._sources)


def test_ingest_accepts_an_event_hub_message_shape(monkeypatch):
    """`ingest` runs the identical path as the Event Hub trigger from
    process_batch onward, so what it accepts must be exactly what Event Hubs
    carries - envelope and all."""
    import function_app

    if not function_app._config.sources:
        return                                  # nothing to ingest into; covered above

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
