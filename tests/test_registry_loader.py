"""The external-DaC seam: LocalBundleSource + BundleLoader.

Proves the two behaviours the design depends on:
  1. Detections are loaded from a bundle DIRECTORY and routed by LogType.
  2. A version change hot-reloads the Registry (a DaC push reflects) while an
     unchanged version does NOT rebuild (the cheap per-batch probe path).
"""
import os

from pyre_engine.dac import LocalBundleSource
from pyre_engine.registry import BundleLoader, Registry
from conftest import SAMPLE_DAC


def test_local_source_routes_by_log_type():
    loader = BundleLoader(LocalBundleSource(SAMPLE_DAC), refresh_interval_seconds=0)
    reg = loader.get()
    palo = reg.for_log_type("Palo.Traffic")
    assert [d.id for d in palo] == ["palo_traffic_high_risk_port"]
    assert reg.for_log_type("Cloudflare.HttpRequest")[0].id == "cloudflare_http_sqli"
    assert reg.for_log_type("Nonexistent.Log") == []


def test_detection_metadata_from_yaml():
    reg = BundleLoader(LocalBundleSource(SAMPLE_DAC), refresh_interval_seconds=0).get()
    det = reg.for_log_type("Cloudflare.HttpRequest")[0]
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
    assert {d.id for lt in ("Palo.Traffic", "Cloudflare.HttpRequest") for d in loader.get().for_log_type(lt)} \
        == {"palo_traffic_high_risk_port"}
    state["enabled"] = {"cloudflare_http_sqli"}
    ids = {d.id for lt in ("Palo.Traffic", "Cloudflare.HttpRequest") for d in loader.get().for_log_type(lt)}
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
