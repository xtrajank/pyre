# dac_bundler — drop this folder into your DaC repo

Turns your detections repo into the two files the pyre engine reads from Blob
storage. Copy this whole folder into your DaC repo and run it there:

```bash
cd <your-dac-repo>
python dac_bundler/bundle.py
```

It needs **nothing but Python**. No Azure CLI, no pyre repo, no network. PyYAML
is used for validation if you have it and skipped with a warning if you don't
(install it — the validation is the point).

The folder excludes *itself* from the bundle, so leaving it in the repo is safe
and `example/` never ships as a live detection.

---

## What it produces

```
dist/
├── current.json                     the pointer
└── bundles/
    └── sha256-<16 hex>.zip          the bundle
```

Upload both to the `detections` container — **zip first, pointer second**. See
[docs/poc/README.md](../../../docs/poc/README.md#step-5--bundle-and-upload-your-detections)
in the pyre repo for the exact portal steps.

The version is a **hash of your detection files**, so it changes when and only
when a detection changes. That version string is what makes a running worker
notice and reload — which is why you never have to name it yourself.

---

## What it checks before zipping

These are the mistakes that produce a bundle which uploads perfectly and then
loads zero detections:

- a `.yml` whose `Filename:` points at a `.py` that isn't **in the same folder**
- a detection missing `RuleID`, `Filename`, or `LogTypes`
- a `AnalysisType: global` helper with no `Filename`
- unparseable YAML

Any of these fails the run with a non-zero exit code — safe to put in CI.

It also prints **every `LogTypes` value your detections declare**:

```
LogTypes declared by these detections - an event's log-type field
must hold one of these EXACTLY, or it will never be routed:
      6  RuntimeAuditLogs
      2  ApplicationMetricsLogs
```

Compare that list against what your events actually carry in the field named by
the engine's `LOG_TYPE_FIELD` setting. A mismatch here — usually casing — is the
single most common reason a correct detection never fires.

YAML that isn't a detection (pipeline configs, schemas, docs) is ignored rather
than reported as broken, matching what the engine itself does.

---

## Options

| Flag | Use |
|---|---|
| `--source rules` | Bundle only a subfolder instead of the whole repo |
| `--extra global_helpers` | Also include a folder outside `--source` (repeatable) |
| `--exclude "legacy/**"` | Skip paths (repeatable) |
| `--version v1.2.3` | Pin the version instead of hashing contents |
| `--out build/` | Write somewhere other than `dist/` |

Always excluded: `.git`, `__pycache__`, `dist/`, `.venv`, `*_test.py`,
`*_tests.py`, and this folder.

A typical panther-analysis-shaped repo, where rules live under `rules/` and
helpers alongside:

```bash
python dac_bundler/bundle.py --source rules --extra global_helpers
```

---

## `example/`

A worked `.py` + `.yml` pair for Azure Event Hubs `RuntimeAuditLogs`, showing
the layout and every optional hook (`title`, `dedup`, `severity`,
`alert_context`). Copy the shape; it is never bundled.

---

## Wiring it into CI

The two upload steps are all a pipeline adds. Run `bundle.py`, then upload
`dist/bundles/*.zip` before `dist/current.json` — that order is not optional. A
worker that reads a pointer to a bundle which isn't there yet fails to load
detections.

`.azure-pipelines/publish-detections.yml` in the pyre repo does exactly this.
