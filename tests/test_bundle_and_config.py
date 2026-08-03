"""The two seams either side of the engine: the detections that come in from a
published bundle, and the source config that says how to read each feed."""
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

from conftest import DAC, REPO
from pyre_engine.bundle import LocalBundleSource
from pyre_engine.config import Source, load_sources
from pyre_engine.registry import BundleLoader, Registry

PUBLISH = os.path.join(DAC, "publish.py")


# ---- sources.yaml -----------------------------------------------------------

def test_the_shipped_sources_file_parses():
    """config/sources.yaml ships inside the deployment, so a typo in it takes
    the whole app down at cold start. Parse it here instead."""
    sources = load_sources(os.path.join(REPO, "config", "sources.yaml"))
    assert sources and all(s.hub for s in sources)


def test_a_source_only_has_to_name_its_hub(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("sources:\n  - hub: logs-in\n")
    assert load_sources(str(f)) == [Source(hub="logs-in")]


def test_per_source_overrides_are_read(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text(
        "sources:\n"
        "  - hub: azure-in\n"
        "  - hub: palo-in\n"
        "    connection: EVENTHUB_CONNECTION_NET\n"
        "    consumer_group: pyre\n"
        "    log_type_field: dataset\n"
        "    event_time_field: _time\n"
        "    envelope_field: ''\n")
    azure, palo = load_sources(str(f))
    assert (azure.log_type_field, azure.envelope_field) == ("category", "records")
    assert palo == Source(hub="palo-in", connection="EVENTHUB_CONNECTION_NET",
                          consumer_group="pyre", log_type_field="dataset",
                          event_time_field="_time", envelope_field="")


def test_a_typo_in_sources_yaml_is_an_error_not_a_shrug(tmp_path):
    """Silently ignoring `log_type_feild:` would mean routing on the default and
    no alerts, with nothing anywhere saying why."""
    f = tmp_path / "s.yaml"
    f.write_text("sources:\n  - hub: a\n    log_type_feild: Category\n")
    with pytest.raises(ValueError, match="log_type_feild"):
        load_sources(str(f))

    f.write_text("sources:\n  - log_type_field: Category\n")
    with pytest.raises(ValueError, match="needs a `hub:`"):
        load_sources(str(f))


def test_a_missing_sources_file_is_empty_not_a_crash(tmp_path):
    assert load_sources(str(tmp_path / "nope.yaml")) == []


# ---- loading a bundle -------------------------------------------------------

def test_the_starter_dac_loads_and_indexes_by_log_type():
    reg = BundleLoader(LocalBundleSource(DAC), refresh_seconds=0).get()
    assert reg.stats() == {"detections": 1, "log_types": ["RuntimeAuditLogs"]}
    det = reg.for_log_type("RuntimeAuditLogs")[0]
    assert det.id == "Azure.EventHub.AuthFailure"
    assert det.threshold == 3 and det.dedup_period_seconds == 3600
    assert reg.for_log_type("Nothing.Here") == []


def test_a_broken_detection_is_skipped_not_fatal(tmp_path):
    """One detection that won't import must not take the rest of the bundle -
    and therefore all detection - down with it."""
    (tmp_path / "bad.py").write_text("from nonexistent_helper import x\ndef rule(e): return True\n")
    (tmp_path / "bad.yml").write_text("AnalysisType: rule\nRuleID: bad\nFilename: bad.py\nLogTypes: [T]\n")
    (tmp_path / "good.py").write_text("def rule(e): return True\n")
    (tmp_path / "good.yml").write_text("AnalysisType: rule\nRuleID: good\nFilename: good.py\nLogTypes: [T]\n")
    assert [d.id for d in Registry.from_bundle(str(tmp_path)).for_log_type("T")] == ["good"]


def test_a_version_change_reloads_and_an_unchanged_one_does_not(monkeypatch):
    src = LocalBundleSource(DAC)
    versions = iter(["v1", "v1", "v2"])
    monkeypatch.setattr(src, "current_version", lambda: next(versions))
    loader = BundleLoader(src, refresh_seconds=0)

    first = loader.get()
    assert loader.get() is first          # same version -> no rebuild
    assert loader.get() is not first      # new version -> rebuilt
    assert loader.version == "v2"


def test_a_failed_refresh_keeps_serving_the_last_good_bundle(monkeypatch):
    """A blob or network blip must never stop detection."""
    src = LocalBundleSource(DAC)
    loader = BundleLoader(src, refresh_seconds=0)
    good = loader.get()
    monkeypatch.setattr(src, "current_version",
                        lambda: (_ for _ in ()).throw(RuntimeError("blob down")))
    assert loader.get() is good


# ---- the DaC repo publishing itself ------------------------------------------

def test_publish_produces_a_bundle_the_engine_can_load(tmp_path):
    """Run the real publisher over a copy of the starter repo, then load its zip
    exactly as the engine does from Blob. If this breaks, a published bundle
    uploads fine and loads nothing."""
    repo = tmp_path / "dac-repo"
    shutil.copytree(DAC, repo, ignore=shutil.ignore_patterns("__pycache__", "dist"))

    out = subprocess.run([sys.executable, "publish.py"], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "RuntimeAuditLogs" in out.stdout          # surfaces the routing value to compare

    zips = list((repo / "dist" / "bundles").glob("*.zip"))
    assert len(zips) == 1
    pointer = json.loads((repo / "dist" / "current.json").read_text())
    assert pointer["path"] == f"bundles/{zips[0].name}"

    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(zips[0]) as z:
        z.extractall(extracted)
        assert "publish.py" not in z.namelist()      # the publisher must not ship itself
    assert Registry.from_bundle(str(extracted)).stats() == {
        "detections": 1, "log_types": ["RuntimeAuditLogs"]}


def test_publish_refuses_a_bundle_that_would_load_nothing(tmp_path):
    """The failure this exists to prevent: a .py that isn't beside its .yml
    uploads cleanly and then silently registers zero detections."""
    repo = tmp_path / "dac-repo"
    (repo / "rules").mkdir(parents=True)
    (repo / "elsewhere").mkdir()
    (repo / "rules" / "r.yml").write_text(
        "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T]\n")
    (repo / "elsewhere" / "r.py").write_text("def rule(e): return True\n")
    shutil.copy(PUBLISH, repo / "publish.py")

    out = subprocess.run([sys.executable, "publish.py"], cwd=repo, capture_output=True, text=True)
    assert out.returncode != 0
    assert "not found next to it" in out.stdout


def test_publish_reports_the_version_changing_when_a_rule_changes(tmp_path):
    """A running worker reloads on a version change and only on a version change,
    so an edited rule MUST produce a different version."""
    repo = tmp_path / "dac-repo"
    shutil.copytree(DAC, repo, ignore=shutil.ignore_patterns("__pycache__", "dist"))
    rule = repo / "detections" / "azure_eventhub" / "eventhub_auth_failure.yml"

    def version():
        subprocess.run([sys.executable, "publish.py"], cwd=repo, capture_output=True, text=True,
                       check=True)
        return json.loads((repo / "dist" / "current.json").read_text())["version"]

    before = version()
    assert version() == before                                   # unchanged repo, same version
    rule.write_text(rule.read_text().replace("Threshold: 3", "Threshold: 5"))
    assert version() != before
