"""SignalWriter under concurrent batches.

function_app.py builds ONE Processor per worker and the Python worker runs sync
invocations on a thread pool, so an instance owning several Event Hub partitions
calls add_signal()/flush() on this single object from several threads at once.
A signal is the audit record of a rule match, so losing one is silent evidence
loss - these tests pin that it can't happen.
"""
import threading

from pyre_engine.models import Signal
from pyre_engine.signals import SignalWriter


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Recorder:
    """Stands in for the pooled session. Serialises slowly on purpose, so a
    concurrent append lands squarely in the window where the old
    read-post-then-clear() implementation dropped records."""

    def __init__(self, status=200):
        self.posted = []
        self.status = status
        self.lock = threading.Lock()

    def post(self, url, json, timeout):
        snapshot = list(json)          # what a real serializer would send
        threading.Event().wait(0.01)   # hold the "connection" open a beat
        with self.lock:
            self.posted.extend(snapshot)
        return _Resp(self.status)


def _signal(i):
    return Signal(detection_id=f"d{i}", log_type="T.A", dedup_string=f"k{i}",
                  event_time="", event_ref={}, p_fields={})


def _writer(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("pyre_engine.signals._SESSION", rec)
    return SignalWriter("http://sink.invalid/signals"), rec


def test_no_signal_is_lost_when_batches_overlap(monkeypatch):
    w, rec = _writer(monkeypatch)
    total = 40

    def worker(start):
        for i in range(start, start + 10):
            w.add_signal(_signal(i))
        w.flush()

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(0, total, 10)]
    for t in threads: t.start()
    for t in threads: t.join()
    w.flush()

    ids = sorted(int(p["detection_id"][1:]) for p in rec.posted)
    assert ids == list(range(total)), "every signal must reach the sink exactly once"


def test_flush_sends_each_signal_only_once(monkeypatch):
    # The buffer swap must transfer ownership: a signal picked up by one flush
    # must not still be sitting there for the next one to send again.
    w, rec = _writer(monkeypatch)
    w.add_signal(_signal(1))
    w.flush()
    w.flush()
    assert len(rec.posted) == 1


def test_flush_with_no_signals_does_not_post(monkeypatch):
    w, rec = _writer(monkeypatch)
    w.flush()
    assert rec.posted == []
