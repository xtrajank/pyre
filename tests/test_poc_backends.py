"""The environment seam: everything that differs between the POC and production.

There are exactly two such things, both selected in `backends/`:

  1. state  - backends.memory_state (POC) vs redis (prod), behind one StateStore
  2. records - backends.blob_sink (POC) vs http_sink (prod), behind one contract

These tests pin the behaviour that must be IDENTICAL across that seam, because
that identity is the whole claim of the POC: what you demo is what ships. If a
test here starts needing an env-specific branch, the seam has leaked.
"""
import json
import os
import shutil
import sys
import time

import pytest

from pyre_engine.backends import build_record_sink, build_state_store
from pyre_engine.backends.blob_sink import BlobSink
from pyre_engine.backends.http_sink import HttpSink
from pyre_engine.backends.memory_state import MemoryClient
from pyre_engine.config import RuntimeConfig, event_hub_names
from pyre_engine.processor import Processor
from pyre_engine.registry import Registry
from pyre_engine.state import StateStore

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POC_DAC = os.path.join(REPO, "tools", "poc", "dac")
POC_SAMPLES = os.path.join(REPO, "tools", "poc", "samples", "cloudtrail_poc.jsonl")
EH_SAMPLES = os.path.join(REPO, "tools", "poc", "samples", "eventhub_diagnostic.jsonl")
EXAMPLE_DAC = os.path.join(REPO, "tools", "poc", "dac_bundler", "example")


class CaptureSink:
    """Stands in for whichever real sink the environment would pick. The
    processor cannot tell the difference - which is the point."""

    def __init__(self):
        self.records: list[dict] = []

    def write(self, records):
        self.records.extend(records)

    @property
    def signals(self):
        return [r for r in self.records if r["p_record_type"] == "signal"]

    @property
    def alerts(self):
        return [r for r in self.records if r["p_record_type"] == "alert"]


def build_processor(monkeypatch, bundle, **cfg_kwargs):
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", bundle)
    cfg = RuntimeConfig(env="poc", state_backend="memory", signals_sink_url="", **cfg_kwargs)
    sink = CaptureSink()
    return Processor(cfg, sink=sink), sink


# ---- the Redis stand-in ----------------------------------------------------

def test_memory_client_matches_the_redis_semantics_the_engine_relies_on():
    c = MemoryClient()
    # SET NX: the second write must not apply (this is the alert claim).
    assert c.set("a", "1", nx=True, ex=60) is True
    assert c.set("a", "2", nx=True, ex=60) is None
    assert c.get("a") == "1"
    # INCR counts from 1 on a missing key (the dedup counter).
    assert [c.incr("n"), c.incr("n"), c.incr("n")] == [1, 2, 3]
    # EXPIRE NX starts the window once and never slides it.
    assert c.expire("n", 60, nx=True) is True
    assert c.expire("n", 999, nx=True) is False
    # PFADD/PFCOUNT back unique() thresholds.
    assert [c.pfadd("u", "x"), c.pfadd("u", "x"), c.pfadd("u", "y")] == [1, 0, 1]
    assert c.pfcount("u") == 2


def test_memory_client_ttl_expires_the_window():
    c = MemoryClient()
    c.set("k", "1", ex=1)
    c.incr("n")
    c.expire("n", 1)
    time.sleep(1.05)
    assert c.get("k") is None
    assert c.incr("n") == 1          # window elapsed -> counting restarts
    assert c.pfcount("gone") == 0


def test_memory_pipeline_returns_one_result_per_queued_command():
    # The processor indexes into execute()'s result list by position
    # ([incr, expire] for count mode, [pfadd, expire, pfcount] for unique mode),
    # so the arity here is load-bearing.
    c = MemoryClient()
    pipe = c.pipeline()
    pipe.incr("d")
    pipe.expire("d", 60, nx=True)
    pipe.pfadd("u", "a")
    pipe.expire("u", 60, nx=True)
    pipe.pfcount("u")
    assert pipe.execute() == [1, True, 1, True, 1]
    assert pipe.execute() == []      # queue is cleared after execute


def test_build_state_store_selects_the_backend():
    store = build_state_store(RuntimeConfig(state_backend="memory"))
    assert isinstance(store, StateStore)
    pipe = store.pipeline()
    store.is_new_event(pipe, "evt-1")
    store.is_new_event(pipe, "evt-1")
    assert pipe.execute() == [True, None]   # second sighting is a redelivery

    with pytest.raises(ValueError):
        build_state_store(RuntimeConfig(state_backend="nonsense"))


def test_build_record_sink_follows_config():
    # An HTTP sink URL means production: post to the lake.
    assert isinstance(build_record_sink(RuntimeConfig(signals_sink_url="https://x/y")), HttpSink)
    # No URL but a blob account means POC.
    assert isinstance(build_record_sink(
        RuntimeConfig(signals_sink_url="",
                      output_blob_account_url="https://a.blob.core.windows.net")), BlobSink)
    # Neither: detection still runs, records go nowhere, nothing raises.
    build_record_sink(RuntimeConfig(signals_sink_url="", output_blob_account_url="")).write([{"a": 1}])


# ---- the record sinks -------------------------------------------------------

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

    def _client(prefix):
        return written.setdefault(prefix, _FakeBlob())

    monkeypatch.setattr(sink, "_blob_client", _client)
    return sink, written


def _lines(fake):
    return [json.loads(l) for l in b"".join(fake.blocks).decode().splitlines()]


def test_blob_sink_splits_signals_and_alerts_into_separate_streams(monkeypatch):
    # A blob container has no routing layer, so the sink does the split the Cribl
    # lake would do on `_dataset`.
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([
        {"p_record_type": "signal", "p_signal_id": "s1"},
        {"p_record_type": "alert", "p_alert_id": "a1"},
        {"p_record_type": "signal", "p_signal_id": "s2"},
    ])
    assert sorted(written) == ["alerts", "signals"]
    assert [r["p_signal_id"] for r in _lines(written["signals"])] == ["s1", "s2"]
    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1"]


def test_blob_sink_dedups_alerts_but_never_signals(monkeypatch):
    """The one behaviour the blob sink adds. Redis makes it redundant, but with
    in-process state an at-least-once redelivery can present the same alert
    twice, and an alert must appear once. Signals are an audit trail - repeats
    there are real and must survive."""
    sink, written = _fake_blob_sink(monkeypatch)
    sink.write([{"p_record_type": "alert", "p_alert_id": "a1"},
                {"p_record_type": "signal", "p_signal_id": "s1", "dedup": "same"}])
    sink.write([{"p_record_type": "alert", "p_alert_id": "a1"},          # repeat
                {"p_record_type": "alert", "p_alert_id": "a2"},          # new
                {"p_record_type": "signal", "p_signal_id": "s2", "dedup": "same"}])

    assert [r["p_alert_id"] for r in _lines(written["alerts"])] == ["a1", "a2"]
    assert len(_lines(written["signals"])) == 2       # both kept, identical dedup


def test_blob_sink_never_raises_into_the_batch(monkeypatch):
    # Losing the write must not fail the batch: Event Hubs would redeliver it and
    # the alert would fire twice.
    sink = BlobSink("https://acct.blob.core.windows.net", "pyre-output")
    monkeypatch.setattr(sink, "_blob_client",
                        lambda prefix: (_ for _ in ()).throw(RuntimeError("403")))
    sink.write([{"p_record_type": "alert", "p_alert_id": "a1"}])   # must not raise


def test_http_sink_never_raises_into_the_batch(monkeypatch):
    sink = HttpSink("https://lake.example/in")
    monkeypatch.setattr("pyre_engine.backends.http_sink.requests.post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    sink.write([{"p_record_type": "signal"}])                      # must not raise


def test_blob_sink_is_inert_without_an_account_url():
    BlobSink("", "pyre-output").write([{"p_record_type": "signal"}])


# ---- record identity: p_signal_id / p_alert_id ------------------------------

def test_every_record_is_uniquely_identified_and_typed(monkeypatch):
    proc, sink = build_processor(monkeypatch, POC_DAC)
    proc.process_batch([l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()])

    signal_ids = [r["p_signal_id"] for r in sink.signals]
    alert_ids = [r["p_alert_id"] for r in sink.alerts]
    assert len(set(signal_ids)) == len(signal_ids) and all(signal_ids)
    assert len(set(alert_ids)) == len(alert_ids) and all(alert_ids)
    # An alert record never carries a signal id, and vice versa.
    assert all("p_signal_id" not in r for r in sink.alerts)


def test_signals_link_to_the_alert_they_rolled_into(monkeypatch):
    """The link that makes the signals stream answerable on its own: which
    matches made up this alert, and which went nowhere."""
    proc, sink = build_processor(monkeypatch, POC_DAC)
    proc.process_batch([l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()])

    alert_ids = {r["p_alert_id"] for r in sink.alerts}

    # Two root logins share a dedup string -> one alert; BOTH signals point at it.
    root = [s for s in sink.signals if s["detection_id"] == "POC.AWS.Console.RootLogin"]
    assert len(root) == 2
    assert {s["p_alert_id"] for s in root} == alert_ids & {s["p_alert_id"] for s in root}
    assert len({s["p_alert_id"] for s in root}) == 1 and root[0]["p_alert_id"] is not None

    # bob failed once, below Threshold 3 -> a signal that belongs to no alert.
    bob = [s for s in sink.signals if "bob" in s["dedup"]]
    assert len(bob) == 1 and bob[0]["p_alert_id"] is None


# ---- the whole POC path -----------------------------------------------------

def test_poc_bundle_produces_the_documented_signals_and_alerts(monkeypatch):
    proc, sink = build_processor(monkeypatch, POC_DAC)
    proc.process_batch([l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()])

    # 10 events in: 8 match a rule, but only 3 clear threshold + dedup.
    assert len(sink.signals) == 8
    assert [(a["severity"], a["detection_id"]) for a in sink.alerts] == [
        ("High", "POC.AWS.Console.RootLogin"),      # 2 matches -> 1 alert (dedup)
        ("Medium", "POC.AWS.IAM.UserCreated"),
        ("Medium", "POC.AWS.Console.LoginFailed"),  # alice hits Threshold 3
    ]
    assert not any("bob" in a["dedup"] for a in sink.alerts)


def test_replaying_the_same_batch_is_suppressed(monkeypatch):
    proc, sink = build_processor(monkeypatch, POC_DAC)
    events = [l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()]
    proc.process_batch(events)
    sink.records.clear()
    proc.process_batch(events)
    assert sink.records == []


def test_the_two_state_backends_produce_identical_results(monkeypatch):
    """THE claim of the POC: what you demo is what ships.

    The same events through the POC's in-process state and through the real Redis
    code path (fakeredis drives redis-py, so StateStore's actual Redis calls run)
    must produce the same signals, the same alerts, and the same signal→alert
    links. If this ever fails, the two backends have drifted and the POC stops
    proving anything about production."""
    fakeredis = pytest.importorskip("fakeredis")

    def run(state):
        monkeypatch.setenv("BUNDLE_MODE", "local")
        monkeypatch.setenv("BUNDLE_LOCAL_DIR", POC_DAC)
        sink = CaptureSink()
        proc = Processor(RuntimeConfig(env="poc", state_backend="memory", signals_sink_url=""),
                         state=state, sink=sink)
        proc.process_batch([l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()])
        return sink

    memory = run(build_state_store(RuntimeConfig(state_backend="memory")))
    redis_ = run(StateStore(fakeredis.FakeStrictRedis(decode_responses=True)))

    def shape(sink):
        # Everything except the randomly-generated ids, plus the alert-linkage
        # structure expressed without them.
        signals = [(r["detection_id"], r["dedup"], r["p_alert_id"] is not None)
                   for r in sink.signals]
        alerts = [(r["detection_id"], r["severity"], r["dedup"]) for r in sink.alerts]
        return signals, alerts

    assert shape(memory) == shape(redis_)
    assert len(memory.signals) == 8 and len(memory.alerts) == 3


# ---- envelopes: one transport message carrying many log records --------------

def test_unwrap_handles_the_three_message_shapes():
    unwrap = Processor.__dict__["_unwrap"]

    class _P:  # avoid building a whole Processor for a pure function
        pass
    p = _P()
    p.cfg = RuntimeConfig(state_backend="memory", event_envelope_field="records")

    assert unwrap(p, {"a": 1}) == [{"a": 1}]                        # single record
    assert unwrap(p, [{"a": 1}, {"b": 2}]) == [{"a": 1}, {"b": 2}]  # JSON array
    assert unwrap(p, {"records": [{"a": 1}]}) == [{"a": 1}]         # Azure envelope
    # A `records` key that isn't a list is data, not an envelope.
    assert unwrap(p, {"records": "nope"}) == [{"records": "nope"}]
    # Disabling the envelope makes the whole message one event again.
    p.cfg = RuntimeConfig(state_backend="memory", event_envelope_field="")
    assert unwrap(p, {"records": [{"a": 1}]}) == [{"records": [{"a": 1}]}]


def test_azure_diagnostic_envelope_is_expanded_and_evaluated(monkeypatch):
    """The POC's real shape: Azure diagnostic settings emit {"records":[...]} to
    Event Hubs, so ONE message carries several records and each must be routed
    and evaluated on its own."""
    proc, sink = build_processor(monkeypatch, EXAMPLE_DAC,
                                 log_type_field="Category", event_time_field="Timestamp")
    assert proc.loader.get().stats()["log_types"] == ["RuntimeAuditLogs"]

    messages = [l.strip() for l in open(EH_SAMPLES, encoding="utf-8") if l.strip()]
    assert len(messages) == 3                       # 3 messages...
    proc.process_batch(messages, event_ids=[f"0:{i}" for i in range(len(messages))])

    # ...carrying 7 records: 4 auth failures (a signal each), 2 successful
    # connections and 1 ApplicationMetricsLogs record no detection covers.
    assert len(sink.signals) == 4
    # Threshold is 3, dedup is per client IP. 203.0.113.55 fails 3 times and
    # alerts; 198.51.100.77 fails once and stays a signal only.
    assert len(sink.alerts) == 1
    assert "203.0.113.55" in sink.alerts[0]["title"]
    assert {s["dedup"] for s in sink.signals} == {
        "eh-auth-failure:203.0.113.55", "eh-auth-failure:198.51.100.77"}


def test_envelope_records_get_independent_redelivery_ids(monkeypatch):
    # A message id alone would mark ALL of a message's records seen at once. Ids
    # must be per record, and stable across a redelivery of that message.
    proc, sink = build_processor(monkeypatch, EXAMPLE_DAC, log_type_field="Category")
    msg = json.dumps({"records": [
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "1.1.1.1"},
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "2.2.2.2"},
    ]})
    proc.process_batch([msg], event_ids=["0:500"])
    assert len(sink.signals) == 2

    sink.records.clear()
    proc.process_batch([msg], event_ids=["0:500"])   # same message redelivered
    assert sink.records == []


# ---- config-driven hub list (one hub in the POC, many in prod) --------------

def test_event_hub_names_prefers_explicit_setting(monkeypatch):
    monkeypatch.setenv("EVENTHUB_NAMES", "a, b ,a")
    assert event_hub_names(RuntimeConfig()) == ["a", "b"]     # order kept, deduped


def test_event_hub_names_falls_back_to_sources_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("EVENTHUB_NAMES", raising=False)
    monkeypatch.delenv("EVENTHUB_NAME", raising=False)
    src = tmp_path / "sources.yaml"
    src.write_text(
        "sources:\n"
        "  - name: a\n    hub: logs-in\n"
        "  - name: b\n    hub: palo-in\n"
        "  - name: c\n    hub: logs-in\n"       # same hub twice -> one trigger
    )
    assert event_hub_names(RuntimeConfig(sources_path=str(src))) == ["logs-in", "palo-in"]


def test_an_app_setting_always_beats_the_packaged_sources_file(monkeypatch, tmp_path):
    """The same package ships everywhere, so a file inside it must never override
    what an environment explicitly asked for. Backwards, the POC would attach
    triggers to the dev/prod hubs listed in the shipped sources.yaml - hubs that
    don't exist in its namespace, which fails the whole app."""
    src = tmp_path / "sources.yaml"
    src.write_text("sources:\n  - name: a\n    hub: prod-hub-1\n  - name: b\n    hub: prod-hub-2\n")
    cfg = RuntimeConfig(sources_path=str(src))

    monkeypatch.delenv("EVENTHUB_NAMES", raising=False)
    monkeypatch.setenv("EVENTHUB_NAME", "poc-hub")
    assert event_hub_names(cfg) == ["poc-hub"]

    monkeypatch.setenv("EVENTHUB_NAMES", "a-hub,b-hub")
    assert event_hub_names(cfg) == ["a-hub", "b-hub"]


def test_no_hubs_configured_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.delenv("EVENTHUB_NAMES", raising=False)
    monkeypatch.delenv("EVENTHUB_NAME", raising=False)
    assert event_hub_names(RuntimeConfig(sources_path=str(tmp_path / "nope.yaml"))) == []


# ---- the DaC path the guide documents ---------------------------------------

def test_bundler_output_loads_in_the_engine(tmp_path):
    """Run the drop-in bundler over a repo, then load the zip exactly as the
    engine does from Blob. If this breaks, the bundle in the guide would upload
    fine and load nothing."""
    import subprocess
    import zipfile

    repo = tmp_path / "dac-repo"
    (repo / "detections" / "azure").mkdir(parents=True)
    (repo / "dac_bundler").mkdir()
    for f in ("eventhub_auth_failure.yml", "eventhub_auth_failure.py"):
        shutil.copy(os.path.join(EXAMPLE_DAC, f), repo / "detections" / "azure" / f)
    shutil.copy(os.path.join(REPO, "tools", "poc", "dac_bundler", "bundle.py"),
                repo / "dac_bundler" / "bundle.py")

    out = subprocess.run([sys.executable, "dac_bundler/bundle.py"], cwd=repo,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "RuntimeAuditLogs" in out.stdout          # surfaces the routing value

    zips = list((repo / "dist" / "bundles").glob("*.zip"))
    assert len(zips) == 1
    pointer = json.loads((repo / "dist" / "current.json").read_text())
    assert pointer["path"] == f"bundles/{zips[0].name}"

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(zips[0]) as z:
        z.extractall(extracted)
        # The bundler must not ship itself into the bundle.
        assert not any(n.startswith("dac_bundler/") for n in z.namelist())
    reg = Registry.from_bundle(str(extracted))
    assert reg.stats() == {"detections": 1, "log_types": ["RuntimeAuditLogs"]}
