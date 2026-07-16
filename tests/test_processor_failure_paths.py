"""What happens when things go wrong mid-batch.

Every test here pins a failure mode that used to be SILENT - the pipeline
carried on, checkpointed, and the evidence of a detection match simply ceased
to exist with nothing logged and nothing raised. For a security tool those are
the worst bugs available, so they get the most tests.

The shape throughout is DDIA ch.11's "exactly-once in practice": the transport
is at-least-once and the sinks aren't transactional, so the engine claims work,
does it, and RELEASES the claim if it didn't finish - preferring a duplicate
over a loss.
"""
import json

import fakeredis
import pytest

from pyre_engine import signals as signals_mod
from pyre_engine.config import load_runtime_config
from pyre_engine.dedup import StateStore
from pyre_engine.processor import Processor

EVENT = json.dumps({"dataset": "T.A", "_time": "t0", "src": "1.2.3.4"})


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Sink:
    """Records what reached the 'lake', and can be told to fail like Cribl would."""

    def __init__(self, status=200):
        self.status = status
        self.posted = []

    def post(self, url, json, timeout):
        if self.status < 400:
            self.posted.extend(json)
        return _Resp(self.status)

    @property
    def signals(self):
        return [r for r in self.posted if r["_dataset"] == "pyre_signals"]


def _bundle(tmp_path, py, rule_id="d1", log_type="T.A", extra_yml=""):
    d = tmp_path / "bundle"
    d.mkdir(exist_ok=True)
    (d / f"{rule_id}.py").write_text(py)
    (d / f"{rule_id}.yml").write_text(
        f"AnalysisType: rule\nRuleID: {rule_id}\nFilename: {rule_id}.py\n"
        f"LogTypes: [{log_type}]\nSeverity: High\n{extra_yml}")
    return d


def _processor(tmp_path, monkeypatch, bundle):
    monkeypatch.setenv("PYRE_ENV", "dev")
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", "http://sink.invalid/signals")
    monkeypatch.setenv("DESTINATIONS_PATH", str(tmp_path / "no-destinations.yaml"))
    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "0")
    cfg = load_runtime_config()
    state = StateStore("", 0, use_entra=False,
                       client=fakeredis.FakeStrictRedis(decode_responses=True))
    proc = Processor(cfg, state=state)
    proc.dispatcher.send = lambda alert, routes: None      # delivery tested separately
    return proc


# --- the big one: a failed batch must not silently vanish --------------------

def test_failed_batch_releases_claims_so_the_redelivery_reprocesses(tmp_path, monkeypatch):
    """Phase 0 claims every event id BEFORE the work. If a later phase dies, the
    claim has to come back - otherwise Event Hubs redelivers the batch, every id
    reads as already-processed, the batch is skipped clean, and the match is gone
    forever with no error anywhere. This is that whole story in one test."""
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"))

    # 1) Cribl is down: the flush 5xx must fail the batch.
    monkeypatch.setattr(signals_mod, "_SESSION", _Sink(status=503))
    with pytest.raises(Exception):
        proc.process_batch([EVENT], event_ids=["evt-1"])

    # 2) Event Hubs redelivers the same batch; Cribl is back.
    good = _Sink(status=200)
    monkeypatch.setattr(signals_mod, "_SESSION", good)
    proc.process_batch([EVENT], event_ids=["evt-1"])

    assert len(good.signals) == 1, (
        "the redelivered batch must actually reprocess; if the claim from the "
        "failed attempt survived, this match is silently lost forever")


def test_successful_batch_still_suppresses_a_genuine_redelivery(tmp_path, monkeypatch):
    # The flip side: release-on-failure must not weaken at-least-once dedup when
    # the batch SUCCEEDED. A redelivery of completed work stays suppressed.
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"))
    sink = _Sink()
    monkeypatch.setattr(signals_mod, "_SESSION", sink)

    proc.process_batch([EVENT], event_ids=["evt-1"])
    proc.process_batch([EVENT], event_ids=["evt-1"])      # redelivery
    assert len(sink.signals) == 1, "a redelivered, already-processed event must not re-signal"


def test_signal_sink_5xx_raises_instead_of_silently_dropping(tmp_path, monkeypatch):
    # requests does not raise on 5xx. Without raise_for_status a Cribl outage was
    # indistinguishable from success: signals dropped, batch checkpointed.
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"))
    monkeypatch.setattr(signals_mod, "_SESSION", _Sink(status=500))
    with pytest.raises(Exception):
        proc.process_batch([EVENT], event_ids=["evt-1"])


# --- poison pills: one bad detection must not take the batch down ------------

def test_a_detection_raising_in_rule_does_not_stop_the_others(tmp_path, monkeypatch):
    d = _bundle(tmp_path, "def rule(event): raise ValueError('boom')\n", rule_id="bad")
    _bundle(tmp_path, "def rule(event): return True\n", rule_id="good")
    proc = _processor(tmp_path, monkeypatch, d)
    sink = _Sink()
    monkeypatch.setattr(signals_mod, "_SESSION", sink)

    proc.process_batch([EVENT], event_ids=["evt-1"])
    assert [s["detection_id"] for s in sink.signals] == ["good"]


def test_a_detection_raising_in_title_does_not_kill_the_batch(tmp_path, monkeypatch):
    # title/dedup/severity/alert_context are detection-authored too. Only rule()
    # was guarded, so a title() that raised escaped process_batch, failed the
    # invocation, and - because Event Hubs redelivers - poisoned the partition:
    # every retry hit the same event and died the same way.
    d = _bundle(tmp_path, "def rule(event): return True\n"
                          "def title(event): raise ValueError('boom')\n", rule_id="bad")
    _bundle(tmp_path, "def rule(event): return True\n", rule_id="good")
    proc = _processor(tmp_path, monkeypatch, d)
    sink = _Sink()
    monkeypatch.setattr(signals_mod, "_SESSION", sink)

    proc.process_batch([EVENT], event_ids=["evt-1"])       # must not raise
    assert [s["detection_id"] for s in sink.signals] == ["good"]


def test_a_non_string_dedup_does_not_kill_the_batch(tmp_path, monkeypatch):
    # `(det.dedup(event) or det.title(event))[:1000]` raised TypeError on an int.
    d = _bundle(tmp_path, "def rule(event): return True\n"
                          "def dedup(event): return 12345\n")
    proc = _processor(tmp_path, monkeypatch, d)
    sink = _Sink()
    monkeypatch.setattr(signals_mod, "_SESSION", sink)

    proc.process_batch([EVENT], event_ids=["evt-1"])
    assert sink.signals[0]["dedup"] == "12345"


# --- an undelivered alert must not be lost, and must not kill the audit ------

def test_undelivered_alert_reopens_its_window_so_a_later_match_refires(tmp_path, monkeypatch):
    """register_alert claims the dedup window BEFORE dispatch. If delivery fails
    and the marker stands, it says "already alerted" for the rest of the window,
    so every later match is grouped under an alert nobody ever received - the
    detection goes quiet for an hour and nothing says so."""
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"
                                        "def dedup(event): return 'same-key'\n"))
    sink = _Sink()
    monkeypatch.setattr(signals_mod, "_SESSION", sink)

    attempts = []

    def failing_send(alert, routes):
        attempts.append(alert.alert_id)
        raise RuntimeError("torq is down")

    proc.dispatcher.send = failing_send
    proc.process_batch([EVENT], event_ids=["evt-1"])       # must NOT raise
    assert len(attempts) == 1
    assert len(sink.signals) == 1, "the signal is the record of record; it survives a dispatch failure"

    proc.process_batch([EVENT], event_ids=["evt-2"])       # a later match
    assert len(attempts) == 2, "the re-opened window must let the alert fire again"


def test_a_delivered_alert_still_dedups_within_its_window(tmp_path, monkeypatch):
    # The flip side of the test above: re-opening on FAILURE must not break
    # first-event-wins grouping on success.
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"
                                        "def dedup(event): return 'same-key'\n"))
    monkeypatch.setattr(signals_mod, "_SESSION", _Sink())
    sent = []
    proc.dispatcher.send = lambda alert, routes: sent.append(alert.alert_id)

    proc.process_batch([EVENT], event_ids=["evt-1"])
    proc.process_batch([EVENT], event_ids=["evt-2"])
    assert len(sent) == 1, "matches sharing a dedup string collapse into ONE alert"


# --- silent drops are now countable -----------------------------------------

def test_malformed_json_is_reported_not_silently_dropped(tmp_path, monkeypatch, caplog):
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"))
    monkeypatch.setattr(signals_mod, "_SESSION", _Sink())

    with caplog.at_level("WARNING"):
        proc.process_batch(["{not json", EVENT], event_ids=["e1", "e2"])
    assert "1 malformed json" in caplog.text


def test_event_with_no_log_type_is_reported(tmp_path, monkeypatch, caplog):
    proc = _processor(tmp_path, monkeypatch,
                      _bundle(tmp_path, "def rule(event): return True\n"))
    monkeypatch.setattr(signals_mod, "_SESSION", _Sink())

    with caplog.at_level("WARNING"):
        proc.process_batch([json.dumps({"no": "dataset"})], event_ids=["e1"])
    assert "missing the 'log type' field" in caplog.text
