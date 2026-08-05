"""The detection path, end to end: an Event Hub message in, signals and alerts
out. If these pass, the engine is not the problem."""
import json

import pytest

from conftest import DAC, sample_messages
from pyre_engine.config import RuntimeConfig, Source
from pyre_engine.processor import Processor, _unwrap

# A real Azure shape: diagnostic settings wrap records in `records`, and Event
# Hubs runtime audit records use PascalCase field names.
AZURE = Source(hub="logs-in", namespace="platform", log_type_field="Category",
               event_time_field="Timestamp")


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


def build(bundle=DAC, state=None, **overrides):
    cfg = RuntimeConfig(sources=[AZURE], detections_source="local",
                        detections_local_dir=bundle, detections_refresh_seconds=0,
                        state_backend="memory", redis_host="",
                        signal_destination="none", alert_destination="none", **overrides)
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
    assert "203.0.113.55" in sink.alerts[0]["p_title"]
    assert {s["p_dedup"] for s in sink.signals} == {
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
    noisy = [s for s in sink.signals if s["p_dedup"].endswith("203.0.113.55")]
    quiet = [s for s in sink.signals if s["p_dedup"].endswith("198.51.100.77")]

    # Threshold 3: the first two are below it, the third raises the alert.
    assert [s["p_alert_id"] for s in noisy] == [None, None, alert_id]
    assert [s["p_alert_id"] for s in quiet] == [None]
    # The alert names the exact signal that raised it.
    assert sink.alerts[0]["p_first_signal_id"] == noisy[2]["p_signal_id"]

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
    assert [s["p_dedup"] for s in sink.signals] == ["eh-auth-failure:8.8.8.8"]


# ---- the record schema ------------------------------------------------------
# docs/signals-and-alerts.md documents these field lists. A field added to
# records.py without being documented, or removed while a consumer still reads
# it, fails here.

SIGNAL_FIELDS = {
    "p_record_type", "p_signal_id", "p_alert_id",
    "p_detection_id", "p_detection_name", "p_severity", "p_tags", "p_reports",
    "p_log_type", "p_source_namespace", "p_source_hub",
    "p_dedup", "p_event_time", "p_processed_time", "p_event",
}
ALERT_FIELDS = {
    "p_record_type", "p_alert_id",
    "p_detection_id", "p_detection_name", "p_severity", "p_title", "p_description",
    "p_runbook", "p_reference", "p_tags", "p_reports",
    "p_log_type", "p_source_namespace", "p_source_hub",
    "p_dedup", "p_threshold", "p_dedup_period_minutes", "p_signal_count",
    "p_first_signal_id", "p_first_event_time", "p_created_time", "p_context", "p_event",
}


def test_a_signal_carries_every_documented_field():
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
    signal = sink.signals[0]

    assert SIGNAL_FIELDS <= set(signal)
    # Anything extra must be a p_any_* pivot field - nothing else may appear at
    # the top level, or a consumer's schema breaks silently.
    assert all(k.startswith("p_any_") for k in set(signal) - SIGNAL_FIELDS)
    assert signal["p_source_namespace"] == "platform" and signal["p_source_hub"] == "logs-in"
    assert signal["p_log_type"] == "RuntimeAuditLogs"
    assert signal["p_event_time"] == "2026-07-31T14:00:00.1234567Z"
    assert signal["p_processed_time"].endswith("Z")          # the engine's own clock
    assert signal["p_event"]["ClientIp"] == "203.0.113.55"   # the raw record, untouched


def test_an_alert_is_self_sufficient_for_case_creation():
    """Everything a case tool needs without joining back to the signals stream:
    what fired, how bad, where from, what to do about it, and one example event."""
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
    alert = sink.alerts[0]

    assert ALERT_FIELDS <= set(alert)
    assert all(k.startswith("p_any_") for k in set(alert) - ALERT_FIELDS)
    assert alert["p_detection_name"] == "Repeated Event Hub Authorization Failures"
    assert alert["p_runbook"] and alert["p_description"] and alert["p_reference"]
    assert alert["p_tags"] == ["Azure", "EventHub"]
    assert alert["p_reports"]["MITRE ATT&CK"] == ["TA0006:T1110"]
    assert alert["p_log_type"] == "RuntimeAuditLogs"
    assert alert["p_source_namespace"] == "platform"
    # The threshold context that fired it.
    assert (alert["p_threshold"], alert["p_signal_count"]) == (3, 3)
    assert alert["p_dedup_period_minutes"] == 60
    assert alert["p_created_time"].endswith("Z")
    assert alert["p_event"]["ClientIp"] == "203.0.113.55"


def test_indicators_become_p_any_fields_on_both_records():
    """`p_any_*` is what makes "everything involving this IP" work across log
    types that spell the field differently."""
    proc, sink = build()
    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])

    for record in (sink.signals[0], sink.alerts[0]):
        assert record["p_any_ip_addresses"] == ["203.0.113.55"]
        assert record["p_any_actor_ids"] == ["RootManageSharedAccessKey"]


def test_indicators_are_normalized_and_a_bad_one_costs_only_itself(tmp_path):
    from pyre_engine.records import normalize_indicators

    # Both spellings work, values become a sorted list of strings, and empties go.
    assert normalize_indicators({"ip_addresses": "1.1.1.1"}) == {"p_any_ip_addresses": ["1.1.1.1"]}
    assert normalize_indicators({"p_any_usernames": ["b", "a", "a"]}) == {
        "p_any_usernames": ["a", "b"]}
    assert normalize_indicators({"x": None, "y": [], "z": [""]}) == {}
    # A detection returning nonsense loses its indicators, not its record.
    assert normalize_indicators("not a dict") == {}
    assert normalize_indicators(None) == {}


def test_a_detection_without_optional_functions_still_produces_both_records(tmp_path):
    """`rule()` is the only requirement. Everything else must fall back rather
    than raise, or a minimal detection takes the batch down."""
    (tmp_path / "m.py").write_text("def rule(e): return True\n")
    (tmp_path / "m.yml").write_text(
        "AnalysisType: rule\nRuleID: minimal\nFilename: m.py\nLogTypes: [T]\n")

    proc, sink = build(bundle=str(tmp_path))
    src = Source(hub="h", log_type_field="lt", envelope_field="")
    proc.process_batch([json.dumps({"lt": "T"})], src)

    assert SIGNAL_FIELDS <= set(sink.signals[0])
    assert ALERT_FIELDS <= set(sink.alerts[0])
    assert sink.alerts[0]["p_title"] == "minimal"        # falls back to the RuleID
    assert sink.alerts[0]["p_severity"] == "INFO"        # the YAML default
    assert sink.alerts[0]["p_context"] == {}
    assert sink.signals[0]["p_dedup"] == "minimal"       # dedup falls back to the title


# ---- redelivery -------------------------------------------------------------

def test_replaying_the_same_batch_is_suppressed():
    proc, sink = build()
    messages = sample_messages()
    proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])
    sink.records.clear()
    proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])
    assert sink.records == []


def test_a_redelivery_is_counted_once_not_logged_per_event(caplog):
    """The per-event version of this line is loudest exactly when a redelivery
    storm makes the logs least readable."""
    import logging
    proc, _sink = build()
    messages = sample_messages()
    proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])
    with caplog.at_level(logging.INFO, logger="pyre.processor"):
        proc.process_batch(messages, AZURE, event_ids=["0:1", "0:2", "0:3"])

    redelivery_lines = [r for r in caplog.records if "redelivery" in r.message]
    assert len(redelivery_lines) == 1
    assert "7 event(s)" in caplog.text


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
    flat = Source(hub="normalized-in", namespace="network", log_type_field="dataset",
                  event_time_field="_time", envelope_field="")

    proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
    assert len(sink.signals) == 4
    assert {s["p_source_hub"] for s in sink.signals} == {"logs-in"}

    sink.records.clear()
    proc.process_batch([json.dumps({
        "dataset": "RuntimeAuditLogs", "_time": "2026-07-31T00:00:00Z",
        "ActivityStatus": "Failure", "ClientIp": "9.9.9.9"})], flat)
    assert len(sink.signals) == 1
    assert sink.signals[0]["p_event_time"] == "2026-07-31T00:00:00Z"
    # Every record says which source produced it, which is what makes one
    # destination readable when twenty sources write into it.
    assert sink.signals[0]["p_source_namespace"] == "network"
    assert sink.signals[0]["p_source_hub"] == "normalized-in"


def test_a_blank_log_type_field_routes_by_hub_name(tmp_path):
    """Azure-native diagnostic logs carry no dataset-like field at all.
    `log_type_field: ""` is how such a source still routes: every record uses
    the source's own hub name as its log type."""
    (tmp_path / "m.py").write_text("def rule(e): return True\n")
    (tmp_path / "m.yml").write_text(
        "AnalysisType: rule\nRuleID: by-hub\nFilename: m.py\nLogTypes: [h]\n")

    proc, sink = build(bundle=str(tmp_path))
    src = Source(hub="h", log_type_field="", envelope_field="")
    proc.process_batch([json.dumps({"x": 1})], src)

    assert len(sink.signals) == 1
    assert sink.signals[0]["p_log_type"] == "h"


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
    """Informational, not a warning: covering fewer log types than a source
    carries is normal, not itself a problem to page on."""
    import logging
    proc, _sink = build()
    with caplog.at_level(logging.INFO, logger="pyre.processor"):
        proc.process_batch([json.dumps({"Category": "SomeOtherLogs", "x": 1})], AZURE)
    assert "SomeOtherLogs" in caplog.text
    unrouted = [r for r in caplog.records if "SomeOtherLogs" in r.message]
    assert unrouted and unrouted[0].levelname == "INFO"


def test_every_batch_reports_what_it_did(caplog):
    """The one line that answers "is it running?" in Azure. One per invocation,
    whatever the volume."""
    import logging
    proc, _sink = build()
    with caplog.at_level(logging.INFO, logger="pyre.processor"):
        proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])

    lines = [r.message for r in caplog.records if r.message.startswith("batch ")]
    assert len(lines) == 1
    assert "platform/logs-in" in lines[0]
    assert "msgs=3 events=7 new=7 signals=4 alerts=1" in lines[0]


def test_a_detection_that_raises_is_isolated_and_logged_once(caplog, tmp_path):
    """One broken detection must not take the batch, and must not emit a stack
    trace per event either."""
    (tmp_path / "boom.py").write_text("def rule(e): raise ValueError('boom')\n")
    (tmp_path / "boom.yml").write_text(
        "AnalysisType: rule\nRuleID: boom\nFilename: boom.py\nLogTypes: [T]\n")
    (tmp_path / "ok.py").write_text("def rule(e): return True\n")
    (tmp_path / "ok.yml").write_text(
        "AnalysisType: rule\nRuleID: ok\nFilename: ok.py\nLogTypes: [T]\n")

    proc, sink = build(bundle=str(tmp_path))
    src = Source(hub="h", log_type_field="lt", envelope_field="")
    proc.process_batch([json.dumps({"lt": "T", "n": i}) for i in range(5)], src)

    # The healthy detection still ran on every event.
    assert len(sink.signals) == 5
    # One traceback, then a counted summary - not five tracebacks.
    assert len([r for r in caplog.records if r.exc_info]) == 1
    assert "boom (5x)" in caplog.text


# ---- state: the two backends must agree -------------------------------------

def test_memory_and_redis_state_produce_identical_results():
    """The claim behind running without Redis on a single instance: what you try
    is what ships. fakeredis drives redis-py, so StateStore's real Redis calls
    run."""
    fakeredis = pytest.importorskip("fakeredis")
    from pyre_engine.state import MemoryClient, StateStore

    def run(client):
        proc, sink = build(state=StateStore(client))
        proc.process_batch(sample_messages(), AZURE, event_ids=["0:1", "0:2", "0:3"])
        return ([(r["p_detection_id"], r["p_dedup"], r["p_alert_id"] is not None)
                 for r in sink.signals],
                [(r["p_detection_id"], r["p_severity"], r["p_dedup"]) for r in sink.alerts])

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


def test_a_dedup_string_cannot_reach_into_the_key_namespace():
    """Dedup strings are detection-authored and derived from event data, so they
    routinely contain ':' (an IP:port, a URL, a DN). Interpolated raw, two
    different detections could collide on one counter."""
    from pyre_engine.state import _scope

    assert _scope("a", "b:c") != _scope("a:b", "c")
    assert ":" not in _scope("det", "1.2.3.4:443").split(":", 1)[1]
    # Same inputs, same key - the window has to be findable again.
    assert _scope("det", "x") == _scope("det", "x")


def test_state_backend_is_chosen_by_its_own_setting():
    """Not by whether REDIS_HOST happens to be set: two settings that can
    disagree is exactly the mode confusion this design removes."""
    assert RuntimeConfig(state_backend="memory", redis_host="x").state_backend == "memory"
    assert RuntimeConfig(state_backend="redis", redis_host="x").state_backend == "redis"


def test_a_storm_dropped_alert_leaves_no_claim_behind(tmp_path):
    """The storm limiter must not consume the dedup claim. Claiming first would
    leave an `alert:` marker for an alert that was never written, and every later
    match in the window would then be stamped with an alert id that exists
    nowhere."""
    (tmp_path / "s.py").write_text(
        "def rule(e): return True\n"
        "def dedup(e): return e['k']\n")
    (tmp_path / "s.yml").write_text(
        "AnalysisType: rule\nRuleID: stormy\nFilename: s.py\nLogTypes: [T]\n")

    proc, sink = build(bundle=str(tmp_path), alert_storm_limit_per_hour=1)
    src = Source(hub="h", log_type_field="lt", envelope_field="")
    proc.process_batch([json.dumps({"lt": "T", "k": k}) for k in ("a", "b")], src)

    # The limit is 1/hour, so 'a' alerts and 'b' is dropped.
    assert [a["p_dedup"] for a in sink.alerts] == ["a"]
    dropped = [s for s in sink.signals if s["p_dedup"] == "b"]
    # The dropped one's signal must NOT point at a nonexistent alert.
    assert [s["p_alert_id"] for s in dropped] == [None]
    # And no claim was left behind, so 'b' can still alert once the limit lifts.
    assert proc.state.alert_exists("stormy", "b") is None


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
