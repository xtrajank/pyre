"""Runtime configuration, loaded once at cold start.

Everything is resolved from environment/app-settings and the declarative
config files. No secrets are hard-coded; secrets arrive via Key Vault
references (already resolved into env vars by the platform) or via Managed
Identity at call time.
"""
import os
from dataclasses import dataclass, field

import yaml

# The function app root - the directory holding function_app.py, host.json and
# (once packaged) the config/ folder. Config paths are anchored to THIS, never to
# the working directory, so the same package behaves identically whether it's
# started by the Azure worker, by pytest, or from a shell somewhere else.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In a deployed package config/ sits inside the app root; in the source repo it
# sits one level up, beside engine/. Checking both means neither layout needs an
# env var to work.
_CONFIG_ROOTS = (APP_ROOT, os.path.dirname(APP_ROOT))


def resolve_path(path: str) -> str:
    """Absolute paths win. Relative ones resolve against the first root that
    actually has the file, falling back to the deployed location so an error
    message points where a deployment would look."""
    if os.path.isabs(path):
        return path
    candidates = [os.path.normpath(os.path.join(root, path)) for root in _CONFIG_ROOTS]
    return next((c for c in candidates if os.path.exists(c)), candidates[0])


@dataclass
class DacConfig:
    """Where the external detections live and how a worker gets/refreshes them.

    Parsed from config/detections.yaml, with env overrides for the per-environment
    and secret-adjacent bits (repo/ref/blob account/refresh) so the same YAML ships
    to every env and the pipeline overrides what differs."""
    repo: str = ""
    ref: str = "main"
    subpath: str = ""
    token_env: str = "DAC_TOKEN"
    include: list[str] = field(default_factory=lambda: ["**/*.yml", "**/*.yaml", "**/*.py"])
    exclude: list[str] = field(default_factory=list)
    bundle_mode: str = "local"          # local | blob
    local_dir: str = "./.bundle"
    blob_account_url: str = ""
    blob_container: str = "detections"
    blob_pointer: str = "current.json"
    refresh_interval_seconds: int = 45


def load_dac_config(path: str | None = None) -> DacConfig:
    path = path or os.environ.get("DETECTIONS_CONFIG_PATH", "config/detections.yaml")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    dac = data.get("dac", {}) or {}
    bundle = data.get("bundle", {}) or {}
    blob = bundle.get("blob", {}) or {}
    return DacConfig(
        repo=os.environ.get("DAC_REPO", dac.get("repo", "")),
        ref=os.environ.get("DAC_REF", dac.get("ref", "main")),
        subpath=dac.get("path", ""),
        token_env=dac.get("token_env", "DAC_TOKEN"),
        include=dac.get("include") or ["**/*.yml", "**/*.yaml", "**/*.py"],
        exclude=dac.get("exclude") or [],
        bundle_mode=os.environ.get("BUNDLE_MODE", bundle.get("mode", "local")),
        local_dir=os.environ.get("BUNDLE_LOCAL_DIR", bundle.get("local_dir", "./.bundle")),
        blob_account_url=os.environ.get("BUNDLE_BLOB_ACCOUNT_URL", blob.get("account_url", "")),
        blob_container=blob.get("container", "detections"),
        blob_pointer=blob.get("pointer_blob", "current.json"),
        refresh_interval_seconds=int(
            os.environ.get("REFRESH_INTERVAL_SECONDS", bundle.get("refresh_interval_seconds", 45))
        ),
    )


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


@dataclass
class RuntimeConfig:
    env: str = field(default_factory=lambda: os.environ.get("PYRE_ENV", "dev"))
    # Where dedup/threshold/unique/storm state lives: "redis" (production) or
    # "memory" (POC - in-process, per-worker, resets on cold start). See state.py.
    state_backend: str = field(default_factory=lambda: os.environ.get("STATE_BACKEND", "redis"))
    redis_host: str = field(default_factory=lambda: os.environ.get("REDIS_HOST", ""))
    redis_port: int = field(default_factory=lambda: int(os.environ.get("REDIS_PORT", "6380")))
    redis_use_entra: bool = field(default_factory=lambda: os.environ.get("REDIS_USE_ENTRA", "true") == "true")
    dac: DacConfig = field(default_factory=load_dac_config)
    destinations_path: str = field(default_factory=lambda: resolve_path(
        os.environ.get("DESTINATIONS_PATH", "config/destinations.yaml")))
    sources_path: str = field(default_factory=lambda: resolve_path(
        os.environ.get("SOURCES_PATH", "config/sources.yaml")))
    signals_sink_url: str = field(default_factory=lambda: os.environ.get("SIGNALS_SINK_URL", ""))  # Cribl HTTP source
    # Append-blob visualisation sink (POC). When set, signals and alerts are
    # appended as JSON lines to <container>/signals|alerts/<date>.jsonl instead of
    # needing Cribl/Torq. Empty = disabled, and the normal HTTP paths apply.
    output_blob_account_url: str = field(default_factory=lambda: os.environ.get("OUTPUT_BLOB_ACCOUNT_URL", ""))
    output_blob_container: str = field(default_factory=lambda: os.environ.get("OUTPUT_BLOB_CONTAINER", "pyre-output"))
    # Routes used when a detection doesn't name its own destinations().
    # Comma-separated app setting, e.g. "blob_alerts".
    default_routes: list[str] = field(default_factory=lambda: _csv("DEFAULT_ROUTES"))
    storm_limit_per_hour: int = field(default_factory=lambda: int(os.environ.get("STORM_LIMIT", "1000")))
    # Which event field selects detections and which carries the event's own
    # timestamp. Defaults match Cribl's own field names (not Panther's `p_`
    # prefix convention); set via Terraform's log_type_field/event_time_field
    # to match whatever your normalizer actually stamps.
    log_type_field: str = field(default_factory=lambda: os.environ.get("LOG_TYPE_FIELD", "dataset"))
    event_time_field: str = field(default_factory=lambda: os.environ.get("EVENT_TIME_FIELD", "_time"))
    # Some producers put MANY log records in ONE Event Hub message, wrapped in an
    # envelope. Azure's own diagnostic settings do exactly this: every message is
    # {"records": [ {...}, {...} ]}. When the parsed message is an object with
    # this field holding a list, each element is processed as its own event.
    # Set to "" to disable and treat every message as a single event.
    event_envelope_field: str = field(default_factory=lambda: os.environ.get("EVENT_ENVELOPE_FIELD", "records"))


def load_runtime_config() -> RuntimeConfig:
    return RuntimeConfig()


def event_hub_names(cfg: RuntimeConfig) -> list[str]:
    """Which Event Hubs to attach a trigger to.

    One hub in the POC, many in production - and that has to be a CONFIG change,
    not a code change, or "add a log source" means redeploying the engine. So
    function_app.py registers one trigger per name returned here.

    An app setting always beats the packaged file. That ordering matters: the
    same package ships to every environment, so a file inside it must never be
    able to override what an environment explicitly asked for. Attaching a
    trigger to a hub that doesn't exist in this namespace fails the whole app, so
    the safe default is "only what I was explicitly told".

      1. EVENTHUB_NAMES - comma-separated. Many hubs, explicitly.
      2. EVENTHUB_NAME  - one hub, explicitly. The POC setting.
      3. config/sources.yaml - the `hub:` of every declared source, deduplicated.
         The same file Terraform sizes the hubs from, so in production onboarding
         a source is one edit in one place. Leave both settings unset to use it.
    """
    names = [n.strip() for n in os.environ.get("EVENTHUB_NAMES", "").split(",") if n.strip()]
    if names:
        return list(dict.fromkeys(names))

    single = os.environ.get("EVENTHUB_NAME", "").strip()
    if single:
        return [single]

    if os.path.exists(cfg.sources_path):
        with open(cfg.sources_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        hubs = [s["hub"] for s in (data.get("sources") or []) if s.get("hub")]
        if hubs:
            return list(dict.fromkeys(hubs))

    return []
