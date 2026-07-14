"""Detection registry.

Loads the detection bundle (your DaC: paired .py + .yml, same structure as
panther-analysis) and builds an index from LOG TYPE -> [detections]. The
processor uses this so an incoming event only runs the detections registered
for its p_log_type. A Palo event never executes Cloudflare rules; this is what
keeps 400+ detections cheap at scale.

A "detection" here wraps the user's module and exposes the Panther-compatible
function contract: rule(), and the optional title/dedup/severity/alert_context/
description/reference/runbook/destinations.
"""
import importlib.util
import logging
import os
import sys
import time
import yaml
from types import ModuleType

log = logging.getLogger("pyre.registry")

# Directories we last put on sys.path for global helpers. Tracked so a reload can
# remove the previous bundle's before adding the new one's (no accumulation, and
# the newest bundle wins). Module-level because from_bundle is a classmethod and
# one worker reloads sequentially.
_HELPER_PATHS: list[str] = []


class Detection:
    def __init__(self, meta: dict, module: ModuleType):
        self.id = meta["RuleID"]
        self.enabled = meta.get("Enabled", True)
        self.log_types = meta.get("LogTypes", [])
        self.create_alert = meta.get("CreateAlert", True)
        self.default_severity = meta.get("Severity", "INFO")
        self.threshold = int(meta.get("Threshold", 1))
        self.dedup_period_seconds = int(meta.get("DedupPeriodMinutes", 60)) * 60
        self._m = module

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
    def __init__(self):
        self._by_log_type: dict[str, list[Detection]] = {}

    @classmethod
    def from_bundle(cls, bundle_dir: str, enabled_ids: set[str] | None = None) -> "Registry":
        reg = cls()
        # Make shared "global helper" modules importable BEFORE loading detections,
        # so a detection doing `from panther_base_helpers import deep_get` resolves.
        _prepare_imports(bundle_dir)
        for root, _dirs, files in os.walk(bundle_dir):
            for f in files:
                if not (f.endswith(".yml") or f.endswith(".yaml")):
                    continue
                meta = _read_meta(os.path.join(root, f))
                if meta.get("AnalysisType") not in (None, "rule"):
                    continue  # streaming rules only; globals/scheduled/policy handled elsewhere
                if "RuleID" not in meta or "Filename" not in meta:
                    continue  # not a well-formed streaming rule; `pyre validate` flags these
                py = os.path.join(root, os.path.basename(meta["Filename"]))
                try:
                    module = _load_module(meta["RuleID"], py)
                    det = Detection(meta, module)
                except Exception as e:
                    # One detection that won't import/parse (e.g. a missing global
                    # helper or a syntax error) is skipped so it can't block the
                    # rest of the bundle. Surfaces in Log Analytics; Panther does
                    # the same (a per-detection "detection error").
                    log.warning("skipping detection %s (%s): %s", meta.get("RuleID"), py, e)
                    continue
                # runtime enable/disable via App Config overrides the YAML flag
                if enabled_ids is not None:
                    det.enabled = det.id in enabled_ids
                if not det.enabled:
                    continue
                for lt in det.log_types:
                    reg._by_log_type.setdefault(lt, []).append(det)
        return reg

    def for_log_type(self, log_type: str) -> list[Detection]:
        return self._by_log_type.get(log_type, [])


def _load_module(name: str, path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_meta(path: str) -> dict:
    """Parse a detection/global YAML; return {} for empty or malformed files so a
    single bad file never crashes the whole bundle load."""
    try:
        with open(path) as fh:
            meta = yaml.safe_load(fh)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _prepare_imports(bundle_dir: str) -> None:
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
    """
    global _HELPER_PATHS
    for d in _HELPER_PATHS:            # undo the previous load's additions first
        try:
            sys.path.remove(d)
        except ValueError:
            pass

    dirs = {os.path.abspath(bundle_dir)}
    helper_modules = set()
    for root, _dirs, files in os.walk(bundle_dir):
        for f in files:
            if not (f.endswith(".yml") or f.endswith(".yaml")):
                continue
            meta = _read_meta(os.path.join(root, f))
            if meta.get("AnalysisType") == "global" and meta.get("Filename"):
                dirs.add(os.path.abspath(root))
                helper_modules.add(os.path.splitext(os.path.basename(meta["Filename"]))[0])

    ordered = list(dirs)
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

    def get(self) -> Registry:
        now = time.monotonic()
        if self._registry is None or now >= self._next_check:
            self._next_check = now + self._interval
            try:
                self._maybe_reload()
            except Exception:
                if self._registry is None:
                    raise                  # cold start with no bundle is fatal
                # otherwise keep serving the last-good Registry; a transient blob/
                # network blip must not stop detection.
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
