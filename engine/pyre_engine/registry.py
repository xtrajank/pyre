"""Detection registry.

Loads the detection bundle (your DaC: paired .py + .yml, same structure as
panther-analysis) and builds an index from LOG TYPE -> [detections]. The
processor uses this so an incoming event only runs the detections registered
for its log type (read from whichever field `cfg.log_type_field` names - see
processor.py and config.py). A Palo event never executes Cloudflare rules;
this is what keeps 400+ detections cheap at scale.

A "detection" here wraps the user's module and exposes the Panther-compatible
function contract: rule(), and the optional title/dedup/severity/alert_context/
description/reference/runbook/destinations.

Loading is LAZY, and that is the whole point of this file. Building the index
costs one YAML parse per detection and executes NO detection code. A detection's
.py is imported the first time an event of its log type actually arrives (see
Registry.for_log_type). A worker fed one log type out of a 900-detection bundle
therefore imports the handful of modules that can match, not all 900 - which is
the same "a Palo event never executes Cloudflare rules" rule applied to import
cost instead of just rule() cost.
"""
import importlib.util
import logging
import os
import sys
import threading
import time
import yaml
from types import ModuleType
from typing import NamedTuple

log = logging.getLogger("pyre.registry")

try:                                   # libyaml is several times faster than the
    from yaml import CSafeLoader as _YamlLoader   # pure-Python parser, and a full
except ImportError:                    # bundle is ~1k YAML files per reload.
    from yaml import SafeLoader as _YamlLoader

# Walk noise that can never hold a detection. Pruned in-place from os.walk's dir
# list so we don't descend into them at all.
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}

# Directories we last put on sys.path for global helpers. Tracked so a reload can
# remove the previous bundle's before adding the new one's (no accumulation, and
# the newest bundle wins). Module-level because from_bundle is a classmethod and
# one worker reloads sequentially.
_HELPER_PATHS: list[str] = []


class Detection:
    """Metadata from the .yml; logic from the .py, imported on demand.

    Constructing one is cheap and side-effect free - it does NOT run the
    detection's module. `load()` does that, and the Registry calls it only for
    log types a worker actually sees.
    """

    def __init__(self, meta: dict, py_path: str):
        self.id = meta["RuleID"]
        self.enabled = meta.get("Enabled", True)
        self.log_types = meta.get("LogTypes", [])
        self.create_alert = meta.get("CreateAlert", True)
        self.default_severity = meta.get("Severity", "INFO")
        self.threshold = int(meta.get("Threshold", 1))
        self.dedup_period_seconds = int(meta.get("DedupPeriodMinutes", 60)) * 60
        self.py_path = py_path
        self._m: ModuleType | None = None
        self._failed = False

    def load(self) -> bool:
        """Import the detection's module; True if it's usable.

        A detection that won't import (missing helper, syntax error, missing
        third-party dep) is logged once and returns False, so the Registry can
        drop it without one bad detection taking out its log type's whole list.
        Panther does the same - a per-detection "detection error".

        At most one exec per Detection: the result is cached both ways, so a
        detection registered under several log types imports once, and a broken
        one is not retried per event.
        """
        if self._m is not None:
            return True
        if self._failed:
            return False
        try:
            self._m = _load_module(self.id, self.py_path)
        except Exception as e:
            log.warning("skipping detection %s (%s): %s", self.id, self.py_path, e)
            self._failed = True
            return False
        return True

    def _call(self, name, event, default=None):
        fn = getattr(self._m, name, None)
        return fn(event) if callable(fn) else default

    def rule(self, event) -> bool:
        return bool(self._m.rule(event))

    def title(self, event) -> str:
        return self._call("title", event, default=self.id)

    def dedup(self, event):
        return self._call("dedup", event, default=None)

    def severity(self, event) -> str:
        return self._call("severity", event, default=self.default_severity)

    def alert_context(self, event) -> dict:
        return self._call("alert_context", event, default={}) or {}

    def destinations(self, event):
        return self._call("destinations", event, default=None)

    def unique(self, event):
        """Optional Panther-style unique() hook: return the value to count
        DISTINCT occurrences of (e.g. source IP) instead of counting every
        match. None (the default - no unique() function on the module) keeps
        the normal total-count threshold behavior."""
        return self._call("unique", event, default=None)


class Registry:
    """LOG TYPE -> [Detection], with each detection's module imported on first use.

    Two maps: `_pending` is everything the YAML index knows about, `_ready` is the
    log types whose modules have been imported and filtered down to the ones that
    work. A log type moves from one to the other the first time it's asked for.
    """

    def __init__(self):
        self._pending: dict[str, list[Detection]] = {}
        self._ready: dict[str, list[Detection]] = {}
        # The worker runs batches on a thread pool, so two threads can hit the
        # same brand-new log type at once. Only materialization is guarded; the
        # warm path never takes it (see for_log_type).
        self._lock = threading.Lock()

    @classmethod
    def from_bundle(cls, bundle_dir: str, enabled_ids: set[str] | None = None) -> "Registry":
        reg = cls()
        scan = _scan(bundle_dir)
        # Make shared "global helper" modules importable BEFORE any detection is
        # loaded, so `from panther_base_helpers import deep_get` resolves whenever
        # the first event of that detection's log type lands.
        _prepare_imports(bundle_dir, scan.helper_dirs, scan.helper_modules)
        for meta, py in scan.rules:
            det = Detection(meta, py)
            # runtime enable/disable via App Config overrides the YAML flag
            if enabled_ids is not None:
                det.enabled = det.id in enabled_ids
            if not det.enabled:
                continue
            for lt in det.log_types:
                reg._pending.setdefault(lt, []).append(det)
        return reg

    def for_log_type(self, log_type: str) -> list[Detection]:
        """The detections to run for this log type. The first call imports their
        modules and drops any that won't load; every later call is one dict hit.

        Double-checked locking: the warm path is a plain dict read (atomic under
        the GIL, no lock), and only the once-per-log-type materialization
        serializes. Without the lock two threads could both miss in `_ready` and
        race on `_pending.pop` - the loser would get [] and silently run NO
        detections for that log type.
        """
        dets = self._ready.get(log_type)
        if dets is None:
            with self._lock:
                dets = self._ready.get(log_type)      # another thread may have won
                if dets is None:
                    dets = [d for d in self._pending.pop(log_type, []) if d.load()]
                    self._ready[log_type] = dets
        return dets


def _load_module(name: str, path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_meta(path: str) -> dict:
    """Parse a detection/global YAML; return {} for empty or malformed files so a
    single bad file never crashes the whole bundle load.

    Opened in BINARY mode deliberately. YAML is UTF-8 by spec and PyYAML does its
    own decoding (including BOM detection) when handed bytes. Text mode would
    instead decode with the platform's locale encoding - cp1252 on Windows - which
    raises UnicodeDecodeError on any non-ASCII byte in a detection's Description.
    """
    try:
        with open(path, "rb") as fh:
            meta = yaml.load(fh, Loader=_YamlLoader)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


class BundleScan(NamedTuple):
    """What one walk of a bundle finds. `rules`/`helper_files` are exactly the
    .py files the engine will ever import, which is what makes this the right
    input for both the Registry and the dependency check in deps.py."""
    rules: list[tuple[dict, str]]      # (meta, .py path) per streaming rule
    helper_files: set[str]             # global-helper .py paths
    helper_dirs: set[str]              # dirs to put on sys.path for bare imports
    helper_modules: set[str]           # helper module names, for cache eviction


def _scan(bundle_dir: str) -> BundleScan:
    """One walk, one YAML parse per file, no detection code executed.

    Both callers used to walk and re-parse the entire tree independently, so a
    reload parsed every YAML twice.
    """
    rules: list[tuple[dict, str]] = []
    helper_files: set[str] = set()
    helper_dirs: set[str] = set()
    helper_modules: set[str] = set()
    for root, dirs, files in os.walk(bundle_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith((".yml", ".yaml")):
                continue
            meta = _read_meta(os.path.join(root, f))
            atype = meta.get("AnalysisType")
            if atype == "global":
                if meta.get("Filename"):
                    helper_files.add(os.path.join(root, os.path.basename(meta["Filename"])))
                    helper_dirs.add(os.path.abspath(root))
                    helper_modules.add(os.path.splitext(os.path.basename(meta["Filename"]))[0])
                continue
            if atype not in (None, "rule"):
                continue  # streaming rules only; scheduled/policy handled elsewhere
            if "RuleID" not in meta or "Filename" not in meta:
                continue  # not a well-formed streaming rule; `pyre validate` flags these
            rules.append((meta, os.path.join(root, os.path.basename(meta["Filename"]))))
    return BundleScan(rules, helper_files, helper_dirs, helper_modules)


def _prepare_imports(bundle_dir: str, helper_dirs: set[str], helper_modules: set[str]) -> None:
    """Support Panther-style GLOBAL HELPERS: shared `.py` files (paired with an
    `AnalysisType: global` YAML) that detections import by bare name throughout the
    DaC, e.g. `from panther_base_helpers import deep_get`.

    Python resolves such a bare import from a directory ON sys.path, so we put the
    bundle root plus every directory that holds a global-helper file onto sys.path
    (newest bundle first). We also evict any cached helper modules so a reload picks
    up helper edits too. The detection files themselves are loaded by path and never
    added to sys.path, so there's no collision between detections and helpers.

    NOTE: the helper files must be PRESENT in the bundle. `pyre pull` copies the
    dirs listed under `dac.global_helpers` in config/detections.yaml alongside the
    detections; if a detection imports a helper that wasn't pulled, it will fail to
    load (and `pyre validate` / the engine logs will show the ImportError).

    `_scan` supplies the dirs/module names, so this does no walking of its own.
    """
    global _HELPER_PATHS
    for d in _HELPER_PATHS:            # undo the previous load's additions first
        try:
            sys.path.remove(d)
        except ValueError:
            pass

    root = os.path.abspath(bundle_dir)
    ordered = [root, *sorted(helper_dirs - {root})]
    for d in ordered:
        sys.path.insert(0, d)          # bundle helper dirs take precedence
    _HELPER_PATHS = ordered
    for name in helper_modules:        # drop stale copies so this load re-imports fresh
        sys.modules.pop(name, None)


class BundleLoader:
    """Keeps the in-memory Registry fresh without paying git/clone cost on the hot
    path. Built once per worker; `get()` is called at the top of every batch.

    It rebuilds the Registry only when the bundle VERSION changes (a push, via the
    BundleSource) or when the runtime enabled-set changes (an enable/disable flip).
    The check is throttled to once per `refresh_interval_seconds`, so a warm worker
    handling millions of events/hour does at most one cheap version probe per
    interval - not one per event. The swap is a single reference assignment, so an
    in-flight batch finishes on the old Registry and the next batch is live.

    This is how "push to the DaC repo reflects within ~a minute, no redeploy" holds
    at scale. See config/detections.yaml and docs/architecture.md.
    """

    def __init__(self, source, refresh_interval_seconds: int = 45, enabled_provider=None):
        self._source = source
        self._interval = max(0, int(refresh_interval_seconds))
        self._enabled_provider = enabled_provider or (lambda: None)
        self._registry: Registry | None = None
        self._loaded_key = None            # (bundle_version, enabled_fingerprint)
        self._next_check = 0.0
        # One Processor is shared by every concurrent invocation on the worker, so
        # get() is called from several threads. Without this, two threads whose
        # check window opens together would BOTH probe the source and BOTH rebuild
        # the whole Registry - duplicate blob downloads and duplicate parses of the
        # entire bundle, on every refresh tick.
        self._lock = threading.Lock()

    def get(self) -> Registry:
        now = time.monotonic()
        if self._registry is None or now >= self._next_check:
            with self._lock:
                # Re-check inside the lock: while we waited, the thread that held
                # it may already have done this tick's reload.
                if self._registry is None or time.monotonic() >= self._next_check:
                    self._next_check = time.monotonic() + self._interval
                    try:
                        self._maybe_reload()
                    except Exception:
                        if self._registry is None:
                            raise          # cold start with no bundle is fatal
                        # otherwise keep serving the last-good Registry; a transient
                        # blob/network blip must not stop detection.
                        log.exception("bundle refresh failed; serving the last-good registry")
        return self._registry

    def _maybe_reload(self) -> None:
        version = self._source.current_version()
        enabled = self._enabled_provider()
        key = (version, _enabled_fingerprint(enabled))
        if self._registry is not None and key == self._loaded_key:
            return
        path = self._source.ensure_local(version)
        new_registry = Registry.from_bundle(path, enabled)
        self._registry = new_registry      # atomic swap
        self._loaded_key = key


def _enabled_fingerprint(enabled: set[str] | None):
    return "*" if enabled is None else tuple(sorted(enabled))
