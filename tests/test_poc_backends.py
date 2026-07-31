"""The two substitutions that make a Redis-less, Cribl-less POC work:

  1. state.MemoryClient   - stands in for Redis behind the SAME StateStore.
  2. blobsink             - stands in for the Cribl lake and for Torq.

Plus one end-to-end pass over the curated POC bundle (tools/poc/dac) with the
POC samples, which is the exact behaviour docs/poc/README.md tells you to expect
in Azure. If that assertion breaks, the guide is wrong.
"""
import json
import os
import time

import pytest

from pyre_engine.config import RuntimeConfig
from pyre_engine.blobsink import AppendBlobSink
from pyre_engine.dedup import StateStore
from pyre_engine.processor import Processor
from pyre_engine.state import MemoryClient, make_state_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POC_DAC = os.path.join(REPO, "tools", "poc", "dac")
POC_SAMPLES = os.path.join(REPO, "tools", "poc", "samples", "cloudtrail_poc.jsonl")


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


def test_make_state_store_selects_the_backend():
    cfg = RuntimeConfig(state_backend="memory")
    store = make_state_store(cfg)
    assert isinstance(store, StateStore)
    pipe = store.pipeline()
    store.is_new_event(pipe, "evt-1")
    store.is_new_event(pipe, "evt-1")
    assert pipe.execute() == [True, None]   # second sighting is a redelivery


# ---- the Cribl/Torq stand-in ------------------------------------------------

class _FakeBlob:
    def __init__(self):
        self.blocks = []

    def create_append_blob(self):
        pass

    def append_block(self, data):
        self.blocks.append(data)


def test_blob_sink_writes_one_json_line_per_record(monkeypatch):
    fake = _FakeBlob()
    sink = AppendBlobSink("https://acct.blob.core.windows.net", "pyre-output", "alerts")
    monkeypatch.setattr(sink, "_blob_client", lambda: fake)
    sink.append([{"a": 1}, {"b": "two"}])
    lines = b"".join(fake.blocks).decode().splitlines()
    assert [json.loads(l) for l in lines] == [{"a": 1}, {"b": "two"}]


def test_blob_sink_never_raises_into_the_batch(monkeypatch):
    # Losing the visualisation must not fail the batch: Event Hubs would redeliver
    # it and the alert would fire twice.
    sink = AppendBlobSink("https://acct.blob.core.windows.net", "pyre-output", "alerts")
    monkeypatch.setattr(sink, "_blob_client", lambda: (_ for _ in ()).throw(RuntimeError("403")))
    sink.append([{"a": 1}])          # must not raise


def test_blob_sink_is_inert_without_an_account_url():
    AppendBlobSink("", "pyre-output", "alerts").append([{"a": 1}])   # no client, no error


# ---- the whole POC path -----------------------------------------------------

@pytest.fixture
def poc_processor(monkeypatch):
    """A Processor configured exactly as the POC Function App is, with the two
    blob sinks captured in memory instead of written to Azure."""
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", POC_DAC)
    cfg = RuntimeConfig(
        env="poc", state_backend="memory",
        output_blob_account_url="https://acct.blob.core.windows.net",
        destinations_path=os.path.join(REPO, "config", "destinations.yaml"),
        default_routes=["blob_alerts"], signals_sink_url="",
    )
    proc = Processor(cfg)
    captured = {"alerts": [], "signals": []}
    monkeypatch.setattr(proc.dispatcher._blob_sink, "append", captured["alerts"].extend)
    monkeypatch.setattr(proc.signals._blob, "append", captured["signals"].extend)
    return proc, captured


def test_poc_bundle_produces_the_documented_signals_and_alerts(poc_processor):
    proc, captured = poc_processor
    events = [l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()]
    proc.process_batch(events)

    signals = [r for r in captured["signals"] if r["_dataset"] != "pyre_alerts"]
    alerts = captured["alerts"]

    # 10 events in: 8 match a rule, but only 3 clear threshold + dedup.
    assert len(signals) == 8
    assert [(a["severity"], a["detection_id"]) for a in alerts] == [
        ("High", "POC.AWS.Console.RootLogin"),      # 2 matches -> 1 alert (dedup)
        ("Medium", "POC.AWS.IAM.UserCreated"),
        ("Medium", "POC.AWS.Console.LoginFailed"),  # alice hits Threshold 3
    ]
    # bob failed once: a signal, but never an alert.
    assert any("bob" in s["dedup"] for s in signals)
    assert not any("bob" in a["dedup"] for a in alerts)
    assert all(a["destination"] == "blob_alerts" for a in alerts)


def test_replaying_the_same_batch_is_suppressed(poc_processor):
    # In-process idempotency: Event Hubs is at-least-once, so a checkpoint retry
    # must not double-alert. The POC's memory backend has to honour this too.
    proc, captured = poc_processor
    events = [l.strip() for l in open(POC_SAMPLES, encoding="utf-8") if l.strip()]
    proc.process_batch(events)
    captured["alerts"].clear(); captured["signals"].clear()
    proc.process_batch(events)
    assert captured["signals"] == [] and captured["alerts"] == []
