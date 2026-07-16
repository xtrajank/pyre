"""The publish gate for the DaC-push-vs-engine-deploy split.

Detections hot-reload from the DaC in ~45s; engine/requirements.txt is only
installed when the Function App is deployed. So a DaC push CAN introduce an
import the running engine doesn't have, and the only symptom is a "skipping
detection" warning while that detection silently covers nothing. These tests
pin the behaviour that stops such a bundle from ever being published.
"""
from pyre_engine.deps import scan_imports


def _write(d, name, py, yml):
    (d / f"{name}.py").write_text(py)
    (d / f"{name}.yml").write_text(yml)


def test_missing_module_is_reported_with_its_users(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", "import definitely_not_installed_xyz\ndef rule(e): return True\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, unparseable = scan_imports(str(d))
    assert list(missing) == ["definitely_not_installed_xyz"]
    assert missing["definitely_not_installed_xyz"][0].endswith("r.py")
    assert unparseable == []


def test_stdlib_and_installed_modules_are_satisfied(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", "import json, re, ipaddress\nimport yaml\nfrom datetime import datetime\n"
                   "def rule(e): return True\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, unparseable = scan_imports(str(d))
    assert missing == {} and unparseable == []


def test_bundle_global_helper_is_not_reported_as_missing(tmp_path):
    # A helper shipped IN the bundle is importable at runtime (the engine puts its
    # dir on sys.path), so it must not be mistaken for a missing pip package.
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "myhelper", "def helper(): return 1\n",
           "AnalysisType: global\nFilename: myhelper.py\n")
    _write(d, "r", "from myhelper import helper\ndef rule(e): return helper()\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, _ = scan_imports(str(d))
    assert missing == {}


def test_helper_imports_are_checked_too(tmp_path):
    # A rule importing a bundle helper that itself needs a missing package is
    # still broken at runtime, so the helper's own imports have to be checked.
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "myhelper", "import definitely_not_installed_xyz\ndef helper(): return 1\n",
           "AnalysisType: global\nFilename: myhelper.py\n")
    _write(d, "r", "from myhelper import helper\ndef rule(e): return helper()\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, _ = scan_imports(str(d))
    assert list(missing) == ["definitely_not_installed_xyz"]


def test_import_inside_a_function_body_is_caught(tmp_path):
    # A deferred import doesn't fail at load - it raises inside rule() on every
    # matching event, which is worse. It counts the same.
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", "def rule(e):\n    import definitely_not_installed_xyz\n    return True\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, _ = scan_imports(str(d))
    assert list(missing) == ["definitely_not_installed_xyz"]


def test_scan_does_not_execute_detection_code(tmp_path):
    # The gate runs in CI against an untrusted-ish DaC repo, and must stay fast.
    # Parsing only - a module with a side effect (or that would crash on import)
    # must not run.
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", f"import pathlib\n"
                   f"pathlib.Path(r'{tmp_path}/EXECUTED').write_text('x')\n"
                   f"raise RuntimeError('import-time boom')\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, unparseable = scan_imports(str(d))
    assert not (tmp_path / "EXECUTED").exists()
    assert missing == {} and unparseable == []


def test_syntax_error_is_reported_not_raised(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", "def rule(event)\n    return True\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, unparseable = scan_imports(str(d))
    assert missing == {}
    assert len(unparseable) == 1 and "SyntaxError" in unparseable[0][1]


def test_relative_imports_are_not_flagged(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    _write(d, "r", "from . import sibling\ndef rule(e): return True\n",
           "AnalysisType: rule\nRuleID: r\nFilename: r.py\nLogTypes: [T.A]\n")
    missing, _ = scan_imports(str(d))
    assert missing == {}
