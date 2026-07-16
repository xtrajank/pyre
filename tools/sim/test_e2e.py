"""End-to-end simulation of the whole pyre pipeline against real infrastructure.

Each test names the property it defends, in the same terms the design docs use.
The engine code under test is unmodified; only Redis/Cribl/Torq/the DaC repo are
stood up locally (see conftest.py).

    docker compose -f tools/sim/docker-compose.yml run --rm sim
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

REPO = "/pyre"


# =============================================================================
# 1. The state store, against a REAL Redis 6.0
# =============================================================================
#
# fakeredis accepts Redis 7.0 syntax that Azure Cache for Redis 6.0 rejects, so
# these are the assertions the existing suite structurally cannot make.

def test_dedup_lua_runs_on_redis_60(state, redis_client):
    """The count/TTL script is Lua precisely so it works on 6.0, where the
    one-command alternative (`EXPIRE key ttl NX`) does not exist."""
    pipe = state.pipeline()
    state.bump_dedup(pipe, "det1", "a:b", 60)
    assert pipe.execute() == [1]

    pipe = state.pipeline()
    state.bump_dedup(pipe, "det1", "a:b", 60)
    assert pipe.execute() == [2], "the window must count, not reset"


def test_expire_nx_really_is_rejected_by_redis_60(redis_client):
    """The premise behind writing the windows in Lua at all.

    If this ever stops raising, Azure has moved and the Lua could be simplified;
    until then this is the evidence that fakeredis passing proves nothing here.
    """
    redis_client.set("probe", "1")
    with pytest.raises(redis_lib_error()):
        redis_client.execute_command("EXPIRE", "probe", "60", "NX")


def redis_lib_error():
    import redis
    return redis.exceptions.ResponseError


def test_every_state_key_gets_a_ttl(state, redis_client):
    """State is bounded on purpose: an INCR whose EXPIRE was lost leaks a key
    for the life of the cache. Nothing this engine writes may be immortal."""
    pipe = state.pipeline()
    state.bump_dedup(pipe, "det1", "x", 60)
    state.bump_unique(pipe, "det2", "y", "val", 60)
    pipe.execute()
    state.register_alert("det1", "x", "alert-1", 60)
    state.storm_ok("det1", "2026071500", 100)
    p = state.pipeline()
    state.is_new_event(p, "evt-1", 900)
    p.execute()

    keys = redis_client.keys("*")
    assert keys, "expected the state store to have written something"
    immortal = [k for k in keys if redis_client.ttl(k) < 0]
    assert immortal == [], f"these keys would never expire: {immortal}"


def test_unique_counts_distinct_values_not_matches(state):
    """unique() is Panther's 'N distinct values' mode - 5 matches from one IP
    must not clear a 3-distinct-IP threshold."""
    for _ in range(5):
        pipe = state.pipeline()
        state.bump_unique(pipe, "det", "win", "10.0.0.1", 60)
        count = pipe.execute()[0]
    assert count == 1, "the same value repeated is still one distinct value"

    for ip in ("10.0.0.2", "10.0.0.3"):
        pipe = state.pipeline()
        state.bump_unique(pipe, "det", "win", ip, 60)
        count = pipe.execute()[0]
    assert count == 3


def test_dedup_key_is_bounded_regardless_of_dedup_string_length(state, redis_client):
    """Dedup strings are detection-authored and only capped at 1000 chars.
    Embedding one raw put ~1KB in every window key, per distinct value, per
    detection, for the whole window."""
    pipe = state.pipeline()
    state.bump_dedup(pipe, "det", "A" * 1000, 60)
    pipe.execute()
    key = redis_client.keys("dd:*")[0]
    assert len(key) < 80, f"window key grew with the dedup string: {len(key)} chars"


def test_register_alert_is_first_event_wins(state):
    assert state.register_alert("det", "same-window", "alert-1", 60) is True
    assert state.register_alert("det", "same-window", "alert-2", 60) is False


def test_storm_limiter_suppresses_past_the_budget(state):
    hour = "2026071512"
    assert all(state.storm_ok("noisy", hour, 3) for _ in range(3))
    assert state.storm_ok("noisy", hour, 3) is False


# =============================================================================
# 2. DaC: pull -> validate -> deps -> build, from a real git repo
# =============================================================================

def _pyre(args, env=None, cwd=REPO):
    e = dict(os.environ)
    e["PYTHONPATH"] = f"{REPO}/engine:{REPO}/cli"
    e.update(env or {})
    return subprocess.run([sys.executable, os.path.join(REPO, "cli", "pyre")] + args,
                          cwd=cwd, env=e, capture_output=True, text=True)


@pytest.fixture
def pulled_bundle(dac_origin, tmp_path):
    """A real `pyre pull` against a real git repo - the publish-side clone."""
    bundle = tmp_path / "bundle"
    env = {
        "DAC_REPO": f"file://{dac_origin}",
        "DAC_REF": "main",
        "BUNDLE_LOCAL_DIR": str(bundle),
    }
    r = _pyre(["pull"], env=env)
    assert r.returncode == 0, f"pyre pull failed:\n{r.stdout}\n{r.stderr}"
    return bundle, env


def test_pull_lands_detections_and_helpers_and_stamps_the_sha(pulled_bundle, dac_origin):
    """`pull` clones at a ref, filters to dac.path, copies the sibling
    global_helpers in, and stamps the commit sha the engine uses as its
    bundle version."""
    bundle, _ = pulled_bundle
    assert (bundle / "network" / "palo_high_risk_port.py").exists()
    assert (bundle / "web" / "cloudflare_sqli.py").exists()
    # global_helpers lives OUTSIDE dac.path (rules/), so only the explicit
    # dac.global_helpers copy step puts it in the bundle. Without it every
    # detection importing it would load-fail and silently cover nothing.
    assert (bundle / "global_helpers" / "pyre_shared.py").exists()

    sha = (bundle / ".bundle-version").read_text().strip()
    real = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dac_origin,
                          capture_output=True, text=True).stdout.strip()
    assert sha == real
    assert not (bundle / ".git").exists(), "the bundle must not carry the DaC repo's git history"


def test_pull_does_not_leave_the_token_on_disk(dac_origin, tmp_path):
    """A PAT must never reach argv or a .git/config. `pull` sends it as an
    env-injected Authorization header instead of splicing it into the URL."""
    bundle = tmp_path / "bundle-tok"
    env = {
        "DAC_REPO": f"file://{dac_origin}",
        "DAC_REF": "main",
        "BUNDLE_LOCAL_DIR": str(bundle),
        "DAC_TOKEN": "super-secret-pat-value",
    }
    r = _pyre(["pull"], env=env)
    assert r.returncode == 0
    assert "super-secret-pat-value" not in r.stdout + r.stderr

    leaked = []
    for root, _d, files in os.walk(tmp_path):
        for f in files:
            p = os.path.join(root, f)
            try:
                if "super-secret-pat-value" in open(p, "rb").read().decode("utf-8", "ignore"):
                    leaked.append(p)
            except OSError:
                pass
    assert leaked == [], f"the DaC token was written to disk: {leaked}"


def test_pull_applies_dac_include_and_exclude(dac_origin, tmp_path):
    """dac.include/dac.exclude must actually filter the bundle.

    They were parsed into DacConfig and used by nothing: `pull` copied all of
    dac.path verbatim, so the documented `exclude: ["**/*_tests.py"]` excluded
    nothing. The bundle is what every worker downloads and what the Registry
    YAML-parses on each reload, so this is a real cost lever - and the only way
    to drop a detection the engine cannot run without narrowing dac.path.
    """
    import subprocess as sp
    # A test file of the kind the shipped exclude pattern names, plus a rule.
    (dac_origin / "rules" / "network" / "palo_high_risk_port_tests.py").write_text(
        "def test_noop():\n    pass\n"
    )
    sp.run(["git", "add", "-A"], cwd=dac_origin, capture_output=True)
    sp.run(["git", "commit", "-m", "add a DaC test file"], cwd=dac_origin, capture_output=True)

    bundle = tmp_path / "pruned"
    cfg = tmp_path / "detections.yaml"
    cfg.write_text(f"""
dac:
  repo: file://{dac_origin}
  ref: main
  path: rules
  global_helpers: [global_helpers]
  include: ["**/*.yml", "**/*.yaml", "**/*.py"]
  exclude: ["**/*_tests.py", "**/web/**"]
bundle:
  local_dir: {bundle}
""")
    r = _pyre(["pull"], env={"BUNDLE_LOCAL_DIR": str(bundle),
                            "DETECTIONS_CONFIG_PATH": str(cfg)})
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    assert (bundle / "network" / "palo_high_risk_port.py").exists(), "an included rule was dropped"
    assert not (bundle / "network" / "palo_high_risk_port_tests.py").exists(), \
        "dac.exclude did not exclude **/*_tests.py"
    assert not (bundle / "web" / "cloudflare_sqli.py").exists(), \
        "dac.exclude did not exclude the **/web/** subtree"
    # Helpers are copied in from OUTSIDE dac.path, so pruning must not strip
    # them - every rule importing them would load-fail at runtime if it did.
    assert (bundle / "global_helpers" / "pyre_shared.py").exists(), \
        "pruning removed the global helpers the rules import"


def test_deps_gate_blocks_a_bundle_the_engine_could_not_import(pulled_bundle):
    """The gate that stops a DaC push from silently disabling a detection.

    Detection code hot-reloads in ~45s; engine/requirements.txt only installs on
    deploy. A push adding an unavailable import would go live and then fail to
    load on every worker, whose only symptom is a warning.
    """
    bundle, env = pulled_bundle
    r = _pyre(["deps"], env=env)
    assert r.returncode == 0, f"the clean bundle should pass the gate:\n{r.stdout}"

    (bundle / "network" / "palo_high_risk_port.py").write_text(
        "import a_package_that_does_not_exist\n\n\ndef rule(event):\n    return True\n"
    )
    r = _pyre(["deps"], env=env)
    assert r.returncode == 1, "a bundle importing a missing package must NOT publish"
    assert "a_package_that_does_not_exist" in r.stdout


def test_deps_gate_catches_an_import_hidden_inside_rule_body(pulled_bundle):
    """An import inside rule() fails per-event at detection time, which is worse
    than failing at load time - so it must count the same."""
    bundle, env = pulled_bundle
    (bundle / "network" / "palo_high_risk_port.py").write_text(
        "def rule(event):\n    import another_missing_package\n    return True\n"
    )
    r = _pyre(["deps"], env=env)
    assert r.returncode == 1
    assert "another_missing_package" in r.stdout


def test_deps_gate_never_executes_detection_code(pulled_bundle):
    """The gate ast-parses; it must not import. Importing would run arbitrary
    DaC code in CI."""
    bundle, env = pulled_bundle
    canary = bundle / "PWNED"
    (bundle / "network" / "palo_high_risk_port.py").write_text(
        f"import pathlib\npathlib.Path({str(canary)!r}).write_text('x')\n\n\ndef rule(event):\n    return True\n"
    )
    r = _pyre(["deps"], env=env)
    assert not canary.exists(), "pyre deps EXECUTED detection code from the DaC repo"
    assert r.returncode == 0


# =============================================================================
# 3. The processor: routing, signals, dedup, alerts, dispatch
# =============================================================================

PALO_HIT = {"dataset": "SEC_Network_Palo_Alto_Traffic", "_time": "2026-07-15T00:00:00Z",
            "action": "allow", "dport": 3389, "src_ip": "10.0.0.5", "dst_ip": "8.8.8.8",
            "app": "rdp"}
PALO_MISS = {"dataset": "SEC_Network_Palo_Alto_Traffic", "_time": "2026-07-15T00:00:00Z",
             "action": "allow", "dport": 443, "src_ip": "10.0.0.5"}
CF_HIT = {"dataset": "SEC_Web_Cloudflare", "_time": "2026-07-15T00:00:00Z",
          "uri": "/x?id=1 UNION SELECT password FROM users"}


@pytest.fixture
def processor(pulled_bundle, state, sink, monkeypatch):
    """The real Processor, wired to real Redis, a real HTTP sink, and a bundle
    pulled from a real git repo."""
    bundle, _ = pulled_bundle
    sink.reset()
    monkeypatch.setenv("PYRE_ENV", "dev")
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", sink.signals_url)
    monkeypatch.setenv("DESTINATION_MOCK_URL", sink.dest_url)
    monkeypatch.setenv("DEFAULT_ROUTES", "mock")
    monkeypatch.setenv("DESTINATIONS_PATH", os.path.join(REPO, "config", "destinations.yaml"))
    monkeypatch.setenv("LOG_TYPE_FIELD", "dataset")
    monkeypatch.setenv("EVENT_TIME_FIELD", "_time")

    from pyre_engine.config import load_runtime_config
    from pyre_engine.processor import Processor
    return Processor(load_runtime_config(), state=state)


def test_a_matching_log_produces_signal_alert_and_dispatch(processor, sink):
    """The whole loop: log in -> routed -> rule() -> signal -> dedup ->
    alert -> delivered."""
    processor.process_batch([json.dumps(PALO_HIT)])

    assert len(sink.signals) == 1
    assert sink.signals[0]["detection_id"] == "palo_high_risk_port"
    assert sink.signals[0]["dedup"] == "10.0.0.5:3389"

    assert len(sink.alert_records) == 1
    assert sink.alert_records[0]["severity"] == "HIGH"

    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["rule_id"] == "palo_high_risk_port"


def test_alert_context_is_attached(processor, sink):
    """alert_context() is what makes an alert triageable rather than a bare
    'something fired'."""
    processor.process_batch([json.dumps(PALO_HIT)])
    ctx = sink.dispatched[0]["alert_context"]
    assert ctx == {"src_ip": "10.0.0.5", "dport": 3389, "app": "rdp"}
    assert sink.dispatched[0]["title"].startswith("Palo: allow to high-risk port 3389")


def test_a_non_matching_log_produces_nothing(processor, sink):
    processor.process_batch([json.dumps(PALO_MISS)])
    assert sink.signals == []
    assert sink.dispatched == []


def test_events_are_routed_only_to_their_own_log_types_detections(processor, sink):
    """A Palo event must never execute the Cloudflare rule. This is what keeps
    400+ detections affordable, and the sim asserts it by having the Cloudflare
    rule record every event it is asked about."""
    processor.process_batch([json.dumps(PALO_HIT), json.dumps(PALO_MISS)])

    reg = processor.loader.get()
    cf = [d for d in reg.for_log_type("SEC_Web_Cloudflare")]
    assert cf, "expected the Cloudflare detection to be registered"
    assert cf[0]._m.SEEN == [], "the Cloudflare rule ran against Palo events"


def test_detection_modules_load_lazily_per_log_type(processor):
    """Loading is lazy so a worker fed one log type out of a 900-detection
    bundle imports only what its own traffic can match."""
    reg = processor.loader.get()
    assert "SEC_Web_Cloudflare" in reg._pending
    assert "SEC_Network_Palo_Alto_Traffic" in reg._pending
    assert reg._ready == {}, "nothing may be imported before an event arrives"

    processor.process_batch([json.dumps(PALO_HIT)])

    assert "SEC_Network_Palo_Alto_Traffic" in reg._ready
    assert "SEC_Web_Cloudflare" in reg._pending, "an unseen log type must stay unimported"
    assert "SEC_Web_Cloudflare" not in reg._ready


def test_each_detection_module_is_executed_at_most_once(processor, pulled_bundle):
    """A detection registered under several log types, or hit by many events,
    must exec once - never per event."""
    bundle, _ = pulled_bundle
    marker = bundle / "exec_count.txt"
    (bundle / "network" / "palo_high_risk_port.py").write_text(textwrap.dedent(f'''
        import pathlib
        _p = pathlib.Path({str(marker)!r})
        _p.write_text(str(int(_p.read_text()) + 1 if _p.exists() else 1))


        def rule(event):
            return True
    '''))
    processor.loader._registry = None      # force a rebuild off the edited bundle
    processor.loader._next_check = 0

    processor.process_batch([json.dumps(PALO_HIT) for _ in range(25)])
    assert marker.read_text() == "1", "the detection module was exec'd more than once"


def test_matches_sharing_a_dedup_string_collapse_into_one_alert(processor, sink):
    """Dedup: 10 matches from one src_ip/port pair are one alert, ten signals.
    The signal is the audit trail; the alert is the page."""
    processor.process_batch([json.dumps(PALO_HIT) for _ in range(10)],
                            event_ids=[f"e{i}" for i in range(10)])
    assert len(sink.signals) == 10, "every match must write a signal"
    assert len(sink.alert_records) == 1, "matches in one dedup window are one alert"
    assert len(sink.dispatched) == 1


def test_distinct_dedup_strings_alert_separately(processor, sink):
    a = dict(PALO_HIT, src_ip="10.0.0.5")
    b = dict(PALO_HIT, src_ip="10.0.0.9")
    processor.process_batch([json.dumps(a), json.dumps(b)], event_ids=["a", "b"])
    assert len(sink.alert_records) == 2


def test_a_match_below_threshold_signals_but_does_not_alert(processor, pulled_bundle, sink):
    bundle, _ = pulled_bundle
    (bundle / "network" / "palo_high_risk_port.yml").write_text(
        META_THRESHOLD_3 := textwrap.dedent("""
            AnalysisType: rule
            RuleID: palo_high_risk_port
            Filename: palo_high_risk_port.py
            Enabled: true
            LogTypes: [SEC_Network_Palo_Alto_Traffic]
            Severity: Medium
            Threshold: 3
            DedupPeriodMinutes: 60
            CreateAlert: true
        """)
    )
    processor.loader._registry = None
    processor.loader._next_check = 0

    processor.process_batch([json.dumps(PALO_HIT)] * 2, event_ids=["a", "b"])
    assert len(sink.signals) == 2
    assert sink.alert_records == [], "two matches must not clear a threshold of 3"

    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["c"])
    assert len(sink.alert_records) == 1, "the third match must fire"


# =============================================================================
# 4. No leakage
# =============================================================================

def test_a_redelivered_batch_is_not_processed_twice(processor, sink):
    """Event Hubs is at-least-once. The same batch delivered again must not
    re-sign or re-count."""
    ids = ["p:1", "p:2"]
    batch = [json.dumps(PALO_HIT), json.dumps(PALO_MISS)]

    processor.process_batch(batch, event_ids=ids)
    assert len(sink.signals) == 1

    processor.process_batch(batch, event_ids=ids)     # redelivery
    assert len(sink.signals) == 1, "a redelivered event was evaluated a second time"
    assert len(sink.dispatched) == 1


def test_a_failed_batch_releases_its_claims_so_the_redelivery_does_real_work(processor, sink):
    """The subtle one. The claim is committed BEFORE the work, so if a later
    phase dies the claim must be released - otherwise Event Hubs redelivers,
    every id reads as already-processed, the batch is skipped clean, and the
    matches are gone with nothing logged."""
    sink.fail("signals", 503)     # Cribl outage: flush() raises

    with pytest.raises(Exception):
        processor.process_batch([json.dumps(PALO_HIT)], event_ids=["x:1"])
    assert sink.signals == []

    sink.heal("signals")
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["x:1"])   # redelivery
    assert len(sink.signals) == 1, "the match was lost: its claim outlived the failed batch"


def test_a_cribl_outage_is_not_mistaken_for_success(processor, sink):
    """requests does not raise on 5xx. Without an explicit status check a Cribl
    outage is byte-for-byte indistinguishable from a successful write."""
    sink.fail("signals", 500)
    with pytest.raises(Exception):
        processor.process_batch([json.dumps(PALO_HIT)], event_ids=["y:1"])


def test_a_failed_dispatch_reopens_the_alert_window(processor, sink):
    """register_alert claims the window BEFORE dispatch, so a failed delivery
    leaves a marker that says 'already alerted' for an alert nobody received.
    The next match must be able to re-fire."""
    sink.fail("dispatch", 503)
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["z:1"])
    assert sink.dispatched == []
    assert len(sink.signals) == 1, "the signal is the record of record and must survive"

    sink.heal("dispatch")
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["z:2"])
    assert len(sink.dispatched) == 1, "the alert window was never reopened; this can never re-fire"


def test_a_detection_that_raises_cannot_poison_the_batch(processor, pulled_bundle, sink):
    """Detections are untrusted code. Unguarded, one bad rule() would fail the
    invocation - and because Event Hubs redelivers, it would poison the
    partition permanently, retrying the same event forever."""
    bundle, _ = pulled_bundle
    (bundle / "web" / "cloudflare_sqli.py").write_text(
        "def rule(event):\n    raise RuntimeError('detection is broken')\n"
    )
    processor.loader._registry = None
    processor.loader._next_check = 0

    processor.process_batch([json.dumps(CF_HIT), json.dumps(PALO_HIT)],
                            event_ids=["c:1", "c:2"])
    assert len(sink.signals) == 1, "the healthy Palo detection must still have matched"
    assert sink.signals[0]["detection_id"] == "palo_high_risk_port"


def test_a_detection_returning_a_non_string_dedup_cannot_kill_the_batch(processor, pulled_bundle, sink):
    bundle, _ = pulled_bundle
    (bundle / "web" / "cloudflare_sqli.py").write_text(
        "def rule(event):\n    return True\n\n\ndef dedup(event):\n    return 12345\n"
    )
    processor.loader._registry = None
    processor.loader._next_check = 0
    processor.process_batch([json.dumps(CF_HIT)], event_ids=["n:1"])
    assert len(sink.signals) == 1
    assert sink.signals[0]["dedup"] == "12345"


def test_malformed_json_is_counted_not_silently_dropped(processor, sink, caplog):
    """At 4-5TB/day a silently dropped feed is invisible until an alert doesn't
    fire. These used to be a bare `continue`."""
    import logging
    caplog.set_level(logging.WARNING)
    processor.process_batch(["{not json", json.dumps(PALO_HIT)], event_ids=["m:1", "m:2"])
    assert "malformed json" in caplog.text
    assert len(sink.signals) == 1, "the valid event in the batch must still be processed"


def test_an_event_with_no_log_type_is_counted_not_silently_dropped(processor, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    processor.process_batch([json.dumps({"no": "log type here"})], event_ids=["q:1"])
    assert "missing the 'log type' field" in caplog.text


def test_a_log_type_no_detection_covers_is_counted(processor, caplog):
    """This is how a log-type rename silently disables coverage."""
    import logging
    caplog.set_level(logging.INFO)
    processor.process_batch([json.dumps({"dataset": "Nobody.Covers.This", "_time": "t"})],
                            event_ids=["u:1"])
    assert "matches no enabled detection" in caplog.text


def test_observability_scales_with_problems_not_traffic(processor, sink, caplog):
    """One log record per batch, not per event: at target volume a per-event
    log.info is a ~50k records/sec firehose that costs the most exactly when
    the system is least healthy."""
    import logging
    caplog.set_level(logging.INFO)
    processor.process_batch([json.dumps(PALO_HIT) for _ in range(200)],
                            event_ids=[f"v:{i}" for i in range(200)])
    assert len(caplog.records) <= 5, (
        f"200 events produced {len(caplog.records)} log records; this must be O(batches)"
    )


# =============================================================================
# 5. Hot reload: a DaC push goes live without a redeploy
# =============================================================================

def test_a_dac_push_goes_live_without_a_redeploy(processor, pulled_bundle, sink):
    """The freshness contract: a push reflects on a warm worker within
    refresh_interval_seconds, with no redeploy and no per-event cost."""
    bundle, _ = pulled_bundle
    processor.process_batch([json.dumps(PALO_MISS)], event_ids=["r:1"])
    assert sink.signals == [], "port 443 must not match the original rule"

    # The DaC author widens the rule and CI republishes: new sha -> new version.
    (bundle / "network" / "palo_high_risk_port.py").write_text(
        "def rule(event):\n    return int(event.get('dport', 0)) in (3389, 443)\n"
    )
    (bundle / ".bundle-version").write_text("second-sha")
    processor.loader._next_check = 0      # the refresh tick lands

    processor.process_batch([json.dumps(PALO_MISS)], event_ids=["r:2"])
    assert len(sink.signals) == 1, "the pushed detection never went live"


def test_the_version_probe_is_throttled_not_per_event(processor, pulled_bundle):
    """Freshness must cost one cheap pointer read per worker per interval - not
    one per event. A probe per event is how this design dies at volume."""
    bundle, _ = pulled_bundle
    processor.loader.get()      # warm the worker first; cold start legitimately probes once

    probes = {"n": 0}
    real = processor.loader._source.current_version

    def counting_probe():
        probes["n"] += 1
        return real()

    processor.loader._source.current_version = counting_probe
    processor.loader._next_check = time.monotonic() + 3600      # inside the refresh window

    processor.process_batch([json.dumps(PALO_HIT) for _ in range(50)],
                            event_ids=[f"w:{i}" for i in range(50)])
    assert probes["n"] == 0, "the bundle version was probed on the hot path"


def test_a_bundle_source_outage_keeps_serving_the_last_good_registry(processor, sink):
    """A storage blip must never stop detection."""
    def boom():
        raise RuntimeError("blob storage is down")

    processor.loader.get()                       # warm
    processor.loader._source.current_version = boom
    processor.loader._next_check = 0

    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["s:1"])
    assert len(sink.signals) == 1, "a transient bundle-source error stopped detection"


def test_a_disabled_detection_stops_matching(processor, pulled_bundle, sink):
    """A detection's DaC `Enabled: false` flag is the disable mechanism: the
    engine drops it at load so it is never evaluated. A republish (version bump)
    reloads on the next refresh tick."""
    bundle, _ = pulled_bundle
    (bundle / "network" / "palo_high_risk_port.yml").write_text(textwrap.dedent("""
        AnalysisType: rule
        RuleID: palo_high_risk_port
        Filename: palo_high_risk_port.py
        Enabled: false
        LogTypes: [SEC_Network_Palo_Alto_Traffic]
        Severity: Medium
        Threshold: 1
        DedupPeriodMinutes: 60
        CreateAlert: true
    """))
    processor.loader._registry = None
    processor.loader._next_check = 0
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["d:1"])
    assert sink.signals == [], "a disabled detection still ran"


# =============================================================================
# 5b. The PROD routing path
# =============================================================================
#
# Everything above runs with PYRE_ENV=dev, which routes to "mock". Prod takes a
# different default_routes value and a different entry in
# destinations.yaml, and nothing in the dev path covers it.

@pytest.fixture
def prod_processor(pulled_bundle, state, sink, monkeypatch):
    """The same engine with the same shipped config/destinations.yaml, but
    PYRE_ENV=prod - exactly what infra sets in the prod instance."""
    bundle, _ = pulled_bundle
    sink.reset()
    monkeypatch.setenv("PYRE_ENV", "prod")
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", sink.signals_url)
    monkeypatch.setenv("DESTINATIONS_PATH", os.path.join(REPO, "config", "destinations.yaml"))
    monkeypatch.setenv("DESTINATION_TORQ_PROD_URL", sink.dest_url)
    monkeypatch.setenv("DESTINATION_TORQ_PROD_TOKEN", "a-token")
    monkeypatch.setenv("DEFAULT_ROUTES", "torq_prod")
    monkeypatch.setenv("LOG_TYPE_FIELD", "dataset")
    monkeypatch.setenv("EVENT_TIME_FIELD", "_time")

    from pyre_engine.config import load_runtime_config
    from pyre_engine.processor import Processor
    return Processor(load_runtime_config(), state=state)


def test_prod_alerts_are_actually_deliverable(prod_processor, sink, caplog):
    """The prod route actually delivers: DEFAULT_ROUTES=torq_prod, torq_prod is
    enabled in destinations.yaml, and a match reaches the destination. Guards
    against torq_prod ever shipping enabled: false again (which would unregister
    the route and drop every prod alert while looking healthy)."""
    import logging
    caplog.set_level(logging.ERROR)
    prod_processor.process_batch([json.dumps(PALO_HIT)], event_ids=["prod:1"])

    assert len(sink.signals) == 1, "the match itself is fine - the signal is written"
    assert len(sink.dispatched) == 1, (
        "NO ALERT WAS DELIVERED IN PROD - the only prod route is unregistered "
        f"(torq_prod enabled: false?). Log said: {caplog.text}"
    )


def test_alert_payload_carries_the_event_and_p_fields(prod_processor, sink):
    """Every destination receives the full context - the raw triggering event, its
    p_ fields, and flat alert metadata (rule id, severity, alert_context) - so a
    case-creating destination has everything a responder needs."""
    prod_processor.process_batch([json.dumps(PALO_HIT)], event_ids=["disp:1"])
    assert len(sink.dispatched) == 1
    case = sink.dispatched[0]
    assert case["rule_id"] == "palo_high_risk_port"
    assert case["severity"] == "HIGH"
    assert case["alert_context"] == {"src_ip": "10.0.0.5", "dport": 3389, "app": "rdp"}
    assert case["event_count"] == 1
    # the whole raw event, so a case has everything a responder needs
    assert case["event"]["action"] == "allow"
    assert case["event"]["dst_ip"] == "8.8.8.8"
    # p_ fields: the configured routing/time fields plus anything p_-prefixed
    assert case["p_fields"]["dataset"] == "SEC_Network_Palo_Alto_Traffic"
    assert case["p_fields"]["_time"] == "2026-07-15T00:00:00Z"
    assert "p_enrichment" in case["p_fields"]


def test_alert_payload_event_count_reflects_the_triggering_count(prod_processor, pulled_bundle, sink):
    """event_count is the count that crossed threshold, not a hardcoded 1. Three
    matches against a Threshold-3 rule fire one case reporting event_count 3."""
    bundle, _ = pulled_bundle
    (bundle / "network" / "palo_high_risk_port.yml").write_text(textwrap.dedent("""
        AnalysisType: rule
        RuleID: palo_high_risk_port
        Filename: palo_high_risk_port.py
        Enabled: true
        LogTypes: [SEC_Network_Palo_Alto_Traffic]
        Severity: Medium
        Threshold: 3
        DedupPeriodMinutes: 60
        CreateAlert: true
    """))
    prod_processor.loader._registry = None
    prod_processor.loader._next_check = 0
    prod_processor.process_batch([json.dumps(PALO_HIT)] * 3, event_ids=["a", "b", "c"])
    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["event_count"] == 3


def test_an_unset_signals_sink_does_not_silently_discard_signals(pulled_bundle, state,
                                                                 sink, monkeypatch):
    """SIGNALS_SINK_URL defaults to "" (Terraform's signals_sink_url is optional).
    SignalWriter.flush() returns early when the url is falsy, so a deployment
    that forgets it drops every signal - the entire audit trail - in silence.
    """
    import logging
    bundle, _ = pulled_bundle
    sink.reset()
    monkeypatch.setenv("PYRE_ENV", "dev")
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", "")          # forgotten in tfvars
    monkeypatch.setenv("DESTINATION_MOCK_URL", sink.dest_url)
    monkeypatch.setenv("DEFAULT_ROUTES", "mock")
    monkeypatch.setenv("DESTINATIONS_PATH", os.path.join(REPO, "config", "destinations.yaml"))

    from pyre_engine.config import load_runtime_config
    from pyre_engine.processor import Processor

    # The warning belongs at cold start (once per worker), not per flush - a
    # per-batch warning would be its own firehose - so capture around the
    # Processor build, which is where SignalWriter is constructed.
    with caplog_at(logging.WARNING) as cap:
        proc = Processor(load_runtime_config(), state=state)
    proc.process_batch([json.dumps(PALO_HIT)], event_ids=["nosink:1"])

    assert sink.dispatched, "the alert still went out"
    assert "SIGNALS_SINK_URL" in cap.text, (
        "signals were discarded with no url configured and NOTHING was logged - "
        "the audit trail is silently gone"
    )


import contextlib


@contextlib.contextmanager
def caplog_at(level):
    """Minimal caplog stand-in usable outside a caplog fixture."""
    import logging

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

        @property
        def text(self):
            return "\n".join(r.getMessage() for r in self.records)

    h = _Cap()
    root = logging.getLogger()
    root.addHandler(h)
    old = root.level
    root.setLevel(level)
    try:
        yield h
    finally:
        root.removeHandler(h)
        root.setLevel(old)


# =============================================================================
# 5c. The host: one trigger per hub, including the catch-all
# =============================================================================
#
# config/sources.yaml can declare several hubs plus one `default: true`
# catch-all, and Terraform subscribes the app to ALL of them. An Event Hubs
# trigger binds to exactly one hub, so function_app.py registers them in a loop.
# A hub that ends up with no trigger accepts logs at full rate and evaluates
# none of them, silently - so the registration is worth asserting directly.
#
# What this does NOT prove: that Azure binds the triggers. That needs a real
# deploy (see tools/sim/README.md "What it does NOT cover").

def _load_host(monkeypatch, hubs: str, default: str = "default-logs-in"):
    """Import engine/function_app.py fresh with a given hub list."""
    import importlib
    monkeypatch.setenv("EVENTHUB_NAMES", hubs)
    monkeypatch.setenv("DEFAULT_EVENTHUB_NAME", default)
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", os.path.join(REPO, "tests", "fixtures", "sample_dac"))
    monkeypatch.setenv("REDIS_USE_ENTRA", "false")
    sys.modules.pop("function_app", None)
    sys.path.insert(0, os.path.join(REPO, "engine"))
    return importlib.import_module("function_app")


def test_one_trigger_is_registered_per_hub(monkeypatch):
    """Every hub in EVENTHUB_NAMES gets its own trigger - including the
    catch-all, which is what makes an unrouted log type actually get evaluated
    rather than pile up in an unconsumed hub."""
    hubs = "palo-traffic-in,cloudflare-in,default-logs-in"
    mod = _load_host(monkeypatch, hubs)

    registered = {f.get_function_name() for f in mod.app.get_functions()}
    assert registered == {"detect_palo_traffic_in", "detect_cloudflare_in",
                          "detect_default_logs_in"}, (
        f"expected one trigger per hub, got {registered}"
    )


def test_each_trigger_binds_to_its_own_hub_not_the_last_one(monkeypatch):
    """The closure trap: registering inside the loop body would make every
    trigger capture the same loop variable and bind to the LAST hub. The symptom
    would be silence on every other hub - not an error."""
    mod = _load_host(monkeypatch, "palo-traffic-in,cloudflare-in,default-logs-in")

    bound = {}
    for f in mod.app.get_functions():
        for b in f.get_bindings():
            d = b.get_dict_repr()
            if d.get("type", "").lower().startswith("eventhub"):
                bound[f.get_function_name()] = d.get("eventHubName")
    assert bound == {
        "detect_palo_traffic_in": "palo-traffic-in",
        "detect_cloudflare_in": "cloudflare-in",
        "detect_default_logs_in": "default-logs-in",
    }, f"triggers bound to the wrong hubs: {bound}"


def test_no_hubs_configured_is_loud(monkeypatch, caplog):
    """An app bound to nothing evaluates nothing forever, and otherwise looks
    exactly like a healthy app with no traffic."""
    import logging
    caplog.set_level(logging.ERROR)
    mod = _load_host(monkeypatch, "")
    assert mod.app.get_functions() == []
    assert "consume NOTHING" in caplog.text


def test_event_ids_are_scoped_per_hub(monkeypatch):
    """Sequence numbers restart per partition PER HUB, so two hubs can produce
    the same partition:sequence pair. Unscoped, an event on one hub would claim
    another hub's idempotency key and the second event would be silently dropped
    as an 'already processed' redelivery."""
    mod = _load_host(monkeypatch, "palo-traffic-in,default-logs-in")
    src = open(os.path.join(REPO, "engine", "function_app.py"), encoding="utf-8").read()
    assert 'f"{hub}:{e.partition_key}:{e.sequence_number}"' in src, (
        "event ids must be scoped by hub, or two hubs collide in the seen: set"
    )


def test_hubs_config_registers_a_trigger_per_hub_with_its_own_connection(monkeypatch):
    """Multi-namespace: HUBS_CONFIG gives each hub its own namespace CONNECTION
    (so one worker binds triggers across several namespaces) and its own shape."""
    import importlib
    cfg = [
        {"hub": "azure-diag-in", "connection": "EVENTHUB_AZURE",
         "log_type_field": "category", "event_time_field": "time", "envelope": "records"},
        {"hub": "cloudflare-in", "connection": "EVENTHUB_CRIBL",
         "log_type_field": "dataset", "event_time_field": "_time", "envelope": ""},
    ]
    monkeypatch.setenv("HUBS_CONFIG", json.dumps(cfg))
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", os.path.join(REPO, "tests", "fixtures", "sample_dac"))
    monkeypatch.setenv("REDIS_USE_ENTRA", "false")
    sys.modules.pop("function_app", None)
    sys.path.insert(0, os.path.join(REPO, "engine"))
    mod = importlib.import_module("function_app")

    conn = {}
    for f in mod.app.get_functions():
        for b in f.get_bindings():
            d = b.get_dict_repr()
            if d.get("type", "").lower().startswith("eventhub"):
                conn[d.get("eventHubName")] = d.get("connection")
    assert conn == {"azure-diag-in": "EVENTHUB_AZURE", "cloudflare-in": "EVENTHUB_CRIBL"}, (
        f"each hub must bind its own namespace connection, got {conn}"
    )


# =============================================================================
# 5d. An AZURE-NATIVE instance (envelope + Azure's own field names)
# =============================================================================
#
# One instance reads ONE feed shape. A Cribl instance is dataset/_time and one
# message = one event. An Azure-native instance is category/time and one message
# carries a `records` ARRAY. Mixing both shapes in one instance is what would
# force per-source config into the engine; deploying a second instance is the
# simpler answer, and these are the settings that make it work.

AZURE_MSG = json.dumps({"records": [
    {"time": "2026-07-15T00:00:00Z", "category": "SignInLogs", "resultType": "0",
     "properties": {"userPrincipalName": "jdoe@corp.com"}},
    {"time": "2026-07-15T00:00:01Z", "category": "SignInLogs", "resultType": "50126",
     "properties": {"userPrincipalName": "jdoe@corp.com"}},
    {"time": "2026-07-15T00:00:02Z", "category": "SignInLogs", "resultType": "50126",
     "properties": {"userPrincipalName": "jdoe@corp.com"}},
]})

AZURE_RULE = '''
def rule(event):
    return event.get("resultType") == "50126"


def dedup(event):
    return event.deep_get("properties", "userPrincipalName") or "unknown"
'''

AZURE_META = """
AnalysisType: rule
RuleID: Entra.FailedSignIn
Filename: failed_signin.py
Enabled: true
LogTypes: [SignInLogs]
Severity: Medium
Threshold: 1
DedupPeriodMinutes: 60
"""


@pytest.fixture
def azure_processor(tmp_path, state, sink, monkeypatch):
    """An Azure-native instance: exactly the tfvars an `azure` instance sets."""
    bundle = tmp_path / "azdac"
    (bundle / "entra").mkdir(parents=True)
    (bundle / "entra" / "failed_signin.py").write_text(AZURE_RULE)
    (bundle / "entra" / "failed_signin.yml").write_text(AZURE_META)
    sink.reset()
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", sink.signals_url)
    monkeypatch.setenv("DESTINATIONS_PATH", os.path.join(REPO, "config", "destinations.yaml"))
    monkeypatch.setenv("DESTINATION_MOCK_URL", sink.dest_url)
    monkeypatch.setenv("DEFAULT_ROUTES", "mock")
    monkeypatch.setenv("LOG_TYPE_FIELD", "category")   # Azure's own name
    monkeypatch.setenv("EVENT_TIME_FIELD", "time")
    monkeypatch.setenv("ENVELOPE", "records")          # unwrap the array

    from pyre_engine.config import load_runtime_config
    from pyre_engine.processor import Processor
    return Processor(load_runtime_config(), state=state)


def test_an_azure_envelope_is_expanded_into_its_records(azure_processor, sink):
    """One diagnostic-settings message carries MANY records. Without unwrapping,
    the engine reads the envelope as a single event with no log type and drops
    the whole message - every record inside it."""
    azure_processor.process_batch([AZURE_MSG], event_ids=["azure-diagnostics-in:0:42"])

    assert len(sink.signals) == 2, "the 2 failed sign-ins inside the envelope must each signal"
    assert len(sink.dispatched) == 1, "both share a dedup string, so one alert"
    # Read from Azure's own `time`, not Cribl's `_time`.
    assert sink.signals[0]["_time"] == "2026-07-15T00:00:01Z"


def test_records_inside_an_envelope_are_claimed_individually(azure_processor, sink):
    """Idempotency must be per RECORD, not per message. Claiming the envelope as
    one id would let a redelivery skip every record inside it - or, worse, a
    partial failure re-signal all of them."""
    azure_processor.process_batch([AZURE_MSG], event_ids=["azure-diagnostics-in:0:42"])
    assert len(sink.signals) == 2

    azure_processor.process_batch([AZURE_MSG], event_ids=["azure-diagnostics-in:0:42"])
    assert len(sink.signals) == 2, "a redelivered envelope re-signalled its records"


def test_a_feed_that_is_not_the_declared_shape_is_loud(azure_processor, sink, caplog):
    """The wrong-instance mistake: a Cribl-shaped event sent to an Azure-shaped
    instance. Every event is dropped, so it must never be quiet."""
    import logging
    caplog.set_level(logging.ERROR)
    azure_processor.process_batch([json.dumps(PALO_HIT)], event_ids=["x:1"])

    assert sink.signals == []
    assert "no 'records' array" in caplog.text
    assert "ENVELOPE matches the feed this instance reads" in caplog.text


def test_a_cribl_instance_is_unaffected_by_the_envelope_code(processor, sink):
    """The default (no ENVELOPE) path must stay exactly as it was: one message is
    one event, and parsing stays lazy behind the idempotency claim."""
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["c:1"])
    assert len(sink.signals) == 1


def test_one_worker_reads_two_feed_shapes_via_per_hub_shape(tmp_path, state, sink, monkeypatch):
    """The multi-namespace guarantee: ONE Processor serves hubs of different
    SHAPES. A Cribl hub (dataset/_time/flat) and an Azure-native hub
    (category/time/records) both go through the same worker, each read with the
    shape passed for that batch - no second deployment."""
    bundle = tmp_path / "mixdac"
    (bundle / "cribl").mkdir(parents=True)
    (bundle / "cribl" / "cribl_bad.py").write_text('def rule(event):\n    return event.get("kind") == "bad"\n')
    (bundle / "cribl" / "cribl_bad.yml").write_text(textwrap.dedent("""
        AnalysisType: rule
        RuleID: cribl_bad
        Filename: cribl_bad.py
        Enabled: true
        LogTypes: [CriblLog]
        Severity: Low
        Threshold: 1
        DedupPeriodMinutes: 60
    """))
    (bundle / "entra").mkdir(parents=True)
    (bundle / "entra" / "failed_signin.py").write_text(AZURE_RULE)
    (bundle / "entra" / "failed_signin.yml").write_text(AZURE_META)
    sink.reset()
    monkeypatch.setenv("BUNDLE_MODE", "local")
    monkeypatch.setenv("BUNDLE_LOCAL_DIR", str(bundle))
    monkeypatch.setenv("SIGNALS_SINK_URL", sink.signals_url)
    monkeypatch.setenv("DESTINATIONS_PATH", os.path.join(REPO, "config", "destinations.yaml"))
    monkeypatch.setenv("DESTINATION_MOCK_URL", sink.dest_url)
    monkeypatch.setenv("DEFAULT_ROUTES", "mock")

    from pyre_engine.config import load_runtime_config, Shape
    from pyre_engine.processor import Processor
    proc = Processor(load_runtime_config(), state=state)

    proc.process_batch([json.dumps({"dataset": "CriblLog", "_time": "t1", "kind": "bad"})],
                       event_ids=["cribl:1"], shape=Shape("dataset", "_time", ""))
    proc.process_batch([AZURE_MSG], event_ids=["azure:1"],
                       shape=Shape("category", "time", "records"))

    fired = {s["detection_id"] for s in sink.signals}
    assert fired == {"cribl_bad", "Entra.FailedSignIn"}, (
        f"one worker must evaluate both feed shapes; got {fired}"
    )


# =============================================================================
# 6. Concurrency
# =============================================================================
#
# function_app.py builds ONE Processor per worker and the Python worker runs
# sync invocations on a thread pool, so an instance owning several partitions
# calls process_batch - and therefore add_signal/flush/for_log_type/loader.get -
# from several threads against these same objects. Single-threaded tests cannot
# see any of the races that creates.

def test_concurrent_batches_lose_no_signals_and_duplicate_no_alerts(processor, sink):
    """The load-bearing concurrency claim, against a real server.

    Every match must produce exactly one signal (SignalWriter's buffer swap) and
    every match sharing a dedup string must collapse to exactly one alert
    (Redis SET NX), no matter how the threads interleave.
    """
    import concurrent.futures

    threads, batches_per_thread, events_per_batch = 8, 10, 5
    total = threads * batches_per_thread * events_per_batch

    def worker(t):
        for b in range(batches_per_thread):
            ids = [f"t{t}:b{b}:e{i}" for i in range(events_per_batch)]
            processor.process_batch([json.dumps(PALO_HIT)] * events_per_batch, event_ids=ids)

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        errors = [f.exception() for f in
                  [ex.submit(worker, t) for t in range(threads)]]
    assert not any(errors), f"a concurrent batch raised: {[e for e in errors if e]}"

    assert len(sink.signals) == total, (
        f"expected {total} signals, got {len(sink.signals)}; signals were lost or duplicated "
        f"under concurrency"
    )
    # Every event shares one dedup string, so first-event-wins means exactly one.
    assert len(sink.dispatched) == 1, (
        f"{len(sink.dispatched)} alerts dispatched for one dedup window; the alert marker raced"
    )


def test_concurrent_first_touch_of_a_log_type_never_yields_an_empty_detection_list(processor, sink):
    """Registry.for_log_type materializes a log type on first touch by POPPING
    it from _pending. Two threads missing together would otherwise race, and the
    loser would get [] and silently run NO detections for that log type - a
    coverage hole that only appears under load.
    """
    import concurrent.futures

    processor.loader.get()._ready.clear()      # force a cold first-touch
    reg = processor.loader.get()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = [f.result() for f in
                   [ex.submit(reg.for_log_type, "SEC_Network_Palo_Alto_Traffic")
                    for _ in range(64)]]

    assert all(len(r) == 1 for r in results), (
        "a thread got an empty detection list for a log type that has one - "
        "that thread evaluated nothing and reported nothing"
    )


# =============================================================================
# 7. Known edge: an alert delivered whose alert RECORD never reached the lake
# =============================================================================

def test_signals_flush_failure_after_dispatch_loses_the_alert_record(processor, sink):
    """Documents a real ordering gap rather than asserting the ideal.

    Order per batch is: dispatch the alert -> flush signals+alert records. If
    the flush fails AFTER a successful dispatch, the batch raises and its claims
    are released, so Event Hubs redelivers. On the redelivery the dedup marker
    still exists, so register_alert returns False and the alert record is never
    re-buffered: the alert WAS delivered to the destination, the signal is
    rewritten, but pyre_alerts has no row for it.

    That is the failure model working as designed (fail toward duplicates, the
    signal is the record of record) - the alert record, not the alert, is what's
    lost. This test pins the behavior so the tradeoff stays a decision instead
    of a surprise.
    """
    sink.fail("signals", 503)
    with pytest.raises(Exception):
        processor.process_batch([json.dumps(PALO_HIT)], event_ids=["ar:1"])

    assert len(sink.dispatched) == 1, "the alert was delivered before the flush failed"

    sink.heal("signals")
    processor.process_batch([json.dumps(PALO_HIT)], event_ids=["ar:1"])   # redelivery

    assert len(sink.signals) == 1, "the signal is rewritten on redelivery - no loss"
    assert sink.alert_records == [], (
        "documented gap: the alert reached the destination but pyre_alerts has no "
        "record of it. If this ever starts passing with a record present, the "
        "ordering was fixed and this test should be inverted."
    )
