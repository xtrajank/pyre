"""Runtime configuration, loaded once at cold start.

Everything is resolved from environment/app-settings and the declarative
config files. No secrets are hard-coded; secrets arrive via Key Vault
references (already resolved into env vars by the platform) or via Managed
Identity at call time.
"""
import os
from dataclasses import dataclass, field

import yaml


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
        with open(path) as fh:
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


@dataclass
class RuntimeConfig:
    env: str = field(default_factory=lambda: os.environ.get("PYRE_ENV", "dev"))
    redis_host: str = field(default_factory=lambda: os.environ.get("REDIS_HOST", ""))
    redis_port: int = field(default_factory=lambda: int(os.environ.get("REDIS_PORT", "6380")))
    redis_use_entra: bool = field(default_factory=lambda: os.environ.get("REDIS_USE_ENTRA", "true") == "true")
    dac: DacConfig = field(default_factory=load_dac_config)
    destinations_path: str = field(default_factory=lambda: os.environ.get("DESTINATIONS_PATH", "config/destinations.yaml"))
    signals_sink_url: str = field(default_factory=lambda: os.environ.get("SIGNALS_SINK_URL", ""))  # Cribl HTTP source
    storm_limit_per_hour: int = field(default_factory=lambda: int(os.environ.get("STORM_LIMIT", "1000")))
    # Which event field selects detections and which carries the event's own
    # timestamp. Defaults match Cribl's own field names (not Panther's `p_`
    # prefix convention); set via Terraform's log_type_field/event_time_field
    # to match whatever your normalizer actually stamps.
    log_type_field: str = field(default_factory=lambda: os.environ.get("LOG_TYPE_FIELD", "dataset"))
    event_time_field: str = field(default_factory=lambda: os.environ.get("EVENT_TIME_FIELD", "_time"))


def load_runtime_config() -> RuntimeConfig:
    return RuntimeConfig()
