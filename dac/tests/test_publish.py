"""publish.py's import-safety check: a detection must not ship if it imports
something the deployed Function App doesn't have. Without this, a missing
dependency only surfaces as a runtime warning on a worker, potentially long
after the bundle went live."""
import os
import sys

# No conftest.py here: a second module also named "conftest" collides with
# tests/conftest.py under pytest's rootless import (both dirs lack an
# __init__.py), so path setup lives in this file instead.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import publish


def _write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source)
    return str(path), name


def test_a_stdlib_import_is_allowed(tmp_path):
    py = _write(tmp_path, "m.py", "import json\n\ndef rule(e): return True\n")
    assert publish._check_imports([py], helper_modules=set()) == []


def test_a_global_helper_import_is_allowed(tmp_path):
    py = _write(tmp_path, "m.py", "from my_helper import thing\n\ndef rule(e): return True\n")
    assert publish._check_imports([py], helper_modules={"my_helper"}) == []


def test_an_approved_third_party_import_is_allowed(tmp_path):
    # `yaml` is what `pyyaml` in requirements.txt actually installs as -
    # proof the name-mismatch cases resolve without a hand-written table.
    py = _write(tmp_path, "m.py", "import yaml\n\ndef rule(e): return True\n")
    assert publish._check_imports([py], helper_modules=set()) == []


def test_an_unapproved_third_party_import_is_rejected(tmp_path):
    py = _write(tmp_path, "m.py", "import dateutil\n\ndef rule(e): return True\n")
    errors = publish._check_imports([py], helper_modules=set())
    assert len(errors) == 1
    assert "m.py" in errors[0] and "dateutil" in errors[0]


def test_a_lazy_import_inside_a_function_is_still_caught(tmp_path):
    py = _write(tmp_path, "m.py",
               "def rule(e):\n    import dateutil\n    return True\n")
    errors = publish._check_imports([py], helper_modules=set())
    assert any("dateutil" in e for e in errors)


def test_a_relative_import_is_never_flagged(tmp_path):
    py = _write(tmp_path, "m.py", "from . import sibling\n\ndef rule(e): return True\n")
    assert publish._check_imports([py], helper_modules=set()) == []


def test_missing_requirements_file_skips_the_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(publish, "HERE", str(tmp_path))
    py = _write(tmp_path, "m.py", "import dateutil\n\ndef rule(e): return True\n")
    assert publish._check_imports([py], helper_modules=set()) == []
    assert "skipping the import-safety check" in capsys.readouterr().out
