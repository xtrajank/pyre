"""Detections, loaded from a bundle directory and indexed by LOG TYPE.

A detection is a paired `.yml` (metadata) + `.py` (logic), the same layout as
panther-analysis. The index is what keeps hundreds of detections cheap: an
event only ever runs the detections registered for its own log type.

Two objects here:
  Registry      an immutable snapshot: log type -> [detections]
  BundleLoader  keeps that snapshot fresh, rebuilding it only when the bundle
                version actually changes
"""
import importlib.util
import logging
import os
import sys
import time
from types import ModuleType

import yaml

log = logging.getLogger("pyre.registry")

# Directories we last put on sys.path for global helpers. Tracked so a reload can
# remove the previous bundle's before adding the new one's (no accumulation, and
# the newest bundle wins).
_HELPER_PATHS: list[str] = []


class Detection:
    """One detection: its YAML metadata plus the module holding `rule()`.

    Only `rule()` is required. Everything else is an optional function on the
    module (`title`, `dedup`, `severity`, `alert_context`, `unique`), and falls
    back to the YAML or to a sane default when absent.
    """

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

    def unique(self, event):
        """Optional Panther-style `unique()`: return the value to count DISTINCT
        occurrences of (e.g. source IP) so `Threshold: 5` means "5 different
        IPs" rather than "5 matches". No `unique()` on the module keeps the
        normal total-count behaviour."""
        return self._call("unique", event, default=None)


class Registry:
    def __init__(self):
        self._by_log_type: dict[str, list[Detection]] = {}

    @classmethod
    def from_bundle(cls, bundle_dir: str) -> "Registry":
        reg = cls()
        # Make shared "global helper" modules importable BEFORE loading detections,
        # so a detection doing `from panther_base_helpers import deep_get` resolves.
        _prepare_imports(bundle_dir)
        for root, _dirs, files in os.walk(bundle_dir):
            for f in files:
                if not f.endswith((".yml", ".yaml")):
                    continue
                meta = _read_meta(os.path.join(root, f))
                if meta.get("AnalysisType") not in (None, "rule"):
                    continue        # global helpers, policies, data models: not streaming rules
                if "RuleID" not in meta or "Filename" not in meta:
                    continue        # not a detection; the bundler flags these before publish
                py = os.path.join(root, os.path.basename(meta["Filename"]))
                try:
                    det = Detection(meta, _load_module(meta["RuleID"], py))
                except Exception as e:
                    # One detection that won't import (a syntax error, a missing
                    # helper) is skipped so it can't take the whole bundle - and
                    # therefore all detection - down with it.
                    log.warning("skipping detection %s (%s): %s", meta.get("RuleID"), py, e)
                    continue
                if not det.enabled:
                    continue
                for lt in det.log_types:
                    reg._by_log_type.setdefault(lt, []).append(det)
        return reg

    def for_log_type(self, log_type: str) -> list[Detection]:
        return self._by_log_type.get(log_type, [])

    def stats(self) -> dict:
        """What actually loaded. `/health` returns this, so you can tell "no
        alerts because nothing matched" apart from "no alerts because the bundle
        is empty or the log type is spelled differently"."""
        ids = {d.id for dets in self._by_log_type.values() for d in dets}
        return {"detections": len(ids), "log_types": sorted(self._by_log_type)}


def _load_module(name: str, path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_meta(path: str) -> dict:
    """Parse a detection YAML; return {} for anything unreadable so one bad file
    never crashes the bundle load.

    The encoding is explicit: detection YAML is UTF-8, but Python defaults to the
    locale codec (cp1252 on Windows), which would make a rule containing any
    non-ASCII character silently vanish locally while loading fine on Linux.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            meta = yaml.safe_load(fh)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        log.warning("skipping unreadable YAML %s", path)
        return {}


def _prepare_imports(bundle_dir: str) -> None:
    """Support Panther-style GLOBAL HELPERS: shared `.py` files (paired with an
    `AnalysisType: global` YAML) that detections import by bare name, e.g.
    `from panther_base_helpers import deep_get`.

    Python resolves such an import from a directory on sys.path, so the bundle
    root plus every directory holding a global helper goes on sys.path, and any
    cached helper module is evicted so a reload picks up helper edits too.
    Detections themselves are loaded by path and never added to sys.path, so
    they can't collide with helpers.
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
            if not f.endswith((".yml", ".yaml")):
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
    """Keeps the Registry fresh without paying download cost on the hot path.

    Built once per worker; `get()` runs at the top of every batch. It rebuilds
    only when the bundle VERSION changes, and checks at most once per
    `refresh_seconds` - so a warm worker doing millions of events an hour makes
    one cheap pointer read per interval, not one per event. The swap is a single
    reference assignment: an in-flight batch finishes on the old Registry and the
    next batch is live on the new one.

    This is what "publish a detection and it's live in a minute, with no
    redeploy" actually is.
    """

    def __init__(self, source, refresh_seconds: int = 60):
        self._source = source
        self._interval = max(0, int(refresh_seconds))
        self._registry: Registry | None = None
        self._version: str | None = None
        self._next_check = 0.0

    def get(self) -> Registry:
        now = time.monotonic()
        if self._registry is None or now >= self._next_check:
            self._next_check = now + self._interval
            try:
                self._maybe_reload()
            except Exception:
                if self._registry is None:
                    raise              # cold start with no bundle: nothing to serve
                # Otherwise keep serving the last-good Registry. A transient blob
                # or network blip must never stop detection.
                log.exception("bundle refresh failed; still serving version %s", self._version)
        return self._registry

    @property
    def version(self) -> str | None:
        return self._version

    def _maybe_reload(self) -> None:
        version = self._source.current_version()
        if self._registry is not None and version == self._version:
            return
        registry = Registry.from_bundle(self._source.ensure_local(version))
        self._registry = registry      # atomic swap
        self._version = version
        log.info("loaded detection bundle %s: %s", version, registry.stats())
