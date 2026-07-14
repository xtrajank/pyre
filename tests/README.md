# tests/ — automated tests

Fast, offline tests that prove the engine and the example detections behave. No Azure, no network. Run them before you change anything and after.

## Run them

```bash
pip install -r engine/requirements.txt fakeredis pytest   # one-time
python -m pytest tests -q          # everything
python cli/pyre test               # same thing, via the CLI
python -m pytest tests -q -k palo  # just tests matching "palo"
```

## What's here

| File | Proves |
|---|---|
| `test_registry_loader.py` | The external-DaC seam: detections load from a **bundle directory**, route by `LogType`, and **hot-reload** when the bundle version or the enabled-set changes. This is the core "detections come from another repo and a push reflects fast" behavior. |
| `test_example_detection.py` | The example detections' `rule()`/`severity()` logic behaves (e.g. RDP-to-3389 matches, HTTPS doesn't). A template for your own detection tests. |
| `fixtures/sample_dac/` | A tiny **fake** detections repo used by the tests so they run fully offline. Stands in for your real external DaC repo. It is test scaffolding, not detections pyre ships. |
| `conftest.py` | Test setup: puts the engine package on the import path and exposes the sample-DaC path. |

## Add a test for your own detection

Real detections live in your external DaC repo and are ideally tested *there*. But to try one against this engine locally, drop its `.py`+`.yml` into `fixtures/sample_dac/<vendor>/` and add a test like:

```python
import importlib.util, os
from conftest import SAMPLE_DAC

def _load(rel):
    p = os.path.join(SAMPLE_DAC, rel)
    spec = importlib.util.spec_from_file_location("d", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_my_detection_matches():
    m = _load("myvendor/my_detection.py")
    assert m.rule({"some_field": "bad value"}) is True
```

CI runs `pyre validate` + `pyre test` on every PR — see [../.azure-pipelines/ci.yml](../.azure-pipelines/ci.yml).
