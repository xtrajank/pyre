"""Runtime configuration, loaded once at cold start.

Everything is resolved from environment/app-settings and the declarative
config files. No secrets are hard-coded; secrets arrive via Key Vault
references (already resolved into env vars by the platform) or via Managed
Identity at call time.
"""
import os
from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class Shape:
    """How to read events on ONE hub: which field names the log type, which
    carries the event time, and an optional envelope key wrapping many records
    in one message. Per-hub, so one Function App can consume feeds of different
    shapes at once (a Cribl namespace using dataset/_time/no-envelope alongside
    an Azure-native namespace using category/time/records). Built once per hub at
    cold start and reused for every batch - never per event."""
    log_type_field: str = "dataset"
    event_time_field: str = "_time"
    envelope: str = ""


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
        # Binary mode: PyYAML decodes UTF-8/BOMs itself. Text mode would use the
        # platform locale encoding (cp1252 on Windows) and fail on non-ASCII.
        with open(path, "rb") as fh:
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
    # Destination names an alert routes to when its detection names none itself.
    # Set per instance by Terraform (var.default_routes), NOT derived from `env`.
    # This used to be `["mock"] if env == "dev" else ["torq_prod"]` in
    # processor.py, which made every env string that wasn't exactly "dev" -
    # "Dev", "lab", "staging", a typo - route to production Torq, and hardcoded
    # one vendor's name into the engine besides.
    default_routes: list[str] = field(
        default_factory=lambda: [r.strip() for r in os.environ.get("DEFAULT_ROUTES", "").split(",") if r.strip()])
    signals_sink_url: str = field(default_factory=lambda: os.environ.get("SIGNALS_SINK_URL", ""))  # Cribl HTTP source
    storm_limit_per_hour: int = field(default_factory=lambda: int(os.environ.get("STORM_LIMIT", "1000")))
    # How long an event id stays claimed in the "already processed" set. This is
    # the ONLY Redis key written per EVENT (everything else is per match), so
    # resident keys = events/sec x this, and it dominates Redis memory at volume:
    # at ~50k events/sec each 900s of TTL is ~45M keys (~4-5GB). It only has to
    # outlive an Event Hubs checkpoint retry - seconds to minutes - so 15 minutes
    # is already generous; an hour cost 4x the memory for no extra safety.
    # Raise it only if you see redeliveries arriving later than this. See dedup.py.
    idempotency_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "900")))
    # The DEFAULT feed shape (Cribl field names). Used for any hub the host does
    # not give an explicit per-hub shape (HUBS_CONFIG). Reading a field the feed
    # already set is not normalization - it says where fields are, never how to
    # transform them; a feed needing more than that belongs behind Cribl.
    log_type_field: str = field(default_factory=lambda: os.environ.get("LOG_TYPE_FIELD", "dataset"))
    event_time_field: str = field(default_factory=lambda: os.environ.get("EVENT_TIME_FIELD", "_time"))
    envelope: str = field(default_factory=lambda: os.environ.get("ENVELOPE", ""))

    @property
    def default_shape(self) -> Shape:
        return Shape(self.log_type_field, self.event_time_field, self.envelope)


def load_runtime_config() -> RuntimeConfig:
    return RuntimeConfig()
