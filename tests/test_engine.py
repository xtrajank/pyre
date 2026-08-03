"""The detection path, end to end: an Event Hub message in, signals and alerts
out. If these pass, the engine is not the problem."""
import json

import pytest

from conftest import DAC, sample_messages
from pyre_engine.config import RuntimeConfig, Source
from pyre_engine.processor import Processor, _unwrap

# The POC's real shape: Azure diagnostic settings wrap records in `records`, and
# Event Hubs runtime audit records use PascalCase field names.
AZURE = Source(hub="logs-in", log_type_field="Category", event_time_field="Timestamp")


class CaptureSink:
    """Stands in for whichever real sink the environment would pick. The
    processor cannot tell the difference - which is the point."""

    def __init__(self):
        self.records = []

    def write(self, records):
        self.records.extend(records)

    @property
    def signals(self):
        return [r for r in self.records if r["p_record_type"] == "signal"]

    @property
    def alerts(self):
        return [r for r in self.records if r["p_record_type"] == "alert"]


def build(bundle=DAC, state=None):
    cfg = RuntimeConfig(sources=[AZURE], dac_local_dir=bundle, dac_refresh_seconds=0,
                        redis_host="", output_blob_account_url="", output_http_url="")
    sink = CaptureSink()
    return Processor(cfg, state=state, sink=sink), sink


# ---- the whole path ---------------------------------------------------------

def test_the_documented_signals_and_alerts_come_out():
    """3 messages carry 7 records: 4 auth failures from public IPs, 2 successful
    connections, and 1 ApplicationMetricsLogs record no detection covers.

    Threshold is 3 and dedup is per client IP, so 203.0.113.55 (3 failures)
    alerts and 198.51.100.77 (1 failure) stays a signal."""
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])

    assert len(sink.signals) == 4
    assert len(sink.alerts) == 1
    assert "203.0.113.55" in sink.alerts[0]["title"]
    assert {s["dedup"] for s in sink.signals} == {
        "eh-auth-failure:203.0.113.55", "eh-auth-failure:198.51.100.77"}


def test_signals_link_to_the_alert_they_rolled_into():
    """`p_alert_id` on a signal is "the alert this match raised or joined", not
    "the alert it contributed to". A match below the threshold has not raised
    anything yet, so it stays null - which is what makes filtering the signals
    stream on `p_alert_id is null` mean "held back by a threshold or by
    CreateAlert: false"."""
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])

    alert_id = sink.alerts[0]["p_alert_id"]
    noisy = [s for s in sink.signals if s["dedup"].endswith("203.0.113.55")]
    quiet = [s for s in sink.signals if s["dedup"].endswith("198.51.100.77")]

    # Threshold 3: the first two are below it, the third raises the alert.
    assert [s["p_alert_id"] for s in noisy] == [None, None, alert_id]
    assert [s["p_alert_id"] for s in quiet] == [None]

    # A fourth failure from the same IP joins the alert already open, rather
    # than raising a second one.
    sink.records.clear()
    proc.process_batch([json.dumps({"records": [{
        "Category": "RuntimeAuditLogs", "ActivityStatus": "Failure",
        "ClientIp": "203.0.113.55"}]})], AZURE, event_ids=["0:9"])
    assert sink.alerts == []
    assert [s["p_alert_id"] for s in sink.signals] == [alert_id]


def test_every_record_is_uniquely_identified_and_typed():
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE)

    signal_ids = [r["p_signal_id"] for r in sink.signals]
    assert len(set(signal_ids)) == len(signal_ids) and all(signal_ids)
    assert all("p_signal_id" not in r for r in sink.alerts)


def test_a_global_helper_is_importable_from_a_detection():
    """`from pyre_helpers import internal_ip` has to resolve from anywhere in the
    bundle, which only works if the helper's folder reaches sys.path. If this
    breaks, every detection using a helper is silently skipped."""
    proc, sink = build()
    internal = json.dumps({"records": [
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "10.1.2.3"}]})
    external = json.dumps({"records": [
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "8.8.8.8"}]})
    proc.process_batch([internal, external], AZURE, event_ids=["0:10", "0:11"])
    assert [s["dedup"] for s in sink.signals] == ["eh-auth-failure:8.8.8.8"]


# ---- redelivery -------------------------------------------------------------

def test_replaying_the_same_batch_is_suppressed():
    proc, sink = build()
    messages = sample_messages()
    proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])
    sink.records.clear()
    proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])
    assert sink.records == []


def test_envelope_records_get_independent_redelivery_ids():
    """A message id alone would mark ALL of a message's records seen at once. The
    ids must be per record, and stable across a redelivery of that message."""
    proc, sink = build()
    msg = json.dumps({"records": [
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "1.1.1.1"},
        {"Category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "ClientIp": "2.2.2.2"},
    ]})
    proc.process_batch([msg], AZURE, event_ids=["0:500"])
    assert len(sink.signals) == 2

    sink.records.clear()
    proc.process_batch([msg], AZURE, event_ids=["0:500"])
    assert sink.records == []


# ---- per-source field config ------------------------------------------------

def test_two_sources_with_different_shapes_share_one_processor():
    """The reason field config is per source: the same engine has to route an
    Azure envelope keyed on `Category` and a flat normalized record keyed on
    `dataset`, in the same deployment."""
    proc, sink = build()
    flat = Source(hub="normalized-in", log_type_field="dataset",
                  event_time_field="_time", envelope_field="")

    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
    assert len(sink.signals) == 4

    sink.records.clear()
    proc.process_batch([json.dumps({
        "dataset": "RuntimeAuditLogs", "_time": "2026-07-31T00:00:00Z",
        "ActivityStatus": "Failure", "ClientIp": "9.9.9.9"})], flat)
    assert len(sink.signals) == 1
    assert sink.signals[0]["event_time"] == "2026-07-31T00:00:00Z"


def test_unwrap_handles_the_three_message_shapes():
    assert _unwrap({"a": 1}, "records") == [{"a": 1}]                       # single record
    assert _unwrap([{"a": 1}, {"b": 2}], "records") == [{"a": 1}, {"b": 2}]  # JSON array
    assert _unwrap({"records": [{"a": 1}]}, "records") == [{"a": 1}]        # Azure envelope
    # A `records` key that isn't a list is data, not an envelope.
    assert _unwrap({"records": "nope"}, "records") == [{"records": "nope"}]
    # No envelope field configured: the whole message is one event again.
    assert _unwrap({"records": [{"a": 1}]}, "") == [{"records": [{"a": 1}]}]


def test_a_wrong_log_type_field_produces_no_signals_and_says_so(caplog):
    """The single most common misconfiguration. It must be loud, and it must name
    the field, or it looks exactly like a broken engine."""
    proc, sink = build()
    wrong = Source(hub="logs-in", log_type_field="category")   # lowercase; data has Category
    proc.process_batch(sample_messages(), wrong)
    assert sink.records == []
    assert "had no value in the log-type field 'category'" in caplog.text


def test_an_unrouted_log_type_is_named_in_the_logs(caplog):
    proc, _sink = build()
    proc.process_batch([json.dumps({"Category": "SomeOtherLogs", "x": 1})], AZURE)
    assert "SomeOtherLogs" in caplog.text


# ---- state: the two backends must agree -------------------------------------

def test_memory_and_redis_state_produce_identical_results():
    """The claim behind running without Redis in the POC: what you demo is what
    ships. fakeredis drives redis-py, so StateStore's real Redis calls run."""
    fakeredis = pytest.importorskip("fakeredis")
    from pyre_engine.state import MemoryClient, StateStore

    def run(client):
        proc, sink = build(state=StateStore(client))
        proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
        return ([(r["detection_id"], r["dedup"], r["p_alert_id"] is not None) for r in sink.signals],
                [(r["detection_id"], r["severity"], r["dedup"]) for r in sink.alerts])

    memory = run(MemoryClient())
    redis_ = run(fakeredis.FakeStrictRedis(decode_responses=True))
    assert memory == redis_
    assert len(memory[0]) == 4 and len(memory[1]) == 1


def test_memory_client_matches_the_redis_semantics_the_engine_relies_on():
    from pyre_engine.state import MemoryClient

    c = MemoryClient()
    # SET NX: the second write must not apply. This is the atomic alert claim.
    assert c.set("a", "1", nx=True, ex=60) is True
    assert c.set("a", "2", nx=True, ex=60) is None
    assert c.get("a") == "1"
    # INCR counts from 1 on a missing key: the dedup counter.
    assert [c.incr("n"), c.incr("n"), c.incr("n")] == [1, 2, 3]
    # EXPIRE NX starts the window once and never slides it.
    assert c.expire("n", 60, nx=True) is True
    assert c.expire("n", 999, nx=True) is False
    # PFADD/PFCOUNT back unique() thresholds.
    assert [c.pfadd("u", "x"), c.pfadd("u", "x"), c.pfadd("u", "y")] == [1, 0, 1]
    assert c.pfcount("u") == 2


def test_memory_pipeline_returns_one_result_per_queued_command():
    """The processor indexes into execute()'s results by position - [incr,
    expire] for count mode, [pfadd, expire, pfcount] for unique mode - so the
    arity here is load-bearing."""
    from pyre_engine.state import MemoryClient

    pipe = MemoryClient().pipeline()
    pipe.incr("d")
    pipe.expire("d", 60, nx=True)
    pipe.pfadd("u", "a")
    pipe.expire("u", 60, nx=True)
    pipe.pfcount("u")
    assert pipe.execute() == [1, True, 1, True, 1]
    assert pipe.execute() == []                  # queue cleared after execute


def test_state_backend_follows_redis_host():
    assert RuntimeConfig(redis_host="").state_backend == "memory"
    assert RuntimeConfig(redis_host="x.redis.cache.windows.net").state_backend == "redis"


def test_unique_counts_distinct_values_not_matches(tmp_path):
    """Panther's unique(): `Threshold: 3` means three DIFFERENT values, so ten
    matches from one IP must not alert but three IPs must."""
    (tmp_path / "u.py").write_text(
        "def rule(e): return True\n"
        "def dedup(e): return 'fixed'\n"
        "def unique(e): return e['ip']\n")
    (tmp_path / "u.yml").write_text(
        "AnalysisType: rule\nRuleID: u\nFilename: u.py\nLogTypes: [T]\nThreshold: 3\n")

    proc, sink = build(bundle=str(tmp_path))
    src = Source(hub="h", log_type_field="lt", envelope_field="")
    same = [json.dumps({"lt": "T", "ip": "1.1.1.1", "n": i}) for i in range(10)]
    proc.process_batch(same, src)
    assert len(sink.signals) == 10 and sink.alerts == []

    sink.records.clear()
    proc.process_batch([json.dumps({"lt": "T", "ip": ip}) for ip in ("2.2.2.2", "3.3.3.3")], src)
    assert len(sink.alerts) == 1                 # third distinct IP crosses the threshold
