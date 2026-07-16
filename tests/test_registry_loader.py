"""The external-DaC seam: LocalBundleSource + BundleLoader.

Proves the behaviours the design depends on:
  1. Detections are loaded from a bundle DIRECTORY and routed by LogType.
  2. A version change hot-reloads the Registry (a DaC push reflects) while an
     unchanged version does NOT rebuild (the cheap per-batch probe path).
  3. A detection's module is imported only when its log type is actually seen -
     the property that keeps a 900-detection bundle cheap to load.
"""
import os
import threading

from pyre_engine.dac import LocalBundleSource
from pyre_engine.registry import BundleLoader, Registry
from conftest import SAMPLE_DAC


def test_local_source_routes_by_log_type():
    loader = BundleLoader(LocalBundleSource(SAMPLE_DAC), refresh_interval_seconds=0)
    reg = loader.get()
    palo = reg.for_log_type("SEC_Network_Palo_Alto_Traffic")
    assert [d.id for d in palo] == ["palo_traffic_high_risk_port"]
    assert reg.for_log_type("SEC_Web_Cloudflare")[0].id == "cloudflare_http_sqli"
    assert reg.for_log_type("Nonexistent.Log") == []


def test_detection_metadata_from_yaml():
    reg = BundleLoader(LocalBundleSource(SAMPLE_DAC), refresh_interval_seconds=0).get()
    det = reg.for_log_type("SEC_Web_Cloudflare")[0]
    assert det.threshold == 5
    assert det.dedup_period_seconds == 30 * 60
    assert det.dedup({"ClientIP": "1.2.3.4"}) == "1.2.3.4"


def test_enabled_set_filters_and_reloads():
    # enabled_provider is re-read each check, so an enable/disable flip reloads.
    state = {"enabled": {"palo_traffic_high_risk_port"}}
    loader = BundleLoader(
        LocalBundleSource(SAMPLE_DAC),
        refresh_interval_seconds=0,
        enabled_provider=lambda: state["enabled"],
    )
    assert {d.id for lt in ("SEC_Network_Palo_Alto_Traffic", "SEC_Web_Cloudflare") for d in loader.get().for_log_type(lt)} \
        == {"palo_traffic_high_risk_port"}
    state["enabled"] = {"cloudflare_http_sqli"}
    ids = {d.id for lt in ("SEC_Network_Palo_Alto_Traffic", "SEC_Web_Cloudflare") for d in loader.get().for_log_type(lt)}
    assert ids == {"cloudflare_http_sqli"}


def test_broken_detection_is_skipped(tmp_path):
    # A detection that fails to import (here: a missing helper) must NOT block the
    # rest of the bundle — it's skipped and logged, the good one still loads.
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "bad.py").write_text("from nonexistent_helper import x\ndef rule(e): return True\n")
    (d / "bad.yml").write_text("AnalysisType: rule\nRuleID: bad\nFilename: bad.py\nLogTypes: [T.A]\n")
    (d / "good.py").write_text("def rule(e): return True\n")
    (d / "good.yml").write_text("AnalysisType: rule\nRuleID: good\nFilename: good.py\nLogTypes: [T.A]\n")
    reg = Registry.from_bundle(str(d))
    assert [x.id for x in reg.for_log_type("T.A")] == ["good"]


def test_global_helper_is_importable():
    # A detection that does `from pyre_shared import is_high_risk_port` must load —
    # the engine has to put the global_helpers dir on sys.path (Panther parity).
    reg = BundleLoader(LocalBundleSource(SAMPLE_DAC), refresh_interval_seconds=0).get()
    dets = reg.for_log_type("Test.HelperLog")
    assert [d.id for d in dets] == ["uses_helper"]
    assert dets[0].rule({"dport": 3389}) is True    # helper says 3389 is high-risk
    assert dets[0].rule({"dport": 8080}) is False


def test_modules_load_lazily_per_log_type(tmp_path):
    # Building the index must not exec ANY detection module - that's what makes a
    # 900-detection bundle load in ~1s instead of ~18s. A module is exec'd only
    # when its own log type is first asked for; asking for one log type must not
    # drag in the other's module.
    d = tmp_path / "bundle"
    d.mkdir()
    for name, lt in (("a", "T.A"), ("b", "T.B")):
        (d / f"{name}.py").write_text(
            f"import pathlib; pathlib.Path(r'{tmp_path}/{name}.loaded').write_text('x')\n"
            "def rule(e): return True\n")
        (d / f"{name}.yml").write_text(
            f"AnalysisType: rule\nRuleID: {name}\nFilename: {name}.py\nLogTypes: [{lt}]\n")

    reg = Registry.from_bundle(str(d))
    assert not (tmp_path / "a.loaded").exists(), "from_bundle must not exec detection modules"
    assert not (tmp_path / "b.loaded").exists()

    assert [x.id for x in reg.for_log_type("T.A")] == ["a"]
    assert (tmp_path / "a.loaded").exists(), "the asked-for log type's module must load"
    assert not (tmp_path / "b.loaded").exists(), "an unrelated log type's module must NOT load"


def test_shared_detection_execs_its_module_once(tmp_path):
    # One detection registered under two log types is a single Detection object,
    # so its module is exec'd once no matter how many log types materialize.
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "s.py").write_text(
        f"import pathlib; p = pathlib.Path(r'{tmp_path}/count')\n"
        "p.write_text(str(len(p.read_text()) + 1) if p.exists() else '1')\n"
        "def rule(e): return True\n")
    (d / "s.yml").write_text(
        "AnalysisType: rule\nRuleID: s\nFilename: s.py\nLogTypes: [T.A, T.B]\n")
    reg = Registry.from_bundle(str(d))
    a = reg.for_log_type("T.A")[0]
    b = reg.for_log_type("T.B")[0]
    assert a is b
    assert (tmp_path / "count").read_text() == "1"


def test_concurrent_first_touch_is_not_racy(tmp_path):
    # The worker runs batches on a thread pool. Two threads hitting the same
    # never-seen log type at once must both get the full detection list; a
    # non-atomic materialize would hand one of them an empty list and silently
    # run no detections for that event.
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "r.py").write_text("import time\ntime.sleep(0.02)\ndef rule(e): return True\n")
    (d / "r.yml").write_text("AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    reg = Registry.from_bundle(str(d))

    start = threading.Barrier(8)
    results = []

    def touch():
        start.wait()
        results.append([x.id for x in reg.for_log_type("T.A")])

    threads = [threading.Thread(target=touch) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results == [["r"]] * 8


def test_version_change_triggers_reload(monkeypatch):
    src = LocalBundleSource(SAMPLE_DAC)
    versions = iter(["v1", "v1", "v2"])
    monkeypatch.setattr(src, "current_version", lambda: next(versions))
    loader = BundleLoader(src, refresh_interval_seconds=0)

    reg1 = loader.get()          # loads at v1
    reg2 = loader.get()          # v1 again -> same object, no rebuild
    assert reg1 is reg2
    reg3 = loader.get()          # v2 -> rebuilt, new object
    assert reg3 is not reg1
